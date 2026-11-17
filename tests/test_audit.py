"""Проверки обхода, правил и запрета внутренних адресов."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.crawl import crawl
from engine.htmlissues import inspect_html
from engine.sitemaps import analyze_sitemap, robots_warnings
from engine.model import CrawlData, Page
from engine.parse import parse_html
from engine.report import build_report
from engine.robots import Robots
from engine.urls import UnsafeUrl, normalize, prepare_start_url
import server


def _page(title, body, robots="", canonical="", description=""):
    metas = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
    ]
    if description:
        metas.append(f'<meta name="description" content="{description}">')
    if robots:
        metas.append(f'<meta name="robots" content="{robots}">')
    if canonical:
        metas.append(f'<link rel="canonical" href="{canonical}">')
    return (
        "<!doctype html><html lang=\"ru\"><head><title>"
        + title
        + "</title>"
        + "".join(metas)
        + "</head><body>"
        + body
        + "</body></html>"
    )


class Site(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        host = self.headers.get("Host")
        base = f"http://{host}"
        path = urlsplit(self.path).path
        if path == "/robots.txt":
            text = f"User-agent: *\nDisallow: /secret\nSitemap: {base}/sitemap.xml\n"
            self._send(200, text, "text/plain; charset=utf-8")
            return
        if path == "/sitemap.xml":
            xml = (
                "<?xml version=\"1.0\"?><urlset>"
                f"<url><loc>{base}/</loc></url>"
                f"<url><loc>{base}/about</loc></url>"
                f"<url><loc>{base}/orphan</loc></url>"
                "</urlset>"
            )
            self._send(200, xml, "application/xml")
            return
        if path == "/hop":
            self._redirect(base + "/about")
            return
        if path == "/double":
            self._redirect(base + "/hop")
            return
        if path == "/gone":
            self._send(404, "нет", "text/plain; charset=utf-8")
            return
        if path == "/about":
            self._send(200, _page(
                "О нас",
                "<h1>О компании</h1><h1>Ещё заголовок</h1><p>"
                + ("Раздел о компании и команде. " * 30)
                + "</p><a href=\"/\">На главную</a>",
            ))
            return
        if path == "/thin":
            self._send(200, _page(
                "Короткая страница с малым объёмом текста",
                "<h1>Короткая</h1><p>Мало текста тут.</p>",
            ))
            return
        if path == "/closed":
            self._send(200, _page(
                "Закрытая от индекса служебная страница",
                "<h1>Служебная</h1><p>Этот адрес закрыт от поиска намеренно.</p>",
                robots="noindex",
                canonical="https://example.com/other",
                description="Служебная страница, которую не нужно показывать в поиске посетителям сайта.",
            ))
            return
        if path == "/orphan":
            self._send(200, _page(
                "Страница сирота есть только в карте сайта",
                "<h1>Сирота</h1><p>" + ("Текст страницы без входящих ссылок. " * 20) + "</p>",
                description="Страница лежит в карте сайта, но из меню на неё никто не ссылается.",
            ))
            return
        if path == "/secret":
            self._send(200, _page("Секрет", "<h1>Секрет</h1><p>Сюда робот не должен зайти.</p>"))
            return
        if path == "/":
            self._send(200, _page(
                "Тестовый сайт аудита для проверки краулера",
                "<h1>Главная</h1><p>"
                + ("Проверка технического аудита сайта. " * 40)
                + "</p>"
                + '<a href="/about">О компании</a>'
                + '<a href="/gone">Битая</a>'
                + '<a href="/hop">Прыжок</a>'
                + '<a href="/double">Двойной</a>'
                + '<a href="/thin">Короткая</a>'
                + '<a href="/secret">Секрет</a>'
                + '<a href="/closed">Закрытая</a>'
                + '<img src="/pic.png">',
                canonical="/",
                description="Главная тестового сайта для проверки технического аудита, ссылок и карты сайта.",
            ))
            return
        self._send(404, "нет", "text/plain; charset=utf-8")


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.site_thread = threading.Thread(target=cls.site.serve_forever, daemon=True)
        cls.site_thread.start()
        cls.base = f"http://127.0.0.1:{cls.site.server_address[1]}"
        cls.app = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.app_thread = threading.Thread(target=cls.app.serve_forever, daemon=True)
        cls.app_thread.start()
        cls.api = f"http://127.0.0.1:{cls.app.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.site.shutdown()
        cls.app.shutdown()

    def test_normalize_and_private_urls(self):
        self.assertEqual(
            normalize("HTTPS://Example.COM/a/?utm_source=z&b=1"),
            "https://example.com/a?b=1",
        )
        self.assertEqual(normalize("https://example.com/a/"), "https://example.com/a")
        self.assertEqual(normalize("https://example.com/"), "https://example.com/")
        for raw in (
            "http://127.0.0.1/",
            "http://169.254.169.254/",
            "http://10.1.2.3/",
            "http://192.168.0.1/",
            "http://198.18.0.1/",
            "http://[::1]/",
            "localhost/test",
            "file:///etc/passwd",
            "http://user@example.com/",
        ):
            with self.assertRaises(UnsafeUrl):
                prepare_start_url(raw)

    def test_robots_longest_rule_wins(self):
        rules = Robots.parse("User-agent: *\nDisallow: /a\nAllow: /a/b\n", "FrogLabrikaAudit/1.0")
        self.assertFalse(rules.allowed("https://ex.com/a"))
        self.assertTrue(rules.allowed("https://ex.com/a/b"))
        self.assertTrue(rules.allowed("https://ex.com/"))
        self.assertEqual(rules.sitemaps, [])

    def test_parser_reads_meta_and_alt(self):
        parsed = parse_html(
            "<html lang='ru'><head><title>Заголовок</title>"
            "<meta name='description' content='Описание страницы достаточно длинное для проверки'>"
            "<link rel='canonical' href='/a'>"
            "<script type='application/ld+json'>{\"@type\":\"WebSite\"}</script>"
            "</head><body><h1>Один</h1><img src='http://cdn.example/a.png'>"
            "<a href='/b'>дальше</a></body></html>",
            "https://example.com/a",
            "example.com",
        )
        self.assertEqual(parsed.title, "Заголовок")
        self.assertEqual(parsed.h1, ["Один"])
        self.assertEqual(parsed.canonical, "https://example.com/a")
        self.assertEqual(parsed.images_missing_alt, 1)
        self.assertEqual(parsed.internal, ["https://example.com/b"])
        self.assertEqual(parsed.jsonld_ok, 1)
        self.assertIn("http://cdn.example/a.png", parsed.mixed)

    def test_dead_homepage_drops_score(self):
        page = Page(url="https://down.example/", status=0, error="timeout")
        report = build_report(CrawlData(
            start_url=page.url,
            final_url=page.url,
            pages={page.url: page},
            home_error="timeout",
        ))
        self.assertEqual(report["issues"][0]["id"], "home-down")
        self.assertLessEqual(report["summary"]["score"], 40)

    def test_local_site_finds_the_main_issues(self):
        data = crawl(
            self.base + "/",
            max_pages=20,
            max_depth=3,
            respect_robots=True,
            allow_private=True,
            timeout=5,
            deadline_s=30,
            workers=4,
        )
        report = build_report(data)
        ids = {item["id"] for item in report["issues"]}
        self.assertTrue(report["summary"]["robots_found"], report["summary"])
        self.assertGreater(report["summary"]["sitemap_count"], 0, report["summary"])
        for issue_id in (
            "not-https",
            "broken-internal",
            "redirect-chains",
            "multiple-h1",
            "images-alt",
            "robots-blocked-links",
            "sitemap-orphans",
            "noindex",
            "bad-canonical",
        ):
            self.assertIn(issue_id, ids, ids)
        self.assertNotIn("home-down", ids)
        self.assertNotIn("robots-block-all", ids)
        urls = [page["url"] for page in report["pages"]]
        self.assertFalse(any(url.endswith("/secret") for url in urls), urls)
        home = next(page for page in report["pages"] if page["depth"] == 0)
        self.assertEqual(home["status"], 200)
        self.assertTrue(home["indexable"], home)
        chains = next(item for item in report["issues"] if item["id"] == "redirect-chains")
        self.assertTrue(any("/double" in row for row in chains["examples"]))
        self.assertGreater(report["summary"]["score"], 0)
        self.assertLess(report["summary"]["score"], 90)

    def test_api_rejects_private_and_serves_ui(self):
        request = Request(
            self.api + "/api/audits",
            data=json.dumps({"url": "http://127.0.0.1/"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        with urlopen(self.api + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Технический аудит", html)


class FormatChecks(unittest.TestCase):
    def test_deliver_uses_colleague_layout(self):
        import tempfile
        from openpyxl import load_workbook
        from docx import Document
        from engine.deliver import write_files

        data = CrawlData(
            start_url="https://shop.example/",
            final_url="https://shop.example/",
            site_key="shop.example",
            robots_text="User-agent: *\nAllow: /\n",
            robots_found=True,
            robots_warnings=["robots.txt закрывает CSS или JavaScript"],
        )
        page = Page(url="https://shop.example/", status=200, indexable=True, depth=0)
        page.html_issues = [{"detail": 'Атрибут "charset" для meta element должен быть в первых 1024 байт документа.', "where": "На строке 2, столбец 3"}]
        data.pages[page.url] = page
        with tempfile.TemporaryDirectory() as folder:
            written = write_files(data, Path(folder))
            book = load_workbook(Path(folder) / "teh-audit-shop.example.xlsx")
            self.assertEqual(
                book.sheetnames,
                ["Robots.txt", "Нет на сайте", "Нет в Sitemap.xml", "Sitemap", "Страницы-сироты", "Битые ссылки", "HTML", "Сводная таблица 1", "Чужие файлы"],
            )
            self.assertEqual(book["Sitemap"]["L1"].value, "Cодержит ссылки на другие sitemap")
            self.assertTrue(str(book["Sitemap"]["A1"].fill.fgColor.rgb).endswith("283D83"))
            self.assertEqual(book["Сводная таблица 1"]["B2"].value, 1)
            self.assertEqual(book["HTML"]["B2"].value, 1)
            self.assertTrue(str(book["HTML"]["E2"].value).startswith("https://validator.w3.org/"))
            docx_path = Path(folder) / "tz-shop.example.docx"
            document = Document(docx_path)
            self.assertEqual(document.part.rels["rId80"].target_ref, "header1.xml")
            self.assertEqual(document.part.rels["rId81"].target_ref, "footer1.xml")
            self.assertIn("image2.png", document.sections[0].header.part.rels["rId1"].target_ref)
            self.assertIn("image1.png", document.sections[0].footer.part.rels["rId1"].target_ref)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertIn("Технический аудит", text)
            self.assertIn("Таблица ошибок: Тех.аудит | Shop", text)
            self.assertIn("Ошибок: 1", text)
            self.assertIn("Предупреждений: 1", text)
            self.assertIn(written["xlsx"], "/files/teh-audit-shop.example.xlsx")


    def test_page_limit_allows_thousands(self):
        from server import clamp_limits

        pages, _depth, deadline = clamp_limits(20_000, 4)
        self.assertEqual(pages, 20_000)
        self.assertGreater(deadline, 10 * 3600)
        self.assertEqual(clamp_limits(100_000, 8)[0], 100_000)
        self.assertEqual(clamp_limits(900_000, 9)[0], 500_000)

    def test_unvisited_sitemap_url_is_not_a_404(self):
        home = Page(url="https://shop.example/", status=200, indexable=True, depth=0)
        data = CrawlData(
            start_url="https://shop.example/",
            final_url=home.url,
            site_key="shop.example",
            pages={home.url: home},
            sitemap_urls=[home.url, "https://shop.example/product"],
            extra_status={"https://shop.example/product": {"status": None, "final": "https://shop.example/product", "unvisited": True}},
            partial=True,
        )
        report = build_report(data)
        self.assertNotIn("sitemap-errors", {item["id"] for item in report["issues"]})

    def test_sitemap_image_counts_as_error(self):
        parsed = analyze_sitemap(
            """<?xml version="1.0"?>
            <urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
              <url>
                <loc>https://shop.example/p</loc>
                <image:image><image:loc>https://cdn.example/a.jpg</image:loc></image:image>
              </url>
            </urlset>"""
        )
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["pages"], ["https://shop.example/p"])
        self.assertEqual(parsed["images"], [("https://shop.example/p", "https://cdn.example/a.jpg")])
        self.assertIn("Есть адрес без даты lastmod", parsed["warnings"])

    def test_sitemap_warning_rules(self):
        parsed = analyze_sitemap(
            """<?xml version="1.0"?>
            <urlset xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
              <!-- parent -->
              <url>
                <loc>https://shop.example/agents.md</loc>
                <image:image>
                  <image:loc>https://cdn.example/a.jpg</image:loc>
                  <image:caption></image:caption>
                </image:image>
              </url>
            </urlset>"""
        )
        self.assertEqual(
            parsed["warnings"],
            [
                "В файле есть XML-комментарий",
                "Есть адрес без даты lastmod",
                "У изображения в карте пустой caption",
                "В карте указан файл, а не HTML-страница",
            ],
        )

    def test_unknown_robots_directive(self):
        notes = robots_warnings("User-agent: *\nAllow: /\nContent-Signal: search=yes\n")
        self.assertTrue(any("content-signal" in note for note in notes))

    def test_charset_after_1024_bytes(self):
        raw = b"<html><head>" + (b" " * 1100) + b'<meta charset="utf-8"></head><body></body></html>'
        details = [item["detail"] for item in inspect_html(raw)]
        self.assertTrue(any("charset" in detail for detail in details))


if __name__ == "__main__":
    unittest.main()
