"""Общие структуры обхода и отчёта."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    url: str
    status: int = 0
    content_type: str = ""
    size: int = 0
    time_ms: int = 0
    title: str = ""
    description: str = ""
    h1: list = field(default_factory=list)
    canonical: str = ""
    meta_robots: str = ""
    x_robots: str = ""
    viewport: bool = False
    lang: str = ""
    words: int = 0
    text_hash: str = ""
    hreflang: list = field(default_factory=list)
    jsonld_ok: int = 0
    jsonld_bad: int = 0
    og_title: str = ""
    og_description: str = ""
    mixed: list = field(default_factory=list)
    meta_refresh: str = ""
    script_count: int = 0
    images: int = 0
    images_missing_alt: int = 0
    internal_links: int = 0
    external_links: int = 0
    depth: int = 0
    indexable: bool = False
    index_reason: str = ""
    error: str = ""
    hsts: bool = False
    truncated: bool = False
    inlinks: int = 0
    via_redirect: bool = False
    html_issues: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    canonical_count: int = 0
    http_internal: list = field(default_factory=list)


@dataclass
class CrawlData:
    start_url: str
    final_url: str
    site_key: str = ""
    pages: dict = field(default_factory=dict)
    redirect_events: list = field(default_factory=list)
    blocked_links: list = field(default_factory=list)
    robots_status: int = 0
    robots_found: bool = False
    blocks_all: bool = False
    sitemap_sources: list = field(default_factory=list)
    sitemap_urls: list = field(default_factory=list)
    extra_status: dict = field(default_factory=dict)
    linked: set = field(default_factory=set)
    http_probe: dict | None = None
    not_found_probe: dict | None = None
    partial: bool = False
    elapsed_ms: int = 0
    max_pages: int = 30
    max_depth: int = 4
    respect_robots: bool = True
    home_error: str | None = None
    tls_note: str | None = None
    robots_text: str = ""
    robots_warnings: list = field(default_factory=list)
    sitemap_files: list = field(default_factory=list)
    sitemap_images: list = field(default_factory=list)
    mirror_probes: list = field(default_factory=list)
    link_from: dict = field(default_factory=dict)
