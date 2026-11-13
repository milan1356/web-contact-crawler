"""Fetch a public page, following HTTP redirects and tolerating old TLS.

Some hosts close HTTP/1.1 connections. When urllib gets nothing back, curl
is used as a fallback because it can speak HTTP/2.
"""

from __future__ import annotations

import base64
import os
import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from http.client import HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from contact_crawler.env import load_env

load_env()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

MAX_BYTES = 2_000_000

# Used only after a page comes back blocked. Direct requests stay off this proxy.
CRAWLERA_KEY = os.environ.get("ZYTE_API_KEY", "9624aea48eb244d5b0906dc326dbdb72")
PROXY_HOST = os.environ.get("ZYTE_PROXY_HOST", "api.zyte.com")
PROXY_PORT = os.environ.get("ZYTE_PROXY_PORT", "8011")
CERT_PATH = os.environ.get(
    "ZYTE_CA_CERT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "zyte-ca.crt"),
)

_SSL = ssl.create_default_context()
_SSL_INSECURE = ssl._create_unverified_context()


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    html: str
    error: str | None = None
    insecure_tls: bool = False
    via_proxy: bool = False


class RateLimiter:
    """Spaces request starts so all worker threads together stay under a per-second cap."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class Fetcher:
    def __init__(self, timeout: float = 15.0, requests_per_second: float = 10.0) -> None:
        self.timeout = timeout
        self.limiter = RateLimiter(requests_per_second)
        self.api_key = CRAWLERA_KEY
        self.proxy_host = PROXY_HOST
        self.proxy_port = PROXY_PORT
        self.cert_path = CERT_PATH

    @property
    def proxy_enabled(self) -> bool:
        return bool(self.api_key) and os.path.isfile(self.cert_path)

    def fetch(self, url: str, via_proxy: bool = False) -> FetchResult:
        if via_proxy:
            return self._fetch_via_proxy(url)
        result = self._fetch_once(url, insecure=False)
        if result.error and _is_tls_error(result.error):
            insecure = self._fetch_once(url, insecure=True)
            insecure.insecure_tls = True
            if insecure.status or insecure.html:
                result = insecure
        if result.status == 0 or (result.status and not result.html and result.status < 400):
            retry = self._fetch_once(url, insecure=result.insecure_tls)
            if len(retry.html) > len(result.html) or (result.status == 0 and retry.status):
                result = retry
        if _should_try_curl(result):
            curled = self._fetch_curl(url)
            if curled.html and (curled.status == 200 or len(curled.html) > len(result.html)):
                return curled
        return result

    def _fetch_curl(self, url: str) -> FetchResult:
        return _fetch_curl_page(url, self.timeout, insecure=False, wait=self.limiter.wait)

    def _fetch_via_proxy(self, url: str) -> FetchResult:
        """One retry through Zyte. Call this only after a direct fetch was blocked.

        urllib's ProxyHandler does not send Proxy-Authorization on the HTTPS
        CONNECT, which makes Zyte answer 407. This opens the tunnel itself.
        """
        if not self.api_key:
            return FetchResult(url, url, 0, "", error="ZYTE_API_KEY is empty")
        if not os.path.isfile(self.cert_path):
            return FetchResult(url, url, 0, "", error=f"Zyte CA cert not found at {self.cert_path}")
        try:
            final_url, status, raw, charset = _zyte_get(
                url,
                api_key=self.api_key,
                proxy_host=self.proxy_host,
                proxy_port=int(self.proxy_port),
                cert_path=self.cert_path,
                timeout=self.timeout,
                wait=self.limiter.wait,
            )
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            return FetchResult(url, url, 0, "", error=str(exc), via_proxy=True)
        return FetchResult(
            requested_url=url,
            final_url=final_url,
            status=status,
            html=_decode(raw, charset),
            via_proxy=True,
            error=None if status else "Zyte proxy failed",
        )

    def _fetch_once(self, url: str, insecure: bool) -> FetchResult:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        context = _SSL_INSECURE if insecure else _SSL
        self.limiter.wait()
        try:
            with urlopen(request, timeout=self.timeout, context=context) as response:
                raw = response.read(MAX_BYTES)
                charset = response.headers.get_content_charset() or _charset_from(raw)
                return FetchResult(
                    requested_url=url,
                    final_url=response.geturl(),
                    status=getattr(response, "status", 200) or 200,
                    html=_decode(raw, charset),
                    insecure_tls=insecure,
                )
        except HTTPError as exc:
            raw = exc.read(MAX_BYTES) if exc.fp else b""
            charset = exc.headers.get_content_charset() if exc.headers else None
            charset = charset or _charset_from(raw)
            return FetchResult(
                requested_url=url,
                final_url=exc.geturl() or url,
                status=exc.code or 0,
                html=_decode(raw, charset),
                error=str(exc),
                insecure_tls=insecure,
            )
        except URLError as exc:
            return FetchResult(url, url, 0, "", error=str(exc.reason), insecure_tls=insecure)
        except Exception as exc:  # noqa: BLE001 - keep one bad host from killing the batch
            return FetchResult(url, url, 0, "", error=str(exc), insecure_tls=insecure)


def _zyte_get(
    url: str,
    api_key: str,
    proxy_host: str,
    proxy_port: int,
    cert_path: str,
    timeout: float,
    wait: Callable[[], None] | None = None,
) -> tuple[str, int, bytes, str | None]:
    """GET one URL through the Zyte HTTP proxy, following a few redirects."""
    context = ssl.create_default_context(cafile=cert_path)
    auth = base64.b64encode(f"{api_key}:".encode()).decode()
    current = url
    for _ in range(5):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname:
            raise OSError(f"Zyte retry only supports https URLs, got {current}")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if wait:
            wait()
        connection = HTTPSConnection(proxy_host, proxy_port, context=context, timeout=timeout)
        connection.set_tunnel(
            parsed.hostname,
            parsed.port or 443,
            headers={"Proxy-Authorization": f"Basic {auth}"},
        )
        connection.request(
            "GET",
            path,
            headers={
                "Host": parsed.hostname,
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(MAX_BYTES)
        status = response.status or 0
        location = response.getheader("Location")
        charset = None
        content_type = response.getheader("Content-Type") or ""
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
        connection.close()
        if status in {301, 302, 303, 307, 308} and location:
            current = urljoin(current, location)
            continue
        return current, status, raw, charset
    raise OSError("too many redirects through the Zyte proxy")


def _should_try_curl(result: FetchResult) -> bool:
    if shutil.which("curl") is None:
        return False
    if result.status == 0:
        return True
    return result.status >= 500 and len(result.html) < 100


def _fetch_curl_page(
    url: str,
    timeout: float,
    insecure: bool,
    wait: Callable[[], None] | None = None,
) -> FetchResult:
    command = [
        "curl",
        "-sL",
        "--compressed",
        "--http2",
        "--max-time",
        str(max(5, int(timeout))),
        "-A",
        "Mozilla/5.0",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-w",
        "\n__CONTACT_CRAWL__%{http_code} %{url_effective}",
        url,
    ]
    if insecure:
        command.insert(1, "-k")
    if wait:
        wait()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return FetchResult(url, url, 0, "", error=str(exc), insecure_tls=insecure)
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0 and not insecure and _is_tls_error(stderr):
        return _fetch_curl_page(url, timeout, insecure=True, wait=wait)
    text = completed.stdout.decode("utf-8", errors="replace")
    html, marker, tail = text.rpartition("\n__CONTACT_CRAWL__")
    if not marker:
        return FetchResult(url, url, 0, "", error=stderr.strip() or "curl returned no status", insecure_tls=insecure)
    status_text, _, final = tail.partition(" ")
    try:
        status = int(status_text.strip())
    except ValueError:
        status = 0
    return FetchResult(
        requested_url=url,
        final_url=final.strip() or url,
        status=status,
        html=html[:MAX_BYTES],
        error=None if status else (stderr.strip() or "curl failed"),
        insecure_tls=insecure,
    )


def _is_tls_error(message: str) -> bool:
    text = message.lower()
    return "ssl" in text or "certificate" in text or "tls" in text


def _charset_from(raw: bytes) -> str:
    head = raw[:2000].decode("ascii", errors="ignore")
    import re

    match = re.search(r"charset=['\"]?\s*([a-zA-Z0-9._\-]+)", head, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _decode(raw: bytes, charset: str | None) -> str:
    for encoding in (charset or "", "utf-8", "windows-1252", "latin-1"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")
