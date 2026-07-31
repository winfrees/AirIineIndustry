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
from urllib.parse import parse_qs, urlparse

from airlinesim.explorer import Mutation, ScenarioTree, linspace
from airlinesim.game import GameSession, new_game

WEBUI_DIR = Path(__file__).parent / "webui"
BASEMAP_PATH = Path(__file__).parent / "data" / "basemap.json"
_BASEMAP_CACHE: dict = {}


def _basemap_bytes():
    """Read the committed base map once. Absent is not an error the server
    should die on — the map degrades to routes and airports with no land."""
    if "bytes" not in _BASEMAP_CACHE:
        _BASEMAP_CACHE["bytes"] = (BASEMAP_PATH.read_bytes()
                                   if BASEMAP_PATH.is_file() else None)
    return _BASEMAP_CACHE["bytes"]
DEFAULT_SAVE_PATH = str(Path.home() / ".airlinesim_save.pkl")

COMMANDS = {
    "set_price": lambda gs, a: gs.set_price(a["route_op_id"], a["price"]),
    "set_frequency": lambda gs, a: gs.set_frequency(a["route_op_id"], a["freq"]),
    "set_layout": lambda gs, a: gs.set_layout(a["route_op_id"], a["seats"]),
    "set_cabin_price": lambda gs, a: gs.set_cabin_price(
        a["route_op_id"], a["cabin"], a.get("price")),
    # Every field the acquisition form sends has to be forwarded here: `seats`
    # used to be dropped on the floor, so a player could type a cabin at
    # acquisition, get "acquired", and receive an all-economy aircraft. The
    # only sign was the fleet row — the seat counts were never rejected, they
    # simply never left this function.
    "acquire_aircraft": lambda gs, a: gs.acquire_aircraft(
        a["spec_id"], a["tail_number"], a["method"], a.get("base_iata"),
        a.get("seats")),
    "open_route": lambda gs, a: gs.open_route(
        a["route_spec_id"], a["tail_number"], a["price"], a.get("freq", 1),
        a.get("seats"), a.get("service_tier", 2)),
    "sell_aircraft": lambda gs, a: gs.sell_aircraft(a["tail_number"]),
    "break_lease": lambda gs, a: gs.break_lease(a["tail_number"]),
    "reconfigure_aircraft": lambda gs, a: gs.reconfigure_aircraft(
        a["tail_number"], a["seats"]),
    "set_service_tier": lambda gs, a: gs.set_service_tier(
        a["route_op_id"], a["tier"]),
    "close_route": lambda gs, a: gs.close_route(a["route_op_id"]),
    "set_hub": lambda gs, a: gs.set_hub(a["iata"], a.get("enabled", True)),
    # --- alliances and consolidation ---
    "form_alliance": lambda gs, a: gs.form_alliance(
        a.get("name", "Alliance"), a.get("kind", "CODESHARE"), a.get("partners")),
    "join_alliance": lambda gs, a: gs.join_alliance(a["alliance_id"]),
    "leave_alliance": lambda gs, a: gs.leave_alliance(),
    "set_no_compete_hub": lambda gs, a: gs.set_no_compete_hub(
        a["iata"], a.get("enabled", True)),
    "acquire_carrier": lambda gs, a: gs.acquire_carrier(
        a["target_id"], a.get("force", False)),
    "hire_crew": lambda gs, a: gs.hire_crew(
        a["crew_type"], a["base_iata"], a["headcount"], a["cost_per_hour"],
        tuple(a.get("certs", ()))),
}


def _as_dict(result) -> dict:
    """Tree ops return dicts already; anything else is wrapped under 'result'."""
    return result if isinstance(result, dict) else {"result": result}


def _mutations(raw) -> tuple:
    """Parse the JSON mutation list into Mutation objects.

    Validation of `kind` belongs to explorer.MUTATION_KINDS, not here — this
    only converts shapes, so a new knob needs no change in the HTTP layer.
    """
    out = []
    for m in raw or ():
        if not isinstance(m, dict):
            raise ValueError("each mutation must be an object")
        if "kind" not in m:
            raise ValueError("mutation is missing 'kind'")
        out.append(Mutation(str(m["kind"]), str(m.get("target", "")),
                            float(m.get("value", 0.0))))
    return tuple(out)


def _values(body: dict) -> list:
    """Sweep values: either an explicit list, or from/to/count as a range."""
    if body.get("values"):
        return [float(v) for v in body["values"]]
    if "from" in body and "to" in body:
        return linspace(float(body["from"]), float(body["to"]),
                        int(body.get("count", 5)))
    raise ValueError("sweep needs 'values', or 'from'/'to' (+ optional 'count')")


