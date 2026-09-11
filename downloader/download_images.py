#!/usr/bin/env python3
"""Run gallery-dl against configured X accounts.

Authentication is supplied at runtime as a Netscape cookies.txt file.
Each account is processed independently so one unavailable/renamed account
cannot discard successful downloads from the other accounts.

Every account's results are committed and pushed to origin/main immediately
after that account finishes, so a later hang or cancellation can never lose
already-downloaded data (runners are ephemeral; only origin persists).

ACCOUNT_FILTER can be set to process one configured account only.
DEEP_BACKFILL=1 additionally pages the search index for full history.
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
STATUS_FILE = ROOT / "metadata" / "download_status.json"

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
    DEEP_WINDOW_DAYS at a time (slight overlaps are harmless: the dedup
    archive skips already-seen tweets)."""
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


def commit_account(username: str) -> int:
    """Commit and push everything this account produced. Returns the number
    of committed files, so a silent no-op pass can never masquerade as
    success."""
    git(["add", "--", f"images/{username}", "metadata", "archive"])
    staged = git(["diff", "--cached", "--name-only"]).stdout.split()
    if not staged:
        return 0

    git(["commit", "-m", f"Archive {username} media"])
    for attempt in range(1, 6):
        push = git(["push", "origin", "HEAD:main"], check=False)
        if push.returncode == 0:
            return len(staged)
        print(f"Push rejected (attempt {attempt}); re-committing on origin/main")
        # Shallow CI checkouts cannot always rebase, so instead: move the
        # branch to the fetched tip with our changes kept staged, and
        # re-commit on top. No merge conflicts are possible this way.
        git(["fetch", "origin", "main"])
        git(["reset", "--soft", "origin/main"])
        commit = git(["commit", "-m", f"Archive {username} media"], check=False)
        if commit.returncode != 0:
            # Nothing left to commit: origin already has these files.
            return 0
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


def run_timeline_pass(username: str) -> tuple[int, str, int]:
    url = build_url(username)
    before = snapshot_images(username)
    code, lines = run_gallery_dl_with_watchdog(
        [
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
    )
    return code, "".join(lines), count_new_files(username, before)


def run_deep_pass(username: str, url: str) -> tuple[int, str, int]:
    before = snapshot_images(username)
    code, lines = run_gallery_dl_with_watchdog(
        [
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
            url,
        ]
    )
    return code, "".join(lines), count_new_files(username, before)


def write_status(status: dict[str, Any]) -> None:
    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


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

    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "deep_backfill" if deep_backfill else "regular",
        "accounts": [],
        "overall_success": False,
        "expected_accounts": [a["username"] for a in accounts],
    }

    git_setup()
    for account in accounts:
        username = account["username"]

        # Each pass commits immediately: if a later pass hangs past the job
        # timeout and the runner is destroyed, earlier passes stay on origin.
        print(f"Running gallery-dl for {username}: {build_url(username)}")
        code, output, downloaded = run_timeline_pass(username)
        entry = account_entry(username, build_url(username), code, output, downloaded)
        try:
            entry["committed_files"] = commit_account(username)
        except SystemExit as exc:
            entry["commit_error"] = str(exc)
        status["accounts"].append(entry)
        write_status(status)

        if deep_backfill:
            floor = os.environ.get("DEEP_FLOOR", DEEP_FLOOR_DEFAULT)
            windows = deep_windows(datetime.now(timezone.utc), floor)
            deep_summary: dict[str, Any] = {
                "status": "ok",
                "windows_total": len(windows),
                "windows_downloaded": 0,
                "downloaded": 0,
            }
            empty_streak = 0
            stopped_early = False
            for since, until in windows:
                url = build_search_url(username, since, until)
                print(f"Running gallery-dl deep backfill for {username}: {url}")
                deep_code, deep_output, deep_downloaded = run_deep_pass(username, url)
                no_results = "No results for " in deep_output
                deep_summary["downloaded"] += deep_downloaded
                entry["downloaded"] = entry.get("downloaded", 0) + deep_downloaded
                if no_results or deep_code != 0:
                    deep_summary["status"] = "failed" if deep_code != 0 else deep_summary["status"]
                    if not no_results:
                        deep_summary.setdefault("errors", []).append(
                            {"window": f"{since}..{until}", "returncode": deep_code}
                        )
                if deep_downloaded == 0:
                    empty_streak += 1
                    if empty_streak >= DEEP_EMPTY_WINDOWS_STOP:
                        stopped_early = True
                        break
                else:
                    empty_streak = 0
                    deep_summary["windows_downloaded"] += 1

                # Commit after EVERY window: a job timeout can then only
                # ever lose the current window, never completed ones.
                try:
                    commit_account(username)
                except SystemExit as exc:
                    deep_summary.setdefault("commit_errors", []).append(
                        {"window": f"{since}..{until}", "error": str(exc)}
                    )
                write_status(status)

            if stopped_early:
                deep_summary["stopped_early"] = f"{DEEP_EMPTY_WINDOWS_STOP} consecutive empty windows"
            entry["deep_backfill"] = deep_summary
            write_status(status)

        if entry["status"] != "ok":
            print(f"Account {username} failed validation: {entry['reason']}")

    status["overall_success"] = all(a["status"] == "ok" for a in status["accounts"])
    write_status(status)

    passed = sum(a["status"] == "ok" for a in status["accounts"])
    print(f"Account extraction finished: {passed}/{len(status['accounts'])} accounts passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
