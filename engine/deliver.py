"""Два файла результата в виде аудита коллеги: таблица и общее ТЗ."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .model import CrawlData

_NAVY = "FF283D83"
_HEADER_FILL = PatternFill(start_color=_NAVY, end_color=_NAVY, fill_type="solid")
_FONT = "Montserrat"
_HEADER_FONT = Font(name=_FONT, bold=True, color="FFFFFFFF", size=11)
_CELL_FONT = Font(name=_FONT, size=11, color="FF000000")
_MUTED_FONT = Font(name=_FONT, size=9, color="FF5F6B7A")
_LINK_FONT = Font(name=_FONT, size=11, color="FF283D83", underline="single")
_PIVOT_LABEL = PatternFill(start_color="FFD9E0E8", end_color="FFD9E0E8", fill_type="solid")
_PIVOT_COUNT = PatternFill(start_color="FF6883A4", end_color="FF6883A4", fill_type="solid")
_PIVOT_FONT = Font(name=_FONT, size=11, color="FF000000")
_CENTER = Alignment(vertical="center", wrap_text=True, horizontal="center")
_LEFT = Alignment(vertical="center", wrap_text=True)

_SHEETS = [
    "Robots.txt",
    "Нет на сайте",
    "Нет в Sitemap.xml",
    "Sitemap",
    "Страницы-сироты",
    "HTML",
    "Сводная таблица 1",
    "Чужие файлы",
]


def _sheet(book: Workbook, title: str, headers: list[str], rows: list[list], widths: list[float], freeze: str | None, muted: bool = False):
    sheet = book.create_sheet(title[:31])
    sheet.append(headers)
    sheet.row_dimensions[1].height = 36
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _CENTER
    font = _MUTED_FONT if muted else _CELL_FONT
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            cell.font = font
            cell.alignment = _LEFT
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = _LINK_FONT
    if freeze:
        sheet.freeze_panes = freeze
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.oddHeader.left.text = title
    sheet.oddHeader.left.font = _FONT
    sheet.oddFooter.right.text = "Технический аудит"
    return sheet


def _kb(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def _yn(flag: bool) -> str:
    return "Да" if flag else "Нет"


def _origin(data: CrawlData) -> str:
    parts = urlsplit(data.final_url or data.start_url)
    return f"{parts.scheme}://{parts.netloc}"


def write_files(data: CrawlData, folder: Path) -> dict[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    host = (data.site_key or "site").replace("/", "-")
    xlsx_name = f"teh-audit-{host}.xlsx"
    docx_name = f"tz-{host}.docx"
    _workbook(data, folder / xlsx_name)
    _document(data, folder / docx_name)
    return {"xlsx": f"/files/{xlsx_name}", "docx": f"/files/{docx_name}"}


def _pages(data: CrawlData):
    return list(data.pages.values())


def _workbook(data: CrawlData, path: Path) -> None:
    book = Workbook()
    book.remove(book.active)
    home = data.final_url
    sitemap_set = set(data.sitemap_urls)
    pages = _pages(data)
    origin = _origin(data)
    robots_url = origin + "/robots.txt"

    robot_rows = [[index, line] for index, line in enumerate((data.robots_text or "").splitlines(), start=1)]
    _sheet(
        book,
        "Robots.txt",
        ["Строка", "Содержимое строки"],
        robot_rows or [["—", "Файл не получен"]],
        [12, 110],
        "A2",
        muted=True,
    )

    missing = []
    for page in pages:
        if page.url in sitemap_set and page.status not in (0, 200, 301, 302, 303, 307, 308):
            tokens = f"{page.meta_robots} {page.x_robots}".lower()
            missing.append([
                page.url,
                page.status or "—",
                page.inlinks,
                _yn("noindex" in tokens or "none" in tokens),
                _yn("nofollow" in tokens),
                "Нет",
            ])
    for url, extra in data.extra_status.items():
        status = extra.get("status") or 0
        if status not in (200, 301, 302, 303, 307, 308):
            missing.append([url, status or "—", "—", "Нет", "Нет", "Нет"])
    _sheet(
        book,
        "Нет на сайте",
        [
            "Есть в sitemap, но не найдены при обходе сайта",
            "HTTP Code",
            "Входящих ссылок",
            "Заблокирован noindex",
            "Заблокирован nofollow или маршрут краулера",
            "Блокировка в robots.txt (yandex)",
        ],
        missing,
        [72, 14, 18, 24, 42, 36],
        "A2",
    )

    not_in = []
    for page in pages:
        if page.indexable and page.url not in sitemap_set:
            tokens = f"{page.meta_robots} {page.x_robots}".lower()
            not_in.append([
                page.url,
                page.status,
                page.inlinks,
                _yn("noindex" in tokens),
                _yn("nofollow" in tokens),
            ])
    _sheet(
        book,
        "Нет в Sitemap.xml",
        [
            "Нет в sitemap, но разрешены для индексации",
            "HTTP Code",
            "Входящих ссылок",
            "Заблокирован noindex",
            "Заблокирован nofollow или маршрут краулера",
        ],
        not_in,
        [72, 14, 18, 24, 42],
        "A2",
    )

    sitemap_rows = []
    for item in data.sitemap_files:
        sitemap_rows.append([
            item["url"],
            item["errors"],
            len(item.get("warnings") or []),
            item.get("found_in") or (robots_url if data.robots_found else ""),
            _kb(item.get("size") or 0),
            item.get("status") or 0,
            item.get("status") or 0,
            "Нет",
            "—",
            "Да" if item.get("checked") else "Нет",
            item.get("kind") or "",
            "Да" if item.get("children") else "Нет",
            item.get("children") or 0,
            item.get("items") or 0,
            item.get("html") or 0,
        ])
    _sheet(
        book,
        "Sitemap",
        [
            "URL файла sitemap",
            "Ошибок",
            "Предупреждений",
            "Где нашли",
            "Размер файла",
            "Код ответа",
            "Финальный код ответа",
            "Сжатие",
            "Правильная распаковка",
            "Файл проверен",
            "Формат",
            "Cодержит ссылки на другие sitemap",
            "Количество ссылок на другие sitemap",
            "Количество элементов",
            "Количество страниц (только html)",
        ],
        sitemap_rows,
        [62, 12, 16, 46, 14, 14, 22, 12, 24, 16, 16, 36, 38, 22, 34],
        None,
    )

    orphans = []
    if not data.partial:
        for url, extra in data.extra_status.items():
            final = extra.get("final") or url
            if extra.get("status") == 200 and url not in data.linked and final not in data.linked and url != home:
                orphans.append([url, "Карта сайта"])
    for page in pages:
        if page.status == 200 and page.inlinks == 0 and page.url != home and page.depth > 0:
            orphans.append([page.url, "Обход"])
    _sheet(book, "Страницы-сироты", ["URL", "Источник"], orphans, [72, 18], None)

    broken_rows = []
    link_from = data.link_from or {}
    broken_pages = [page for page in pages if 400 <= page.status < 500]
    broken_pages.sort(key=lambda page: len(link_from.get(page.url, [])), reverse=True)
    for page in broken_pages:
        sources = link_from.get(page.url, [])
        if not sources:
            broken_rows.append([page.url, page.status, 0, ""])
            continue
        for source in sources:
            broken_rows.append([page.url, page.status, len(sources), source])
    _sheet(
        book,
        "Битые ссылки",
        ["Куда ведёт", "Код ответа", "Сколько раз", "Страница"],
        broken_rows,
        [62, 14, 16, 72],
        "A2",
    )

    html_groups: dict[str, dict] = {}
    for page in pages:
        for issue in page.html_issues or []:
            detail = issue.get("detail") or "Ошибка HTML"
            group = html_groups.setdefault(detail, {"count": 0, "pages": []})
            group["count"] += 1
            group["pages"].append({"url": page.url, "where": issue.get("where") or ""})
    html_rows = []
    for detail, group in html_groups.items():
        for item in group["pages"]:
            look = "https://validator.w3.org/nu/?doc=" + quote(item["url"], safe="")
            html_rows.append([detail, group["count"], item["url"], item["where"], look])
    _sheet(
        book,
        "HTML",
        [
            "Сведения об ошибке",
            "Сколько раз",
            "Страница",
            "Расположение",
            "Посмотреть пример",
        ],
        html_rows,
        [72, 16, 55, 32, 46],
        "A2",
    )

    summary = book.create_sheet("Сводная таблица 1")
    summary["A1"] = "Сведения об ошибке"
    summary["B1"] = "COUNTA из Сведения об ошибке"
    summary["A1"].font = _PIVOT_FONT
    summary["B1"].font = Font(name=_FONT, size=11, color="FFFFFFFF")
    summary["A1"].fill = _PIVOT_LABEL
    summary["B1"].fill = _PIVOT_COUNT
    summary["A1"].alignment = _LEFT
    summary["B1"].alignment = _CENTER
    summary.row_dimensions[1].height = 32
    seen_details: set[str] = set()
    details = []
    for row in html_rows:
        if row[0] and row[0] not in seen_details:
            seen_details.add(row[0])
            details.append(row)
    for index, row in enumerate(details, start=2):
        summary.cell(index, 1, row[0]).font = _CELL_FONT
        summary.cell(index, 2, row[1]).font = _CELL_FONT
        summary.cell(index, 1).alignment = _LEFT
        summary.cell(index, 2).alignment = _CENTER
    summary.column_dimensions["A"].width = 78
    summary.column_dimensions["B"].width = 36
    summary.page_setup.orientation = "landscape"
    summary.page_setup.fitToWidth = 1
    summary.page_setup.fitToHeight = 0
    summary.sheet_properties.pageSetUpPr.fitToPage = True

    grouped: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    for page in pages:
        for asset in page.assets or []:
            url = asset.get("url") or ""
            if not url:
                continue
            grouped.setdefault(url, [])
            if page.url not in grouped[url]:
                grouped[url].append(page.url)
            kinds[url] = asset.get("kind") or ""
    assets = []
    for url, owners in grouped.items():
        domain = url.split("/")[2] if url.startswith("http") and url.count("/") >= 2 else ""
        for owner in owners:
            assets.append([url, domain, kinds.get(url, ""), owner])
    _sheet(
        book,
        "Чужие файлы",
        ["URL", "Домен", "Тип файла", "Страница"],
        assets,
        [68, 24, 14, 72],
        "A2",
    )

    book.save(path)


def _counts(data: CrawlData) -> dict:
    home = data.final_url
    pages = _pages(data)
    sitemap_set = set(data.sitemap_urls)
    html_rows = [issue for page in pages for issue in (page.html_issues or [])]
    assets = {asset.get("url") for page in pages for asset in (page.assets or []) if asset.get("url")}
    direct = f"https://{(data.site_key or '').rstrip('/')}"
    open_http = []
    indirect = []
    for probe in data.mirror_probes or []:
        address = str(probe.get("url") or "")
        location = str(probe.get("location") or "").split("?")[0].rstrip("/")
        if probe.get("status") == 200 and address.startswith("http://"):
            open_http.append(address)
        elif probe.get("status") in (301, 302, 303, 307, 308) and location and location != direct:
            indirect.append(f"{address} → {probe.get('location')}")
    return {
        "robots_errors": len([item for item in data.robots_warnings if "закрывает весь" in item]),
        "robots_warnings": len(data.robots_warnings),
        "blocked": [page.url for page in pages if page.url in sitemap_set and page.status >= 500],
        "missing": [page.url for page in pages if page.url in sitemap_set and 400 <= page.status < 500],
        "not_in": [page.url for page in pages if page.indexable and page.url not in sitemap_set],
        "sitemap_errors": sum(int(item.get("errors") or 0) for item in data.sitemap_files),
        "sitemap_warnings": sum(len(item.get("warnings") or []) for item in data.sitemap_files),
        "noindex": [page.url for page in pages if page.status == 200 and "noindex" in (page.index_reason or "")],
        "orphans": [page.url for page in pages if page.status == 200 and page.inlinks == 0 and page.url != home and page.depth > 0],
        "server": [page.url for page in pages if page.status >= 500],
        "client": [page.url for page in pages if 400 <= page.status < 500],
        "broken": [page.url for page in pages if 400 <= page.status < 500 and page.inlinks],
        "open_http": open_http,
        "indirect": indirect,
        "html": html_rows,
        "comments": [issue for issue in html_rows if "комментар" in issue.get("detail", "")],
        "assets": assets,
        "plugins": [issue for issue in html_rows if "плагин" in issue.get("detail", "")],
        "mixed": [page.url for page in pages if page.mixed],
        "http_links": list(dict.fromkeys(url for page in pages for url in (page.http_internal or []))),
        "missing_canon": [page.url for page in pages if page.status == 200 and page.canonical_count == 0],
        "multi_canon": [page.url for page in pages if page.canonical_count > 1],
        "slow": [page.url for page in pages if page.status == 200 and page.time_ms >= 2500],
    }


_DOC_NAVY = RGBColor(0x10, 0x1A, 0x4A)
_DOC_GRAY = RGBColor(0x66, 0x66, 0x66)


def _brand(host: str) -> str:
    name = (host or "сайт").split(".")[0]
    return name[:1].upper() + name[1:] if name else "Сайт"


def _force_font(style, size: int, color: RGBColor | None = None, bold: bool | None = None) -> None:
    style.font.name = _FONT
    style.font.size = Pt(size)
    if color is not None:
        style.font.color.rgb = color
    if bold is not None:
        style.font.bold = bold
    r_pr = style.element.get_or_add_rPr()
    fonts = r_pr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        r_pr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), _FONT)


def _run(paragraph, text: str, size: int, color: RGBColor, bold: bool = False):
    run = paragraph.add_run(text)
    run.font.name = _FONT
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.bold = bold
    fonts = run._element.get_or_add_rPr().find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run._element.rPr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), _FONT)
    return run


def _body(document: Document, text: str):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    _run(paragraph, text, 11, RGBColor(0x00, 0x00, 0x00))
    return paragraph


def _gap(document: Document):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    return paragraph


def _meta(document: Document, text: str):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    _run(paragraph, text, 11, _DOC_GRAY)
    return paragraph


def _heading(document: Document, text: str):
    paragraph = document.add_heading(text, level=2)
    paragraph.paragraph_format.space_before = Pt(14)
    paragraph.paragraph_format.space_after = Pt(6)
    for run in paragraph.runs:
        run.font.name = _FONT
        run.font.color.rgb = _DOC_NAVY
        run.font.size = Pt(16)
        run.bold = True
        fonts = run._element.get_or_add_rPr().find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            run._element.rPr.append(fonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            fonts.set(qn(attr), _FONT)
    return paragraph


def _where(document: Document, urls: list[str]) -> None:
    _meta(document, "Страницы, где есть этот повтор:")
    for url in urls:
        _meta(document, url)


def _result(document: Document, brand: str, sheet: str | None, errors: int, warnings: int):
    if errors or warnings:
        if sheet:
            _meta(document, f"Ссылка на страницу таблицы: Тех.аудит | {brand}")
        _meta(document, f"Ошибок: {errors}")
        _meta(document, f"Предупреждений: {warnings}")
    else:
        _meta(document, "Ошибок не обнаружено")


_BRAND = Path(__file__).resolve().parent.parent / "assets" / "brand"


def _stamp_branding(path: Path) -> None:
    """Колонтитулы SEO Performance: логотип сверху и контакты снизу, как в образце."""
    header = (_BRAND / "header1.xml").read_bytes()
    footer = (_BRAND / "footer1.xml").read_bytes()
    header_rels = (_BRAND / "header1.xml.rels").read_bytes()
    footer_rels = (_BRAND / "footer1.xml.rels").read_bytes()
    logo = (_BRAND / "image2.png").read_bytes()
    contacts = (_BRAND / "image1.png").read_bytes()
    snippet = (
        '<w:headerReference w:type="default" r:id="rId80"/>'
        '<w:footerReference w:type="default" r:id="rId81"/>'
    )
    rels = (
        '<Relationship Id="rId80" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
        '<Relationship Id="rId81" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
    )
    packed = io.BytesIO()
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                text, count = re.subn(r"<w:sectPr\b[^>]*>", lambda match: match.group(0) + snippet, text, count=1)
                if count != 1:
                    raise RuntimeError("В документе нет секции страницы")
                data = text.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                text = data.decode("utf-8")
                data = text.replace("</Relationships>", rels + "</Relationships>").encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                extra = ""
                if 'Extension="png"' not in text:
                    extra += '<Default Extension="png" ContentType="image/png"/>'
                extra += (
                    '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
                    '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
                )
                data = text.replace("</Types>", extra + "</Types>").encode("utf-8")
            target.writestr(item, data)
        target.writestr("word/header1.xml", header)
        target.writestr("word/footer1.xml", footer)
        target.writestr("word/_rels/header1.xml.rels", header_rels)
        target.writestr("word/_rels/footer1.xml.rels", footer_rels)
        target.writestr("word/media/image2.png", logo)
        target.writestr("word/media/image1.png", contacts)
    path.write_bytes(packed.getvalue())


def _document(data: CrawlData, path: Path) -> None:
    host = data.site_key or urlsplit(data.start_url).hostname or "сайт"
    found = _counts(data)
    sections = [
        ("Ошибки в файле Robots.txt", "Файл robots.txt содержит синтаксические ошибки, некорректные директивы или блокирует важных ботов. Он находится в корне домена и управляет доступом поисковых систем к ресурсам сайта.", "Неправильные правила могут полностью закрыть сайт от индексации или заблокировать загрузку критически важных CSS и JS-файлов. Это приводит к выпадению страниц из поиска, неправильному сканированию и потере органического трафика.", "Robots.txt", found["robots_errors"], found["robots_warnings"]),
        ("Страницы в Sitemap, недоступные для краулинга", "URL-адреса добавлены в файл sitemap.xml, но фактически закрыты от сканирования в robots.txt, требуют авторизации или возвращают ошибки сервера. Краулер не может получить к ним доступ.", "Поисковые системы тратят краулинговый бюджет на недоступные адреса, что замедляет индексацию остального сайта. Страницы не получают ссылочного веса и не участвуют в ранжировании, снижая общую эффективность продвижения.", "Нет на сайте", len(found["blocked"]), 0),
        ("Страницы есть в файле Sitemap, но отсутствуют на сайте", "URL-адреса добавлены в файл sitemap.xml, но фактически не существуют на сайте (возвращают ошибку 404 или 410) либо закрыты от сканирования. Краулер не может получить к ним доступ.", "Поисковые системы тратят краулинговый бюджет на сканирование несуществующих адресов, что замедляет индексацию остального сайта. Регулярное наличие таких ошибок снижает доверие поисковых роботов к файлу sitemap.xml и общей технической оценке сайта.", "Нет на сайте", len(found["missing"]), 0),
        ("Страницы, отсутствующие в файле Sitemap", "Важные для продвижения страницы, открытые для индексации, не добавлены в файл sitemap.xml. Поисковые системы могут не узнать об их существовании без прямых внутренних ссылок.", "Замедляется обнаружение и индексация нового контента. Поисковые роботы могут реже заходить на такие страницы, что приведет к потере позиций и органического трафика по целевым запросам.", "Нет в Sitemap.xml", len(found["not_in"]), 0),
        ("Ошибки в формате или структуре файла Sitemap", "Файл sitemap.xml содержит синтаксические ошибки, некорректные XML-теги, недопустимые символы в URL-адресах или не соответствует официальному протоколу Sitemap (например, отсутствует обязательный тег <urlset>, неверная кодировка или размер файла превышает допустимые лимиты без разбивки на индекс).", "Поисковые системы не могут корректно прочитать и обработать такой файл, из-за чего весь список URL-адресов или его часть будут полностью проигнорированы. Это лишает сайт преимуществ ускоренной индексации и может привести к выпадению важных страниц из поисковой выдачи.", "Sitemap", found["sitemap_errors"], found["sitemap_warnings"]),
        ("Страницы с директивой noindex", "Наличие мета-тега noindex или HTTP-заголовка X-Robots-Tag, который запрещает поисковым системам добавлять страницу в индекс. Робот сканирует URL, но удаляет его из поиска.", "Страница полностью исчезает из органической выдачи. Если запрет установлен ошибочно на коммерческих или информационных страницах, это приводит к прямой и критической потере целевого трафика.", None, len(found["noindex"]), 0),
        ("Страницы-сироты (Orphaned Pages)", "Страницы, на которые не ведет ни одна внутренняя ссылка на сайте, и которые отсутствуют в файле sitemap.xml. Они изолированы от основной структуры ресурса.", "Поисковые роботы не могут найти такие страницы без прямых внешних ссылок. Они не получают внутреннего ссылочного веса, не участвуют в ранжировании и часто выпадают из индекса.", "Страницы-сироты", len(found["orphans"]), 0),
        ("Ошибки сервера 5xx", "Сервер возвращает коды состояния 500-599, что означает внутреннюю ошибку на стороне хостинга. Страница полностью недоступна для загрузки как пользователями, так и поисковыми роботами.", "Приводит к выпадению контента из индекса и потере трафика. Частые сбои снижают доверие поисковых систем к сайту, увеличивают время простоя и могут спровоцировать алгоритмические санкции.", None, len(found["server"]), 0),
        ("Страницы с кодом ответа 4xx (404)", "Существующие URL-адреса отдают серверу код 404 (Not Found). Страница физически доступна по ссылке, но сервер сообщает поисковому роботу, что контент не найден.", "Поисковые системы перестают индексировать такие страницы и удаляют их из выдачи. Это приводит к потере накопленного ссылочного веса, снижению видимости сайта и ухудшению пользовательского опыта.", None, len(found["client"]), 0),
        ("Битые внутренние ссылки", "Ссылки на страницах сайта ведут на несуществующие URL, которые отдают код ответа 404 или 410. Пользователь или робот, переходя по ним, попадает на страницу ошибки.", "Разрушается внутренняя перелинковка, теряется ссылочный вес. Ухудшается пользовательский опыт, растут показатели отказов. Поисковые системы снижают доверие к качеству и технической поддержке ресурса.", "Битые ссылки", len(found["broken"]), 0),
        ("Некорректные коды ответа и дубли URL", "Наличие цепочек редиректов, склейка зеркал с разными протоколами или слэшами, а также отдача кода 200 для несуществующих страниц (Soft 404). URL-адреса не имеют единого канонического формата.", "Размывается ссылочный вес между дублями, тратится краулинговый бюджет. Поисковики не понимают, какую версию страницы ранжировать, что ведет к каннибализации запросов и потере позиций в выдаче.", "mirror", len(found["open_http"]), len(found["indirect"])),
        ("Критические ошибки в HTML-коде", "Наличие незакрытых тегов, дублирующихся ID, невалидной верстки и синтаксических ошибок в коде страницы. Структура документа не соответствует стандартам W3C.", "Может приводить к некорректному отображению контента в разных браузерах, ломать верстку и скрывать важные блоки от поисковых роботов. Снижается скорость рендеринга и доверие к техническому качеству сайта.", "HTML", len(found["html"]), 0),
        ("Ошибки в HTML-комментариях", "Неправильно закрытые или вложенные HTML-комментарии, которые ломают синтаксис кода. Из-за этого часть контента или важные теги могут случайно попасть внутрь комментария.", "Поисковые роботы не видят скрытый за ошибкой контент, мета-теги или микроразметку. Это приводит к некорректной индексации, потере семантического веса и неправильному формированию сниппетов в выдаче.", None, len(found["comments"]), 0),
        ("Потерянные файлы CSS", "Поисковый робот не может получить доступ к файлам каскадных таблиц стилей (CSS) из-за ошибок в путях, блокировок в robots.txt или сбоев на сервере.", "Google и Яндекс используют CSS для оценки мобильности и удобства сайта. Без стилей робот видит «сломанную» страницу, что ведет к понижению в мобильной выдаче и ухудшению поведенческих факторов.", "css", 0, 0),
        ("Потерянные файлы JavaScript", "Файлы JavaScript недоступны для скачивания поисковым роботом из-за некорректных ссылок, блокировок или ошибок сервера. Динамический контент не может быть отрендерен.", "Современные поисковики рендерят страницы как браузеры. Если JS не загружается, робот не видит важный контент, меню или интерактивные элементы, что критически снижает релевантность и позиции страницы.", "js", 0, 0),
        ("Подключение сторонних ресурсов (Hotlinking)", "Использование изображений, CSS или JS-файлов, загруженных напрямую с серверов других сайтов, вместо размещения их на собственном хостинге.", "Создает критическую зависимость от чужой инфраструктуры. При блокировке или удалении файлов ломается верстка. Замедляется скорость загрузки, возникают риски безопасности и нарушения авторских прав.", "Чужие файлы", len(found["assets"]), 0),
        ("Использование устаревших плагинов", "Внедрение на сайт контента, требующего работы устаревших технологий, таких как Flash или Silverlight, которые больше не поддерживаются современными браузерами.", "Контент полностью недоступен на мобильных устройствах и в современных браузерах. Поисковые роботы не могут его проиндексировать, что ведет к потере трафика и резкому ухудшению пользовательского опыта.", None, len(found["plugins"]), 0),
        ("Смешанный контент (Mixed Content)", "Загрузка незащищенных ресурсов по протоколу HTTP (изображения, скрипты, стили) на страницах, работающих по защищенному протоколу HTTPS.", "Браузеры блокируют такой контент или выводят предупреждения о небезопасном соединении. Это ломает верстку, отпугивает пользователей, снижает доверие и косвенно ухудшает позиции из-за падения поведенческих факторов.", None, len(found["mixed"]), 0),
        ("Ссылки на незащищенные HTTP-страницы", "Наличие на HTTPS-страницах внутренних ссылок, ведущих на HTTP-версии других страниц сайта. Переход по ним сбрасывает защищенное соединение.", "Ухудшает безопасность и пользовательский опыт, браузеры помечают такие переходы как небезопасные. Замедляет загрузку страниц и препятствует корректной склейке зеркал, снижая общее доверие поисковиков.", None, len(found["http_links"]), 0),
        ("Отсутствие тега rel=canonical", 'Полное отсутствие тега rel="canonical" на страницах, которые имеют потенциальные дубли (с параметрами URL, сортировками, фильтрами, разными протоколами или версиями для печати).', "Поисковые системы самостоятельно выбирают каноническую версию страницы, что часто приводит к индексации дублей, размыванию ссылочного веса между копиями и каннибализации запросов. Это снижает релевантность основного URL и ухудшает позиции в выдаче.", None, len(found["missing_canon"]), 0),
        ("Множественные теги rel=canonical", 'Наличие более одного тега rel="canonical" в коде страницы или дублирование этой директивы в HTTP-заголовках и sitemap.xml с разными значениями.', "Поисковые системы не понимают, какую версию страницы считать главной, и могут проигнорировать все указания. Это приводит к дублированию контента в индексе, размыванию ссылочного веса и потере позиций.", None, len(found["multi_canon"]), 0),
        ("Низкая производительность сайта", "Медленная загрузка страниц из-за тяжелого кода, неоптимизированных изображений, отсутствия кэширования или медленного ответа сервера (высокий TTFB).", "Скорость загрузки является прямым фактором ранжирования (Core Web Vitals). Медленные сайты получают пессимизацию в поиске, особенно в мобильной выдаче. Высокое время ожидания увеличивает показатель отказов, снижает конверсию и ухудшает поведенческие факторы, что критически влияет на общие позиции ресурса.", None, len(found["slow"]), 0),
    ]

    document = Document()
    normal = document.styles["Normal"]
    _force_font(normal, 11)
    title_style = document.styles["Title"]
    _force_font(title_style, 20, _DOC_NAVY, True)
    title_style.paragraph_format.space_before = Pt(14)
    title_style.paragraph_format.space_after = Pt(6)
    subtitle_style = document.styles["Subtitle"]
    _force_font(subtitle_style, 12, _DOC_GRAY, False)
    subtitle_style.paragraph_format.space_before = Pt(0)
    subtitle_style.paragraph_format.space_after = Pt(8)
    heading_style = document.styles["Heading 2"]
    _force_font(heading_style, 16, _DOC_NAVY, True)
    heading_style.paragraph_format.space_before = Pt(14)
    heading_style.paragraph_format.space_after = Pt(6)

    section = document.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.left_margin = Emu(1080135)
    section.right_margin = Emu(539750)
    section.top_margin = Emu(1866900)
    section.bottom_margin = Emu(720090)
    section.header_distance = Emu(449580)
    section.footer_distance = Emu(449580)

    brand = _brand(host)
    title = document.add_paragraph(style="Title")
    _run(title, "Технический аудит", 20, _DOC_NAVY, bold=True)
    subtitle = document.add_paragraph(style="Subtitle")
    _run(subtitle, "Сайт: ", 12, _DOC_GRAY)
    _run(subtitle, data.start_url, 12, _DOC_NAVY)
    _run(subtitle, f"  Таблица ошибок: Тех.аудит | {brand}", 12, _DOC_GRAY)
    _gap(document)
    _body(
        document,
        "Данный аудит подготовлен с целью комплексной диагностики сайта и выявления технических ошибок. Для ошибок , требующих исправления , подготовлены соответствующие технические задания. Все ТЗ продублированы и закреплены в плане работ.",
    )
    if data.partial:
        _meta(document, "Обход открыл не все страницы каталога. Остальные адреса из карты записаны в лист «Нет на сайте» с кодом «—».")

    for heading, what, impact, sheet, errors, warnings in sections:
        _heading(document, heading)
        _body(document, what)
        _gap(document)
        _body(document, impact)
        _gap(document)
        if sheet == "mirror":
            if found["open_http"] or found["indirect"]:
                _meta(document, "Обнаружены незакрытые склейки, примеры ошибок включены в ТЗ.")
                for example in (found["open_http"] + found["indirect"])[:6]:
                    _meta(document, str(example))
            else:
                _meta(document, "Ошибок не обнаружено")
        elif sheet in {"css", "js"}:
            _meta(document, "Коды ответа отдельных файлов в этом обходе не запрашивались.")
        else:
            _result(document, brand, sheet, errors, warnings)
        if heading.startswith("Критические ошибки"):
            grouped_pages: dict[str, list[str]] = {}
            for page in _pages(data):
                for issue in page.html_issues or []:
                    detail = issue.get("detail") or "Ошибка HTML"
                    bucket = grouped_pages.setdefault(detail, [])
                    if page.url not in bucket:
                        bucket.append(page.url)
            for detail, urls in grouped_pages.items():
                if len(urls) < 2:
                    continue
                _meta(
                    document,
                    f"Однотипная ошибка, повторяется {len(urls)} раз: {detail}. Достаточно исправить её в шаблоне один раз.",
                )
                _where(document, urls)
        if heading.startswith("Битые внутренние"):
            link_from = data.link_from or {}
            repeated = sorted(
                ((url, link_from.get(url, [])) for url in found["broken"]),
                key=lambda item: len(item[1]),
                reverse=True,
            )
            for url, sources in repeated:
                if len(sources) < 2:
                    if sources:
                        _meta(document, f"{url} — ссылка со страницы {sources[0]}.")
                    continue
                _meta(document, f"Повтор {len(sources)} раз, цель {url}.")
                _where(document, sources)
        if heading.startswith("Страницы есть в файле Sitemap"):
            unvisited = sum(1 for item in data.extra_status.values() if item.get("unvisited"))
            if unvisited:
                _meta(document, f"В таблице «Нет на сайте» ещё {unvisited} адресов из карты. Обход их не открывал, поэтому код «—». Это не подтверждённый 404.")
        if heading.startswith("Ошибки в формате") and found["sitemap_errors"]:
            _meta(document, f"Адреса изображений внутри карты: {found['sitemap_errors']}. Предупреждение ставится на файл целиком.")

    document.save(path)
    _stamp_branding(path)
