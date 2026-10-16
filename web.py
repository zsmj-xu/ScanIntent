"""Local, read-only web viewer. Collection is a separate, one-shot CLI command."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from reconstruction import dialogue_messages


def conversation_page(row, page, internal=False):
    records = row["messages"] if internal else dialogue_messages(row["messages"])
    pages = max(1, (len(records) + 39) // 40)
    page = min(max(1, int(page)), pages)
    result = {k: v for k, v in row.items() if k not in ("messages", "observation_ids")}
    result.update(records=records[(page - 1) * 40:page * 40], record_total=len(records),
                  message_page=page, observation_id_count=len(row.get("observation_ids", [])))
    return result


def serve(store, project, port, *, host="127.0.0.1"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # URLs and content are deliberately excluded from access logs.

        def send(self, status, body, mime="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode())

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            try:
                if parsed.path in assets:
                    file, mime = assets[parsed.path]
                    self.send(200, (Path(__file__).parent / "static" / file).read_bytes(), mime)
                elif parsed.path == "/api/conversations":
                    if "page" in params:
                        result = store.browse(project, alias=params.get("alias", ""),
                            query=params.get("q", ""), start=params.get("start"), end=params.get("end"),
                            warning=params.get("warning") == "1", order=params.get("order", "newest"),
                            page=params["page"], size=params.get("size", 20))
                        self.send(200, dict(project_id=project, **result))
                    else:
                        rows = store.list(project, alias=params.get("alias", ""),
                                          before_ms=params.get("before"), before_trace=params.get("trace", ""))
                        self.send(200, dict(project_id=project, conversations=rows))
                elif parsed.path == "/api/aliases":
                    self.send(200, store.aliases(project))
                elif parsed.path == "/api/conversation":
                    row = store.get(project, params.get("trace", ""))
                    if row:
                        if "message_page" in params:
                            row = conversation_page(row, params["message_page"], params.get("view") == "all")
                        else:
                            row["dialogue_messages"] = dialogue_messages(row["messages"])
                    self.send(200 if row else 404, row or {"error": "未找到会话"})
                elif parsed.path == "/api/runs":
                    self.send(200, store.runs(project))
                else:
                    self.send(404, {"error": "页面不存在"})
            except (ValueError, RuntimeError, OSError):
                self.send(503, {"error": "读取失败，请检查本地数据库后重试"})

    server = ThreadingHTTPServer((host, port), Handler)
    print("ScanIntent: http://" + host + ":" + str(port) + " (read-only viewer; does not start collection)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
