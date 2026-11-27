"""Pull ``GET /api/export.zip`` from an edge. The token is a header, never a log line."""
from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from typing import Callable

RETRYABLE = {429, 500, 502, 503, 504}
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


class FetchError(RuntimeError):
    """A pull failed. The message has no token."""


def safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ")
    if "Bearer " in text:
        text = text.split("Bearer ", 1)[0] + "Bearer [redacted]"
    return text[:300]


def _ssl_context(verify: bool) -> ssl.SSLContext:
    if verify:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would send the bearer token to whatever host answered."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise FetchError(f"HTTP {code}")


def _opener(verify: bool):
    context = _ssl_context(verify)
    https = urllib.request.HTTPSHandler(context=context)
    return urllib.request.build_opener(_RefuseRedirect, https)


def _read_limited(response, max_bytes: int) -> bytes:
    headers = getattr(response, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared:
        try:
            if int(declared) > max_bytes:
                raise FetchError("export exceeds max_upload_bytes")
        except ValueError:
            pass
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise FetchError("export exceeds max_upload_bytes")
    return body


def pull_export(
    base_url: str,
    token: str,
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
    verify: bool = True,
    timeout: float = 60.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    sleep: Callable[[float], None] = time.sleep,
    urlopen=None,
) -> bytes:
    """GET ``{base}/api/export.zip`` with ``Authorization: Bearer``.

    Retries connection failures and HTTP 429, 500, 502, 503, and 504.
    Delay is ``backoff_s * 2**attempt``, or ``Retry-After`` when it is a
    number of seconds. 401, 403, 404, and redirects are not retried.
    The body is refused when it is larger than ``max_bytes``.
    """
    url = base_url.rstrip("/") + "/api/export.zip"
    context = _ssl_context(verify)
    builtin = _opener(verify).open

    def _open(request):
        if urlopen is None:
            return builtin(request, timeout=timeout)
        return urlopen(request, timeout=timeout, context=context)

    last = "pull failed"
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/zip",
                "User-Agent": "open-hvac-central",
            },
            method="GET",
        )
        try:
            with _open(request) as response:
                return _read_limited(response, max_bytes)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code not in RETRYABLE or attempt + 1 == attempts:
                raise FetchError(last) from None
            delay = backoff_s * (2**attempt)
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    delay = min(float(retry_after), 30.0)
                except ValueError:
                    pass
            sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = safe_error(exc) or "connection failed"
            if attempt + 1 == attempts:
                raise FetchError(last) from None
            sleep(backoff_s * (2**attempt))
    raise FetchError(last)
