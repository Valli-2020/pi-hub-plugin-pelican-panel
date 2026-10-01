"""Pelican client-API HTTP layer: one request, one masked error type.

Plain ``urllib``, no redirects (they would replay the bearer key), a
per-request SSL context when verification is off.  Callers only ever see
:class:`PanelError` with a user-safe ``kind``; the detail goes to the log.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF_S = 60

#: Scheme + host + optional port, nothing else.  ``fullmatch`` (not a
#: ``$``-anchored search, which would accept a trailing newline) plus an
#: explicit CR/LF check.  IPv6 literals and sub-path installs are
#: deliberately unsupported — see README "Limitations".
_BASE_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?")


class PanelError(Exception):
    """A failed panel request, reduced to a user-safe ``kind``.

    ``kind`` is one of ``unauthorized`` / ``unavailable`` / ``rate
    limited`` / ``unreachable`` / ``error``.  The detailed cause stays in
    ``detail`` and only ever reaches the server log.
    """

    def __init__(self, kind: str, detail: str = "", retry_after: float = 0.0):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    urllib's default handler would replay the ``Authorization`` header
    against the redirect target — a one-hop API key exfiltration.
    Returning ``None`` makes urllib raise the 3xx as an ``HTTPError``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_base_url(url: str) -> bool:
    """True when *url* is a bare ``scheme://host[:port]`` with no CR/LF."""
    if not url or "\r" in url or "\n" in url:
        return False
    return _BASE_URL_RE.fullmatch(url) is not None


def request(cfg: dict[str, Any], method: str, path: str,
            payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform one panel request and return the decoded JSON body.

    *cfg* needs ``base_url``, ``api_key``, ``timeout`` and ``verify_ssl``.
    Raises :class:`PanelError` for every failure.
    """
    if not valid_base_url(cfg["base_url"]):
        raise PanelError("error", "base_url invalid")
    if not cfg["api_key"]:
        raise PanelError("unauthorized", "api_key not configured")

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        cfg["base_url"] + path, data=data, headers=headers, method=method)

    # Per-request SSL context when verification is off — mutating the
    # module-level default would disable verification for the whole
    # Pi Hub process, not just this plugin.
    handlers: list[Any] = [_NoRedirect()]
    if not cfg["verify_ssl"]:
        insecure = ssl.create_default_context()
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=insecure))
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=cfg["timeout"]) as resp:
            raw = resp.read()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise _http_error(e, method, path) from None
    except urllib.error.URLError as e:
        log.warning("pelican-panel: %s %s unreachable: %s", method, path,
                    e.reason)
        raise PanelError("unreachable", str(e.reason)) from None
    except (OSError, ValueError) as e:
        log.warning("pelican-panel: %s %s failed: %s", method, path, e)
        raise PanelError("unreachable", str(e)) from None


def _http_error(e: urllib.error.HTTPError, method: str, path: str) -> PanelError:
    """Map an HTTP status onto a masked :class:`PanelError`."""
    log.warning("pelican-panel: %s %s returned HTTP %s", method, path, e.code)
    if e.code in (401, 403):
        return PanelError("unauthorized", f"HTTP {e.code}")
    if e.code == 409:
        return PanelError("unavailable", "HTTP 409")
    if e.code == 429:
        retry_after = float(RATE_LIMIT_BACKOFF_S)
        header = e.headers.get("Retry-After") if e.headers else None
        try:
            if header is not None:
                retry_after = max(1.0, float(str(header).strip()))
        except ValueError:
            pass
        return PanelError("rate limited", "HTTP 429", retry_after)
    return PanelError("error", f"HTTP {e.code}")
