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

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "accounts.json"
GALLERY_CONFIG = ROOT / "config" / "gallery-dl.json"
COOKIE_FILE = ROOT / ".runtime" / "x-cookies.txt"
STATUS_FILE = ROOT / "metadata" / "download_status.json"


def build_url(username: str) -> str:
    # Use the account's media profile directly. The gallery-dl Twitter
    # extractor can paginate the media timeline and use its configured
    # search fallback when necessary.
    return f"https://x.com/{username}"


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

        if no_results:
            reason = "no media results returned by X/gallery-dl"
        elif result.returncode != 0:
            reason = f"gallery-dl exited with code {result.returncode}"
        else:
            reason = "media extraction completed"

        status["accounts"].append({
            "username": username,
            "url": url,
            "returncode": result.returncode,
            "status": "ok" if ok else "failed",
            "reason": reason,
        })

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
