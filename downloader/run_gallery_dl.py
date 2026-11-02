#!/usr/bin/env python3
"""Run gallery-dl with Cloudflare bypass patches applied."""

import sys
from pathlib import Path

# Ensure repo root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from downloader.patch_gallery_dl import apply_patches
    apply_patches()
except Exception as exc:
    print(f"[run_gallery_dl] Warning: failed to apply patches: {exc}", file=sys.stderr)

import gallery_dl

if __name__ == "__main__":
    sys.exit(gallery_dl.main())
