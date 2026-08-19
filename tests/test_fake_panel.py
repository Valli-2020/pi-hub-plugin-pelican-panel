"""Self-contained test for the pelican-panel Pi Hub plugin.

Runs a fake Pelican panel (stdlib http.server on an ephemeral port) that
serves the JSON:API shapes the real panel uses, plus a minimal fake
``PluginContext`` so the plugin can be imported and driven without the Pi
Hub core.  Run from the repo root:

    PYTHONPATH=/home/valentin/projects/pi-hub python3 tests/test_fake_panel.py

The test covers: snapshot build (list + parallel resources + allocations),
the flat row contract (identical keys, no None), 401 handling, power POST
flow (body, signal, 204), single-flight 409, and inert-state exclusion.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Load the plugin the same way the Pi Hub core does:
# spec_from_file_location with the hyphenated module name (a plain
# `import pi_hub_plugins.pelican_panel` would fail — hyphenated names are
# not valid Python identifiers).
_INIT = os.path.join(REPO, "pi_hub_plugins", "pelican-panel", "__init__.py")
_spec = importlib.util.spec_from_file_location(
    "pi_hub_plugins.pelican-panel", _INIT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["pi_hub_plugins.pelican-panel"] = _mod
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

PelicanPanelPlugin = _mod.PelicanPanelPlugin
valid_base_url = _mod.valid_base_url

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {detail}")
        raise SystemExit(1)


# ── Fake panel ──────────────────────────────────────────────────────────────

SERVERS = [
    {
        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "name": "Minecraft",
        "status": None,
        "is_suspended": False,
        "is_installing": False,
        "limits": {"memory": 4096, "disk": 10240, "cpu": 100},
        "relationships": {
            "allocations": {
                "data": [
                    {"attributes": {"id": 1, "ip": "10.0.0.5", "ip_alias": "",
                                    "port": 25565, "notes": None,
                                    "is_default": True}},
                    {"attributes": {"id": 2, "ip": "10.0.0.5", "ip_alias": "",
                                    "port": 25566, "notes": None,
                                    "is_default": False}},
                ]
            }
        },
    },
    {
        "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "name": "CS2",
        "status": None,
        "is_suspended": False,
        "is_installing": False,
        "limits": {"memory": 2048, "disk": 0, "cpu": 0},
        "relationships": {"allocations": {"data": [
            {"attributes": {"id": 3, "ip": "10.0.0.6",
                            "ip_alias": "cs.example.com", "port": 27015,
                            "notes": None, "is_default": True}},
        ]}},
    },
    {
        "uuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "name": "WIP-Game",
        "status": "installing",
        "is_suspended": False,
        "is_installing": True,
        "limits": {"memory": 1024, "disk": 5120, "cpu": 0},
        "relationships": {"allocations": {"data": []}},
    },
]

RESOURCES = {
    SERVERS[0]["uuid"]: {
        "current_state": "running",
        "is_suspended": False,
        "resources": {
            "memory_bytes": 1610612736,       # 1.5 GiB
            "cpu_absolute": 12.5,
            "disk_bytes": 3221225472,          # 3 GiB
            "network_rx_bytes": 1000,
            "network_tx_bytes": 2000,
            "uptime": 90061000,                # 1d 1h 1m
        },
    },
    SERVERS[1]["uuid"]: {
        "current_state": "stopped",
        "is_suspended": False,
        "resources": {
            "memory_bytes": 0,
            "cpu_absolute": 0,
            "disk_bytes": 0,
            "network_rx_bytes": 0,
            "network_tx_bytes": 0,
            "uptime": 0,
        },
    },
}


class FakePanelHandler(http.server.BaseHTTPRequestHandler):
    mode = "ok"          # ok | unauthorized | user-key | ratelimit |
                         # redirect | conflict | slow
    power_hits: list[dict[str, Any]] = []
    # Set by the test to block the FIRST power POST (single-flight test).
    block_power = None
    redirect_hits = 0

    def log_message(self, *a: Any) -> None:
        pass

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _servers_payload(self) -> dict[str, Any]:
        data = [{"object": "server", "attributes": s} for s in SERVERS]
        return {"object": "list", "data": data,
                "meta": {"pagination": {"total_pages": 1}}}

    def do_GET(self) -> None:  # noqa: N802
        if self.mode == "unauthorized":
            return self._json(401, {"errors": [{"detail": "no"}]})
        if self.mode == "ratelimit":
            return self._json(429, {"errors": [{"detail": "slow down"}]})
        if self.mode == "redirect":
            type(self).redirect_hits += 1
            self.send_response(302)
            self.send_header("Location", "http://evil.example.com/api/client")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/api/client?per_page"):
            # user-key mode: admin-all resolves to an empty list, the
            # type-less retry returns the servers.
            if self.mode == "user-key" and "type=admin-all" in self.path:
                return self._json(200, {"object": "list", "data": [],
                                        "meta": {"pagination":
                                                 {"total_pages": 1}}})
            return self._json(200, self._servers_payload())
        if self.path.startswith("/api/client/servers/") \
                and self.path.endswith("/resources"):
            if self.mode == "conflict":
                return self._json(409, {"errors": [{"detail": "busy"}]})
            if self.mode == "slow":
                time.sleep(3.0)
                return self._json(200, {"object": "stats",
                                        "attributes": {}})
            uuid = self.path.split("/")[4]
            attrs = RESOURCES.get(uuid)
            if attrs is None:
                return self._json(404, {"errors": []})
            return self._json(200, {"object": "stats", "attributes": attrs})
        if self.path.startswith("/api/client/servers/"):
            uuid = self.path.split("/")[4]
            entry = next((s for s in SERVERS if s["uuid"] == uuid), None)
            if entry is None:
                return self._json(404, {"errors": []})
            return self._json(200, {"object": "server",
                                    "attributes": entry})
        return self._json(404, {"errors": []})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode()) if raw else {}
        if self.path.startswith("/api/client/servers/") \
                and self.path.endswith("/power"):
            self.power_hits.append({
                "path": self.path,
                "body": body,
                "auth": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "accept": self.headers.get("Accept", ""),
            })
            if self.block_power is not None:
                self.block_power.wait(5.0)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self._json(404, {"errors": []})


class FakePanel:
    def __init__(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakePanelHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()


# ── Fake PluginContext ──────────────────────────────────────────────────────

class FakeContext:
    """Minimal stand-in for pi_hub.plugins.base.PluginContext."""

    def __init__(self, plugin_dir: str) -> None:
        self._path = os.path.join(plugin_dir, "config.json")
        self._cfg: dict[str, Any] = {}
        self.tasks: dict[str, dict[str, str]] = {}
        self.toasts: list[tuple[str, str]] = []

    def get_config(self) -> dict[str, Any]:
        if not self._cfg and os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as fh:
                self._cfg = json.load(fh)
        return self._cfg

    def save_config(self) -> None:
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(self._cfg, fh)

    def run_task(self, name: str, fn: Any) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def set_task_status(self, name: str, status: str, message: str = "") -> None:
        self.tasks[name] = {"status": status, "message": message}

    def get_task_status(self, name: str) -> dict[str, str] | None:
        return self.tasks.get(name)

    def toast(self, message: str, kind: str = "info") -> None:
        self.toasts.append((message, kind))


def make_plugin(panel: FakePanel,
                timeout: int = 5) -> tuple[PelicanPanelPlugin, FakeContext]:
    tmp = tempfile.mkdtemp(prefix="pelican-test-")
    ctx = FakeContext(tmp)
    cfg = ctx.get_config()
    cfg.update({
        "base_url": f"http://127.0.0.1:{panel.port}",
        "api_key": "test-key-123",
        "timeout": timeout,
        "verify_ssl": True,
        "poll_interval": 10,
    })
    ctx.save_config()
    plugin = PelicanPanelPlugin()
    plugin.load(ctx)
    return plugin, ctx


def wait_snapshot(plugin: PelicanPanelPlugin, timeout: float = 8.0) -> dict:
    """Wait until the refresher produced a non-stale snapshot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with plugin._lock:
            if not plugin._stale:
                return {"rows": [dict(r) for r in plugin._rows],
                        "targets": [dict(t) for t in plugin._targets]}
        time.sleep(0.05)
    raise AssertionError("refresher produced no snapshot in time")


