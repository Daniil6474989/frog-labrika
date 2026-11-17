"""Локальный сайт технического аудита. Запуск: py -3 server.py"""
from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from engine.crawl import crawl
from engine.deliver import write_files
from engine.report import build_report
from engine.urls import UnsafeUrl, prepare_start_url

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
OUTPUT = (ROOT / "output").resolve()
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _trim_jobs() -> None:
    finished = [job for job in jobs.values() if job["status"] != "running"]
    finished.sort(key=lambda job: job["created"])
    while len(finished) > 12:
        jobs.pop(finished.pop(0)["id"], None)


def _public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "error": job["error"],
        "report": job["report"] if job["status"] == "done" else None,
    }


def clamp_limits(max_pages: int, max_depth: int) -> tuple[int, int, int]:
    pages = min(500_000, max(5, int(max_pages)))
    depth = min(8, max(1, int(max_depth)))
    deadline = min(24 * 3600, 600 + pages * 2)
    return pages, depth, deadline


def start_audit(url: str, max_pages: int, max_depth: int, respect_robots: bool) -> str:
    prepared = prepare_start_url(url)
    max_pages, max_depth, deadline = clamp_limits(max_pages, max_depth)
    with jobs_lock:
        running = sum(1 for job in jobs.values() if job["status"] == "running")
        if running >= 2:
            raise RuntimeError("Уже идут два аудита. Подождите, пока один закончится.")
        job_id = secrets.token_hex(8)
        jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "created": time.time(),
            "error": None,
            "report": None,
            "progress": {
                "phase": "Открываю стартовую страницу",
                "pages": 0,
                "limit": max_pages,
                "queued": 0,
                "current": prepared,
                "log": [],
            },
        }
        _trim_jobs()

    def run() -> None:
        def on_progress(event: dict) -> None:
            with jobs_lock:
                current = jobs.get(job_id)
                if current and current["status"] == "running":
                    current["progress"] = event

        try:
            data = crawl(
                prepared,
                max_pages=max_pages,
                max_depth=max_depth,
                respect_robots=respect_robots,
                allow_private=False,
                timeout=20,
                deadline_s=deadline,
                workers=8,
                on_progress=on_progress,
            )
            report = build_report(data)
            report["summary"]["downloads"] = write_files(data, OUTPUT)
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["report"] = report
            summary = report["summary"]
            print(
                f"Аудит {prepared}: оценка {summary['score']}, "
                f"замечаний {len(report['issues'])}, страниц {summary['pages']}"
            )
        except Exception as exc:
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"] = "Аудит оборвался. Попробуйте ещё раз или возьмите меньше страниц."
            print(f"Аудит {prepared} оборвался: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str, content_type: str) -> None:
        file_path = (STATIC_DIR / name).resolve()
        if not file_path.is_relative_to(STATIC_DIR) or not file_path.is_file():
            self.send_error(404)
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str) -> None:
        file_path = (OUTPUT / name).resolve()
        if not file_path.is_relative_to(OUTPUT) or not file_path.is_file():
            self.send_error(404)
            return
        kind = {
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(file_path.suffix.lower(), "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(file_path.name)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/audits/"):
            job_id = path.removeprefix("/api/audits/").strip("/")
            with jobs_lock:
                job = jobs.get(job_id)
                payload = _public_job(job) if job else None
            if not payload:
                self._json(404, {"error": "Такого аудита нет. Запустите проверку заново."})
                return
            self._json(200, payload)
            return
        if path.startswith("/files/"):
            self._file(path.removeprefix("/files/"))
            return
        if path in STATIC:
            name, content_type = STATIC[path]
            self._static(name, content_type)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/audits":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length > 20_000:
            self._json(413, {"error": "Слишком большой запрос"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "Не понял запрос"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "Не понял запрос"})
            return
        try:
            max_pages = int(payload.get("max_pages", 40))
        except (TypeError, ValueError):
            max_pages = 40
        try:
            max_depth = int(payload.get("max_depth", 4))
        except (TypeError, ValueError):
            max_depth = 4
        max_pages, max_depth, _deadline = clamp_limits(max_pages, max_depth)
        respect = payload.get("respect_robots", True)
        if not isinstance(respect, bool):
            respect = str(respect).lower() not in {"0", "false", "no"}
        try:
            job_id = start_audit(str(payload.get("url") or ""), max_pages, max_depth, respect)
        except UnsafeUrl as exc:
            self._json(400, {"error": str(exc)})
            return
        except RuntimeError as exc:
            self._json(429, {"error": str(exc)})
            return
        self._json(200, {"id": job_id})


STATIC_DIR = (ROOT / "static").resolve()


def serve(port: int = 8765) -> None:
    httpd = None
    bound = port
    for candidate in range(port, port + 6):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            bound = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("Не удалось занять порт 8765–8770")
    print(f"Технический аудит: http://127.0.0.1:{bound}", flush=True)
    print("Остановка: Ctrl+C", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    chosen = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    serve(chosen)
