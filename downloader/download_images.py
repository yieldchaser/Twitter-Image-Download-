#!/usr/bin/env python3
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.x.com/2"
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
IMAGE_ROOT = ROOT / "images"
META_ROOT = ROOT / "metadata"
TIMEOUT = 30


def slug(text: str, limit: int = 90) -> str:
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().replace(" ", "_")
    return (text[:limit].strip("_") or "untitled")


def ext_from_url(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def get_json(session, url, params=None):
    for attempt in range(5):
        r = session.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429:
            wait = int(r.headers.get("x-rate-limit-reset", time.time() + 30)) - int(time.time())
            time.sleep(max(5, min(wait, 120)))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Rate limit persisted: {url}")


def user_id(session, username):
    data = get_json(session, f"{API}/users/by/username/{username}")
    return data["data"]["id"]


def download(session, url, path):
    if path.exists() and path.stat().st_size > 0:
        return False
    r = session.get(url, timeout=TIMEOUT, stream=True)
    r.raise_for_status()
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("wb") as f:
        for chunk in r.iter_content(1024 * 256):
            if chunk:
                f.write(chunk)
    tmp.replace(path)
    return True


def process_account(session, account):
    username = account["username"]
    uid = user_id(session, username)
    out = IMAGE_ROOT / username
    out.mkdir(parents=True, exist_ok=True)
    META_ROOT.mkdir(parents=True, exist_ok=True)
    metadata_path = META_ROOT / f"{username}.jsonl"
    seen = set()
    if metadata_path.exists():
        for line in metadata_path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["media_key"])
            except Exception:
                pass

    params = {
        "max_results": 100,
        "exclude": "retweets,replies",
        "tweet.fields": "id,text,created_at,attachments",
        "expansions": "attachments.media_keys",
        "media.fields": "media_key,type,url,preview_image_url,width,height,alt_text,duration_ms,variants",
    }
    token = None
    downloaded = 0
    tweets = 0
    while True:
        if token:
            params["pagination_token"] = token
        else:
            params.pop("pagination_token", None)
        data = get_json(session, f"{API}/users/{uid}/tweets", params=params)
        tweets += len(data.get("data", []))
        media_map = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}
        with metadata_path.open("a", encoding="utf-8") as mf:
            for tweet in data.get("data", []):
                for key in tweet.get("attachments", {}).get("media_keys", []):
                    media = media_map.get(key, {})
                    if media.get("type") not in {"photo", "animated_gif", "video"}:
                        continue
                    if media.get("type") == "video":
                        # Prefer the highest-bitrate MP4 variant when available.
                        variants = media.get("variants", [])
                        mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
                        url = max(mp4, key=lambda v: v.get("bit_rate", 0)).get("url") if mp4 else None
                    else:
                        url = media.get("url") or media.get("preview_image_url")
                    if not url or key in seen:
                        continue
                    date = tweet.get("created_at", "unknown")[:10]
                    title = slug(tweet.get("text", ""))
                    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
                    ext = ".mp4" if media.get("type") == "video" else ext_from_url(url)
                    filename = f"{date}_{username}_{title}_{digest}{ext}"
                    path = out / filename
                    try:
                        if download(session, url, path):
                            downloaded += 1
                        record = {
                            "media_key": key,
                            "tweet_id": tweet["id"],
                            "username": username,
                            "created_at": tweet.get("created_at"),
                            "text": tweet.get("text", ""),
                            "tweet_url": f"https://x.com/{username}/status/{tweet['id']}",
                            "media_type": media.get("type"),
                            "source_url": url,
                            "filename": str(path.relative_to(ROOT)),
                            "alt_text": media.get("alt_text"),
                        }
                        mf.write(json.dumps(record, ensure_ascii=False) + "\n")
                        mf.flush()
                        seen.add(key)
                    except Exception as exc:
                        print(f"WARN {username} {tweet['id']} {key}: {exc}")
        token = data.get("meta", {}).get("next_token")
        if not token:
            break
    print(f"{username}: scanned {tweets} posts, downloaded {downloaded} media files")


def main():
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        raise SystemExit("X_BEARER_TOKEN is not set")
    accounts = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "yieldchaser-twitter-image-downloader/1.0"})
    for account in accounts:
        process_account(session, account)


if __name__ == "__main__":
    main()
