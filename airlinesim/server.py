"""
airlinesim GUI server — stdlib-only HTTP + Server-Sent-Events front end for
a GameSession. Serves the static webui/ PWA and a small JSON/SSE API.

No third-party dependencies: http.server for the transport, SSE (not
WebSockets) for the one-way live-state push — trivial to support in stdlib
and enough for a background sim pushing snapshots to a dashboard. Any
browser on any device on the LAN can reach it; no engine or game-logic code
lives here, this module only translates HTTP <-> GameSession.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from airlinesim.game import GameSession, new_game

WEBUI_DIR = Path(__file__).parent / "webui"
DEFAULT_SAVE_PATH = str(Path.home() / ".airlinesim_save.pkl")

COMMANDS = {
    "set_price": lambda gs, a: gs.set_price(a["route_op_id"], a["price"]),
    "set_frequency": lambda gs, a: gs.set_frequency(a["route_op_id"], a["freq"]),
    "set_layout": lambda gs, a: gs.set_layout(a["route_op_id"], a["seats"]),
    "acquire_aircraft": lambda gs, a: gs.acquire_aircraft(
        a["spec_id"], a["tail_number"], a["method"], a.get("base_iata")),
    "open_route": lambda gs, a: gs.open_route(
        a["route_spec_id"], a["tail_number"], a["price"], a.get("freq", 1), a.get("seats")),
    "hire_crew": lambda gs, a: gs.hire_crew(
        a["crew_type"], a["base_iata"], a["headcount"], a["cost_per_hour"],
        tuple(a.get("certs", ()))),
}


class Hub:
    """Owns the live GameSession and fans out its tick snapshots to every
    connected SSE client. Swapping sessions (new game / load) re-wires the
    callback and stops the old session's background thread."""

    def __init__(self, session: GameSession):
        self._lock = threading.Lock()
        self._clients: list = []
        self.session: GameSession = None
        self._set_session(session)

    def _set_session(self, session: GameSession):
        if self.session is not None:
            self.session.stop()
        self.session = session
        self.session.on_tick = self._broadcast

    def new_game(self, **kwargs):
        with self._lock:
            self._set_session(new_game(**kwargs))

    def load_game(self, path: str):
        with self._lock:
            self._set_session(GameSession.load(path))

    def add_client(self) -> "queue.Queue":
        q = queue.Queue(maxsize=2)
        with self._lock:
            self._clients.append(q)
        return q

    def remove_client(self, q: "queue.Queue"):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _broadcast(self, snapshot: dict):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            # drop the oldest pending snapshot rather than block the tick
            # loop or grow unbounded for a slow/gone client
            try:
                q.put_nowait(snapshot)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(snapshot)
                except queue.Full:
                    pass


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "airlinesim/0.1"

        def log_message(self, fmt, *args):
            pass  # keep stdout clean; comment out to debug requests

        # -- helpers ----------------------------------------------------
        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw) if raw else {}

        def _serve_static(self, path: str):
            if path == "/":
                path = "/index.html"
            full = (WEBUI_DIR / path.lstrip("/")).resolve()
            if WEBUI_DIR.resolve() not in full.parents:
                self.send_error(403)
                return
            if not full.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
            data = full.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- GET ----------------------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                self._send_json(hub.session.snapshot())
            elif path == "/api/catalog":
                self._send_json(hub.session.catalog())
            elif path == "/api/events":
                self._serve_sse()
            else:
                self._serve_static(path)

        def _serve_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.add_client()
            try:
                self._write_sse(hub.session.snapshot())  # so a new tab isn't blank
                while True:
                    try:
                        snap = q.get(timeout=15.0)
                        self._write_sse(snap)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.remove_client(q)

        def _write_sse(self, obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
            self.wfile.flush()

        # -- POST ---------------------------------------------------------
        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._read_json()
            except json.JSONDecodeError:
                self._send_json({"ok": False, "message": "invalid JSON"}, status=400)
                return

            if path == "/api/command":
                self._handle_command(body)
            elif path == "/api/control":
                self._handle_control(body)
            elif path == "/api/game/save":
                p = body.get("path") or DEFAULT_SAVE_PATH
                hub.session.save(p)
                self._send_json({"ok": True, "path": p})
            elif path == "/api/game/load":
                p = body.get("path") or DEFAULT_SAVE_PATH
                try:
                    hub.load_game(p)
                    self._send_json({"ok": True, "state": hub.session.snapshot()})
                except FileNotFoundError:
                    self._send_json({"ok": False, "message": "save file not found"}, status=404)
            elif path == "/api/game/new":
                kwargs = {k: v for k, v in body.items()
                         if k in ("human_name", "ai_name", "ai_step_frac")}
                hub.new_game(**kwargs)
                self._send_json({"ok": True, "state": hub.session.snapshot()})
            else:
                self.send_error(404)

        def _handle_command(self, body: dict):
            handler = COMMANDS.get(body.get("type"))
            if handler is None:
                self._send_json({"ok": False, "message": f"unknown command {body.get('type')}"},
                               status=400)
                return
            try:
                ok, message = handler(hub.session, body)
            except KeyError as e:
                self._send_json({"ok": False, "message": f"missing field {e}"}, status=400)
                return
            except (TypeError, ValueError) as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            self._send_json({"ok": ok, "message": message, "state": hub.session.snapshot()})

        def _handle_control(self, body: dict):
            gs = hub.session
            action = body.get("action")
            if action == "pause":
                gs.pause()
            elif action == "resume":
                gs.resume()
            elif action == "speed":
                gs.set_speed(body.get("value", gs.speed))
            elif action == "advance":
                gs.advance_days(body.get("days", 1))
            else:
                self._send_json({"ok": False, "message": f"unknown action {action}"}, status=400)
                return
            self._send_json({"ok": True, "state": gs.snapshot()})

    return Handler


def run_server(host: str = "0.0.0.0", port: int = 8765, session: GameSession = None):
    """Build and return (httpd, hub); caller owns calling serve_forever()."""
    session = session or new_game()
    hub = Hub(session)
    httpd = ThreadingHTTPServer((host, port), make_handler(hub))
    httpd.daemon_threads = True
    return httpd, hub


def lan_url(port: int) -> str:
    """Best-effort LAN-reachable URL so another device (phone/tablet) can connect."""
    ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
    except OSError:
        pass
    return f"http://{ip}:{port}"
