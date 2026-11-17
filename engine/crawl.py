"""Обход сайта: внутренние HTML-страницы, robots.txt, sitemap, пробы https и 404."""
from __future__ import annotations

import re
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html import unescape
from urllib.parse import urljoin, urlsplit

from .model import CrawlData, Page
from .net import REDIRECTS, UA, fetch, tls_note
from .htmlissues import inspect_html
from .parse import decode_body, is_html, parse_html
from .robots import Robots
from .sitemaps import load_sitemaps, robots_warnings
from .urls import assert_public_url, host_key, normalize, same_site

_SKIP_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip",
    ".rar", ".7z", ".css", ".js", ".mjs", ".mp4", ".mp3", ".woff", ".woff2",
    ".ttf", ".eot", ".xml", ".json", ".txt", ".gz", ".avif",
}


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _skip_ext(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in _SKIP_EXT)


def _tokens(value: str) -> set[str]:
    return {item for item in re.split(r"[\s,;]+", (value or "").lower()) if item}


def _classify(url: str, status: int, html_ok: bool, parsed, x_robots: str, error: str) -> tuple[bool, str]:
    if status == 0:
        return False, error or "не открылась"
    if status != 200:
        return False, f"код {status}"
    if not html_ok:
        return False, "не HTML"
    tokens = _tokens(f"{parsed.meta_robots if parsed else ''} {x_robots}")
    if "noindex" in tokens or "none" in tokens:
        return False, "noindex"
    if parsed and parsed.canonical:
        canon = normalize(parsed.canonical)
        if urlsplit(canon).scheme in ("http", "https") and canon != normalize(url):
            return False, "canonical на другой адрес"
    return True, "можно индексировать"


def _locs(xml: str) -> list[str]:
    return [unescape(item).strip() for item in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml or "", re.I)]


def _page_from(requested: str, depth: int, result, site_key: str, prepared=None) -> Page:
    final = normalize(result.final_url or requested)
    html_ok = bool(result.body) and is_html(result.content_type, result.body)
    parsed = None
    issues: list = []
    if prepared:
        parsed, issues = prepared
    elif result.status == 200 and html_ok:
        parsed = parse_html(decode_body(result.body, result.headers.get("content-type", "")), final, site_key)
        issues = inspect_html(result.body or b"")
    indexable, reason = _classify(
        final, result.status, html_ok, parsed, result.headers.get("x-robots-tag", ""), result.error or ""
    )
    page = Page(
        url=final,
        status=result.status,
        content_type=result.content_type,
        size=len(result.body or b""),
        time_ms=result.elapsed_ms,
        x_robots=result.headers.get("x-robots-tag", ""),
        hsts="strict-transport-security" in result.headers,
        truncated=result.truncated,
        depth=depth,
        indexable=indexable,
        index_reason=reason,
        error=result.error or "",
        via_redirect=bool(result.chain),
    )
    if parsed:
        page.title = parsed.title
        page.description = parsed.description
        page.h1 = parsed.h1
        page.canonical = parsed.canonical
        page.meta_robots = parsed.meta_robots
        page.viewport = parsed.viewport
        page.lang = parsed.lang
        page.words = parsed.words
        page.text_hash = parsed.text_hash
        page.hreflang = parsed.hreflang
        page.jsonld_ok = parsed.jsonld_ok
        page.jsonld_bad = parsed.jsonld_bad
        page.og_title = parsed.og_title
        page.og_description = parsed.og_description
        page.mixed = parsed.mixed
        page.meta_refresh = parsed.meta_refresh
        page.script_count = parsed.script_count
        page.images = parsed.images
        page.images_missing_alt = parsed.images_missing_alt
        page.internal_links = len(parsed.internal)
        page.external_links = len(parsed.external)
        page.assets = parsed.assets
        page.canonical_count = parsed.canonical_count
        page.http_internal = parsed.http_internal
        page.html_issues = issues
        page._links = parsed.internal  # type: ignore[attr-defined]
    else:
        page._links = []  # type: ignore[attr-defined]
    return page


