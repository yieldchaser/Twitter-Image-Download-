#!/usr/bin/env python3
"""File newly downloaded images from images/<account>/ into library/.

Scope: accounts from config/accounts.json; when ACCOUNT_FILTER is set it
names the single account to process (same case-insensitive semantics as
downloader/download_images.py).

New images are *.jpg/*.jpeg/*.png files sitting directly inside
images/<account>/ (metadata/ is never descended into; anything else is
ignored). Each new image is classified:

  (a) redundant re-download: (account, tweet_id, num) already has a
      catalog row AND that row's target exists on disk -> the content is
      already archived, so the new file is removed (os.remove) after
      logging `already archived at <target>, removing redundant download`.
      Runs BEFORE hashing, so it also catches re-downloads whose bytes
      differ slightly (re-encoded) that sha1 matching misses;
  (b) sha1 already present in library/catalog.csv -> filed into the same
      series as that row (exact repost);
  (c) else the closest perceptual-hash (dHash) template match: best
      distance <= PHASH_NEAR_MAX (2) auto-files without requiring a
      margin (near-identical layout = same template); otherwise best
      distance 3..PHASH_AUTO_MAX (6) with a margin over the runner-up of
      at least PHASH_MARGIN_MIN (4) -> inherits that row's type/series;
  (d) else -> library/_inbox/<account>/ for human/agent review.

Auto-filed rows are appended to catalog.csv, library/phashes.json is
updated (new entries added, stale entries pruned), files are moved into
place, and library/INDEX.md is regenerated from the catalog.

Corrupt/unreadable images are left in place with a warning on stdout.
Nothing is ever deleted, except for two redundant-download cases:
when (account, tweet_id, num) already has a catalog row whose target
exists on disk (checked before hashing; covers re-encoded re-downloads
sha1 matching misses), and the provable byte-identical case (a new
image's sha1 is already in the catalog AND that catalog target exists
on disk). In both cases the redundant download is removed (os.remove)
after logging `already archived at <target>, removing redundant download`.
This prevents gallery-dl re-downloads of archived images from appending
duplicate-target rows on every run. Always exits 0.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
CATALOG = ROOT / "library" / "catalog.csv"
PHASHES = ROOT / "library" / "phashes.json"
INDEX = ROOT / "library" / "INDEX.md"
IMAGES = ROOT / "images"
LIBRARY = ROOT / "library"

# dHash auto-file policy: best template match at distance <= PHASH_NEAR_MAX
# (near-identical layout = same template) auto-files without requiring a
# margin; distances 3..PHASH_AUTO_MAX still need to be unambiguous
# (runner-up at least PHASH_MARGIN_MIN bits further away).
PHASH_AUTO_MAX = 6
PHASH_MARGIN_MIN = 4
PHASH_NEAR_MAX = 2

CATALOG_COLUMNS = [
    "file", "account", "date", "tweet_id", "num", "width", "height",
    "bytes", "sha1", "type", "series", "title", "publisher", "subject",
    "recurrence", "alt_type", "notes", "final_type", "final_series",
    "target",
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_accounts() -> list[str]:
    accounts = [a["username"] for a in
                json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]]
    account_filter = os.environ.get("ACCOUNT_FILTER", "").strip().lower()
    if account_filter:
        matched = [a for a in accounts if a.lower() == account_filter]
        if not matched:
            log(f"WARNING: ACCOUNT_FILTER did not match a configured "
                f"account: {account_filter!r}; nothing to do.")
            return []
        return matched
    return accounts


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash_of(img: Image.Image) -> str:
    """64-bit dHash of an already-opened image as 16 hex chars."""
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    # get_flattened_data() is the Pillow >= 12.1 API; fall back to getdata()
    # on older versions (getdata() is removed in Pillow 14).
    try:
        px = list(small.get_flattened_data())
    except AttributeError:
        px = list(small.getdata())  # type: ignore[attr-defined]
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return format(bits, "016x")


def dhash_file(path: Path) -> str | None:
    """dHash hex of an image file, or None when unreadable/corrupt."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.load()
            return dhash_of(img)
    except Exception as exc:
        log(f"WARNING: unreadable image, leaving in place: {path} ({exc})")
        return None


