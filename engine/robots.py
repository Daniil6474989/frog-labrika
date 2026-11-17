"""Разбор robots.txt: правила обхода и адреса карт сайта."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    pieces = ["^"]
    for char in body:
        pieces.append(".*" if char == "*" else re.escape(char))
    if anchored:
        pieces.append("$")
    return re.compile("".join(pieces))


def robots_path(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if parts.query:
        path += "?" + parts.query
    return path


class Robots:
    def __init__(self, rules: list[_Rule], sitemaps: list[str], crawl_delay: float):
        self.rules = rules
        self.sitemaps = sitemaps
        self.crawl_delay = crawl_delay

    @classmethod
    def parse(cls, text: str, user_agent: str) -> "Robots":
        lines: list[tuple[str, str]] = []
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            lines.append((key.strip().lower(), value.strip()))

        groups: list[dict] = []
        current: dict | None = None
        sitemaps: list[str] = []
        for key, value in lines:
            if key == "sitemap" and value:
                sitemaps.append(value)
                continue
            if key == "user-agent":
                if current is None or current["has_rules"]:
                    current = {"agents": [], "rules": [], "delay": 0.0, "has_rules": False}
                    groups.append(current)
                current["agents"].append(value.lower())
                continue
            if current is None:
                continue
            if key in ("allow", "disallow"):
                current["has_rules"] = True
                if value:
                    current["rules"].append(
                        _Rule(allow=key == "allow", pattern=value, regex=_compile(value))
                    )
            elif key == "crawl-delay":
                current["has_rules"] = True
                try:
                    current["delay"] = min(float(value.replace(",", ".")), 1.0)
                except ValueError:
                    pass

        agent = (user_agent or "").lower()
        specific = [
            group
            for group in groups
            if any(name != "*" and name in agent for name in group["agents"])
        ]
        chosen = specific or [group for group in groups if "*" in group["agents"]]
        rules: list[_Rule] = []
        delay = 0.0
        for group in chosen:
            rules.extend(group["rules"])
            if group["delay"]:
                delay = group["delay"]
        return cls(rules, sitemaps, delay)

    def allowed(self, url: str) -> bool:
        path = robots_path(url)
        best: tuple[int, int] | None = None
        for rule in self.rules:
            if rule.regex.match(path):
                candidate = (len(rule.pattern), 1 if rule.allow else 0)
                if best is None or candidate > best:
                    best = candidate
        if best is None:
            return True
        return bool(best[1])
