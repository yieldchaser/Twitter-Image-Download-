#!/usr/bin/env python3
"""Convert Cookie-Editor JSON export to Netscape cookies.txt.

Cookie values are never printed. The converter accepts the common Cookie-Editor
array format and a few equivalent object formats.
"""

import json
import sys
from pathlib import Path


def normalise(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        # Cookie-Editor can export an object keyed by cookie name.
        if all(isinstance(v, (str, int, float, type(None))) for v in raw.values()):
            return [{"name": k, "value": "" if v is None else str(v), "domain": ".x.com", "path": "/"}
                    for k, v in raw.items()]
        for key in ("cookies", "data"):
            if isinstance(raw.get(key), list):
                return raw[key]
    raise ValueError("Unsupported Cookie-Editor JSON structure")


def main(src, dst):
    raw = json.loads(Path(src).read_text(encoding="utf-8"))
    cookies = normalise(raw)
    lines = ["# Netscape HTTP Cookie File", "# Generated locally for gallery-dl", ""]
    written = 0
    for c in cookies:
        name = c.get("name")
        value = c.get("value", "")
        domain = c.get("domain") or ".x.com"
        path = c.get("path") or "/"
        if not name or value is None:
            continue
        # Restrict the secret to X domains; unrelated Cookie-Editor entries are ignored.
        if not (domain == "x.com" or domain.endswith(".x.com")):
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure", True) else "FALSE"
        expiry = c.get("expirationDate", c.get("expiration", c.get("expires", 0)))
        try:
            expiry = int(float(expiry or 0))
        except (TypeError, ValueError):
            expiry = 0
        lines.append("\t".join([domain, include_subdomains, path, secure, str(expiry), str(name), str(value)]))
        written += 1

    if written == 0:
        raise SystemExit("No x.com cookies found in the supplied export")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Converted {written} X cookies")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: convert_cookie_editor.py INPUT_JSON OUTPUT_TXT")
    main(sys.argv[1], sys.argv[2])
