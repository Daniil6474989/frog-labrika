"""Загрузка страниц: редиректы вручную, без ухода во внутреннюю сеть."""
from __future__ import annotations

import gzip
import http.client
import socket
import ssl
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from .urls import UnsafeUrl, assert_public_url

UA = (
    "Mozilla/5.0 (compatible; FrogLabrikaAudit/1.0; local technical audit) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
REDIRECTS = {301, 302, 303, 307, 308}
_tls_lock = threading.Lock()
_tls_relaxed = False
_tls_note: str | None = None


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    headers: dict
    body: bytes
    elapsed_ms: int
    chain: list = field(default_factory=list)
    content_type: str = ""
    error: str | None = None
    truncated: bool = False
    tls_note: str | None = None


def _headers(headers) -> dict:
    if not headers:
        return {}
    return {key.lower(): headers.get(key) for key in headers.keys() if key}


def tls_note() -> str | None:
    with _tls_lock:
        return _tls_note


def _remember_tls(message: str) -> None:
    global _tls_relaxed, _tls_note
    with _tls_lock:
        if _tls_note is None:
            _tls_note = message
        lowered = message.lower()
        if "local issuer" in lowered or "self signed" in lowered or "self-signed" in lowered:
            _tls_relaxed = True


def _context(insecure: bool) -> ssl.SSLContext:
    with _tls_lock:
        relaxed = _tls_relaxed
    if insecure or relaxed:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


_local = threading.local()


def _drop_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _local.conn = None
    _local.meta = None


def _conn(scheme: str, host: str, port: int, timeout: float, insecure: bool):
    key = (scheme, host, port, insecure)
    current = getattr(_local, "conn", None)
    if current is not None and getattr(_local, "meta", None) == key:
        current.timeout = timeout
        return current
    _drop_conn()
    if scheme == "https":
        opened = http.client.HTTPSConnection(host, port, timeout=timeout, context=_context(insecure))
    else:
        opened = http.client.HTTPConnection(host, port, timeout=timeout)
    _local.conn = opened
    _local.meta = key
    return opened


def _once(url: str, timeout: float, max_bytes: int, allow_private: bool, insecure: bool = False) -> FetchResult:
    """Один запрос. Соединение с тем же сайтом остаётся открытым, чтобы не поднимать TLS заново."""
    assert_public_url(url, allow_private=allow_private)
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    started = time.perf_counter()
    try:
        conn = _conn(parts.scheme, host, port, timeout, insecure)
        conn.request(
            "GET",
            path,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip",
                "Accept-Language": "ru,en;q=0.8",
                "Connection": "keep-alive",
                "Host": parts.netloc,
            },
        )
        response = conn.getresponse()
        status = int(response.status or 0)
        headers = _headers(response.headers)
        body = response.read(max_bytes + 1)
    except Exception:
        _drop_conn()
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    truncated = len(body) > max_bytes
    if truncated or (headers.get("connection") or "").lower() == "close":
        _drop_conn()
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=status,
        headers=headers,
        body=body[:max_bytes],
        elapsed_ms=elapsed,
        content_type=content_type,
        truncated=truncated,
    )


def fetch(
    url: str,
    *,
    follow: bool = True,
    timeout: float = 10,
    max_hops: int = 5,
    allow_private: bool = False,
    max_bytes: int = 1_500_000,
) -> FetchResult:
    """Скачивает адрес. При follow собирает цепочку редиректов и возвращает последний ответ."""
    current = url
    chain: list[dict] = []
    seen: set[str] = set()
    elapsed = 0
    hops = max_hops if follow else 0
    retries = 0
    for hop in range(hops + 1):
        try:
            try:
                result = _once(current, timeout, max_bytes, allow_private)
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                if not isinstance(reason, ssl.SSLError):
                    raise
                _remember_tls(str(reason))
                result = _once(current, timeout, max_bytes, allow_private, insecure=True)
                result.tls_note = str(reason)
            except ssl.SSLError as exc:
                _remember_tls(str(exc))
                result = _once(current, timeout, max_bytes, allow_private, insecure=True)
                result.tls_note = str(exc)
        except UnsafeUrl as exc:
            return FetchResult(url, current, 0, {}, b"", elapsed, chain, "", str(exc))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            return FetchResult(url, current, 0, {}, b"", elapsed, chain, "", str(reason or exc))
        elapsed += result.elapsed_ms
        result.elapsed_ms = elapsed
        result.requested_url = url
        result.chain = list(chain)
        result.final_url = current
        if result.status in (429, 503) and retries < 1:
            retries += 1
            pause = 1.0
            raw_wait = (result.headers.get("retry-after") or "").strip()
            if raw_wait.isdigit():
                pause = min(3.0, float(raw_wait))
            time.sleep(pause)
            continue
        if follow and result.status in REDIRECTS and hop < hops:
            location = (result.headers.get("location") or "").strip()
            chain.append({"url": current, "status": result.status, "location": location})
            if not location:
                result.error = "редирект без адреса назначения"
                return result
            nxt = urljoin(current, location)
            if nxt in seen:
                result.error = "цикл редиректов"
                return result
            seen.add(nxt)
            current = nxt
            continue
        if follow and result.status in REDIRECTS and hop >= hops:
            result.error = "слишком длинная цепочка редиректов"
        return result
    return FetchResult(url, current, 0, {}, b"", elapsed, chain, "", "редирект не разрешился")
