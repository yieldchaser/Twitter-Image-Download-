#!/usr/bin/env python3
"""Run gallery-dl against configured X accounts.

Authentication is supplied at runtime as a Netscape cookies.txt file.
Each account is processed independently so one unavailable/renamed account
cannot discard successful downloads from the other accounts.

Every pass (and every deep-backfill window) is committed and pushed to
origin/main the moment it finishes, so a later hang or cancellation can
never lose already-downloaded data (runners are ephemeral; only origin
persists).

ACCOUNT_FILTER can be set to process one configured account only.
DEEP_BACKFILL=1 additionally pages the search index for full history in
date-bounded windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
GALLERY_CONFIG = ROOT / "config" / "gallery-dl.json"
GALLERY_DEEP_CONFIG = ROOT / "config" / "gallery-dl-deep.json"
COOKIE_FILE = ROOT / ".runtime" / "x-cookies.txt"
STATUS_DIR = ROOT / "metadata" / "status"

# Kill a gallery-dl pass that produces no output for this long. gallery-dl
# logs every page it fetches, so total silence means the process is wedged
# (observed in CI: one stalled request hung a 2-hour job with zero output).
# 10 minutes is far above any legitimate rate-limit backoff gallery-dl does.
STALL_TIMEOUT_SECONDS = 600

# Deep backfill pages the search index in date-bounded windows, committing
# after each one, so a job timeout can only ever lose the current window.
DEEP_WINDOW_DAYS = 183
DEEP_FLOOR_DEFAULT = "2015-01-01"
# Stop walking backwards after this many consecutive windows with no new
# media (empty windows cost seconds, so generous slack is cheap).
DEEP_EMPTY_WINDOWS_STOP = 3

GIT_AUTHOR_NAME = "github-actions[bot]"
GIT_AUTHOR_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def build_url(username: str) -> str:
    # The media profile timeline: covers roughly the last ~3,200 tweets.
    return f"https://x.com/{username}"


def build_search_url(username: str, since: str | None = None, until: str | None = None) -> str:
    # The search index reaches back years, so a from:<user> filter:media
    # search paginates much deeper for a full-history backfill. Requesting
    # the live tab avoids X defaulting to its "Top" ranking cutoff.
    query = f"from:{username} filter:media -filter:replies -filter:retweets"
    if since and until:
        query += f" since:{since} until:{until}"
    return f"https://x.com/search?q={quote(query, safe='')}&f=live"


def deep_windows(now: datetime, floor: str) -> list[tuple[str, str]]:
    """(since, until) date bounds walking backwards from now to floor,
    DEEP_WINDOW_DAYS at a time. Slight overlaps between windows are
    harmless: the dedup archive skips already-seen tweets."""
    floor_date = datetime.strptime(floor, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    windows: list[tuple[str, str]] = []
    until = now
    while until > floor_date:
        since = max(until - timedelta(days=DEEP_WINDOW_DAYS), floor_date)
        windows.append((since.date().isoformat(), until.date().isoformat()))
        until = since
    return windows


def run_gallery_dl_with_watchdog(args: list[str]) -> tuple[int, list[str]]:
    """Run gallery-dl, streaming its output live; kill the process when no
    new output appears for STALL_TIMEOUT_SECONDS."""
    process = subprocess.Popen(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    lines: list[str] = []
    last_output = time.monotonic()

    def watchdog() -> None:
        nonlocal last_output
        while process.poll() is None:
            time.sleep(5)
            if time.monotonic() - last_output > STALL_TIMEOUT_SECONDS:
                print(
                    f"!!! gallery-dl produced no output for "
                    f"{STALL_TIMEOUT_SECONDS}s; killing stalled process"
                )
                process.kill()
                return

    thread = threading.Thread(target=watchdog, daemon=True)
    thread.start()

    assert process.stdout is not None
    for line in process.stdout:
        last_output = time.monotonic()
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    code = process.wait()
    thread.join(timeout=15)
    return code, lines


def git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=False, text=True, capture_output=True
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def git_setup() -> None:
    git(["config", "user.name", GIT_AUTHOR_NAME])
    git(["config", "user.email", GIT_AUTHOR_EMAIL])


def commit_account(username: str, skip: set[str]) -> int:
    """Commit and push everything this account produced. Returns the number
    of committed files, so a silent no-op pass can never masquerade as
    success. Only new files or dedup-archive growth trigger a commit:
    metadata-only changes (live view/favorite counts) would be noise."""
    git(["add", "--", f"images/{username}", "metadata", "archive"])
    added = git(["diff", "--cached", "--name-only", "--diff-filter=A"]).stdout.split()
    added = [p for p in added if not any(p == s or p.endswith(s) for s in skip)]
    archive_changed = bool(
        git(["diff", "--cached", "--name-only", "--", "archive"]).stdout.strip()
    )
    if not added and not archive_changed:
        # Keep rewritten metadata staged; it rides along with the next real
        # commit instead of producing a noisy no-op one.
        return 0
    staged = git(["diff", "--cached", "--name-only"]).stdout.split()

    git(["commit", "-m", f"Archive {username} media"])
    for attempt in range(1, 6):
        push = git(["push", "origin", "HEAD:main"], check=False)
        if push.returncode == 0:
            return len(staged)
        print(f"Push rejected (attempt {attempt}); re-applying on origin/main")
        # Parallel matrix jobs push concurrently. Never use stash/add -A
        # here: -A once staged the runtime cookie file into a commit, and
        # reset --hard drops this job's own committed files from the
        # workspace. Cherry-picking our single commit sha is safe on a
        # shallow clone: only the sqlite dedup archive can conflict, and
        # keeping our version there is self-healing (a later run re-adds
        # any rows the other side wrote).
        ours = git(["rev-parse", "HEAD"]).stdout.strip()
        git(["fetch", "origin", "main"])
        git(["reset", "--hard", "origin/main"])
        pick = git(["cherry-pick", ours], check=False)
        if pick.returncode != 0:
            git(
                ["checkout", "--ours", "archive/gallery-dl-twitter.sqlite3"],
                check=False,
            )
            cont = git(
                ["-c", "core.editor=true", "cherry-pick", "--continue"],
                check=False,
            )
            if cont.returncode != 0:
                git(["cherry-pick", "--abort"], check=False)
    raise SystemExit("Could not push after 5 attempts")


def snapshot_images(username: str) -> set[str]:
    folder = ROOT / "images" / username
    if not folder.exists():
        return set()
    return {p.name for p in folder.glob("*") if p.is_file()}


def count_new_files(username: str, before: set[str]) -> int:
    return len(snapshot_images(username) - before)


def account_entry(
    username: str,
    url: str,
    code: int,
    output: str,
    downloaded: int,
) -> dict[str, Any]:
    no_results = "No results for " in output
    ok = code == 0 and not no_results
    if no_results:
        reason = "no media results returned by X/gallery-dl"
    elif code != 0:
        reason = f"gallery-dl exited with code {code}"
    else:
        reason = "media extraction completed"
    return {
        "username": username,
        "url": url,
        "returncode": code,
        "status": "ok" if ok else "failed",
        "reason": reason,
        "downloaded": downloaded,
    }


def run_pass(username: str, url: str, deep: bool) -> tuple[int, str, int]:
    before = snapshot_images(username)
    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--config",
        str(GALLERY_CONFIG),
    ]
    if deep:
        cmd += ["--config", str(GALLERY_DEEP_CONFIG)]
    cmd += ["--cookies", str(COOKIE_FILE), "--verbose", url]
    code, lines = run_gallery_dl_with_watchdog(cmd)
    return code, "".join(lines), count_new_files(username, before)


def write_status(username: str, status: dict[str, Any]) -> None:
    # Per-account files only: parallel matrix jobs must never write the
    # same status file, or their commits would conflict on every push.
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    path = STATUS_DIR / f"{username}.json"
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    accounts = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    account_filter = os.environ.get("ACCOUNT_FILTER", "").strip().lower()
    if account_filter:
        accounts = [a for a in accounts if a["username"].lower() == account_filter]
        if not accounts:
            raise SystemExit(f"ACCOUNT_FILTER did not match a configured account: {account_filter}")

    if not COOKIE_FILE.exists() or COOKIE_FILE.stat().st_size == 0:
        raise SystemExit("Missing runtime X cookie file")

    deep_backfill = os.environ.get("DEEP_BACKFILL", "").strip().lower() in {"1", "true", "yes"}
    deep_floor = os.environ.get("DEEP_FLOOR", DEEP_FLOOR_DEFAULT)
    windows = deep_windows(datetime.now(timezone.utc), deep_floor) if deep_backfill else []

    git_setup()
    for account in accounts:
        username = account["username"]
        status: dict[str, Any] = {
            "username": username,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "deep_backfill" if deep_backfill else "regular",
            "status": "ok",
            "reason": "media extraction completed",
            "downloaded": 0,
            "committed_files": 0,
        }
        skip = set()

        # Each pass commits immediately: a later hang past the job timeout
        # cannot lose this pass's data.
        print(f"Running gallery-dl for {username}: {build_url(username)}")
        code, output, downloaded = run_pass(username, build_url(username), deep=False)
        entry = account_entry(username, build_url(username), code, output, downloaded)
        if not deep_backfill:
            # The daily media-timeline pass is authoritative for health.
            status.update({k: entry[k] for k in ("status", "reason", "returncode")})
        status["downloaded"] += downloaded

        try:
            status["committed_files"] += commit_account(username, skip)
        except SystemExit as exc:
            status["status"] = "failed"
            status["reason"] = f"commit failed: {exc}"

        if deep_backfill:
            deep_summary: dict[str, Any] = {
                "windows_total": len(windows),
                "windows_with_downloads": 0,
                "downloaded": 0,
            }
            empty_streak = 0
            for since, until in windows:
                url = build_search_url(username, since, until)
                print(f"Running gallery-dl deep backfill for {username}: {url}")
                d_code, d_output, d_downloaded = run_pass(username, url, deep=True)
                no_results = "No results for " in d_output
                deep_summary["downloaded"] += d_downloaded
                status["downloaded"] += d_downloaded
                if d_code != 0 and not no_results:
                    status["status"] = "failed"
                    status["reason"] = f"deep window {since}..{until} exited {d_code}"
                    deep_summary.setdefault("errors", []).append(
                        {"window": f"{since}..{until}", "returncode": d_code}
                    )
                if d_downloaded == 0:
                    empty_streak += 1
                    if empty_streak >= DEEP_EMPTY_WINDOWS_STOP:
                        deep_summary["stopped_early"] = (
                            f"{DEEP_EMPTY_WINDOWS_STOP} consecutive empty windows"
                        )
                        break
                else:
                    empty_streak = 0
                    deep_summary["windows_with_downloads"] += 1

                # Commit after EVERY window: a job timeout can then only
                # ever lose the current window, never completed ones.
                try:
                    status["committed_files"] += commit_account(username, skip)
                except SystemExit as exc:
                    status["status"] = "failed"
                    status["reason"] = f"commit failed: {exc}"
                write_status(username, status)

            entry["deep_backfill"] = deep_summary

        status["detail"] = entry
        write_status(username, status)

        if status["status"] != "ok":
            print(f"Account {username} failed: {status['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
