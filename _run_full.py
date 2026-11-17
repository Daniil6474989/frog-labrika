import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from engine.crawl import crawl
from engine.deliver import write_files
from engine.report import build_report

last = {"phase": "", "pages": -1}

def ping(event):
    phase = event.get("phase") or ""
    pages = event.get("pages") or 0
    if phase != last["phase"] or pages - last["pages"] >= 50:
        last["phase"] = phase
        last["pages"] = pages
        print(phase, pages, "/", event.get("limit"), str(event.get("current") or "")[:90], flush=True)

def main() -> None:
    data = crawl(
        "https://handsomecake.com/",
        max_pages=500000,
        max_depth=6,
        timeout=12,
        deadline_s=20 * 3600,
        workers=8,
        on_progress=ping,
    )
    report = build_report(data)
    files = write_files(data, Path("output"))
    pages = list(data.pages.values())
    unvisited = sum(1 for item in data.extra_status.values() if item.get("unvisited"))
    real_missing = [
        item for item in data.extra_status.values()
        if not item.get("unvisited") and item.get("status") not in (200, 301, 302, 303, 307, 308, None)
    ]
    html_n = sum(len(page.html_issues or []) for page in pages)
    orphans = sum(1 for page in pages if page.status == 200 and page.inlinks == 0 and page.depth > 0)
    not_in = sum(1 for page in pages if page.indexable and page.url not in set(data.sitemap_urls))
    print("DONE", flush=True)
    print("SCORE", report["summary"]["score"], report["summary"]["label"], flush=True)
    print("OPENED", len(pages), "SITEMAP_URLS", len(data.sitemap_urls), "PARTIAL", data.partial, "ELAPSED_S", data.elapsed_ms // 1000, flush=True)
    print("SITEMAP_ERRORS", sum(item["errors"] for item in data.sitemap_files), "SITEMAP_WARN", sum(len(item["warnings"]) for item in data.sitemap_files), flush=True)
    print("ROBOTS", data.robots_warnings, flush=True)
    print("UNVISITED", unvisited, "CONFIRMED_BAD", len(real_missing), flush=True)
    print("HTML_ISSUES", html_n, "ORPHANS_OPENED", orphans, "NOT_IN_SITEMAP", not_in, flush=True)
    print("STATUS", {code: sum(1 for page in pages if page.status == code) for code in sorted({page.status for page in pages})}, flush=True)
    print("FILES", files, flush=True)


if __name__ == "__main__":
    main()
