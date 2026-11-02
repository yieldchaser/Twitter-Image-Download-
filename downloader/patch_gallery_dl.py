#!/usr/bin/env python3
"""Patch gallery-dl to transparently bypass Cloudflare bot detection on X/Twitter.

On cloud/datacenter IPs (such as GitHub Actions Azure runners), Cloudflare challenges
requests to https://x.com/home with a 403 ChallengeError because of datacenter IP reputation.

gallery-dl only accesses x.com/home to initialize Twitter client transaction keys
(verification key, ondemand.s script, indices, and SVG animation frames).

This patch:
1. Intercepts `ClientTransaction.initialize`:
   - First attempts the public `https://x.com/i/jf/` endpoint (which contains all
     required keys/frames without tripping Cloudflare /home bot mitigation).
   - Falls back to `curl_cffi` (Chrome TLS impersonation) if needed.
   - If Cloudflare still challenges or network fails, falls back to pre-cached
     client transaction keys stored in `config/twitter_keys.json`.
   - Caches any freshly extracted keys to `config/twitter_keys.json`.
2. Intercepts `ClientTransaction._extract_indices`:
   - Falls back to `curl_cffi` or cached indices if the JS bundle fetch fails.
3. Catches and recovers from `ChallengeError` on `/home` so the runner never aborts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Built-in fallback keys (valid current X client transaction keys)
DEFAULT_KEY_BYTES_HEX = (
    "bc7e9222e13c9d2227f4fbcc3a14d68075af046422ee1f1e8b49591773e1e5cb"
    "677191da4a66cd80ac43ac4d20c89cc3"
)
DEFAULT_ANIMATION_KEY = (
    "582470fd70a3d70a3d701c28f5c28f5c2901c28f5c28f5c290fd70a3d70a3d700"
)

KEYS_FILE = Path(__file__).resolve().parents[1] / "config" / "twitter_keys.json"
_PATCHED = False


def load_cached_keys() -> tuple[bytes, str]:
    """Load cached keys from disk or built-in defaults."""
    if KEYS_FILE.exists():
        try:
            data = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
            k_hex = data.get("key_bytes")
            a_key = data.get("animation_key")
            if k_hex and a_key:
                return bytes.fromhex(k_hex), a_key
        except Exception:
            pass
    return bytes.fromhex(DEFAULT_KEY_BYTES_HEX), DEFAULT_ANIMATION_KEY


def save_cached_keys(key_bytes: bytes, animation_key: str) -> None:
    """Save extracted keys to disk for offline / Cloudflare fallback."""
    try:
        KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "key_bytes": key_bytes.hex(),
            "animation_key": animation_key,
        }
        KEYS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        import gallery_dl.extractor.utils.twitter_transaction_id as tid
        import gallery_dl.util as gdl_util
        from gallery_dl import exception as gdl_exc
        from gallery_dl.extractor.common import Extractor
    except ImportError as exc:
        print(f"[patch_gallery_dl] Could not import gallery_dl internals: {exc}", file=sys.stderr)
        return

    orig_initialize = tid.ClientTransaction.initialize

    def patched_initialize(self, extractor, homepage=None):
        root = getattr(extractor, "root", "https://x.com") or "https://x.com"

        # If homepage was provided, try standard initialize first
        if homepage:
            try:
                orig_initialize(self, extractor, homepage=homepage)
                if hasattr(self, "key_bytes") and hasattr(self, "animation_key"):
                    save_cached_keys(self.key_bytes, self.animation_key)
                    return
            except Exception as exc:
                extractor.log.warning("[patch_gallery_dl] Provided homepage extraction failed: %s", exc)

        # Strategy 1: Fetch https://x.com/i/jf/ (public, no Cloudflare /home challenge)
        try:
            jf_url = root + "/i/jf/"
            extractor.log.info("[patch_gallery_dl] Fetching transaction keys from %s", jf_url)
            resp = extractor.request(jf_url)
            if resp.status_code == 200 and "loading-x-anim-" in resp.text:
                orig_initialize(self, extractor, homepage=resp.text)
                if hasattr(self, "key_bytes") and hasattr(self, "animation_key"):
                    extractor.log.info("[patch_gallery_dl] Successfully initialized keys via /i/jf/")
                    save_cached_keys(self.key_bytes, self.animation_key)
                    return
        except Exception as exc:
            extractor.log.warning("[patch_gallery_dl] /i/jf/ fetch failed (%s); trying /home", exc)

        # Strategy 2: Try /home (catching Cloudflare challenge)
        try:
            home_url = root + "/home"
            extractor.log.info("[patch_gallery_dl] Fetching transaction keys from %s", home_url)
            resp = extractor.request(home_url, allow_redirects=False)
            if 300 <= resp.status_code < 400:
                resp = extractor.request(root + "/i/jf/")
            if resp.status_code == 200 and "loading-x-anim-" in resp.text:
                orig_initialize(self, extractor, homepage=resp.text)
                if hasattr(self, "key_bytes") and hasattr(self, "animation_key"):
                    extractor.log.info("[patch_gallery_dl] Successfully initialized keys via /home")
                    save_cached_keys(self.key_bytes, self.animation_key)
                    return
        except (gdl_exc.ChallengeError, gdl_exc.HttpError) as exc:
            extractor.log.warning("[patch_gallery_dl] /home blocked by Cloudflare (%s)", exc)
        except Exception as exc:
            extractor.log.warning("[patch_gallery_dl] /home fetch failed: %s", exc)

        # Strategy 3: Try curl_cffi Chrome impersonation if available
        try:
            import curl_cffi.requests
            session = curl_cffi.requests.Session(impersonate="chrome")
            if hasattr(extractor, "cookies") and extractor.cookies:
                session.cookies.update(extractor.cookies)
            resp = session.get(root + "/i/jf/", timeout=20)
            if resp.status_code == 200 and "loading-x-anim-" in resp.text:
                orig_initialize(self, extractor, homepage=resp.text)
                if hasattr(self, "key_bytes") and hasattr(self, "animation_key"):
                    extractor.log.info("[patch_gallery_dl] Initialized keys via curl_cffi /i/jf/")
                    save_cached_keys(self.key_bytes, self.animation_key)
                    return
        except Exception as exc:
            extractor.log.warning("[patch_gallery_dl] curl_cffi fallback failed: %s", exc)

        # Strategy 4: Fallback to pre-cached keys (bypasses any Cloudflare block)
        extractor.log.warning("[patch_gallery_dl] Using pre-cached client transaction keys as fallback")
        k_bytes, a_key = load_cached_keys()
        self.key_bytes = k_bytes
        self.animation_key = a_key

    tid.ClientTransaction.initialize = patched_initialize

    # Patch 2: ClientTransaction._extract_indices fallback
    orig_extract_indices = tid.ClientTransaction._extract_indices

    def patched_extract_indices(self, ondemand_s, extractor):
        try:
            return orig_extract_indices(self, ondemand_s, extractor)
        except Exception as exc:
            extractor.log.warning("[patch_gallery_dl] Standard ondemand fetch failed (%s); trying curl_cffi", exc)
            try:
                import curl_cffi.requests
                url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{ondemand_s}a.js"
                page = curl_cffi.requests.get(url, impersonate="chrome", timeout=25).text
                pattern = gdl_util.re_compile(r"\(\w\[(\d\d?)\],\s*16\)")
                return [int(i) for i in pattern.findall(page)]
            except Exception:
                raise exc

    tid.ClientTransaction._extract_indices = patched_extract_indices

    # Patch 3: Suppress ChallengeError on /home request in extractor
    orig_request = Extractor.request

    def patched_request(self, url, *args, **kwargs):
        try:
            return orig_request(self, url, *args, **kwargs)
        except gdl_exc.ChallengeError as exc:
            if "/home" in url:
                self.log.warning("[patch_gallery_dl] Cloudflare challenge on %s intercepted; falling back to /i/jf/", url)
                root = getattr(self, "root", "https://x.com") or "https://x.com"
                return orig_request(self, root + "/i/jf/", *args, **kwargs)
            raise exc

    Extractor.request = patched_request


if __name__ == "__main__":
    apply_patches()
    print("Patches applied successfully.")
