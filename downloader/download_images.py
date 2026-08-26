#!/usr/bin/env python3
"""Run gallery-dl against the configured X accounts.

Authentication is supplied at runtime as a Netscape cookies.txt file.
No X API key is required.
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


def main() -> int:
    accounts = json.loads(CONFIG.read_text(encoding="utf-8"))["accounts"]
    if not COOKIE_FILE.exists() or COOKIE_FILE.stat().st_size == 0:
        raise SystemExit("Missing runtime X cookie file")

    urls = [f"https://x.com/{a['username']}" for a in accounts]
    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--config",
        str(GALLERY_CONFIG),
        "--cookies",
        str(COOKIE_FILE),
        "--verbose",
        *urls,
    ]
    print("Running gallery-dl for", ", ".join(a["username"] for a in accounts))
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
