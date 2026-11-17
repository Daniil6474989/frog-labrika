"""Нормализация адресов и запрет обхода внутренней сети."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DROP_QUERY = {"yclid", "gclid", "fbclid", "_openstat"}
# Clash и похожие клиенты подменяют DNS на эту сеть. Это не локальная сеть компьютера.
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


class UnsafeUrl(ValueError):
    """Адрес нельзя обходить: не http(s) или смотрит во внутреннюю сеть."""


def host_key(host: str) -> str:
    value = (host or "").lower().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    try:
        value = value.encode("idna").decode("ascii")
    except Exception:
        pass
    return value


def normalize(url: str) -> str:
    raw = (url or "").strip()
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return raw
    try:
        host_idna = host.encode("idna").decode("ascii")
    except Exception:
        host_idna = host
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host_idna}:{port}"
    else:
        netloc = host_idna
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _DROP_QUERY:
            continue
        query.append((key, value))
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def same_site(url: str, site_key: str) -> bool:
    host = urlsplit(url).hostname or ""
    return bool(site_key) and host_key(host) == site_key


def _unwrap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(_unwrap(ip).is_global)


def _dangerous_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    real = _unwrap(ip)
    if isinstance(real, ipaddress.IPv4Address) and real in _FAKE_IP:
        return False
    return not real.is_global


def assert_public_url(url: str, allow_private: bool = False) -> None:
    raw = (url or "").strip()
    if not raw or any(char in raw for char in "\r\n\t "):
        raise UnsafeUrl("Некорректный адрес")
    if len(raw) > 2000:
        raise UnsafeUrl("Слишком длинный адрес")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl("Нужен адрес http или https")
    if parts.username or parts.password:
        raise UnsafeUrl("Адрес с логином отклонён")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("В адресе нет домена")
    name = host.lower().rstrip(".")
    if name in {"localhost", "localhost.localdomain"} or name.endswith(
        (".local", ".internal", ".localhost")
    ):
        if not allow_private:
            raise UnsafeUrl("Локальный адрес отклонён")
        return
    if name.isdigit():
        raise UnsafeUrl("Такой адрес отклонён")
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        ip = None
    if ip is not None:
        if not allow_private and not _public_ip(ip):
            raise UnsafeUrl("Внутренний адрес отклонён")
        return
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror as exc:
        raise UnsafeUrl("Домен не открывается") from exc
    if not infos:
        raise UnsafeUrl("Домен не открывается")
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _dangerous_ip(resolved):
            raise UnsafeUrl("Домен смотрит во внутреннюю сеть")


def prepare_start_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise UnsafeUrl("Укажите адрес сайта")
    if "://" not in text:
        text = "https://" + text
    assert_public_url(text, allow_private=False)
    normalized = normalize(text)
    assert_public_url(normalized, allow_private=False)
    return normalized
