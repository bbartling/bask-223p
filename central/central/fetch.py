"""Pull ``GET /api/export.zip`` from an edge. The token is a header, never a log line."""
from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from typing import Callable

RETRYABLE = {429, 500, 502, 503, 504}


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


def pull_export(
    base_url: str,
    token: str,
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
    verify: bool = True,
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    urlopen=None,
) -> bytes:
    """GET ``{base}/api/export.zip`` with ``Authorization: Bearer``.

    Retries connection failures and HTTP 429, 500, 502, 503, and 504.
    Delay is ``backoff_s * 2**attempt``, or ``Retry-After`` when it is a
    number of seconds. 401, 403, and 404 are not retried.
    """
    url = base_url.rstrip("/") + "/api/export.zip"
    opener = urlopen or urllib.request.urlopen
    context = _ssl_context(verify)
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
            with opener(request, timeout=timeout, context=context) as response:
                return response.read()
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
