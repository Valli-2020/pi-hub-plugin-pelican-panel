"""Self-contained test for the pelican-panel Pi Hub plugin.

Runs a fake Pelican panel (stdlib http.server on an ephemeral port) that
serves the JSON:API shapes the real panel uses, plus a minimal fake
``PluginContext`` so the plugin can be imported and driven without the Pi
Hub core.  Run from the repo root with a Pi Hub 8.x checkout on the path:

    PYTHONPATH=/home/pi/pi-hub-dev python3 tests/test_fake_panel.py

The test covers: snapshot build (list + parallel resources + allocations),
the /monitor payload (raw numbers, sparkline buckets), per-server and bulk
power (body, signal, 204), single-flight 409, pending flags, the
"stopped not by Pi Hub" toast, inert-state exclusion, config schema and
migration, error paths, and the header pill / Settings card nodes through
the core's own node validator.
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
    "pi_hub_plugins.pelican-panel", _INIT,
    submodule_search_locations=[os.path.dirname(_INIT)])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["pi_hub_plugins.pelican-panel"] = _mod
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

PelicanPanelPlugin = _mod.PelicanPanelPlugin
valid_base_url = _mod.valid_base_url
BUCKET_S = _mod.BUCKET_S
HISTORY_S = _mod.HISTORY_S

from pi_hub.plugins import contrib  # noqa: E402  (core node validator)

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
        "node": "Hetzner VPS",
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
        "current_state": "offline",
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

    def run_task(self, name: str, fn: Any, interval: float = 0) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def set_task_status(self, name: str, status: str, message: str = "") -> None:
        self.tasks[name] = {"status": status, "message": message}

    def get_task_status(self, name: str) -> dict[str, str] | None:
        return self.tasks.get(name)

    def toast(self, message: str, kind: str = "info") -> None:
        self.toasts.append((message, kind))


def make_plugin(panel: FakePanel, timeout: int = 5,
                notify_crash: bool = True) -> tuple[PelicanPanelPlugin, FakeContext]:
    tmp = tempfile.mkdtemp(prefix="pelican-test-")
    ctx = FakeContext(tmp)
    cfg = ctx.get_config()
    cfg.update({
        "base_url": f"http://127.0.0.1:{panel.port}",
        "api_key": "test-key-123",
        "timeout": timeout,
        "verify_ssl": True,
        "poll_interval": 10,
        "notify_crash": notify_crash,
    })
    ctx.save_config()
    plugin = PelicanPanelPlugin()
    plugin.load(ctx)
    return plugin, ctx


def wait_until(cond: Any, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def wait_snapshot(plugin: PelicanPanelPlugin, timeout: float = 8.0) -> list[dict]:
    """Wait until the refresher produced a non-stale snapshot."""
    def ready() -> bool:
        with plugin._lock:
            return not plugin._stale
    if not wait_until(ready, timeout):
        raise AssertionError("refresher produced no snapshot in time")
    with plugin._lock:
        return [dict(s) for s in plugin._servers]


def wait_cycles(plugin: PelicanPanelPlugin, n: int = 1) -> None:
    """Kick the refresher and wait for *n* more completed cycles."""
    for _ in range(n):
        with plugin._lock:
            before = plugin._last_ok
        plugin._kick.set()
        check_ok = wait_until(lambda: plugin._last_ok != before)
        if not check_ok:
            raise AssertionError("refresh cycle did not complete")


def wait_power_hits(n: int) -> None:
    wait_until(lambda: len(FakePanelHandler.power_hits) >= n, 3.0)


def wait_idle(plugin: PelicanPanelPlugin) -> None:
    def idle() -> bool:
        with plugin._lock:
            return not plugin._running
    wait_until(idle, 3.0)


MC, CS, WIP = (s["uuid"] for s in SERVERS)


def main() -> None:
    panel = FakePanel()
    try:
        print("== descriptors ==")
        p = PelicanPanelPlugin()
        check("api v2, core 8", p.plugin_api_version == 2
              and p.min_core_version == "8.0.0")
        manifest = json.load(open(os.path.join(os.path.dirname(_INIT),
                                               "pihub-plugin.json")))
        root_manifest = json.load(open(os.path.join(REPO, "pihub-plugin.json")))
        check("manifest caps == class caps",
              manifest["capabilities"] == p.capabilities
              and manifest["version"] == p.version
              and root_manifest == manifest, str(manifest))
        schema = contrib.validate_config_schema(p.get_config_schema())
        check("config schema accepted by core",
              [f["name"] for f in schema] == ["base_url", "api_key", "verify_ssl",
                                             "timeout", "poll_interval",
                                             "notify_crash"], str(schema))
        check("api key is secret",
              next(f for f in schema if f["name"] == "api_key")["secret"])
        migrated = p.migrate_config("1.2.0", {"base_url": "https://x.example",
                                              "api_key": "k", "timeout": 3,
                                              "verify_ssl": False,
                                              "poll_interval": 20})
        check("1.x config migrates", migrated["notify_crash"] is True
              and migrated["timeout"] == 3 and migrated["api_key"] == "k",
              str(migrated))
        frame = p.get_frames()[0]
        check("frame files exist", all(os.path.isfile(os.path.join(
            os.path.dirname(_INIT), "static", "frame", f))
            for f in frame.entry + frame.css), str(frame.entry))
        js = open(os.path.join(os.path.dirname(_INIT), "static", "frame",
                               "monitor.js"), encoding="utf-8").read()
        check("frame js has no </script, <!-- or innerHTML",
              "</script" not in js and "<!--" not in js
              and "innerHTML" not in js)
        routes = {(r.method, r.path) for r in p.get_routes()}
        check("routes for every frame call",
              {("GET", "/monitor"), ("POST", "/power"),
               ("POST", "/bulk/{action}"), ("POST", "/refresh")} == routes,
              str(routes))
        check("every route admin-only",
              all(r.caps == ["admin"] for r in p.get_routes()))

        print("== snapshot build ==")
        plugin, ctx = make_plugin(panel)
        try:
            servers = wait_snapshot(plugin)
            check("3 servers listed", len(servers) == 3, str(len(servers)))
            mc = next(s for s in servers if s["uuid"] == MC)
            check("running state", mc["state"] == "running", mc["state"])
            check("raw numbers", mc["cpu"] == 12.5 and mc["mem"] == 1610612736
                  and mc["disk"] == 3221225472 and mc["uptime"] == 90061,
                  str(mc))
            check("limits in bytes", mc["mem_limit"] == 4096 * 1024 ** 2
                  and mc["disk_limit"] == 10240 * 1024 ** 2
                  and mc["cpu_limit"] == 100, str(mc))
            check("node + primary ip", mc["node"] == "Hetzner VPS"
                  and mc["ip"] == "10.0.0.5:25565", str(mc))
            cs = next(s for s in servers if s["uuid"] == CS)
            check("offline state", cs["state"] == "offline", cs["state"])
            check("unlimited disk = 0", cs["disk_limit"] == 0, str(cs))
            check("ip alias preferred", cs["ip"] == "cs.example.com:27015",
                  cs["ip"])
            wip = next(s for s in servers if s["uuid"] == WIP)
            check("inert state shown", wip["state"] == "installing",
                  wip["state"])

            print("== net rate + history ==")
            RESOURCES[MC]["resources"]["network_rx_bytes"] += 10240
            wait_cycles(plugin)
            with plugin._lock:
                mc = next(s for s in plugin._servers if s["uuid"] == MC)
            check("rx rate from counter delta", mc["rx"] is not None
                  and mc["rx"] > 0 and mc["tx"] == 0, str(mc))

            payload, code = plugin.monitor_handler()
            check("/monitor 200", code == 200, str(code))
            check("real api key never leaked",
                  "test-key-123" not in json.dumps(payload))
            check("summary", payload["summary"]["running"] == 1
                  and payload["summary"]["total"] == 3
                  and payload["summary"]["cpu"] == 12.5,
                  str(payload["summary"]))
            check("configured + fresh", payload["configured"]
                  and payload["stale"] is False and payload["notice"] == "",
                  str(payload))
            mc = next(s for s in payload["servers"] if s["uuid"] == MC)
            n = HISTORY_S // BUCKET_S
            check("120 sparkline buckets", len(mc["cpu_pts"]) == n
                  and len(mc["mem_pts"]) == n, str(len(mc["cpu_pts"])))
            check("latest bucket filled", mc["cpu_pts"][-1] == 12.5
                  and mc["mem_pts"][-1] == 1536, str(mc["cpu_pts"][-3:]))
            check("old buckets empty (gap, not zero)", mc["cpu_pts"][0] is None)
            cs = next(s for s in payload["servers"] if s["uuid"] == CS)
            check("offline server has no samples",
                  all(v is None for v in cs["cpu_pts"]))
            check("payload small", len(json.dumps(payload)) < 64 * 1024)

            # Old samples fall out of the window; bucketing averages.
            now = time.time()
            with plugin._lock:
                plugin._hist[MC].appendleft((now - HISTORY_S - 5, 99.0, 1.0))
            pts = plugin._points(plugin._hist[MC], now)[0]
            check("sample older than the window ignored", 99.0 not in pts)

            print("== pill + settings card nodes ==")
            pill = contrib.validate_node(plugin.p_pill())
            check("pill counts running", pill["text"] == "Games 1/3"
                  and pill["tone"] == "ok", str(pill))
            card = contrib.validate_node(plugin.p_settings())
            flat = json.dumps(card)
            check("settings card valid, key masked",
                  "test-key-123" not in flat and '"set"' in flat
                  and '"refresh"' in flat, flat)

            print("== per-server power ==")
            FakePanelHandler.power_hits.clear()
            body, code = plugin.power_handler(
                body={"signal": "start", "servers": [CS, "unknown-uuid"]})
            check("power 202", code == 202 and body["message"] == "Starting CS2",
                  str((body, code)))
            wait_power_hits(1)
            hit = FakePanelHandler.power_hits[0]
            check("only the known uuid hit", len(FakePanelHandler.power_hits) == 1
                  and hit["path"].endswith(CS + "/power")
                  and hit["body"] == {"signal": "start"},
                  str(FakePanelHandler.power_hits))
            check("auth + headers",
                  hit["auth"] == "Bearer test-key-123"
                  and hit["content_type"] == "application/json"
                  and hit["accept"] == "application/json", str(hit))
            wait_idle(plugin)
            check("single-server toast", ("CS2 started", "ok") in ctx.toasts,
                  str(ctx.toasts))
            payload = plugin.monitor_handler()[0]
            cs = next(s for s in payload["servers"] if s["uuid"] == CS)
            check("pending while panel still says offline",
                  cs["pending"] == "start", str(cs["pending"]))
            RESOURCES[CS]["current_state"] = "starting"
            wait_cycles(plugin)
            cs = next(s for s in plugin.monitor_handler()[0]["servers"]
                      if s["uuid"] == CS)
            check("pending cleared on state change", cs["pending"] == ""
                  and cs["state"] == "starting", str(cs))
            RESOURCES[CS]["current_state"] = "offline"

            _, code = plugin.power_handler(body={"signal": "explode"})
            check("invalid signal 400", code == 400, str(code))
            _, code = plugin.power_handler(body={"signal": "start",
                                                 "servers": "bogus"})
            check("invalid selector 400", code == 400, str(code))
            FakePanelHandler.power_hits.clear()
            body, code = plugin.power_handler(body={"signal": "start",
                                                    "servers": [WIP]})
            check("inert server never a target", code == 200
                  and body["started"] is False
                  and not FakePanelHandler.power_hits, str(body))

            print("== bulk + single flight ==")
            wait_cycles(plugin)
            FakePanelHandler.power_hits.clear()
            gate = threading.Event()
            FakePanelHandler.block_power = gate
            try:
                _, code = plugin.bulk_handler(params={"action": "start-stopped"})
                check("bulk start-stopped 202", code == 202, str(code))
                wait_power_hits(1)
                _, code = plugin.power_handler(body={"signal": "stop",
                                                     "servers": [MC]})
                check("second power run refused while in flight",
                      code == 409, str(code))
            finally:
                gate.set()
                FakePanelHandler.block_power = None
            wait_idle(plugin)
            check("bulk targeted the offline server only",
                  [h["path"].split("/")[4] for h in FakePanelHandler.power_hits]
                  == [CS], str(FakePanelHandler.power_hits))
            _, code = plugin.bulk_handler(params={"action": "kill-everything"})
            check("unknown bulk action 404", code == 404, str(code))

            print("== stop detection ==")
            ctx.toasts.clear()
            # Our own stop: no crash toast.
            plugin.power_handler(body={"signal": "stop", "servers": [MC]})
            wait_idle(plugin)
            RESOURCES[MC]["current_state"] = "offline"
            wait_cycles(plugin)
            check("own stop is not reported",
                  not any("not by Pi Hub" in t[0] for t in ctx.toasts),
                  str(ctx.toasts))
            RESOURCES[MC]["current_state"] = "running"
            wait_cycles(plugin)
            with plugin._lock:
                plugin._issued.clear()
            RESOURCES[MC]["current_state"] = "stopped"     # legacy alias
            wait_cycles(plugin)
            check("foreign stop toasts",
                  ("Minecraft stopped (not by Pi Hub)", "err") in ctx.toasts,
                  str(ctx.toasts))
            with plugin._lock:
                mc = next(s for s in plugin._servers if s["uuid"] == MC)
            check("'stopped' read as offline", mc["state"] == "offline",
                  mc["state"])
            RESOURCES[MC]["current_state"] = "running"

            print("== config change ==")
            plugin.on_config_change(ctx.get_config(), dict(ctx.get_config()))
            check("refresh handler kicks", plugin.refresh_handler()[1] == 200)
        finally:
            plugin.unload()

        print("== error paths ==")
        panel_mode = FakePanelHandler.mode

        def expect_notice(mode: str, message: str, label: str,
                          pill_tone: str) -> None:
            FakePanelHandler.mode = mode
            p, _ = make_plugin(panel)
            try:
                wait_until(lambda: bool(p._error))
                with p._lock:
                    check(label, p._error == message, p._error)
                payload = p.monitor_handler()[0]
                check(label + " -> notice, payload intact",
                      payload.get("notice") == message
                      and "servers" in payload and "error" not in payload,
                      str(payload))
                pill = contrib.validate_node(p.p_pill())
                check(label + " -> pill " + pill_tone, pill["tone"] == pill_tone,
                      str(pill))
            finally:
                p.unload()

        try:
            expect_notice("unauthorized", "API key invalid or expired",
                          "401 -> key hint", "bad")
            expect_notice("ratelimit", "rate limited", "429 -> backoff notice",
                          "bad")
            expect_notice("redirect", "panel error", "3xx -> refused redirect",
                          "bad")
            check("redirect never replayed bearer",
                  FakePanelHandler.redirect_hits == 1,
                  str(FakePanelHandler.redirect_hits))

            # 409 on /resources: servers unavailable, poll survives.
            FakePanelHandler.mode = "conflict"
            p, _ = make_plugin(panel)
            try:
                servers = wait_snapshot(p)
                check("409 -> unavailable",
                      all(s["state"] in ("unavailable", "installing", "suspended")
                          for s in servers), str(servers))
            finally:
                p.unload()

            # user-key mode: admin-all empty -> type-less retry finds servers.
            FakePanelHandler.mode = "user-key"
            p, _ = make_plugin(panel)
            try:
                check("admin-all empty -> type-less retry",
                      len(wait_snapshot(p)) == 3)
            finally:
                p.unload()

            # slow mode: resources time out -> unknown values, poll survives.
            FakePanelHandler.mode = "slow"
            p, _ = make_plugin(panel, timeout=1)
            try:
                servers = wait_snapshot(p)
                live = [s for s in servers
                        if s["state"] not in ("installing", "suspended")]
                check("timeout -> None values",
                      live and all(s["cpu"] is None and s["mem"] is None
                                   for s in live), str(servers))
            finally:
                p.unload()
        finally:
            FakePanelHandler.mode = panel_mode

        print("== not configured ==")
        tmp = tempfile.mkdtemp(prefix="pelican-test-")
        ctx = FakeContext(tmp)
        p = PelicanPanelPlugin()
        p.load(ctx)
        try:
            wait_until(lambda: bool(p._error))
            pill = contrib.validate_node(p.p_pill())
            check("unconfigured pill muted", pill["tone"] == "muted"
                  and pill["text"] == "Games: not set up", str(pill))
            check("defaults written", ctx.get_config()["notify_crash"] is True
                  and ctx.get_config()["poll_interval"] == 10)
            check("monitor says not configured",
                  p.monitor_handler()[0]["configured"] is False)
        finally:
            p.unload()

        print("== base_url validation ==")
        check("valid url ok", valid_base_url("https://panel.example.com:8443"))
        check("path rejected",
              not valid_base_url("https://panel.example.com/pelican"))
        check("ipv6 rejected", not valid_base_url("https://[fd00::1]:8443"))
        check("newline rejected",
              not valid_base_url("https://panel.example.com\n"))

        print(f"ALL {PASS} CHECKS PASSED")
    finally:
        panel.stop()


if __name__ == "__main__":
    main()