def crawl(
    start_url: str,
    *,
    max_pages: int = 30,
    max_depth: int = 4,
    respect_robots: bool = True,
    allow_private: bool = False,
    timeout: float = 10,
    deadline_s: float = 90,
    workers: int = 8,
    on_progress=None,
) -> CrawlData:
    started = time.perf_counter()
    deadline = time.time() + deadline_s
    max_pages = max(1, max_pages)
    pages: dict[str, Page] = {}
    alias: dict[str, str] = {}
    graph: dict[str, set[str]] = {}
    redirect_events: list[dict] = []
    blocked: list[str] = []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    log: list[str] = []
    partial = False
    robots = Robots([], [], 0)
    robots_status = 0
    robots_text = ""
    robots_warn: list[str] = []
    sitemap_sources: list[str] = []
    sitemap_urls: list[str] = []
    sitemap_files: list[dict] = []
    sitemap_images: list[dict] = []
    mirror_probes: list[dict] = []
    extra_status: dict[str, dict] = {}
    http_probe = None
    not_found_probe = None
    blocks_all = False

    def ping(phase: str, current: str = "") -> None:
        if not on_progress:
            return
        on_progress(
            {
                "phase": phase,
                "pages": len(pages),
                "limit": max_pages,
                "queued": len(queue),
                "current": current,
                "log": log[-8:],
            }
        )

    def remember(result_url: str, final: str) -> None:
        try:
            alias[normalize(result_url)] = final
        except Exception:
            alias[result_url] = final

    def absorb(requested: str, depth: int, result, prepared=None) -> str | None:
        final = normalize(result.final_url or requested)
        if result.status in REDIRECTS or result.error:
            final = normalize(requested)
        if result.status not in REDIRECTS and result.status != 0:
            final = normalize(result.final_url or requested)
        remember(requested, final)
        for hop in result.chain:
            remember(hop["url"], final)
        remember(final, final)
        if result.chain:
            redirect_events.append(
                {
                    "from": normalize(requested),
                    "chain": result.chain,
                    "to": final if result.status not in REDIRECTS else normalize(result.final_url or requested),
                    "hops": len(result.chain),
                }
            )
        key = final
        if result.status not in (0,) and result.status not in REDIRECTS:
            key = normalize(result.final_url or requested)
        if key in pages:
            pages[key].depth = min(pages[key].depth, depth)
            pages[key].via_redirect = pages[key].via_redirect or bool(result.chain)
            return None
        if len(pages) >= max_pages:
            return None
        page = _page_from(requested, depth, result, site_key, prepared)
        page.url = key if result.status == page.status or page.status == 0 else page.url
        # Ключ совпадает с адресом конечного документа.
        if page.status not in REDIRECTS:
            page.url = normalize(result.final_url or requested) if page.status else normalize(requested)
        pages[page.url] = page
        alias[page.url] = page.url
        if page.status == 200:
            graph[page.url] = set(getattr(page, "_links", []))
        return page.url

    def enqueue(url: str, depth: int) -> None:
        if len(seen) >= max_pages * 5:
            return
        try:
            normalized = normalize(url)
        except Exception:
            return
        parts = urlsplit(normalized)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return
        if normalized in seen or len(normalized) > 2000 or _skip_ext(normalized):
            return
        if not same_site(normalized, site_key):
            return
        try:
            assert_public_url(normalized, allow_private=allow_private)
        except Exception:
            return
        if not robots.allowed(normalized):
            if normalized not in blocked:
                blocked.append(normalized)
            if respect_robots:
                seen.add(normalized)
                return
        seen.add(normalized)
        queue.append((normalized, depth))

    ping("Открываю стартовую страницу", start_url)
    home_result = fetch(start_url, timeout=timeout, allow_private=allow_private)
    landing = home_result.final_url or start_url
    if home_result.status in (0,) or home_result.status in REDIRECTS:
        landing = start_url
    if home_result.status not in REDIRECTS and home_result.status != 0:
        landing = home_result.final_url or start_url
    site_key = host_key(urlsplit(landing).hostname or urlsplit(start_url).hostname or "")
    seen.add(normalize(start_url))
    absorb(start_url, 0, home_result)
    home_key = next(iter(pages))
    log.append(f"{home_result.status or 'ошибка'} {home_key}")

    def _stamp(data):
        data.robots_text = robots_text
        data.robots_warnings = robots_warn
        data.sitemap_files = sitemap_files
        data.sitemap_images = sitemap_images
        data.mirror_probes = mirror_probes
        sources: dict[str, list[str]] = {}
        for src, dests in graph.items():
            for dest in dests:
                target = alias.get(dest, dest)
                bucket = sources.setdefault(target, [])
                if src not in bucket:
                    bucket.append(src)
        data.link_from = sources
        return data

    if home_result.status == 0:
        ping("Стартовая страница не ответила", home_key)
        finished = _finish(
            start_url, home_key, site_key, pages, redirect_events, blocked, robots_status,
            False, False, sitemap_sources, sitemap_urls, extra_status, set(), None, None,
            False, started, max_pages, max_depth, respect_robots, home_result.error, tls_note(),
        )
        return _stamp(finished)

    origin = _origin(pages[home_key].url if pages[home_key].status == 200 else landing)
    # origin должен быть у конечного документа, а не у промежуточного редиректа
    if pages[home_key].status == 200:
        origin = _origin(pages[home_key].url)
    else:
        origin = _origin(normalize(start_url) if "://" in start_url else start_url)

    ping("Читаю robots.txt и карту сайта")
    robots_result = fetch(origin + "/robots.txt", timeout=timeout, allow_private=allow_private)
    robots_status = robots_result.status
    robots_found = robots_result.status == 200 and bool(robots_result.body)
    if robots_found:
        robots_text = decode_body(robots_result.body, robots_result.headers.get("content-type", ""))
        robots = Robots.parse(robots_text, UA)
        robots_warn = robots_warnings(robots_text)
    home_for_rules = origin + "/"
    blocks_all = robots_found and not robots.allowed(home_for_rules)
    declared = []
    for item in robots.sitemaps:
        declared.append(urljoin(origin + "/robots.txt", item))
    if not declared:
        declared = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
    def _sitemap_progress(url, count):
        ping(f"Читаю карту сайта, файлов {count}", url)

    sitemap_files, sitemap_urls, sitemap_images = load_sitemaps(
        declared,
        site_key,
        allow_private,
        timeout,
        workers,
        deadline,
        _sitemap_progress,
    )
    sitemap_sources = [item["url"] for item in sitemap_files]

    if not blocks_all:
        home_page = pages.get(home_key)
        if home_page and home_page.status == 200 and max_depth > 0:
            for link in getattr(home_page, "_links", []):
                enqueue(link, 1)
        for url in sitemap_urls:
            enqueue(url, 1)

    delay = robots.crawl_delay if respect_robots else 0
    ping("Обхожу внутренние страницы", home_key)

    def _store(url: str, depth: int, result, prepared) -> None:
        log.append(f"{result.status or 'ошибка'} {url}")
        added = absorb(url, depth, result, prepared)
        if not added:
            return
        page = pages[added]
        if page.status == 200 and depth < max_depth and not blocks_all:
            for link in getattr(page, "_links", []):
                enqueue(link, depth + 1)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        inflight: dict = {}
        stopped = False
        pace = max(1, workers)
        again: dict[str, int] = {}

        def _fill() -> None:
            while (
                not stopped
                and queue
                and len(inflight) < pace
                and len(pages) + len(inflight) < max_pages
            ):
                url, depth = queue.popleft()
                inflight[pool.submit(fetch, url, timeout=timeout, allow_private=allow_private)] = (url, depth)

        _fill()
        while inflight or (queue and not stopped):
            if time.time() > deadline:
                stopped = True
                partial = True
            _fill()
            if not inflight:
                break
            done, _pending_futs = wait(set(inflight), timeout=0.3, return_when=FIRST_COMPLETED)
            for fut in done:
                url, depth = inflight.pop(fut)
                result = fut.result()
                if result.status in (429, 503) and again.get(url, 0) < 1 and not stopped:
                    again[url] = again.get(url, 0) + 1
                    queue.appendleft((url, depth))
                    pace = max(4, pace - 1)
                    continue
                if result.status == 200 and pace < workers:
                    pace = min(workers, pace + 1)
                _store(url, depth, result, None)
            if delay and not stopped and len(done) >= workers:
                time.sleep(delay)
            if done:
                current = next(iter(inflight.values()))[0] if inflight else ""
                ping("Обхожу внутренние страницы", current)
    if queue:
        partial = True

    ping("Проверяю HTTP, 404 и адреса из sitemap")
    if pages[home_key].status == 200 and urlsplit(pages[home_key].url).scheme == "https":
        http_url = "http://" + urlsplit(origin).netloc + "/"
        probe = fetch(http_url, follow=False, timeout=timeout, allow_private=allow_private)
        http_probe = {
            "url": http_url,
            "status": probe.status,
            "location": (probe.headers.get("location") or "") if probe.headers else "",
            "error": probe.error,
        }
        mirror_probes.append(http_probe)
        bare = site_key
        for extra in (f"https://www.{bare}/", f"http://www.{bare}/"):
            if urlsplit(pages[home_key].url).hostname == urlsplit(extra).hostname:
                continue
            extra_probe = fetch(extra, follow=False, timeout=timeout, allow_private=allow_private)
            mirror_probes.append(
                {
                    "url": extra,
                    "status": extra_probe.status,
                    "location": (extra_probe.headers.get("location") or "") if extra_probe.headers else "",
                    "error": extra_probe.error,
                }
            )
    missing_url = origin + "/frog-labrika-missing-8f3a"
    missing = fetch(missing_url, follow=False, timeout=timeout, allow_private=allow_private)
    not_found_probe = {"url": missing_url, "status": missing.status, "error": missing.error}

    linked: set[str] = {pages[home_key].url}
    for dests in graph.values():
        for dest in dests:
            linked.add(dest)
            if dest in alias:
                linked.add(alias[dest])
    need = []
    for url in sitemap_urls:
        target = alias.get(url, url)
        if url in pages or target in pages:
            continue
        need.append(url)
        extra_status[url] = {"status": None, "final": url, "error": None, "unvisited": True}
    for url in need[:8]:
        if time.time() > deadline:
            partial = True
            break
        result = fetch(url, timeout=timeout, allow_private=allow_private)
        final = normalize(result.final_url or url) if result.status else url
        extra_status[url] = {"status": result.status, "final": final, "error": result.error, "unvisited": False}
        if result.chain:
            for hop in result.chain:
                alias[normalize(hop["url"])] = final
            alias[url] = final

    inlinks: Counter[str] = Counter()
    for src, dests in graph.items():
        for dest in dests:
            target = alias.get(dest, dest)
            if target in pages and target != src:
                inlinks[target] += 1
    for url, page in pages.items():
        page.inlinks = inlinks[url]

    ping("Собираю замечания")
    finished = _finish(
        start_url, pages[home_key].url, site_key, pages, redirect_events, blocked,
        robots_status, robots_found, blocks_all, sitemap_sources, sitemap_urls,
        extra_status, linked, http_probe, not_found_probe, partial, started,
        max_pages, max_depth, respect_robots, home_result.error, tls_note(),
    )
    return _stamp(finished)


