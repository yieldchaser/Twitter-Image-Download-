#!/usr/bin/env python3
"""Patch gallery-dl to transparently bypass Cloudflare bot detection on X/Twitter.

When running on cloud/datacenter IPs (such as GitHub Actions Azure/AWS runners),
Cloudflare challenges requests to x.com/home with a 403 ChallengeError because
standard Python requests/urllib3 TLS client-hello fingerprints match known bot patterns.

curl_cffi impersonates genuine Chrome browser TLS handshakes (ciphers, extensions,
JA3/JA4 fingerprint, ALPN, HTTP/2 settings).

This module patches:
1. ClientTransaction.initialize(): fetches https://x.com/home using curl_cffi
   with Chrome impersonation and the current cookies, bypassing the Cloudflare wall
   and feeding the genuine verification key, ondemand.s script, and animation frames
   into the key generator.
2. ClientTransaction._extract_indices(): falls back to curl_cffi if the ondemand
   JS bundle fetch is blocked.
3. Extractor.request(): if any other request encounters a ChallengeError or 403,
   retries with curl_cffi Chrome impersonation.
"""

from __future__ import annotations

import os
import sys

_PATCHED = False


def apply_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    try:
        import curl_cffi.requests
    except ImportError:
        print("[patch_gallery_dl] Note: curl_cffi not installed; running unpatched gallery-dl", file=sys.stderr)
        return

    try:
        import gallery_dl.extractor.utils.twitter_transaction_id as tid
        import gallery_dl.util as gdl_util
        from gallery_dl import exception as gdl_exc
        from gallery_dl.extractor.common import Extractor
    except ImportError as exc:
        print(f"[patch_gallery_dl] Could not import gallery_dl internals: {exc}", file=sys.stderr)
        return

    # Patch 1: ClientTransaction.initialize
    orig_initialize = tid.ClientTransaction.initialize

    def patched_initialize(self, extractor, homepage=None):
        if homepage is None:
            try:
                session = curl_cffi.requests.Session(impersonate="chrome")
                # Load cookies from extractor session
                if hasattr(extractor, "cookies") and extractor.cookies:
                    session.cookies.update(extractor.cookies)
                # Also load directly from cookies_file if present
                cookie_file = getattr(extractor, "cookies_file", None)
                if cookie_file and os.path.exists(cookie_file):
                    import http.cookiejar
                    try:
                        jar = http.cookiejar.MozillaCookieJar(cookie_file)
                        jar.load(ignore_discard=True, ignore_expires=True)
                        session.cookies.update(jar)
                    except Exception:
                        pass

                url = (extractor.root if hasattr(extractor, "root") and extractor.root else "https://x.com") + "/home"
                resp = session.get(url, allow_redirects=False, timeout=25)
                if resp.status_code == 200 and "loading-x-anim-" in resp.text:
                    extractor.log.info("[patch_gallery_dl] Fetched X homepage via curl_cffi (Chrome TLS impersonation)")
                    homepage = resp.text
                elif 300 <= resp.status_code < 400:
                    location = resp.headers.get("location") or ""
                    extractor.log.warning("[patch_gallery_dl] HTTP redirect to %s; trying /i/jf/", location)
                    jf_url = (extractor.root if hasattr(extractor, "root") and extractor.root else "https://x.com") + "/i/jf/"
                    resp_jf = session.get(jf_url, timeout=25)
                    if resp_jf.status_code == 200:
                        homepage = resp_jf.text
            except Exception as exc:
                extractor.log.warning("[patch_gallery_dl] curl_cffi /home fetch failed (%s); trying default", exc)

        return orig_initialize(self, extractor, homepage=homepage)

    tid.ClientTransaction.initialize = patched_initialize

    # Patch 2: ClientTransaction._extract_indices
    orig_extract_indices = tid.ClientTransaction._extract_indices

    def patched_extract_indices(self, ondemand_s, extractor):
        try:
            return orig_extract_indices(self, ondemand_s, extractor)
        except Exception as exc:
            extractor.log.warning("[patch_gallery_dl] Standard fetch of ondemand script failed (%s); using curl_cffi", exc)
            url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{ondemand_s}a.js"
            page = curl_cffi.requests.get(url, impersonate="chrome", timeout=25).text
            pattern = gdl_util.re_compile(r"\(\w\[(\d\d?)\],\s*16\)")
            return [int(i) for i in pattern.findall(page)]

    tid.ClientTransaction._extract_indices = patched_extract_indices

    # Patch 3: Extractor.request fallback for Cloudflare ChallengeError
    orig_request = Extractor.request

    def patched_request(self, url, *args, **kwargs):
        try:
            return orig_request(self, url, *args, **kwargs)
        except gdl_exc.ChallengeError as exc:
            self.log.warning("[patch_gallery_dl] Cloudflare challenge encountered for %s; retrying via curl_cffi", url)
            try:
                session = curl_cffi.requests.Session(impersonate="chrome")
                if hasattr(self, "cookies") and self.cookies:
                    session.cookies.update(self.cookies)
                cookie_file = getattr(self, "cookies_file", None)
                if cookie_file and os.path.exists(cookie_file):
                    import http.cookiejar
                    try:
                        jar = http.cookiejar.MozillaCookieJar(cookie_file)
                        jar.load(ignore_discard=True, ignore_expires=True)
                        session.cookies.update(jar)
                    except Exception:
                        pass
                method = kwargs.get("method", "GET")
                headers = kwargs.get("headers") or {}
                data = kwargs.get("data")
                timeout = kwargs.get("timeout", 30)
                resp = session.request(method, url, headers=headers, data=data, timeout=timeout)
                if resp.status_code < 400:
                    return resp
            except Exception as inner_exc:
                self.log.debug("[patch_gallery_dl] curl_cffi retry failed: %s", inner_exc)
            raise exc

    Extractor.request = patched_request


if __name__ == "__main__":
    apply_patches()
    print("Patches applied successfully.")
