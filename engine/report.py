"""Замечания и оценка: что мешает обходу и индексации, по важности."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit

from .model import CrawlData, Page
from .net import REDIRECTS
from .urls import normalize

_SEVERITY = {"critical": 0, "warning": 1, "notice": 2}


def _count(number: int, one: str, few: str, many: str) -> str:
    value = abs(int(number))
    if value % 10 == 1 and value % 100 != 11:
        word = one
    elif 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        word = few
    else:
        word = many
    return f"{value} {word}"


def _chain_text(event: dict) -> str:
    parts = [event["from"]]
    for hop in event.get("chain") or []:
        parts.append(f"{hop.get('status')} {hop.get('location') or ''}".strip())
    return " → ".join(parts)


def _page_public(page: Page) -> dict:
    return {
        "url": page.url,
        "status": page.status,
        "indexable": page.indexable,
        "index_reason": page.index_reason,
        "title": page.title[:300],
        "title_len": len(page.title),
        "description": page.description[:500],
        "description_len": len(page.description),
        "h1": page.h1[:4],
        "canonical": page.canonical,
        "meta_robots": page.meta_robots,
        "words": page.words,
        "time_ms": page.time_ms,
        "size": page.size,
        "depth": page.depth,
        "inlinks": page.inlinks,
        "internal_links": page.internal_links,
        "external_links": page.external_links,
        "images": page.images,
        "images_missing_alt": page.images_missing_alt,
        "via_redirect": page.via_redirect,
        "error": page.error,
        "lang": page.lang,
    }


def build_report(data: CrawlData) -> dict:
    issues: list[dict] = []
    pages = list(data.pages.values())
    home = data.pages.get(data.final_url)
    if home is None and pages:
        home = pages[0]

    def add(issue_id, severity, category, title, why, fix, urls=None, examples=None, penalty=0):
        found = list(dict.fromkeys(urls or []))
        issues.append(
            {
                "id": issue_id,
                "severity": severity,
                "category": category,
                "title": title,
                "why": why,
                "fix": fix,
                "urls": found[:50],
                "url_count": len(found),
                "examples": (examples or [])[:20],
                "penalty": penalty,
            }
        )

    if not home or home.status != 200:
        status = home.status if home else 0
        error = (home.error if home else "") or (data.home_error or "")
        if status == 0:
            why = "Сайт не ответил."
            penalty = 60
        elif status >= 500:
            why = f"Стартовая страница отдала код {status}."
            penalty = 45
        else:
            why = f"Стартовая страница отдала код {status}."
            penalty = 40
        if error:
            why += " " + error
        why += " Пока она не открывается с кодом 200, поисковый робот не начнёт нормальный обход."
        add(
            "home-down",
            "critical",
            "Доступность",
            "Стартовая страница не открывается",
            why,
            "Проверьте, что адрес открывается в браузере без ошибки, сертификат живой, а сервер отдаёт 200.",
            urls=[data.final_url or data.start_url],
            penalty=penalty,
        )
        if status == 0:
            return _pack(data, issues, pages, home)

    indexable = [page for page in pages if page.indexable]
    ok_pages = [page for page in pages if page.status == 200]
    home_url = home.url if home else data.final_url

    if data.tls_note:
        local_store = "local issuer" in data.tls_note.lower() or "self-signed" in data.tls_note.lower() or "self signed" in data.tls_note.lower()
        add(
            "tls-trust",
            "notice" if local_store else "warning",
            "Безопасность",
            "Сертификат не подтверждён с этого компьютера",
            "Проверка доверия не прошла: "
            + data.tls_note
            + " Страницы всё равно прочитаны. Если браузер открывает сайт без предупреждения, это хранилище сертификатов Python или локальный прокси, а не поломка сайта.",
            "Откройте сайт в браузере. Предупреждение там значит, что сертификат надо заменить. Если браузер молчит, на вывод аудита это не влияет.",
            penalty=3 if local_store else 8,
        )

    if home and urlsplit(home.url).scheme != "https":
        add(
            "not-https",
            "critical",
            "Безопасность",
            "Сайт открывается без HTTPS",
            "Страница пришла по обычному HTTP. Браузер помечает такие сайты как небезопасные, а поисковик предпочитает HTTPS.",
            "Поставьте сертификат и направьте HTTP на HTTPS одним редиректом 301.",
            urls=[home.url],
            penalty=18,
        )

    if data.blocks_all:
        add(
            "robots-block-all",
            "critical",
            "Индексация",
            "robots.txt закрывает весь сайт",
            "Правило Disallow для главной не пускает роботов дальше. Сайт может выпасть из поиска.",
            "Оставьте закрытыми только служебные разделы. Главная и публичные страницы должны быть разрешены.",
            penalty=25,
        )

    tokens = _robots_tokens(home) if home else set()
    if home and home.status == 200 and ("noindex" in tokens or "none" in tokens):
        add(
            "home-noindex",
            "critical",
            "Индексация",
            "Стартовая страница закрыта noindex",
            "В meta robots или заголовке X-Robots-Tag стоит запрет индексации. Для главной это почти всегда ошибка.",
            "Уберите noindex со страниц, которые должны быть в поиске.",
            urls=[home.url],
            penalty=20,
        )
    if home and home.status == 200 and "nofollow" in tokens:
        add(
            "home-nofollow",
            "warning",
            "Индексация",
            "Со стартовой страницы запрещена передача ссылок",
            "nofollow на главной обрывает сигналы внутренним страницам.",
            "Уберите nofollow из robots на главной, если ссылки в меню должны учитываться.",
            urls=[home.url],
            penalty=6,
        )

    server_errors = [page.url for page in pages if page.status >= 500 and page.url != home_url]
    if server_errors:
        add(
            "server-error",
            "critical",
            "Доступность",
            "Страницы отвечают ошибкой сервера",
            f"{len(server_errors)} адресов отдают код 5xx. Робот не получает документ.",
            "Откройте эти адреса и почините ошибку приложения или сервера.",
            urls=server_errors,
            penalty=min(24, 8 * len(server_errors)),
        )

    broken = [page.url for page in pages if 400 <= page.status < 500 and page.url != home_url]
    if broken:
        add(
            "broken-internal",
            "warning",
            "Ссылки",
            "Внутренние страницы не открываются",
            f"{len(broken)} адресов из обхода ответили кодом 4xx. Ссылки на них ведут в тупик.",
            "Почините адрес или уберите ссылку. Для удалённых страниц оставьте ответ 404, а не мягкую заглушку.",
            urls=broken,
            penalty=min(18, 5 * len(broken)),
        )

    failed = [page.url for page in pages if page.status == 0 and page.url != home_url]
    if failed:
        add(
            "fetch-error",
            "warning",
            "Доступность",
            "Часть адресов не ответила",
            "Запрос оборвался по таймауту или ошибке сети.",
            "Проверьте, что адрес стабильно открывается и не режет незнакомый User-Agent.",
            urls=failed,
            penalty=min(10, 4 * len(failed)),
        )

    if isinstance(data.http_probe, dict):
        status = data.http_probe.get("status") or 0
        location = (data.http_probe.get("location") or "").strip().lower()
        if status == 200 or (status in REDIRECTS and not location.startswith("https://")):
            add(
                "http-not-redirect",
                "warning",
                "Безопасность",
                "HTTP-версия не уводит на HTTPS",
                "Обычный HTTP открывается сам или уходит не на HTTPS. Получаются две версии сайта.",
                "С http:// поставьте один 301 на ту же страницу с https://.",
                urls=[data.http_probe.get("url") or ""],
                penalty=8,
            )

    if home and urlsplit(home.url).scheme == "https" and not home.hsts:
        add(
            "no-hsts",
            "notice",
            "Безопасность",
            "Нет заголовка HSTS",
            "Без Strict-Transport-Security браузер может снова спросить сайт по HTTP.",
            "Добавьте заголовок Strict-Transport-Security на HTTPS-ответы.",
            urls=[home.url],
            penalty=2,
        )

    if not data.robots_found and not (home and home.status == 0):
        add(
            "robots-missing",
            "warning",
            "Индексация",
            "Нет файла robots.txt",
            f"Запрос файла завершился кодом {data.robots_status or 'ошибки'}. Робот не видит правил и карты сайта.",
            "Положите robots.txt в корень: откройте нужные разделы и укажите строку Sitemap.",
            penalty=5,
        )

    if not data.blocks_all and data.sitemap_sources and not data.sitemap_urls:
        add(
            "sitemap-broken",
            "warning",
            "Карта сайта",
            "Карта сайта не читается",
            "Адрес sitemap указан или лежит на стандартном пути, но адресов страниц из него получить не удалось.",
            "Откройте sitemap в браузере: это должен быть XML со списком <loc>, без авторизации и HTML-заглушки.",
            urls=data.sitemap_sources,
            penalty=8,
        )
    elif not data.blocks_all and data.sitemap_files:
        image_errors = sum(int(item.get("errors") or 0) for item in data.sitemap_files)
        if image_errors:
            add(
                "sitemap-images",
                "warning",
                "Карта сайта",
                "В карте сайта указаны адреса картинок",
                f"{image_errors} записей ведут на файл изображения, а не на HTML-страницу. "
                "Поисковая карта из-за этого раздувается, а часть адресов не является страницами.",
                "Оставьте в sitemap только адреса страниц. Картинки поисковик забирает с самой страницы.",
                penalty=8,
            )
    elif not data.blocks_all and not data.sitemap_urls:
        add(
            "no-sitemap",
            "warning",
            "Карта сайта",
            "Не найдена карта сайта",
            "В robots.txt нет Sitemap, и по адресам /sitemap.xml и /sitemap_index.xml карты тоже нет.",
            "Соберите XML-карту из индексируемых страниц и добавьте её адрес в robots.txt.",
            penalty=6,
        )

    bad_sitemap = []
    for url in data.sitemap_urls:
        target = url
        page = data.pages.get(url)
        if page is None:
            page = data.pages.get(target)
        if page:
            if page.status != 200:
                bad_sitemap.append(f"{url} → {page.status}")
            continue
        extra = data.extra_status.get(url)
        if extra and not extra.get("unvisited") and extra.get("status") not in (200, *REDIRECTS):
            bad_sitemap.append(f"{url} → {extra.get('status') or extra.get('error') or 'ошибка'}")
    if bad_sitemap:
        add(
            "sitemap-errors",
            "warning",
            "Карта сайта",
            "В карте сайта есть адреса без кода 200",
            "Поисковик тратит обход на адреса, которые не отдают страницу.",
            "Оставьте в sitemap только конечные адреса с кодом 200.",
            examples=bad_sitemap,
            urls=[item.split(" → ")[0] for item in bad_sitemap],
            penalty=min(12, 4 * len(bad_sitemap)),
        )

    if data.sitemap_urls and not data.partial:
        orphans = []
        for url, extra in data.extra_status.items():
            if extra.get("status") != 200:
                continue
            final = extra.get("final") or url
            if url == home_url or final == home_url:
                continue
            if url in data.linked or final in data.linked:
                continue
            if final in data.pages and (final in data.linked or data.pages[final].inlinks):
                continue
            orphans.append(url)
        known = set(data.sitemap_urls)
        for page in pages:
            if page.url in known and page.status == 200 and page.inlinks == 0 and page.url != home_url and page.depth > 0:
                orphans.append(page.url)
        orphans = list(dict.fromkeys(orphans))
        if orphans:
            add(
                "sitemap-orphans",
                "notice",
                "Карта сайта",
                "В карте есть страницы без внутренних ссылок",
                "Адрес есть в sitemap, открывается, но в обойдённых страницах на него никто не ссылается.",
                "Поставьте ссылку из раздела или меню. Сироту из карты лучше убрать, если страница больше не нужна.",
                urls=orphans,
                penalty=3,
            )

    if data.sitemap_urls:
        known = set(data.sitemap_urls)
        missing = [page.url for page in indexable if page.url not in known]
        if missing:
            add(
                "not-in-sitemap",
                "notice",
                "Карта сайта",
                "Индексируемые страницы не попали в карту",
                f"{len(missing)} страниц можно индексировать, но их нет в найденной карте.",
                "Добавляйте в sitemap конечный адрес каждой публичной страницы.",
                urls=missing,
                penalty=min(6, 2 + len(missing) // 4),
            )

    long_chains = [event for event in data.redirect_events if event.get("hops", 0) >= 2]
    if long_chains:
        add(
            "redirect-chains",
            "warning",
            "Ссылки",
            "Длинные цепочки редиректов",
            "До страницы два и больше прыжков. Обход замедляется, а вес ссылки теряется.",
            "Оставьте один 301 сразу на конечный адрес.",
            examples=[_chain_text(event) for event in long_chains],
            urls=[event["from"] for event in long_chains],
            penalty=min(10, 4 * len(long_chains)),
        )
    single_hops = [event for event in data.redirect_events if event.get("hops", 0) == 1]
    if single_hops:
        add(
            "redirect-links",
            "notice",
            "Ссылки",
            "Внутренние ссылки ведут на редирект",
            "Ссылка открывается, но сначала уходит на другой адрес. В меню и текстах лучше ставить конечный URL.",
            "Замените ссылки на адрес после редиректа.",
            examples=[_chain_text(event) for event in single_hops],
            urls=[event["from"] for event in single_hops],
            penalty=3,
        )

    missing_title = [page.url for page in indexable if not page.title]
    if missing_title:
        add(
            "missing-title",
            "critical",
            "Мета-теги",
            "Нет тега title",
            "Без title поисковик собирает заголовок сниппета сам.",
            "На каждой индексируемой странице нужен свой title, обычно 30–65 символов, с темой страницы в начале.",
            urls=missing_title,
            penalty=min(18, 6 * len(missing_title)),
        )
    odd_title = [
        page.url
        for page in indexable
        if page.title and (len(page.title) < 30 or len(page.title) > 65)
    ]
    if odd_title:
        add(
            "title-length",
            "notice",
            "Мета-теги",
            "Неудачная длина title",
            _count(len(odd_title), "заголовок", "заголовка", "заголовков")
            + " короче 30 или длиннее 65 символов. Короткие не объясняют страницу, длинные обрезаются в выдаче.",
            "Уложите title в 30–65 символов и не повторяйте один шаблон на весь сайт.",
            urls=odd_title,
            penalty=2,
        )
    titles: dict[str, list[str]] = defaultdict(list)
    for page in indexable:
        if page.title:
            titles[page.title.casefold()].append(page.url)
    dup_titles = [urls for urls in titles.values() if len(urls) > 1]
    if dup_titles:
        flat = [url for group in dup_titles for url in group]
        add(
            "duplicate-title",
            "warning",
            "Мета-теги",
            "Одинаковые title",
            f"{len(dup_titles)} групп страниц делят один title. В выдаче они выглядят как дубли.",
            "Сделайте title уникальным и понятным по тексту страницы.",
            urls=flat,
            penalty=min(10, 4 * len(dup_titles)),
        )

    missing_desc = [page.url for page in indexable if not page.description]
    if missing_desc:
        add(
            "missing-description",
            "warning",
            "Мета-теги",
            "Нет meta description",
            "Описание сниппета поисковик напишет сам, часто неудачным куском текста.",
            "Добавьте уникальное описание примерно на 70–170 символов.",
            urls=missing_desc,
            penalty=min(10, 2 * len(missing_desc)),
        )
    odd_desc = [
        page.url
        for page in indexable
        if page.description and (len(page.description) < 70 or len(page.description) > 170)
    ]
    if odd_desc:
        add(
            "description-length",
            "notice",
            "Мета-теги",
            "Неудачная длина description",
            "Слишком короткое описание не продаёт переход, слишком длинное обрезается.",
            "Держите description в диапазоне 70–170 символов.",
            urls=odd_desc,
            penalty=2,
        )
    descriptions: dict[str, list[str]] = defaultdict(list)
    for page in indexable:
        if len(page.description) >= 20:
            descriptions[page.description.casefold()].append(page.url)
    dup_desc = [urls for urls in descriptions.values() if len(urls) > 1]
    if dup_desc:
        flat = [url for group in dup_desc for url in group]
        add(
            "duplicate-description",
            "warning",
            "Мета-теги",
            "Одинаковые description",
            "Несколько страниц отдают одно и то же описание.",
            "Напишите описание под конкретную страницу.",
            urls=flat,
            penalty=min(8, 3 * len(dup_desc)),
        )

    missing_h1 = [page.url for page in indexable if not page.h1]
    if missing_h1:
        add(
            "missing-h1",
            "warning",
            "Заголовки",
            "Нет заголовка H1",
            "У страницы нет главного заголовка. Структура документа для робота хуже.",
            "Оставьте один H1 — название страницы, видимое на экране.",
            urls=missing_h1,
            penalty=min(8, 2 * len(missing_h1)),
        )
    multi_h1 = [page.url for page in ok_pages if len(page.h1) > 1]
    if multi_h1:
        add(
            "multiple-h1",
            "warning",
            "Заголовки",
            "Несколько H1 на странице",
            "Два и больше H1 размывают тему документа.",
            "Оставьте один H1, остальные уровни опустите до H2–H3.",
            urls=multi_h1,
            penalty=min(6, 2 * len(multi_h1)),
        )
    h1s: dict[str, list[str]] = defaultdict(list)
    for page in indexable:
        if page.h1:
            h1s[page.h1[0].casefold()].append(page.url)
    dup_h1 = [urls for urls in h1s.values() if len(urls) >= 3]
    if dup_h1:
        flat = [url for group in dup_h1 for url in group]
        add(
            "duplicate-h1",
            "notice",
            "Заголовки",
            "Один и тот же H1 на многих страницах",
            "Три и больше страниц начинаются с одинакового H1. Похоже на общий шаблон.",
            "Поставьте в H1 название именно этой страницы.",
            urls=flat,
            penalty=2,
        )

    thin = []
    scripted = []
    for page in indexable:
        if page.words < 20 and page.script_count >= 5:
            scripted.append(page.url)
        elif page.words < 120:
            thin.append(page.url)
    if scripted:
        add(
            "js-rendered",
            "warning",
            "Контент",
            "Текст, похоже, собирается только в браузере",
            "В ответе сервера почти нет слов, зато много скриптов. Этот аудит, как и робот без рендера, видит пустую оболочку.",
            "Отдавайте готовый HTML с сервера или пререндером. Иначе в поиске окажется пустая страница.",
            urls=scripted,
            penalty=8,
        )
    if thin:
        add(
            "thin-content",
            "notice",
            "Контент",
            "Мало текста на странице",
            _count(len(thin), "индексируемая страница", "индексируемые страницы", "индексируемых страниц")
            + " короче 120 слов.",
            "Добавьте полезный текст или закройте пустые страницы от индекса, если это фильтры и заглушки.",
            urls=thin,
            penalty=3,
        )
    hashes: dict[str, list[str]] = defaultdict(list)
    for page in indexable:
        if page.text_hash:
            hashes[page.text_hash].append(page.url)
    dup_body = [urls for urls in hashes.values() if len(urls) > 1]
    if dup_body:
        flat = [url for group in dup_body for url in group]
        add(
            "duplicate-content",
            "warning",
            "Контент",
            "Одинаковый текст на разных адресах",
            "Несколько индексируемых страниц совпали по видимому тексту.",
            "Оставьте одну каноническую страницу, на остальных поставьте canonical или уберите дубль.",
            urls=flat,
            penalty=min(10, 4 * len(dup_body)),
        )

    missing_canon = [page.url for page in indexable if not page.canonical]
    if missing_canon:
        add(
            "missing-canonical",
            "notice",
            "Индексация",
            "Не указан canonical",
            "Без canonical поисковик сам выбирает основной адрес, если страница доступна в нескольких вариантах.",
            "Поставьте rel=canonical на конечный адрес этой же страницы.",
            urls=missing_canon,
            penalty=2,
        )
    bad_canon = []
    for page in ok_pages:
        if not page.canonical:
            continue
        canon = normalize(page.canonical)
        if urlsplit(canon).scheme in ("http", "https") and canon != page.url:
            bad_canon.append(page.url)
    if bad_canon:
        add(
            "bad-canonical",
            "warning",
            "Индексация",
            "Canonical ведёт на другой адрес",
            "Страница просит индексировать не себя. Так бывает у дублей, пагинации и ошибочного шаблона.",
            "На уникальной странице canonical должен указывать на неё саму. Чужой canonical — только если это сознательный дубль.",
            urls=bad_canon,
            penalty=min(8, 3 * len(bad_canon)),
        )

    noindex = [
        page.url
        for page in ok_pages
        if page.url != home_url and "noindex" in page.index_reason
    ]
    if noindex:
        add(
            "noindex",
            "notice",
            "Индексация",
            "Страницы закрыты от индекса",
            "На этих адресах стоит noindex. Если так и задумано для служебных страниц — это нормально.",
            "Проверьте, что закрыты только те страницы, которых не должно быть в поиске.",
            urls=noindex,
            penalty=2,
        )

    no_viewport = [page.url for page in indexable if not page.viewport]
    if no_viewport:
        add(
            "no-viewport",
            "warning",
            "Структура",
            "Нет viewport",
            "Без meta viewport телефон показывает страницу как узкий рабочий стол.",
            "Добавьте meta name=viewport с content=\"width=device-width, initial-scale=1\".",
            urls=no_viewport,
            penalty=min(8, 3 * len(no_viewport)),
        )
    no_lang = [page.url for page in indexable if not page.lang]
    if no_lang:
        add(
            "no-lang",
            "notice",
            "Структура",
            "У страницы не задан язык",
            "В теге html нет атрибута lang. Роботу и скринридеру сложнее понять язык.",
            "Поставьте <html lang=\"ru\"> или язык конкретной страницы.",
            urls=no_lang,
            penalty=2,
        )
    deep = [page.url for page in ok_pages if page.depth >= 4]
    if deep:
        add(
            "deep-pages",
            "notice",
            "Структура",
            "Страницы глубоко в структуре",
            "До этих адресов четыре клика и больше от старта обхода. Важные страницы так находят хуже.",
            "Дайте им ссылку из раздела ближе к главной.",
            urls=deep,
            penalty=2,
        )
    huge = [page.url for page in ok_pages if page.truncated or page.size > 1_000_000]
    if huge:
        add(
            "huge-html",
            "notice",
            "Скорость",
            "Слишком тяжёлый HTML",
            "Документ больше мегабайта или обрезан при чтении. Страница долго приезжает в браузер.",
            "Уберите из HTML встроенные данные, которых там не должно быть, и отдавайте документ компактнее.",
            urls=huge,
            penalty=2,
        )

    missing_alt_pages = [page.url for page in ok_pages if page.images_missing_alt]
    missing_alt_total = sum(page.images_missing_alt for page in ok_pages)
    if missing_alt_total:
        add(
            "images-alt",
            "warning",
            "Изображения",
            "Изображения без alt",
            f"{missing_alt_total} картинок без альтернативного текста на {len(missing_alt_pages)} страницах. Поиск картинок и скринридер их не понимают.",
            "Подпишите содержание в alt. Чисто декоративным картинкам можно оставить пустой alt=\"\".",
            urls=missing_alt_pages,
            penalty=min(8, 2 + missing_alt_total // 10),
        )

    slow = [page.url for page in ok_pages if page.time_ms >= 2500]
    if slow:
        add(
            "slow-pages",
            "warning",
            "Скорость",
            "Страницы отвечают дольше 2,5 секунд",
            "Долгий ответ сервера режет и людей, и обход. Это время до HTML, без отрисовки в браузере.",
            "Найдите тяжёлые запросы к базе и лишние ожидания на сервере. Цель — ответ заметно быстрее двух секунд.",
            urls=slow,
            penalty=min(10, 3 * len(slow)),
        )

    if ok_pages and sum(page.jsonld_ok for page in ok_pages) == 0:
        add(
            "no-structured-data",
            "notice",
            "Разметка",
            "Нет микроразметки JSON-LD",
            "В обойдённых страницах нет годного блока schema.org. Расширенные сниппеты так не получить.",
            "Добавьте JSON-LD под тип страницы: Organization или WebSite на главной, Article, Product или FAQ где уместно.",
            penalty=3,
        )
    bad_json = [page.url for page in ok_pages if page.jsonld_bad]
    if bad_json:
        add(
            "bad-structured-data",
            "warning",
            "Разметка",
            "JSON-LD не разбирается",
            "Блок application/ld+json есть, но это невалидный JSON. Робот его выбрасывает.",
            "Проверьте запятые и кавычки. В разметке не должно быть HTML-комментариев.",
            urls=bad_json,
            penalty=6,
        )
    no_og = [page.url for page in indexable if not page.og_title or not page.og_description]
    if no_og:
        add(
            "no-open-graph",
            "notice",
            "Разметка",
            "Нет Open Graph",
            "Без og:title и og:description ссылка в мессенджерах и соцсетях собирается наугад.",
            "Добавьте og:title и og:description, для витрины ещё og:image.",
            urls=no_og,
            penalty=2,
        )
    mixed = [page.url for page in ok_pages if page.mixed]
    if mixed:
        add(
            "mixed-content",
            "warning",
            "Безопасность",
            "На HTTPS-странице есть ресурсы по HTTP",
            "Картинки, скрипты или стили грузятся по небезопасному http://. Браузер может их заблокировать.",
            "Переведите адреса ресурсов на https://.",
            urls=mixed,
            penalty=min(8, 3 * len(mixed)),
        )
    refresh = [page.url for page in ok_pages if page.meta_refresh]
    if refresh:
        add(
            "meta-refresh",
            "warning",
            "Ссылки",
            "Редирект через meta refresh",
            "Страница перекидывает человека тегом refresh. Роботы обрабатывают это хуже обычного 301.",
            "Замените meta refresh на серверный редирект 301.",
            urls=refresh,
            penalty=6,
        )

    bad_lang = []
    for page in ok_pages:
        if not page.hreflang:
            continue
        if not any(normalize(item.get("href") or "") == page.url for item in page.hreflang):
            bad_lang.append(page.url)
    if bad_lang:
        add(
            "hreflang-self",
            "warning",
            "Индексация",
            "В hreflang нет ссылки на саму страницу",
            "Альтернативные языковые адреса указаны, но среди них нет этого же URL.",
            "Добавьте hreflang текущей страницы на неё саму и взаимные ссылки между языками.",
            urls=bad_lang,
            penalty=4,
        )

    if data.blocked_links:
        add(
            "robots-blocked-links",
            "notice",
            "Индексация",
            "Ссылки ведут в закрытые разделы robots.txt",
            "Со страниц есть ссылки на адреса, которые robots.txt запрещает обходить.",
            "Если раздел служебный, не ссылайтесь на него из публичного меню. Если он нужен в поиске — откройте его в robots.txt.",
            urls=data.blocked_links,
            penalty=2,
        )

    probe = data.not_found_probe or {}
    code = probe.get("status")
    if code == 200:
        add(
            "soft-404",
            "warning",
            "Доступность",
            "Несуществующий адрес отвечает 200",
            "Вместо 404 сервер отдал обычную страницу. В поиск попадают тупики.",
            "Для несуществующих адресов отдавайте настоящий код 404 и короткую страницу «не найдено».",
            urls=[probe.get("url") or ""],
            penalty=10,
        )
    elif code in REDIRECTS:
        add(
            "soft-404",
            "warning",
            "Доступность",
            "Несуществующий адрес уходит в редирект",
            "Выдуманный URL не отвечает 404, а перенаправляет. Часто так прячут ошибки и плодят мягкие 404.",
            "Отдавайте 404. Редирект уместен только со старого реального адреса на новый.",
            urls=[probe.get("url") or ""],
            penalty=8,
        )
    elif code not in (None, 404, 410) and home and home.status == 200:
        add(
            "bad-404",
            "notice" if code == 0 else "warning",
            "Доступность",
            "Несуществующий адрес отвечает необычным кодом",
            f"Проверочный адрес вернул {code or 'ошибку сети'} вместо 404.",
            "Настройте для неизвестных URL ответ 404.",
            urls=[probe.get("url") or ""],
            penalty=2 if code == 0 else 6,
        )

    return _pack(data, issues, pages, home)


def _robots_tokens(page: Page) -> set[str]:
    return {item for item in re.split(r"[\s,;]+", f"{page.meta_robots} {page.x_robots}".lower()) if item}


def _label(score: int) -> str:
    if score >= 85:
        return "Технически крепко"
    if score >= 65:
        return "Есть заметные дыры"
    if score >= 45:
        return "Много технических ошибок"
    return "Сайт плохо готов к обходу"


def _pack(data: CrawlData, issues: list[dict], pages: list[Page], home: Page | None) -> dict:
    issues.sort(key=lambda item: (_SEVERITY.get(item["severity"], 9), -item["penalty"], item["title"]))
    penalty = sum(item["penalty"] for item in issues)
    score = max(0, min(100, 100 - penalty))
    times = [page.time_ms for page in pages if page.time_ms]
    ordered = sorted(pages, key=lambda page: (0 if page.status == 0 or page.status >= 400 else 1, page.url))
    limitations = [
        "Смотрим HTML, который отдал сервер. JavaScript в браузере не исполняется.",
        "Обход только этого домена, включая www и вариант без www. Другие поддомены не идём.",
        f"Лимит страниц: {data.max_pages}. Фильтры и пагинация берутся без отдельного потолка. Глубина ссылок: {data.max_depth}.",
        "Оценка — 100 минус штрафы замечаний. Это техническое состояние обхода, не позиция в поиске.",
    ]
    if data.partial:
        limitations.insert(
            0,
            "Обход открыл не все страницы каталога. Адреса из sitemap, которые не открывались, лежат в листе «Нет на сайте» с кодом «—»: это не подтверждённый 404.",
        )
    return {
        "summary": {
            "score": score,
            "label": _label(score),
            "start_url": data.start_url,
            "final_url": data.final_url,
            "host": data.site_key,
            "pages": len(pages),
            "indexable": sum(1 for page in pages if page.indexable),
            "critical": sum(1 for item in issues if item["severity"] == "critical"),
            "warnings": sum(1 for item in issues if item["severity"] == "warning"),
            "notices": sum(1 for item in issues if item["severity"] == "notice"),
            "avg_time_ms": int(sum(times) / len(times)) if times else 0,
            "elapsed_ms": data.elapsed_ms,
            "partial": data.partial,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "robots_found": data.robots_found,
            "robots_status": data.robots_status,
            "sitemap_count": len(data.sitemap_urls),
            "limitations": limitations,
        },
        "top_fixes": [
            {"title": item["title"], "fix": item["fix"], "severity": item["severity"], "penalty": item["penalty"]}
            for item in sorted(issues, key=lambda item: -item["penalty"])
            if item["penalty"] > 0
        ][:5],
        "issues": issues,
        "pages": [_page_public(page) for page in ordered],
    }
