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
COOKIE_FILE = ROOT / ".runtime" / "x-cookies.txt"
STATUS_FILE = ROOT / "metadata" / "download_status.json"


def build_url(username: str) -> str:
    # X can expose a public profile in a browser while gallery-dl's direct
    # user-resolution path fails. Search URLs avoid that resolution step.
    if username.lower() == "casharmax":
        query = quote(f"from:{username} filter:links", safe="")
        return f"https://x.com/search?q={query}"
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
    status = {"accounts": [], "overall_success": False}
    any_success = False

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
        result = subprocess.run(cmd, cwd=ROOT, check=False)
        ok = result.returncode == 0
        any_success = any_success or ok
        status["accounts"].append({
            "username": username,
            "url": url,
            "returncode": result.returncode,
            "status": "ok" if ok else "failed",
        })
        if not ok:
            print(f"Account {username} failed with exit code {result.returncode}; continuing")

    status["overall_success"] = any_success
    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    if not any_success:
        print("All configured accounts failed")
        return 1

    print("At least one account completed successfully; preserving downloaded media")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
