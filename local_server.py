"""Dependency-free launcher for the local SQLite workspace."""
import argparse
import json
import mimetypes
import os
import threading
import webbrowser
import time
import ingress
import reporting
from service import authorize, change, FieldError, Unavailable, frame, access_required
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from store import initialize

ROOT = Path(__file__).resolve().parent
ASSETS = {"/": "index.html", "/style.css": "style.css", "/app.js": "app.js"}

class Handler(BaseHTTPRequestHandler):
    def identity(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("scarline_access")
        return authorize(self.headers.get("Authorization") or "Bearer " + (value.value if value else ""))

    def reply(self, status, data, media="application/json", attachment=False):
        content = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control","no-store")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="scarline-audit.json"')
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        if (path.startswith("/api/") and path != "/api/health") or path == "/audit":
            try:
                self.identity()
            except PermissionError as exc:
                return self.reply(403, {"detail":str(exc)})
        if path in ASSETS:
            f = ROOT / "web" / ASSETS[path]
            return self.reply(200, f.read_bytes(), mimetypes.guess_type(f)[0] or "application/octet-stream")
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            previous = self.headers.get("Last-Event-ID")
            try:
                while True:
                    current = reporting.revision()
                    message = frame(current) if current != previous else ": heartbeat\n\n"
                    self.wfile.write(message.encode())
                    self.wfile.flush()
                    previous = current
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                return
        if path == "/api/health":
            return self.reply(200, {"status":"ok"})
        if path == "/api/workspace":
            return self.reply(200, {**reporting.workspace(), "operator_required": access_required()})
        if path == "/audit":
            return self.reply(200, reporting.report().encode(), "text/html; charset=utf-8")
        if path == "/api/audit":
            return self.reply(200, reporting.audit(), attachment=True)
        return self.reply(404, {"detail":"Unknown route"})

    def do_POST(self):
        try:
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"detail":"Expected JSON."})
            size = int(self.headers.get("Content-Length","0"))
            if not 0 <= size <= 65536:
                return self.reply(413, {"detail":"Request too large."})
            parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
            raw = self.rfile.read(size)
            if len(parts) == 3 and parts[:2] == ["api", "webhooks"]:
                return self.reply(202, ingress.receive(parts[2], raw, self.headers))
            if parts == ["api", "session"]:
                data = json.loads(raw)
                token = data.get("token") if isinstance(data,dict) else None
                if not isinstance(token,str) or not 1 <= len(token) <= 1024:
                    raise FieldError("Enter an access token.")
                identity = authorize("Bearer " + token)
                cookie = SimpleCookie()
                cookie["scarline_access"] = token
                cookie["scarline_access"]["httponly"] = True
                cookie["scarline_access"]["samesite"] = "Strict"
                cookie["scarline_access"]["path"] = "/"
                cookie["scarline_access"]["max-age"] = 28800
                content = json.dumps({"role":(identity or {}).get("role","operator")}).encode()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(content)))
                self.send_header("Set-Cookie",cookie.output(header="").strip())
                self.end_headers()
                self.wfile.write(content)
                return
            identity = self.identity()
            status, result = change(parts, json.loads(raw or b"{}"), identity)
            self.reply(status, result)
        except KeyError as exc:
            self.reply(404, {"detail":exc.args[0]})
        except PermissionError as exc:
            self.reply(403, {"detail":str(exc)})
        except Unavailable as exc:
            self.reply(503, {"detail":str(exc)})
        except FieldError as exc:
            self.reply(422, {"detail":str(exc)})
        except ValueError as exc:
            self.reply(400, {"detail":str(exc)})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)
    initialize()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno not in (48, 98, 10048):
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Scarline is ready at {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        stop = ingress.start_worker()
        try:
            server.serve_forever()
        finally:
            stop.set()

if __name__ == "__main__":
    main()