def image_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.load()
            return img.size
    except Exception as exc:
        log(f"WARNING: unreadable image, leaving in place: {path} ({exc})")
        return None


def hamming(a_hex: str, b_hex: str) -> int:
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


def parse_filename(name: str) -> tuple[str, str, str, str] | None:
    """Split date_account_tweetid_num.ext. Accounts may contain
    underscores, so anchor on both ends: first chunk is the date, last
    two are tweet_id and num, everything between is the account."""
    stem, ext = os.path.splitext(name)
    if ext.lower() not in IMAGE_EXTS:
        return None
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    date, num, tweet_id = parts[0], parts[-1], parts[-2]
    account = "_".join(parts[1:-2])
    if (len(date) != 10 or date[4] != "-" or date[7] != "-"
            or not date.replace("-", "").isdigit()
            or not tweet_id.isdigit() or not num.isdigit()
            or not account):
        return None
    return date, account, tweet_id, num


def tweet_text(account: str, tweet_id: str) -> str:
    # Literal braces path, e.g. images/HBGrimes/metadata/{author[name]}/<id>.json
    meta = IMAGES / account / "metadata" / "{author[name]}" / f"{tweet_id}.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    content = data.get("content", "") if isinstance(data, dict) else ""
    return content if isinstance(content, str) else ""


