"""Разбор HTML: мета, заголовки, ссылки, картинки, микроразметка."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .urls import normalize, same_site

try:
    from selectolax.lexbor import LexborHTMLParser
except ImportError:  # запасной путь — чистый html.parser
    LexborHTMLParser = None


def decode_body(body: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset=([^\s;]+)", content_type or "", re.I)
    if match:
        charset = match.group(1).strip("\"'")
    else:
        head = body[:4000].decode("ascii", "ignore")
        match = re.search(r"charset=['\"]?\s*([a-zA-Z0-9._-]+)", head, re.I)
        if match:
            charset = match.group(1)
    for name in (charset, "utf-8"):
        if not name:
            continue
        try:
            return body.decode(name, "replace")
        except LookupError:
            continue
    return body.decode("utf-8", "replace")


def is_html(content_type: str, body: bytes) -> bool:
    if content_type in ("text/html", "application/xhtml+xml"):
        return True
    if content_type and content_type not in ("text/plain", "application/octet-stream"):
        return False
    head = body[:800].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:400]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


@dataclass
class Parsed:
    title: str = ""
    description: str = ""
    h1: list = field(default_factory=list)
    canonical: str = ""
    meta_robots: str = ""
    viewport: bool = False
    lang: str = ""
    words: int = 0
    text_hash: str = ""
    internal: list = field(default_factory=list)
    external: list = field(default_factory=list)
    images: int = 0
    images_missing_alt: int = 0
    hreflang: list = field(default_factory=list)
    jsonld_ok: int = 0
    jsonld_bad: int = 0
    og_title: str = ""
    og_description: str = ""
    mixed: list = field(default_factory=list)
    meta_refresh: str = ""
    script_count: int = 0
    assets: list = field(default_factory=list)
    canonical_count: int = 0
    http_internal: list = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self, page_url: str, site_key: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.site_key = site_key
        self.base = page_url
        self._skip = 0
        self._in_title = False
        self._title_done = False
        self._title_parts: list[str] = []
        self._heading: list | None = None
        self._text: list[str] = []
        self._ld = False
        self._ld_parts: list[str] = []
        self._ld_raw: list[str] = []
        self.description = ""
        self.h1: list[str] = []
        self.canonical = ""
        self.robots: list[str] = []
        self.viewport = False
        self.lang = ""
        self.internal: list[str] = []
        self.external: list[str] = []
        self.images = 0
        self.images_missing_alt = 0
        self.hreflang: list[dict] = []
        self.og_title = ""
        self.og_description = ""
        self.mixed: list[str] = []
        self.meta_refresh = ""
        self.script_count = 0
        self.assets: list[dict] = []
        self.canonical_count = 0
        self.http_internal: list[str] = []
        self._https = urlsplit(page_url).scheme == "https"

    def _abs(self, href: str) -> str:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            return ""
        return urljoin(self.base or self.page_url, href)

    def _note_mixed(self, href: str) -> None:
        if self._https and urlsplit(href).scheme == "http":
            self.mixed.append(href)

    def _collect(self, href: str) -> None:
        if urlsplit(href).scheme not in ("http", "https"):
            return
        if same_site(href, self.site_key):
            normalized = normalize(href)
            self.internal.append(normalized)
            if urlsplit(normalized).scheme == "http":
                self.http_internal.append(normalized)
        else:
            self.external.append(normalize(href))

    def _asset(self, href: str, kind: str) -> None:
        if not href or urlsplit(href).scheme not in ("http", "https"):
            return
        if not same_site(href, self.site_key):
            self.assets.append({"url": href, "kind": kind})

    def handle_starttag(self, tag, attrs):
        attr = {key.lower(): (value if value is not None else "") for key, value in attrs}
        if tag == "html" and not self.lang:
            self.lang = attr.get("lang", "")
        if tag == "base" and attr.get("href"):
            self.base = urljoin(self.page_url, attr["href"].strip())
        if tag == "title" and not self._title_done:
            self._in_title = True
        if tag in ("h1", "h2"):
            self._heading = [tag, []]
        if tag in ("script", "style"):
            self._skip += 1
            if tag == "script":
                self.script_count += 1
                if "ld+json" in attr.get("type", "").lower():
                    self._ld = True
                    self._ld_parts = []
                src = self._abs(attr.get("src", ""))
                if src:
                    self._note_mixed(src)
                    self._asset(src, "script")
        if tag == "meta":
            name = attr.get("name", "").lower()
            prop = attr.get("property", "").lower()
            content = attr.get("content", "")
            if attr.get("http-equiv", "").lower() == "refresh":
                self.meta_refresh = content
            if name == "description" and not self.description:
                self.description = content
            if name in ("robots", "googlebot", "yandex") and content:
                self.robots.append(content)
            if name == "viewport" and "width" in content.lower():
                self.viewport = True
            if prop == "og:title" and not self.og_title:
                self.og_title = content
            if prop == "og:description" and not self.og_description:
                self.og_description = content
        if tag == "link":
            rels = set(attr.get("rel", "").lower().split())
            href = self._abs(attr.get("href", ""))
            if href and "canonical" in rels:
                self.canonical_count += 1
                if not self.canonical:
                    self.canonical = href
            if href and "alternate" in rels and attr.get("hreflang"):
                self.hreflang.append({"lang": attr.get("hreflang", ""), "href": href})
            if href and "stylesheet" in rels:
                self._note_mixed(href)
                self._asset(href, "css")
        if tag == "a":
            href = self._abs(attr.get("href", ""))
            if href:
                self._collect(href)
        if tag == "img":
            self.images += 1
            if "alt" not in attr or not str(attr.get("alt", "")).strip():
                self.images_missing_alt += 1
            src = self._abs(attr.get("src") or attr.get("data-src") or "")
            if src:
                self._note_mixed(src)
                self._asset(src, "image")
        if tag in ("iframe", "source", "audio", "video"):
            src = self._abs(attr.get("src") or attr.get("poster") or "")
            if src:
                self._note_mixed(src)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        if self._heading and tag == self._heading[0]:
            text = _clean("".join(self._heading[1]))
            if tag == "h1" and text:
                self.h1.append(text)
            self._heading = None
        if tag == "script" and self._ld:
            self._ld_raw.append("".join(self._ld_parts))
            self._ld = False
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._ld:
            self._ld_parts.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._heading:
            self._heading[1].append(data)
        self._text.append(data)

    def result(self) -> Parsed:
        ok = bad = 0
        for raw in self._ld_raw:
            raw = raw.strip()
            if not raw:
                continue
            try:
                json.loads(raw)
                ok += 1
            except json.JSONDecodeError:
                bad += 1
        text = _clean("".join(self._text))
        words = re.findall(r"\w+", text, flags=re.UNICODE)
        digest = ""
        if len(text) >= 180:
            digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:16]
        return Parsed(
            title=_clean("".join(self._title_parts)),
            description=_clean(self.description),
            h1=self.h1[:12],
            canonical=self.canonical,
            meta_robots=", ".join(self.robots),
            viewport=self.viewport,
            lang=_clean(self.lang),
            words=len(words),
            text_hash=digest,
            internal=list(dict.fromkeys(self.internal)),
            external=list(dict.fromkeys(self.external)),
            images=self.images,
            images_missing_alt=self.images_missing_alt,
            hreflang=self.hreflang,
            jsonld_ok=ok,
            jsonld_bad=bad,
            og_title=_clean(self.og_title),
            og_description=_clean(self.og_description),
            mixed=list(dict.fromkeys(self.mixed)),
            meta_refresh=_clean(self.meta_refresh),
            script_count=self.script_count,
            assets=self.assets,
            canonical_count=self.canonical_count,
            http_internal=list(dict.fromkeys(self.http_internal)),
        )


def _attrs(node) -> dict:
    return node.attributes or {}


def _parse_fast(html: str, page_url: str, site_key: str) -> Parsed:
    """Разбор через lexbor: на тяжёлых страницах магазина это быстрее html.parser в разы."""
    tree = LexborHTMLParser(html or "")
    parsed = Parsed()
    secure = urlsplit(page_url).scheme == "https"
    base = page_url
    base_node = tree.css_first("base")
    if base_node is not None:
        href = (_attrs(base_node).get("href") or "").strip()
        if href:
            base = urljoin(page_url, href)

    def absolute(href: str) -> str:
        href = (href or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            return ""
        return urljoin(base, href)

    html_node = tree.css_first("html")
    if html_node is not None:
        parsed.lang = _clean(_attrs(html_node).get("lang") or "")
    title = tree.css_first("title")
    if title is not None:
        parsed.title = _clean(title.text(strip=True) or "")
    headings: list[str] = []
    for node in tree.css("h1"):
        text = _clean(node.text(strip=True) or "")
        if text:
            headings.append(text)
        if len(headings) >= 12:
            break
    parsed.h1 = headings

    robots: list[str] = []
    for node in tree.css("meta"):
        attrs = _attrs(node)
        name = (attrs.get("name") or "").lower()
        prop = (attrs.get("property") or "").lower()
        content = attrs.get("content") or ""
        if (attrs.get("http-equiv") or "").lower() == "refresh":
            parsed.meta_refresh = content
        if name == "description" and not parsed.description:
            parsed.description = content
        if name in ("robots", "googlebot", "yandex") and content:
            robots.append(content)
        if name == "viewport" and "width" in content.lower():
            parsed.viewport = True
        if prop == "og:title" and not parsed.og_title:
            parsed.og_title = content
        if prop == "og:description" and not parsed.og_description:
            parsed.og_description = content
    parsed.meta_robots = ", ".join(robots)
    parsed.description = _clean(parsed.description)
    parsed.og_title = _clean(parsed.og_title)
    parsed.og_description = _clean(parsed.og_description)
    parsed.meta_refresh = _clean(parsed.meta_refresh)

    internal: list[str] = []
    external: list[str] = []
    mixed: list[str] = []
    http_internal: list[str] = []
    assets: list[dict] = []

    def note_mixed(href: str) -> None:
        if secure and urlsplit(href).scheme == "http":
            mixed.append(href)

    def collect(href: str) -> None:
        if urlsplit(href).scheme not in ("http", "https"):
            return
        if same_site(href, site_key):
            normalized = normalize(href)
            internal.append(normalized)
            if urlsplit(normalized).scheme == "http":
                http_internal.append(normalized)
        else:
            external.append(normalize(href))

    def asset(href: str, kind: str) -> None:
        if href and urlsplit(href).scheme in ("http", "https") and not same_site(href, site_key):
            assets.append({"url": href, "kind": kind})

    for node in tree.css("link"):
        attrs = _attrs(node)
        rels = set((attrs.get("rel") or "").lower().split())
        href = absolute(attrs.get("href") or "")
        if href and "canonical" in rels:
            parsed.canonical_count += 1
            if not parsed.canonical:
                parsed.canonical = href
        if href and "alternate" in rels and attrs.get("hreflang"):
            parsed.hreflang.append({"lang": attrs.get("hreflang") or "", "href": href})
        if href and "stylesheet" in rels:
            note_mixed(href)
            asset(href, "css")

    for node in tree.css("a"):
        href = absolute(_attrs(node).get("href") or "")
        if href:
            collect(href)

    for node in tree.css("img"):
        parsed.images += 1
        attrs = _attrs(node)
        if "alt" not in attrs or not str(attrs.get("alt") or "").strip():
            parsed.images_missing_alt += 1
        src = absolute(attrs.get("src") or attrs.get("data-src") or "")
        if src:
            note_mixed(src)
            asset(src, "image")

    for node in tree.css("script"):
        parsed.script_count += 1
        attrs = _attrs(node)
        if "ld+json" in (attrs.get("type") or "").lower():
            raw = (node.text() or "").strip()
            if raw:
                try:
                    json.loads(raw)
                    parsed.jsonld_ok += 1
                except json.JSONDecodeError:
                    parsed.jsonld_bad += 1
        src = absolute(attrs.get("src") or "")
        if src:
            note_mixed(src)
            asset(src, "script")

    for selector in ("iframe", "source", "audio", "video"):
        for node in tree.css(selector):
            attrs = _attrs(node)
            src = absolute(attrs.get("src") or attrs.get("poster") or "")
            if src:
                note_mixed(src)

    text = ""
    if tree.body is not None:
        text = _clean(tree.body.text(separator=" ", strip=True) or "")
    parsed.words = len(re.findall(r"\w+", text, flags=re.UNICODE))
    if len(text) >= 180:
        parsed.text_hash = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:16]
    parsed.internal = list(dict.fromkeys(internal))
    parsed.external = list(dict.fromkeys(external))
    parsed.mixed = list(dict.fromkeys(mixed))
    parsed.assets = assets
    parsed.http_internal = list(dict.fromkeys(http_internal))
    return parsed


def parse_html(html: str, page_url: str, site_key: str) -> Parsed:
    if LexborHTMLParser is not None:
        try:
            return _parse_fast(html or "", page_url, site_key)
        except Exception:
            pass
    parser = _Parser(page_url, site_key)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    return parser.result()
