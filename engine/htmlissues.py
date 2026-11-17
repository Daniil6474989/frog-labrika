"""Грубые ошибки HTML, которые в таблице коллеги занимают сводку."""
from __future__ import annotations

import re

_WATCH_TAG = re.compile(r"</?(div|span|main|form|section|ul|ol|table)\b[^>]*?/?>", re.I)


def _place(raw: bytes, at: int) -> str:
    chunk = raw[: max(0, at)]
    line = chunk.count(b"\n") + 1
    last = chunk.rfind(b"\n")
    column = at - last
    return f"На строке {line}, столбец {column}"


def inspect_html(raw: bytes) -> list[dict]:
    issues: list[dict] = []
    lowered = raw.lower()
    meta_charset = re.search(br"<meta[^>]*charset", lowered)
    charset_at = meta_charset.start() if meta_charset else -1
    if charset_at >= 1024:
        issues.append(
            {
                "detail": 'Атрибут "charset" для meta element должен быть в первых 1024 байт документа.',
                "where": _place(raw, charset_at),
            }
        )
    elif charset_at < 0 and b"<meta" in lowered[:8000]:
        issues.append({"detail": "В документе нет charset.", "where": "head"})

    text = raw.decode("utf-8", "replace")
    low = text.lower()
    if text.count("<!--") != text.count("-->"):
        comment_at = low.find("<!--")
        issues.append(
            {
                "detail": "HTML-комментарий открыт и не закрыт.",
                "where": _place(raw, comment_at) if comment_at >= 0 else "<!-- -->",
            }
        )
    if ".swf" in low or "application/x-silverlight" in low or "<applet" in low:
        issues.append({"detail": "На странице остался устаревший плагин Flash, Silverlight или applet.", "where": "body"})

    stack: list[str] = []
    mismatches = 0
    for match in _WATCH_TAG.finditer(text):
        chunk = match.group(0)
        name = (match.group(1) or "").lower()
        if chunk.endswith("/>"):
            continue
        if chunk.startswith("</"):
            if stack and stack[-1] == name:
                stack.pop()
            elif stack:
                mismatches += 1
                if mismatches <= 4:
                    issues.append(
                        {
                            "detail": f"Закрывающий тег {name} стоит не на своём месте.",
                            "where": _place(raw, match.start()),
                        }
                    )
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
        else:
            stack.append(name)
    return issues