def main() -> None:
    panel = FakePanel()
    try:
        print("== snapshot build ==")
        plugin, ctx = make_plugin(panel)
        try:
            snap = wait_snapshot(plugin)
            rows = snap["rows"]
            check("3 servers listed", len(rows) == 3, str(len(rows)))

            minecraft = next(r for r in rows if r["name"] == "Minecraft")
            check("row keys identical",
                  {tuple(r.keys()) for r in rows} == {tuple(rows[0].keys())},
                  str([tuple(r.keys()) for r in rows]))
            check("no None values",
                  all(v is not None for r in rows for v in r.values()),
                  str(rows))
            check("running state", minecraft["status"] == "running",
                  minecraft["status"])
            check("cpu formatted", minecraft["cpu"] == "12%",
                  minecraft["cpu"])
            check("ram used/limit", minecraft["ram"] == "1.5 / 4.0 GiB",
                  minecraft["ram"])
            check("disk used/limit", minecraft["disk"] == "3.0 / 10 GiB",
                  minecraft["disk"])
            check("primary ip", minecraft["ip"] == "10.0.0.5:25565",
                  minecraft["ip"])
            check("uptime human", minecraft["uptime"] == "1d 1h 1m",
                  minecraft["uptime"])

            cs2 = next(r for r in rows if r["name"] == "CS2")
            check("stopped state", cs2["status"] == "stopped",
                  cs2["status"])
            check("limited ram", cs2["ram"] == "0.0 / 2.0 GiB", cs2["ram"])
            check("unlimited disk", cs2["disk"] == "0.0 GiB / ∞", cs2["disk"])
            check("ip alias preferred", cs2["ip"] == "cs.example.com:27015",
                  cs2["ip"])

            wip = next(r for r in rows if r["name"] == "WIP-Game")
            check("inert state shown", wip["status"] == "installing",
                  wip["status"])
            check("inert not a power target",
                  all(t["uuid"] != SERVERS[2]["uuid"] for t in snap["targets"]),
                  str(snap["targets"]))

            payload = plugin.status_handler()[0]
            check("status payload flat scalars + servers",
                  payload["api_key"] == "set" and payload["stale"] is False
                  and isinstance(payload["servers"], list),
                  str(payload))
            check("real api key never leaked",
                  "test-key-123" not in json.dumps(payload))

            print("== bodyless actions ==")
            _, code = plugin.start_stopped_handler()
            check("start-stopped 202", code == 202, str(code))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline \
                    and not FakePanelHandler.power_hits:
                time.sleep(0.02)
            check("power POST hit panel", len(FakePanelHandler.power_hits) == 1,
                  str(FakePanelHandler.power_hits))
            hit = FakePanelHandler.power_hits[0]
            check("correct server + signal",
                  hit["body"] == {"signal": "start"} and
                  hit["path"].endswith(SERVERS[1]["uuid"] + "/power"),
                  str(hit))
            check("auth + headers",
                  hit["auth"] == "Bearer test-key-123" and
                  hit["content_type"] == "application/json" and
                  hit["accept"] == "application/json",
                  str(hit))

            print("== single flight (real concurrency) ==")
            # Block the power endpoint so the first task is genuinely in
            # flight, then fire a second action: 202, then 409.
            FakePanelHandler.power_hits.clear()
            gate = threading.Event()
            FakePanelHandler.block_power = gate
            try:
                _, code = plugin.start_stopped_handler()
                check("first action 202", code == 202, str(code))
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline \
                        and not FakePanelHandler.power_hits:
                    time.sleep(0.02)
                _, code = plugin.start_stopped_handler()
                check("second power run refused while in flight",
                      code == 409, str(code))
            finally:
                gate.set()
                FakePanelHandler.block_power = None
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with plugin._lock:
                    if not plugin._running and ctx.toasts:
                        break
                time.sleep(0.02)
            check("toast fired", any("started" in t[0] for t in ctx.toasts),
                  str(ctx.toasts))

            print("== /power with explicit selection ==")
            FakePanelHandler.power_hits.clear()
            _, code = plugin.power_handler(
                body={"signal": "stop",
                      "servers": [SERVERS[0]["uuid"], "unknown-uuid"]})
            check("power 202", code == 202, str(code))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline \
                    and not FakePanelHandler.power_hits:
                time.sleep(0.02)
            check("unknown uuid dropped",
                  len(FakePanelHandler.power_hits) == 1 and
                  SERVERS[0]["uuid"] in FakePanelHandler.power_hits[0]["path"],
                  str(FakePanelHandler.power_hits))
            _, code = plugin.power_handler(body={"signal": "explode"})
            check("invalid signal 400", code == 400, str(code))
            _, code = plugin.power_handler(body={"signal": "start",
                                                 "servers": "bogus"})
            check("invalid selector 400", code == 400, str(code))

            print("== 401 ==")
            plugin.unload()
            panel_mode = FakePanelHandler.mode
            FakePanelHandler.mode = "unauthorized"
            plugin2, _ = make_plugin(panel)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    with plugin2._lock:
                        if plugin2._error:
                            break
                    time.sleep(0.05)
                with plugin2._lock:
                    check("401 -> key hint", plugin2._error
                          == "API key invalid or expired",
                          plugin2._error)
            finally:
                plugin2.unload()
                FakePanelHandler.mode = panel_mode

            print("== error paths ==")

            def expect_notice(mode: str, message: str, label: str) -> None:
                FakePanelHandler.mode = mode
                p, _ = make_plugin(panel)
                try:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        with p._lock:
                            if p._error:
                                break
                        time.sleep(0.05)
                    with p._lock:
                        check(label, p._error == message, p._error)
                    # The notice must not blank the payload contract.
                    payload = p.status_handler()[0]
                    check(label + " -> notice, tab intact",
                          payload.get("notice") == message
                          and "servers" in payload
                          and "error" not in payload,
                          str(payload))
                finally:
                    p.unload()

            expect_notice("ratelimit", "rate limited", "429 -> backoff notice")
            expect_notice("redirect", "panel error", "3xx -> refused redirect")
            check("redirect never replayed bearer",
                  FakePanelHandler.redirect_hits == 1,
                  str(FakePanelHandler.redirect_hits))

            # 409 on /resources: rows render unavailable, poll survives.
            FakePanelHandler.mode = "conflict"
            p, _ = make_plugin(panel)
            try:
                snap = wait_snapshot(p)
                rows = snap["rows"]
                check("409 -> unavailable rows",
                      all(r["status"] == "unavailable"
                          or r["status"] in ("installing", "suspended")
                          for r in rows),
                      str(rows))
            finally:
                p.unload()

            # user-key mode: admin-all empty -> type-less retry finds servers.
            FakePanelHandler.mode = "user-key"
            p, _ = make_plugin(panel)
            try:
                snap = wait_snapshot(p)
                check("admin-all empty -> type-less retry",
                      len(snap["rows"]) == 3, str(snap["rows"]))
            finally:
                p.unload()

            # slow mode: resources sleep past the request timeout ->
            # the rows render dashes, the poll survives.
            FakePanelHandler.mode = "slow"
            p, _ = make_plugin(panel, timeout=1)
            try:
                snap = wait_snapshot(p)
                rows = snap["rows"]
                live = [r for r in rows
                        if r["status"] not in ("installing", "suspended")]
                check("timeout -> dash values",
                      live and all(r["cpu"] == "–" and r["ram"] == "–"
                                   and r["disk"] == "–" for r in live),
                      str(rows))
            finally:
                p.unload()
                FakePanelHandler.mode = panel_mode

            print("== base_url validation ==")
            check("valid url ok", valid_base_url("https://panel.example.com:8443"), "")
            check("path rejected",
                  not valid_base_url("https://panel.example.com/pelican"), "")
            check("ipv6 rejected",
                  not valid_base_url("https://[fd00::1]:8443"), "")
            check("newline rejected", not valid_base_url("https://panel.example.com\n"),
                  "")

            print(f"ALL {PASS} CHECKS PASSED")
        finally:
            plugin.unload()
    finally:
        panel.stop()


if __name__ == "__main__":
    main()