class Hub:
    """Owns the live GameSession and fans out its tick snapshots to every
    connected SSE client. Swapping sessions (new game / load) re-wires the
    callback and stops the old session's background thread."""

    def __init__(self, session: GameSession, world: str = "demo",
                 hub_iata: str = "ORD"):
        self._lock = threading.Lock()
        self._clients: list = []
        self.session: GameSession = None
        # what "New Game" should rebuild — the world this server was started
        # with, not new_game()'s defaults
        self.world_kind = world
        self.hub_iata = hub_iata
        self._set_session(session)
        # The scenario tree is independent of the live game — it has its own
        # root world and is never ticked by the real-time loop. Built lazily so
        # a player who only ever opens the game GUI doesn't pay to construct
        # one, and so `airlinesim gui` starts as fast as it used to.
        self._tree: ScenarioTree = None
        self._tree_lock = threading.Lock()

    @property
    def tree(self) -> ScenarioTree:
        with self._tree_lock:
            if self._tree is None:
                self._tree = ScenarioTree()
            return self._tree

    def reset_tree(self, cycles: int = 0) -> ScenarioTree:
        with self._tree_lock:
            if self._tree is None:
                self._tree = ScenarioTree()
                if cycles:
                    self._tree.reset(cycles)
            else:
                self._tree.reset(cycles)
            return self._tree

    def _set_session(self, session: GameSession):
        if self.session is not None:
            self.session.stop()
        self.session = session
        self.session.on_tick = self._broadcast

    def new_game(self, **kwargs):
        kwargs.setdefault("world", self.world_kind)
        kwargs.setdefault("hub", self.hub_iata)
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
            # The shell is served off localhost/LAN and is versioned by whatever
            # build is installed, not by a URL hash — so heuristic browser
            # caching has nothing to key on and will happily serve last week's
            # CSS after an upgrade. Revalidate every time; the fetch is local.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        # -- GET ----------------------------------------------------------
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/state":
                self._send_json(hub.session.snapshot())
            elif path == "/api/catalog":
                self._send_json(hub.session.catalog())
            elif path == "/api/basemap":
                # The committed Natural Earth base map (states, highways,
                # coast, lakes, rivers). Static and ~140 KB, so it is read
                # once, cached in memory, and served with a long cache header
                # — it never changes within a run.
                self._send_basemap()
            elif path == "/api/mergers":
                self._send_json(hub.session.merger_candidates())
            elif path == "/api/cabin":
                self._send_json(self._cabin_fit(urlparse(self.path).query))
            elif path == "/api/events":
                self._serve_sse()
            elif path == "/api/explore/tree":
                self._send_json(hub.tree.to_json())
            elif path == "/api/explore/targets":
                self._send_json(hub.tree.targets())
            elif path == "/api/explore/node":
                qs = parse_qs(urlparse(self.path).query)
                node_id = (qs.get("id") or [""])[0]
                self._explore(lambda: hub.tree.node_detail(node_id))
            else:
                self._serve_static(path)

        def _send_basemap(self):
            body = _basemap_bytes()
            if body is None:
                self._send_json({"error": "no basemap.json in airlinesim/data — "
                                          "run tools/build_basemap.py"}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def _cabin_fit(self, query: str) -> dict:
            """
            GET /api/cabin?spec_id=A320&business=16 — what that cabin becomes
            on that airframe, plus the per-cabin maxima the seat fields clamp
            to. Read-only, and it goes through the same fitter the acquisition
            command does, so the preview can't disagree with the outcome.
            """
            qs = parse_qs(query)
            spec_id = (qs.get("spec_id") or [""])[0]
            if not spec_id:
                return {"error": "spec_id is required"}
            seats = {k: v[0] for k, v in qs.items()
                     if k not in ("spec_id",) and v and v[0] != ""}
            return hub.session.cabin_fit(spec_id, seats)

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
            elif path.startswith("/api/explore/"):
                self._handle_explore(path[len("/api/explore/"):], body)
            else:
                self.send_error(404)

        # -- scenario explorer -------------------------------------------
        # Branching is CPU-bound (it ticks the engine), so these are plain
        # synchronous request handlers: the response IS the completed run.
        # ScenarioTree takes its own lock, so concurrent tabs serialize there.

        def _explore(self, fn):
            """Run a tree operation, mapping its exceptions onto status codes."""
            try:
                self._send_json({"ok": True, **_as_dict(fn())})
            except KeyError as e:
                self._send_json({"ok": False, "message": str(e).strip("'\"")},
                                status=404)
            except (ValueError, TypeError) as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)

        def _handle_explore(self, action: str, body: dict):
            tree = hub.tree
            if action == "branch":
                self._explore(lambda: tree.branch(
                    body.get("parent") or tree.root_id,
                    _mutations(body.get("mutations", ())),
                    int(body.get("cycles", 30)),
                    body.get("label", "")).to_json())
            elif action == "sweep":
                self._explore(lambda: {"created": [
                    n.to_json() for n in tree.sweep(
                        body.get("parent") or tree.root_id,
                        body.get("kind", ""), body.get("target", ""),
                        _values(body), int(body.get("cycles", 30)))]})
            elif action == "expand":
                self._explore(lambda: {"created": [
                    n.to_json() for n in tree.expand(
                        body.get("parent") or tree.root_id,
                        body.get("kind", ""), body.get("target", ""),
                        _values(body), int(body.get("cycles", 30)),
                        int(body.get("depth", 2)))]})
            elif action == "evaluate":
                self._explore(lambda: tree.evaluate(body.get("expr", "")))
            elif action == "delete":
                self._explore(lambda: {"removed": tree.delete(body.get("node_id", ""))})
            elif action == "reset":
                self._explore(lambda: hub.reset_tree(int(body.get("cycles", 0))).to_json())
            else:
                self._send_json({"ok": False, "message": f"unknown explore action {action}"},
                                status=404)

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
                # sim HOURS per real second
                gs.set_speed(body.get("value", gs.speed))
            elif action == "resolution":
                gs.set_tick_hours(body.get("value", gs.engine.dt))
            elif action == "advance":
                # `hours` is the native form; `days` stays accepted so an older
                # client (or a saved bookmark) still fast-forwards correctly.
                if "hours" in body:
                    gs.advance_hours(body.get("hours", 1))
                else:
                    gs.advance_days(body.get("days", 1))
            else:
                self._send_json({"ok": False, "message": f"unknown action {action}"}, status=400)
                return
            self._send_json({"ok": True, "state": gs.snapshot()})

    return Handler


def run_server(host: str = "0.0.0.0", port: int = 8765, session: GameSession = None,
               world: str = "demo", hub_iata: str = "ORD"):
    """Build and return (httpd, hub); caller owns calling serve_forever()."""
    session = session or new_game(world=world, hub=hub_iata)
    hub = Hub(session, world=world, hub_iata=hub_iata)
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