def load_catalog() -> list[dict]:
    with open(CATALOG, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows:
        missing = [c for c in CATALOG_COLUMNS if c not in rows[0]]
        if missing:
            log(f"WARNING: catalog.csv missing columns {missing}")
    # Dedupe by target (keep last occurrence). Guards against
    # double-append artifacts; no-op when targets are unique.
    counts: dict[str, int] = {}
    for row in rows:
        target = row.get("target", "")
        if target:
            counts[target] = counts.get(target, 0) + 1
    dup_total = sum(n - 1 for n in counts.values() if n > 1)
    if dup_total:
        log(f"WARNING: catalog.csv has {dup_total} duplicate "
            f"target row(s); keeping last occurrence.")
        seen: set[str] = set()
        kept_rev: list[dict] = []
        for row in reversed(rows):
            target = row.get("target", "")
            if target:
                if target in seen:
                    continue
                seen.add(target)
            kept_rev.append(row)
        rows = list(reversed(kept_rev))
    return rows


def load_phashes() -> dict:
    if not PHASHES.exists():
        return {}
    dup_count = 0

    def _hook(pairs: list) -> dict:
        nonlocal dup_count
        seen_keys: set = set()
        for key, _ in pairs:
            if key in seen_keys:
                dup_count += 1
            seen_keys.add(key)
        return dict(pairs)  # dict keeps last occurrence

    try:
        data = json.loads(PHASHES.read_text(encoding="utf-8"),
                           object_pairs_hook=_hook)
    except ValueError as exc:
        log(f"WARNING: {PHASHES} unparsable ({exc}); rebuilding from scratch.")
        return {}
    if dup_count:
        log(f"WARNING: {PHASHES.name} has {dup_count} duplicate "
            f"key(s); keeping last occurrence.")
    return data if isinstance(data, dict) else {}


def split_target(target: str) -> tuple[str, str] | None:
    parts = target.replace("\\", "/").split("/")
    if len(parts) != 4 or parts[0] != "library" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def is_inbox_row(row: dict) -> bool:
    return (row.get("type") == "inbox"
            or row.get("target", "").replace("\\", "/").startswith("library/_inbox/"))


def section_table(members: list[dict]) -> list[str]:
    """Render one INDEX section body for catalog rows in catalog order."""
    counts: dict[str, int] = {}
    titles: dict[str, Counter] = {}
    title_first: dict[str, dict[str, int]] = {}
    dates: dict[str, list] = {}
    accounts: dict[str, set] = {}
    for row in members:
        i, series = row["_order"], row["_series"]
        counts[series] = counts.get(series, 0) + 1
        titles.setdefault(series, Counter())[row.get("title", "")] += 1
        title_first.setdefault(series, {}).setdefault(row.get("title", ""), i)
        dates.setdefault(series, []).append(row.get("date", ""))
        accounts.setdefault(series, set()).add(row.get("account", ""))
    lines = ["| series | files | date range | accounts | example |",
             "| --- | --- | --- | --- | --- |"]
    for series in sorted(counts, key=lambda s: (-counts[s], s)):
        best = max(titles[series],
                   key=lambda t: (titles[series][t], -title_first[series][t]))
        span_dates = [d for d in dates[series] if d]
        span = f"{min(span_dates)}..{max(span_dates)}" if span_dates else ""
        accts = sorted(a for a in accounts[series] if a)
        lines.append(f"| {series} | {counts[series]} | {span} | "
                     f"{','.join(accts)} | {best} |")
    return lines


INDEX_FOOTER = """## How to find things

Filenames follow the convention `date_account_tweetid_num`, for example
`2026-08-23_casharmax_1958123456789012345_1.jpg`: post date, account that
posted it, tweet ID, and image number within the tweet. Names are preserved
from the download so files sort chronologically inside each series folder.

`catalog.csv` has one row per image with columns:

`file`, `account`, `date`, `tweet_id`, `num`, `width`, `height`, `bytes`, `sha1`, `type`, `series`, `title`, `publisher`, `subject`, `recurrence`, `alt_type`, `notes`, `final_type`, `final_series`, `target`

Use it to filter by account, date, tweet ID, publisher, subject, or series.
The `target` column gives the file's path under `library/`.

To read the full tweet text for an image, look up its tweet ID in the
per-account metadata under `images/<account>/metadata/`."""


def render_index(rows: list[dict]) -> str:
    for i, row in enumerate(rows):
        key = split_target(row.get("target", ""))
        row["_type"], row["_series"] = key if key else ("", "")
        row["_order"] = i
    by_type: dict[str, list[dict]] = {}
    for row in rows:
        if not row["_type"]:
            continue
        by_type.setdefault(row["_type"], []).append(row)
    inbox = by_type.pop("_inbox", [])
    types = sorted(by_type)
    total_series = sum(len({r["_series"] for r in by_type[t]}) for t in types)
    if inbox:
        total_series += len({r["_series"] for r in inbox})
    lines = ["# Library index", "",
             f"Generated: {datetime.now(timezone.utc).date().isoformat()}", "",
             f"Total images: {len(rows)} in "
             f"{total_series} series across "
             f"{len(types)} type folders.", "",
             "Per-type totals:", ""]
    for t in types:
        series = {r["_series"] for r in by_type[t]}
        lines.append(f"- {t}: {len(by_type[t])} files in {len(series)} series")
    if inbox:
        iseries = {r["_series"] for r in inbox}
        lines.append(f"- _inbox: {len(inbox)} files in {len(iseries)} series")
    for t in types:
        members = by_type[t]
        lines += ["", f"## {t} ({len(members)} files, "
                  f"{len({r['_series'] for r in members})} series)", ""]
        lines += section_table(members)
    if inbox:
        lines += ["", f"## _inbox arrivals ({len(inbox)} files, "
                  f"{len({r['_series'] for r in inbox})} series)", ""]
        lines += section_table(inbox)
    lines += ["", INDEX_FOOTER, ""]
    for row in rows:
        row.pop("_type", None)
        row.pop("_series", None)
        row.pop("_order", None)
    # CRLF to match the existing file byte-for-byte style on any platform.
    return "\r\n".join(lines)


def classify() -> int:
    started = time.monotonic()
    accounts = load_accounts()
    rows = load_catalog()
    phashes = load_phashes()

    sha1_lookup: dict[str, dict] = {}
    for row in rows:
        if row.get("sha1") and row["sha1"] not in sha1_lookup:
            sha1_lookup[row["sha1"]] = row
    target_lookup = {row.get("target", ""): row for row in rows}
    # Redundant-download lookup by (account, tweet_id, num). When several
    # rows share the key, prefer one whose target exists on disk.
    triple_lookup: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        key = (row.get("account", ""), row.get("tweet_id", ""),
               row.get("num", ""))
        if not key[0] or not key[1] or not key[2]:
            continue
        prev = triple_lookup.get(key)
        if prev is None:
            triple_lookup[key] = row
        else:
            prev_target = prev.get("target", "")
            prev_ok = (bool(prev_target)
                       and (ROOT / prev_target.replace("\\", "/")).is_file())
            cur_target = row.get("target", "")
            if (not prev_ok and cur_target
                    and (ROOT / cur_target.replace("\\", "/")).is_file()):
                triple_lookup[key] = row

    # Backfill hashes for catalogued targets and any stray library images
    # missing from phashes.json (first run: the full ~6489-image build).
    want: set[str] = set()
    for target in target_lookup:
        if target and (ROOT / target.replace("\\", "/")).is_file():
            want.add(target)
    for path in LIBRARY.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            want.add(path.relative_to(ROOT).as_posix())
    backfilled = 0
    t0 = time.monotonic()
    for target in sorted(want - set(phashes)):
        dh = dhash_file(ROOT / target.replace("\\", "/"))
        if dh is not None:
            phashes[target] = dh
            backfilled += 1
    if backfilled:
        log(f"hashed {backfilled} library images in "
            f"{time.monotonic() - t0:.1f}s")
    # Template pool: (series placement, hash, target) for catalogued
    # library images with a known hash, excluding unreviewed inbox arrivals
    # (never auto-file off an unreviewed template).
    pool: list[tuple[tuple[str, str], str, str]] = []
    for t, h in phashes.items():
        if t not in target_lookup or is_inbox_row(target_lookup[t]):
            continue
        placed = split_target(t)
        if placed is None:
            continue
        pool.append((placed, h, t))

    per_account: dict[str, dict] = {}
    new_rows: list[dict] = []

    for account in accounts:
        folder = IMAGES / account
        found = auto = inbox_n = 0
        breakdown: Counter = Counter()
        if folder.is_dir():
            candidates = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
        else:
            candidates = []
        for src in candidates:
            parsed = parse_filename(src.name)
            if parsed is None:
                continue
            found += 1
            date, _, tweet_id, num = parsed
            # Redundant-download guard (before hashing): gallery-dl
            # re-downloads of an archived (account, tweet_id, num) may be
            # re-encoded so sha1 matching misses them. When the catalogued
            # target still exists on disk the image is already archived.
            prior = triple_lookup.get((account, tweet_id, num))
            if prior is not None:
                prior_target = prior.get("target", "")
                if (prior_target
                        and (ROOT / prior_target.replace("\\", "/")).is_file()):
                    log(f"already archived at {prior_target}, removing redundant download")
                    try:
                        os.remove(src)
                    except OSError as exc:
                        log(f"WARNING: could not remove redundant download {src} ({exc})")
                    continue
            try:
                digest = sha1_of(src)
            except OSError as exc:
                log(f"WARNING: unreadable image, leaving in place: {src} ({exc})")
                continue
            size = image_size(src)
            if size is None:
                continue
            width, height = size
            nbytes = src.stat().st_size
            dh = dhash_file(src)
            if dh is None:
                continue

            match = sha1_lookup.get(digest)
            if match is not None:
                # Single exception to never-delete: gallery-dl occasionally
                # re-downloads bytes already archived (identical sha1). When
                # the catalogued target still exists on disk the content is
                # provably already archived, so remove the redundant download
                # instead of appending a duplicate-target row.
                existing_target = match.get("target", "")
                if existing_target and (ROOT / existing_target.replace("\\", "/")).is_file():
                    log(f"already archived at {existing_target}, removing redundant download")
                    try:
                        os.remove(src)
                    except OSError as exc:
                        log(f"WARNING: could not remove redundant download {src} ({exc})")
                    continue
            if match is not None and is_inbox_row(match):
                # A duplicate of an unreviewed inbox file is still
                # unreviewed: route to this account's inbox, not to _inbox.
                match = None
            reason = ""
            if match is not None:
                reason = "exact"
            elif pool:
                # Margin is judged between series, not files: several
                # near-identical templates inside one recurring series must
                # not veto each other. A near-identical layout
                # (d <= PHASH_NEAR_MAX) is the same template, so the margin
                # is waived; distances 3..PHASH_AUTO_MAX still need it.
                best_per_series: dict[tuple[str, str], list] = {}
                for placed0, h, t in pool:
                    d = hamming(dh, h)
                    if placed0 not in best_per_series or d < best_per_series[placed0][0]:
                        best_per_series[placed0] = [d, t]
                ranked = sorted(best_per_series.values())
                if ranked and ranked[0][0] <= PHASH_NEAR_MAX:
                    best, best_target = ranked[0]
                    match = target_lookup[best_target]
                    reason = f"template d={best} margin=waived"
                elif len(ranked) >= 2:
                    (best, best_target), (second, _) = ranked[0], ranked[1]
                    if best <= PHASH_AUTO_MAX and second - best >= PHASH_MARGIN_MIN:
                        match = target_lookup[best_target]
                        reason = f"template d={best} margin={second - best}"
            subject = tweet_text(account, tweet_id)[:280]
            if match is not None:
                placed = split_target(match.get("target", ""))
                if placed is None:
                    log(f"WARNING: matched row has bad target "
                        f"{match.get('target')!r}; sending to inbox: {src.name}")
                    match = None
            if match is None:
                series_dir, type_dir = account, "_inbox"
                target = f"library/_inbox/{account}/{src.name}"
                new_rows.append({
                    "file": target, "account": account, "date": date,
                    "tweet_id": tweet_id, "num": num, "width": width,
                    "height": height, "bytes": nbytes, "sha1": digest,
                    "type": "inbox", "series": account,
                    "title": f"unclassified {account} {date}",
                    "publisher": "", "subject": subject, "recurrence": "",
                    "alt_type": "", "notes": "needs review",
                    "final_type": "inbox", "final_series": account,
                    "target": target,
                })
                inbox_n += 1
            else:
                type_dir, series_dir = placed  # type: ignore[misc]
                target = f"library/{type_dir}/{series_dir}/{src.name}"
                notes = ("auto-filed as exact repost" if reason == "exact"
                         else "auto-filed by template match")
                new_rows.append({
                    "file": target, "account": account, "date": date,
                    "tweet_id": tweet_id, "num": num, "width": width,
                    "height": height, "bytes": nbytes, "sha1": digest,
                    "type": type_dir, "series": series_dir,
                    "title": f"auto-filed {series_dir} {date}",
                    "publisher": match.get("publisher", ""),
                    "subject": subject,
                    "recurrence": match.get("recurrence", ""),
                    "alt_type": "", "notes": notes,
                    "final_type": type_dir, "final_series": series_dir,
                    "target": target,
                })
                auto += 1
                breakdown[series_dir] += 1
                log(f"auto-filed {src.name} -> {target} ({reason})")
            dest = ROOT / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            phashes[target] = dh
            placed_new = split_target(target)
            if placed_new is not None and not is_inbox_row(new_rows[-1]):
                pool.append((placed_new, dh, target))
            target_lookup[target] = new_rows[-1]
            if digest not in sha1_lookup:
                sha1_lookup[digest] = new_rows[-1]
            triple_lookup[(account, tweet_id, num)] = new_rows[-1]
        per_account[account] = {"found": found, "auto": auto,
                                "inbox": inbox_n, "breakdown": breakdown}

    if new_rows:
        rows.extend(new_rows)
        with open(CATALOG, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CATALOG_COLUMNS,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        with open(INDEX, "w", encoding="utf-8", newline="") as fh:
            fh.write(render_index(rows))

    # Prune hashes whose targets no longer exist (on any run, even idle).
    stale = [t for t in phashes if not (ROOT / t.replace("\\", "/")).exists()]
    for t in stale:
        del phashes[t]
    if new_rows or stale or backfilled or not PHASHES.exists():
        with open(PHASHES, "w", encoding="utf-8") as fh:
            json.dump({k: phashes[k] for k in sorted(phashes)}, fh, indent=2)
            fh.write("\n")

    total_found = sum(v["found"] for v in per_account.values())
    total_auto = sum(v["auto"] for v in per_account.values())
    total_inbox = sum(v["inbox"] for v in per_account.values())
    for account in accounts:
        v = per_account[account]
        log(f"{account}: new found={v['found']} auto-filed={v['auto']} "
            f"inbox={v['inbox']}")
        for series, n in sorted(v["breakdown"].items()):
            log(f"  {series}: {n}")
    elapsed = time.monotonic() - started
    log(f"done: {total_found} new, {total_auto} auto-filed, "
        f"{total_inbox} inbox in {elapsed:.1f}s")
    return 0


def main() -> int:
    try:
        return classify()
    except Exception:
        # Never fail the workflow; report and leave everything in place.
        log("ERROR: classifier failed; leaving new images in place:")
        log(traceback.format_exc())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
