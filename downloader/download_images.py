#!/usr/bin/env python3
"""Run gallery-dl against configured X accounts.

Authentication is supplied at runtime as a Netscape cookies.txt file.
Each account is processed independently so one unavailable/renamed account
cannot discard successful downloads from the other accounts.

ACCOUNT_FILTER can be set to process one configured account only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
GALLERY_CONFIG = ROOT / "config" / "gallery-dl.json"
GALLERY_DEEP_CONFIG = ROOT / "config" / "gallery-dl-deep.json"
COOKIE_FILE = ROOT / ".runtime" / "x-cookies.txt"
STATUS_FILE = ROOT / "metadata" / "download_status.json"


def build_url(username: str) -> str:
    # Use the account's media profile directly. The gallery-dl Twitter
    # extractor can paginate the media timeline and use its configured
    # search fallback when necessary.
    return f"https://x.com/{username}"


def build_search_url(username: str) -> str:
    # The media timeline only exposes roughly the last few thousand tweets.
    # X's search index reaches back years, so a from:<user> filter:media
    # search paginates much deeper for a full-history backfill. Requesting
    # the live tab avoids X defaulting to its "Top" ranking cutoff.
    query = f"from:{username} filter:media -filter:replies -filter:retweets"
    return f"https://x.com/search?q={quote(query, safe='')}&f=live"


def main() -> int:
    accounts = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    account_filter = os.environ.get("ACCOUNT_FILTER", "").strip().lower()
    if account_filter:
        accounts = [a for a in accounts if a["username"].lower() == account_filter]
        if not accounts:
            raise SystemExit(f"ACCOUNT_FILTER did not match a configured account: {account_filter}")

    if not COOKIE_FILE.exists() or COOKIE_FILE.stat().st_size == 0:
        raise SystemExit("Missing runtime X cookie file")

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "accounts": [],
        "overall_success": False,
        "expected_accounts": [a["username"] for a in accounts],
    }

    deep_backfill = os.environ.get("DEEP_BACKFILL", "").strip().lower() in {"1", "true", "yes"}

    for account in accounts:
        username = account["username"]
        url = build_url(username)
        cmd = [
            sys.executable,
            "-m",
            "gallery_dl",
            "--config",
            str(GALLERY_CONFIG),
            "--cookies",
            str(COOKIE_FILE),
            "--verbose",
            url,
        ]
        print(f"Running gallery-dl for {username}: {url}")
        result = subprocess.run(
            cmd,
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print(result.stdout, end="")

        output = result.stdout or ""
        no_results = "No results for " in output
        ok = result.returncode == 0 and not no_results
        downloaded = sum(1 for line in output.splitlines() if line.startswith("images/"))

        if no_results:
            reason = "no media results returned by X/gallery-dl"
        elif result.returncode != 0:
            reason = f"gallery-dl exited with code {result.returncode}"
        else:
            reason = "media extraction completed"

        entry = {
            "username": username,
            "url": url,
            "returncode": result.returncode,
            "status": "ok" if ok else "failed",
            "reason": reason,
            "downloaded": downloaded,
        }

        # Best-effort deep backfill via the search index. Failures here do
        # not fail the account; the media-timeline pass above is authoritative.
        if deep_backfill:
            search_url = build_search_url(username)
            search_cmd = [
                sys.executable,
                "-m",
                "gallery_dl",
                "--config",
                str(GALLERY_CONFIG),
                "--config",
                str(GALLERY_DEEP_CONFIG),
                "--cookies",
                str(COOKIE_FILE),
                "--verbose",
                search_url,
            ]
            print(f"Running gallery-dl deep backfill for {username}: {search_url}")
            search_result = subprocess.run(
                search_cmd,
                cwd=ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            print(search_result.stdout, end="")
            search_output = search_result.stdout or ""
            search_no_results = "No results for " in search_output
            entry["deep_backfill"] = {
                "url": search_url,
                "returncode": search_result.returncode,
                "status": (
                    "no_results" if search_no_results
                    else "ok" if search_result.returncode == 0
                    else "failed"
                ),
                "downloaded": sum(1 for line in search_output.splitlines() if line.startswith("images/")),
            }

        status["accounts"].append(entry)

        if not ok:
            print(f"Account {username} failed validation: {reason}")

    status["overall_success"] = all(a["status"] == "ok" for a in status["accounts"])
    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    # Always return success here so the workflow can commit successful media
    # from healthy accounts. A later workflow step validates overall_success
    # and makes the run red when any required account failed.
    print(
        "Account extraction finished: "
        f"{sum(a['status'] == 'ok' for a in status['accounts'])}/"
        f"{len(status['accounts'])} accounts passed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
