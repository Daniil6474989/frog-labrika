"""Полный разбор sitemap: страницы, картинки внутри карты и предупреждения."""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

from .net import fetch
from .parse import decode_body
from .urls import normalize, same_site

_KNOWN_ROBOTS = {"user-agent", "allow", "disallow", "sitemap", "crawl-delay", "host", "clean-param"}
_PAGE_EXT = {"html", "htm", "php", "asp", "aspx"}


def _file_like(url: str) -> bool:
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() not in _PAGE_EXT


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def robots_warnings(text: str) -> list[str]:
    warnings: list[str] = []
    unknown: list[str] = []
    blocks_assets = False
    blocks_all = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key not in _KNOWN_ROBOTS:
            unknown.append(f"{key}: {value}"[:180])
        if key == "disallow":
            if value in {"/", "/*"}:
                blocks_all = True
            if ".css" in value.lower() or ".js" in value.lower():
                blocks_assets = True
    if blocks_all:
        warnings.append("Disallow закрывает весь сайт")
    if blocks_assets:
        warnings.append("robots.txt закрывает CSS или JavaScript")
    if unknown:
        shown = "; ".join(dict.fromkeys(unknown))
        warnings.append("Нестандартная директива: " + shown[:400])
    return warnings


def analyze_sitemap(text: str) -> dict:
    warnings: list[str] = []
    if "<!--" in text:
        warnings.append("В файле есть XML-комментарий")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return {
            "ok": False,
            "kind": "xml",
            "warnings": warnings + [f"XML не разбирается: {exc}"],
            "children": [],
            "pages": [],
            "images": [],
        }
    kind = _local(root.tag)
    if kind == "sitemapindex":
        children = []
        for node in root:
            if _local(node.tag) != "sitemap":
                continue
            for child in node:
                if _local(child.tag) == "loc" and child.text:
                    children.append(child.text.strip())
        return {
            "ok": True,
            "kind": "sitemapindex",
            "warnings": warnings,
            "children": children,
            "pages": [],
            "images": [],
        }
    pages: list[str] = []
    images: list[tuple[str, str]] = []
    missing_lastmod = False
    empty_caption = False
    file_page = False
    for node in root:
        if _local(node.tag) != "url":
            continue
        loc = ""
        has_lastmod = False
        pictures: list[str] = []
        for child in node:
            name = _local(child.tag)
            if name == "loc" and child.text and not loc:
                loc = child.text.strip()
            elif name == "lastmod":
                has_lastmod = True
            elif name == "image":
                for sub in child.iter():
                    sub_name = _local(sub.tag)
                    if sub_name == "loc" and sub.text:
                        pictures.append(sub.text.strip())
                    if sub_name == "caption" and not (sub.text or "").strip():
                        empty_caption = True
        if loc:
            pages.append(loc)
            if not has_lastmod:
                missing_lastmod = True
            if _file_like(loc):
                file_page = True
        for picture in pictures:
            images.append((loc, picture))
    if missing_lastmod:
        warnings.append("Есть адрес без даты lastmod")
    if empty_caption:
        warnings.append("У изображения в карте пустой caption")
    if file_page:
        warnings.append("В карте указан файл, а не HTML-страница")
    return {
        "ok": True,
        "kind": "urlset",
        "warnings": warnings,
        "children": [],
        "pages": pages,
        "images": images,
    }


def load_sitemaps(seeds, site_key, allow_private, timeout, workers=4, deadline=None, on_file=None):
    """Качает индекс и вложенные карты. Возвращает файлы, адреса страниц и картинки."""
    pending = [url for url in seeds if url]
    seen: set[str] = set()
    files: list[dict] = []
    pages: list[str] = []
    images: list[dict] = []
    found_in: dict[str, str] = {}

    def take_batch():
        batch = []
        while pending and len(batch) < max(1, workers) and len(files) + len(batch) < 40:
            url = pending.pop(0)
            if url in seen:
                continue
            seen.add(url)
            batch.append(url)
        return batch

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while True:
            if deadline and time.time() > deadline:
                break
            batch = take_batch()
            if not batch:
                break
            futures = [
                pool.submit(fetch, url, timeout=timeout, allow_private=allow_private) for url in batch
            ]
            for url, future in zip(batch, futures):
                result = future.result()
                text = decode_body(result.body or b"", (result.headers or {}).get("content-type", ""))
                parsed = analyze_sitemap(text) if result.status == 200 and text else {
                    "ok": False,
                    "kind": "",
                    "warnings": ["Файл не открылся"] if result.status != 200 else [],
                    "children": [],
                    "pages": [],
                    "images": [],
                }
                file_pages = []
                for loc in parsed["pages"]:
                    if same_site(loc, site_key):
                        file_pages.append(normalize(loc))
                for loc in parsed["children"]:
                    absolute = urljoin(url, loc)
                    found_in.setdefault(absolute, url)
                    if absolute not in seen:
                        pending.append(absolute)
                for page_url, image_url in parsed["images"]:
                    images.append(
                        {
                            "file": url,
                            "page": normalize(page_url) if page_url else "",
                            "image": image_url,
                            "error": "В карте указан адрес изображения, а не HTML-страница",
                        }
                    )
                kind = parsed["kind"] or "xml"
                is_index = kind == "sitemapindex"
                files.append(
                    {
                        "url": url,
                        "status": result.status,
                        "size": len(result.body or b""),
                        "found_in": found_in.get(url, ""),
                        "kind": "sitemapindex" if is_index else "xml",
                        "children": len(parsed["children"]),
                        "items": len(parsed["children"]) if is_index else len(parsed["pages"]) + len(parsed["images"]),
                        "html": 0 if is_index else len(file_pages),
                        "errors": 1 if not parsed["ok"] else (0 if is_index else len(parsed["images"])),
                        "warnings": parsed["warnings"] if parsed["ok"] else parsed["warnings"] or ["Файл не открылся"],
                        "checked": result.status == 200 and parsed["ok"],
                    }
                )
                pages.extend(file_pages)
                if on_file:
                    on_file(url, len(files))
    unique_pages = list(dict.fromkeys(pages))
    return files, unique_pages, images