def _load_sitemaps(sources, site_key, allow_private, timeout, declared: bool):
    urls: list[str] = []
    used: list[str] = []
    seen: set[str] = set()
    pending = list(sources)
    checks = 0
    while pending and checks < 6 and len(urls) < 1000:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        checks += 1
        result = fetch(current, timeout=timeout, allow_private=allow_private)
        if result.status != 200 or not result.body:
            continue
        text = decode_body(result.body, result.headers.get("content-type", ""))
        if "<html" in text[:500].lower() and "<loc" not in text.lower():
            continue
        used.append(current)
        found = _locs(text)
        if "<sitemapindex" in text.lower():
            for loc in found:
                child = urljoin(current, loc)
                if child not in seen:
                    pending.append(child)
            continue
        for loc in found:
            absolute = urljoin(current, loc)
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https") or not same_site(absolute, site_key):
                continue
            normalized = normalize(absolute)
            if normalized not in urls:
                urls.append(normalized)
            if len(urls) >= 1000:
                break
    if not declared:
        # Пустые стандартные пути не считаем найденной картой.
        if not urls:
            used = []
    return used, urls


def _finish(
    start_url, final_url, site_key, pages, redirect_events, blocked, robots_status,
    robots_found, blocks_all, sitemap_sources, sitemap_urls, extra_status, linked,
    http_probe, not_found_probe, partial, started, max_pages, max_depth,
    respect_robots, home_error, tls_message=None,
) -> CrawlData:
    for page in pages.values():
        if hasattr(page, "_links"):
            delattr(page, "_links")
    return CrawlData(
        start_url=start_url,
        final_url=final_url,
        site_key=site_key,
        pages=pages,
        redirect_events=redirect_events,
        blocked_links=blocked[:50],
        robots_status=robots_status,
        robots_found=robots_found,
        blocks_all=blocks_all,
        sitemap_sources=sitemap_sources,
        sitemap_urls=sitemap_urls,
        extra_status=extra_status,
        linked=linked,
        http_probe=http_probe,
        not_found_probe=not_found_probe,
        partial=partial,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        max_pages=max_pages,
        max_depth=max_depth,
        respect_robots=respect_robots,
        home_error=home_error,
        tls_note=tls_message,
    )
