"""Pelican Panel plugin (Plugin API v2): game-server monitor and power control.

Talks to the Pelican **client** API (`/api/client/...`) over plain
``urllib`` (see ``client.py``); no core system capability is needed, only
UI grants: a sandboxed frame for the tab, a header pill and a Settings card.

Moving parts:

* a **background refresher thread** that rebuilds a snapshot every
  ``poll_interval`` seconds (server list once, per-server ``/resources``
  in parallel through one long-lived executor), swaps it under a lock and
  appends CPU/RAM samples to a one-hour history.  ``GET /monitor`` and the
  UI providers never do panel I/O.  A power action or "Refresh now" kicks
  the loop early;
* a **single-flight power task** that walks the selected servers and
  POSTs ``{"signal": ...}`` to each one;
* the **frame** (``static/frame/monitor.js``) that draws one card per
  server with sparklines and per-server buttons.

Semantics worth remembering when reading this file:

* Power state comes ONLY from ``/resources`` (``current_state``, Pelican's
  ``ContainerStatus``: ``offline``/``starting``/``running``/``stopping``
  plus rarer values folded in by ``STATE_ALIASES``; there is no
  ``stopped``).  The list's
  ``status`` field is the *install/suspend* state.
* ``/resources`` reports bytes; the list's ``limits`` are MiB with ``0``
  meaning unlimited.  ``cpu_absolute`` is a percentage where 100 % is one
  core.  Wings reports ``uptime`` in milliseconds.
* The panel caches ``/resources`` for ~20 s, so a power action shows up a
  little late; until then the server is flagged ``pending``.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pi_hub.plugins.base import (
    Contribution,
    FrameDef,
    Plugin,
    PluginContext,
    RouteDef,
    thread_cancel,
)

from .client import PanelError, request, valid_base_url

log = logging.getLogger(__name__)

TASK_POWER = "power"

SIGNALS = ("start", "stop", "restart", "kill")
#: Present participle per signal, for the live task status line.
SIGNAL_PROGRESS = {
    "start": "starting",
    "stop": "stopping",
    "restart": "restarting",
    "kill": "killing",
}
#: Past participle per signal, for the completion toast.
SIGNAL_DONE = {
    "start": "started",
    "stop": "stopped",
    "restart": "restarted",
    "kill": "killed",
}

#: Which power states a bulk action targets.
BULK_ACTIONS = {
    "start-stopped": ("start", ("offline",)),
    "stop-running": ("stop", ("running",)),
    "restart-running": ("restart", ("running",)),
}

#: Wings power states the UI knows.
LIVE_STATES = ("running", "offline", "starting", "stopping")
#: Other ``current_state`` values (Pelican ``ContainerStatus``) folded
#: into the four above; anything else renders as ``unavailable``.
STATE_ALIASES = {
    "stopped": "offline",
    "exited": "offline",
    "missing": "offline",
    "dead": "offline",
    "created": "offline",
    "restarting": "starting",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "timeout": 8,
    "verify_ssl": True,
    "poll_interval": 10,
    "notify_crash": True,
}

MIN_TIMEOUT, MAX_TIMEOUT = 1, 120
MIN_POLL, MAX_POLL = 5, 3600
MAX_PAGES = 50           # page-loop guard against a misbehaving panel
MAX_WORKERS = 8

HISTORY_S = 3600         # sparkline window
BUCKET_S = 30            # one sparkline point per 30 s -> 120 points
PENDING_TTL_S = 60       # how long a server shows "starting…" etc. at most
OWN_ACTION_GRACE_S = 180  # a stop within this window was ours, not a crash

#: Scoped under .plg-pelican-panel by the core: matches the header's own pills.
PILL_CSS = (".ph-badge { height: 26px; padding: 0 10px; font-size: 12px; "
            "font-weight: 500; letter-spacing: 0; }")

ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="7" rx="2"/>'
    '<rect x="3" y="13" width="18" height="7" rx="2"/>'
    '<path d="M7 7.5h.01M7 16.5h.01"/></svg>'
)


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _primary_allocation(allocations: list[dict[str, Any]]) -> str:
    """Render the default (or first) allocation as ``host:port``."""
    for alloc in allocations or []:
        if alloc.get("is_default"):
            chosen = alloc
            break
    else:
        chosen = (allocations or [None])[0]
    if not isinstance(chosen, dict):
        return ""
    host = str(chosen.get("ip_alias") or chosen.get("ip") or "").strip()
    port = chosen.get("port")
    if not host or port is None:
        return ""
    return f"{host}:{port}"


def _allocations_of(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the ``relationships.allocations`` list out of a server entry."""
    rel = attributes.get("relationships")
    if not isinstance(rel, dict):
        return []
    allocations = rel.get("allocations")
    if not isinstance(allocations, dict):
        return []
    out = []
    for entry in allocations.get("data") or []:
        if isinstance(entry, dict) and isinstance(entry.get("attributes"), dict):
            out.append(entry["attributes"])
    return out


def _inert_state(entry: dict[str, Any]) -> str:
    """Return ``installing``/``suspended`` when the server is not live."""
    status = str(entry.get("status") or "")
    if entry.get("is_suspended") or status == "suspended":
        return "suspended"
    if entry.get("is_installing") or status == "installing":
        return "installing"
    return ""


def _fmt_age(seconds: int) -> str:
    if seconds < 0:
        return "never"
    if seconds < 90:
        return f"{seconds} s ago"
    return f"{seconds // 60} min ago"


class PelicanPanelPlugin(Plugin):
    name = "pelican-panel"
    version = "2.0.1"
    description = (
        "Pelican game servers: live monitor with CPU/RAM sparklines, "
        "per-server start / stop / restart / kill, header pill"
    )
    min_core_version = "8.0.0"
    plugin_api_version = 2
    # No system capability: everything happens over the Pelican HTTP API.
    # ui.style (scoped, not global) only sizes the header pill like its
    # neighbours.
    capabilities = ["ui.frame", "ui.header", "ui.settings", "ui.style"]

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._servers: list[dict[str, Any]] = []
        self._last_ok = 0.0
        self._stale = True
        self._error = ""
        self._error_kind = ""          # "" | config | unauthorized | unreachable | rate | error
        self._running = False
        self._backoff_until = 0.0
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        # uuid -> deque[(wall ts, cpu %, mem bytes)]
        self._hist: dict[str, deque] = {}
        # Refresher-thread only: uuid -> (monotonic, rx bytes, tx bytes)
        self._net_prev: dict[str, tuple[float, float, float]] = {}
        # uuid -> (signal, state when issued, monotonic)
        self._pending: dict[str, tuple[str, str, float]] = {}
        # uuid -> monotonic of the last power signal we sent
        self._issued: dict[str, float] = {}
        self._prev_state: dict[str, str] = {}

        cfg = self.ctx.get_config()
        changed = False
        for key, value in DEFAULT_CONFIG.items():
            if key not in cfg:
                cfg[key] = value
                changed = True
        if changed:
            self.ctx.save_config()

        self._thread = threading.Thread(
            target=self._refresh_loop, name="pelican-panel-refresh", daemon=True)
        self._thread.start()

    def unload(self) -> None:
        self._stop.set()
        self._kick.set()
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None

    def migrate_config(self, old_version: str, config: dict) -> dict:
        # 1.x keys are a subset of 2.0's; only the new defaults are added.
        for key, value in DEFAULT_CONFIG.items():
            config.setdefault(key, value)
        return config

    def get_config_schema(self) -> list[dict]:
        return [
            {"name": "base_url", "label": "Panel URL", "type": "text",
             "required": True, "placeholder": "https://panel.example.com",
             "help": "scheme://host[:port], no path."},
            {"name": "api_key", "label": "Client API key", "type": "password",
             "help": "Panel → Account → API Keys (a client key, not an "
                     "application key). Left blank keeps the current key."},
            {"name": "verify_ssl", "label": "Verify SSL", "type": "checkbox",
             "default": True},
            {"name": "timeout", "label": "Timeout (s)", "type": "number",
             "default": 8, "min": MIN_TIMEOUT, "max": MAX_TIMEOUT},
            {"name": "poll_interval", "label": "Poll interval (s)",
             "type": "number", "default": 10, "min": MIN_POLL, "max": MAX_POLL},
            {"name": "notify_crash", "label": "Toast when a server stops "
             "without Pi Hub", "type": "checkbox", "default": True},
        ]

    def on_config_change(self, old: dict, new: dict) -> None:
        # Drop the backoff and history of a different panel, then poll now.
        if str(old.get("base_url", "")) != str(new.get("base_url", "")):
            with self._lock:
                self._servers = []
                self._hist.clear()
                self._prev_state.clear()
                self._last_ok = 0.0
            self._net_prev.clear()
        self._backoff_until = 0.0
        self._kick.set()

    # ── Descriptors ────────────────────────────────────────────────────────

    def get_routes(self) -> list[RouteDef]:
        return [
            RouteDef("GET", "/monitor", self.monitor_handler, caps=["admin"]),
            RouteDef("POST", "/power", self.power_handler, caps=["admin"]),
            RouteDef("POST", "/bulk/{action}", self.bulk_handler,
                     caps=["admin"]),
            RouteDef("POST", "/refresh", self.refresh_handler, caps=["admin"]),
        ]

    def get_frames(self) -> list[FrameDef]:
        return [FrameDef(
            "monitor",
            entry=["monitor.js"], css=["monitor.css"],
            surfaces=[{"type": "tab", "label": "Game servers",
                       "icon_svg": ICON_SVG}],
            height=420,
        )]

    def get_contributions(self) -> list[Contribution]:
        return [
            Contribution("header.pill", "games", self.p_pill, poll=15,
                         caps=["admin"]),
            Contribution("settings.card", "connection", self.p_settings,
                         poll=30, label="Pelican Panel"),
            # The core's header pills are 26 px; a generic badge is 20 px.
            Contribution("style", "pill", static={"css": PILL_CSS}),
        ]

    # ── Config ─────────────────────────────────────────────────────────────

    def _config_snapshot(self) -> dict[str, Any]:
        with self._lock:
            cfg = dict(self.ctx.get_config())
        return {
            "base_url": str(cfg.get("base_url", "") or "").strip().rstrip("/"),
            "api_key": str(cfg.get("api_key", "") or ""),
            "timeout": self._clamp(cfg.get("timeout"), MIN_TIMEOUT, MAX_TIMEOUT,
                                   DEFAULT_CONFIG["timeout"]),
            "verify_ssl": bool(cfg.get("verify_ssl", True)),
            "poll_interval": self._clamp(cfg.get("poll_interval"), MIN_POLL,
                                         MAX_POLL, DEFAULT_CONFIG["poll_interval"]),
            "notify_crash": bool(cfg.get("notify_crash", True)),
        }

    @staticmethod
    def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return fallback

    # ── Refresher ──────────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self._refresh_once()
            except PanelError:
                if self._stop.is_set():       # unloaded mid-cycle
                    return
                delay = float(DEFAULT_CONFIG["poll_interval"])
            except Exception as e:            # a crashed refresher is worse
                log.exception("pelican-panel: refresh cycle crashed: %s", e)
                delay = float(DEFAULT_CONFIG["poll_interval"])
            self._kick.wait(delay)
            self._kick.clear()

    def _refresh_once(self) -> float:
        """Rebuild the snapshot.  Returns the seconds to sleep afterwards."""
        cfg = self._config_snapshot()
        interval = float(cfg["poll_interval"])

        remaining = self._backoff_until - time.monotonic()
        if remaining > 0:
            return min(remaining, interval)

        if not cfg["base_url"] or not cfg["api_key"]:
            self._mark_failed("not configured — set the panel URL and API key",
                              "config")
            return interval
        if not valid_base_url(cfg["base_url"]):
            self._mark_failed("panel URL invalid — use scheme://host[:port]",
                              "config")
            return interval

        try:
            entries = self._fetch_servers(cfg)
        except PanelError as e:
            if e.kind == "unauthorized":
                self._mark_failed("API key invalid or expired", "unauthorized")
            elif e.kind == "rate limited":
                self._backoff_until = time.monotonic() + e.retry_after
                self._mark_failed("rate limited", "rate")
                return min(e.retry_after, interval)
            elif e.kind == "unreachable":
                self._mark_failed("panel unreachable", "unreachable")
            else:
                self._mark_failed("panel error", "error")
            return interval

        servers = self._build_snapshot(cfg, entries)
        crashed = self._track(servers, cfg)
        with self._lock:
            self._servers = servers
            # monotonic: age_seconds must never jump on an NTP step.
            self._last_ok = time.monotonic()
            self._stale = False
            self._error = ""
            self._error_kind = ""
        for name in crashed:
            self._safe_toast(f"{name} stopped (not by Pi Hub)", "err")
        # Auto-scale: ~1 + N requests per cycle, so a large panel polls
        # slower and never saturates the client API's rate limit.
        scaled = math.ceil(len(servers) / 5) * 10
        return float(max(cfg["poll_interval"], scaled))

    def _mark_failed(self, message: str, kind: str) -> None:
        """Keep the last good snapshot, flag it stale, set the error."""
        with self._lock:
            self._stale = True
            self._error = message
            self._error_kind = kind

    def _fetch_servers(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Fetch every page of the client server list, unwrapped.

        ``type=admin-all`` lists ALL panel servers for a root-admin key,
        but resolves to an empty list for ordinary user keys (the panel
        does not fall back to owned servers).  Self-healing: when the
        admin-all page 1 comes back empty, retry once without ``type`` so
        a plain key still sees its own servers.
        """
        entries = self._fetch_pages(cfg, admin_all=True)
        if not entries:
            entries = self._fetch_pages(cfg, admin_all=False)
        return entries

    def _fetch_pages(self, cfg: dict[str, Any], admin_all: bool) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            query = f"/api/client?per_page=100&page={page}"
            if admin_all:
                query += "&type=admin-all"
            body = request(cfg, "GET", query)
            for item in body.get("data") or []:
                if not isinstance(item, dict):
                    continue
                attributes = item.get("attributes")
                if isinstance(attributes, dict) and attributes.get("uuid"):
                    entries.append(attributes)
            pagination = (body.get("meta") or {}).get("pagination") or {}
            try:
                total_pages = int(pagination.get("total_pages", 1))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        return entries

    def _ensure_executor(self) -> ThreadPoolExecutor:
        """Create the one long-lived resources executor (never per cycle).

        Locked because ``unload()`` may null it while the refresher is
        mid-cycle; the refresher must never submit to a shut-down pool or
        recreate one after unload.
        """
        with self._lock:
            if self._stop.is_set():
                raise PanelError("error", "plugin unloading")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS, thread_name_prefix="pelican-res")
            return self._executor

    def _build_snapshot(self, cfg: dict[str, Any],
                        entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pollable = [e for e in entries if not _inert_state(e)]
        results: dict[str, dict[str, Any] | None] = {}
        ips: dict[str, str] = {}
        if pollable:
            executor = self._ensure_executor()
            futures: dict[str, Any] = {}
            for e in pollable:
                uuid = str(e["uuid"])
                futures[uuid] = executor.submit(self._fetch_resources, cfg, uuid)
                # The list normally carries allocations; a key without the
                # allocation.read permission needs the per-server detail
                # call.  Run those in parallel too — sequential fallbacks
                # would stall the whole refresh cycle.
                if not _allocations_of(e):
                    futures[uuid + ":ip"] = executor.submit(
                        self._fetch_ip, cfg, uuid)
            for key, future in futures.items():
                try:
                    value = future.result()
                except PanelError as e:
                    value = None
                    # A rate-limited fan-out must back off like a
                    # rate-limited list — otherwise the next cycle just
                    # hammers the panel again.
                    if e.kind == "rate limited":
                        self._backoff_until = time.monotonic() + e.retry_after
                if key.endswith(":ip"):
                    if value:
                        ips[key[:-3]] = value
                else:
                    results[key] = value

        now = time.monotonic()
        servers = [self._build_server(e, results.get(str(e["uuid"])),
                                      ips.get(str(e["uuid"]), ""), now)
                   for e in entries]
        live = {s["uuid"] for s in servers}
        for uuid in list(self._net_prev):
            if uuid not in live:
                del self._net_prev[uuid]
        return servers

    def _fetch_resources(self, cfg: dict[str, Any], uuid: str) -> dict[str, Any]:
        body = request(cfg, "GET", f"/api/client/servers/{uuid}/resources")
        attributes = body.get("attributes")
        return attributes if isinstance(attributes, dict) else {}

    def _fetch_ip(self, cfg: dict[str, Any], uuid: str) -> str:
        """Fallback primary allocation via the per-server detail call."""
        body = request(cfg, "GET", f"/api/client/servers/{uuid}")
        attributes = body.get("attributes")
        if not isinstance(attributes, dict):
            return ""
        return _primary_allocation(_allocations_of(attributes))

    def _build_server(self, entry: dict[str, Any],
                      resources: dict[str, Any] | None,
                      ip: str, now: float) -> dict[str, Any]:
        """One server as raw numbers; ``None`` = unknown.  Sizes in bytes."""
        limits = entry.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        mib = 1024 * 1024
        uuid = str(entry["uuid"])
        server: dict[str, Any] = {
            "uuid": uuid,
            "name": str(entry.get("name") or uuid)[:120],
            "node": str(entry.get("node") or "")[:120],
            "ip": ip or _primary_allocation(_allocations_of(entry)),
            "state": "unavailable",
            "cpu": None,
            "cpu_limit": _num(limits.get("cpu")) or 0,
            "mem": None,
            "mem_limit": (_num(limits.get("memory")) or 0) * mib,
            "disk": None,
            "disk_limit": (_num(limits.get("disk")) or 0) * mib,
            "rx": None,
            "tx": None,
            "uptime": None,
        }
        inert = _inert_state(entry)
        if inert:
            server["state"] = inert
            return server
        if not resources:
            return server
        state = str(resources.get("current_state") or "")
        state = STATE_ALIASES.get(state, state)
        server["state"] = state if state in LIVE_STATES else "unavailable"
        usage = resources.get("resources")
        if not isinstance(usage, dict):
            return server
        server["cpu"] = _num(usage.get("cpu_absolute"))
        server["mem"] = _num(usage.get("memory_bytes"))
        server["disk"] = _num(usage.get("disk_bytes"))
        uptime_ms = _num(usage.get("uptime"))
        server["uptime"] = int(uptime_ms / 1000) if uptime_ms else None
        rx, tx = _num(usage.get("network_rx_bytes")), _num(usage.get("network_tx_bytes"))
        prev = self._net_prev.get(uuid)
        if rx is not None and tx is not None:
            if prev is not None and now > prev[0]:
                drx, dtx = rx - prev[1], tx - prev[2]
                # Counters reset when the container restarts.
                if drx >= 0 and dtx >= 0:
                    server["rx"] = drx / (now - prev[0])
                    server["tx"] = dtx / (now - prev[0])
            self._net_prev[uuid] = (now, rx, tx)
        return server

    def _track(self, servers: list[dict[str, Any]],
               cfg: dict[str, Any]) -> list[str]:
        """History, pending flags and crash detection for a new snapshot.

        Returns the names of servers that went running -> offline without
        a power signal from this plugin (for the toast).
        """
        wall = time.time()
        now = time.monotonic()
        crashed: list[str] = []
        with self._lock:
            live = {s["uuid"] for s in servers}
            for table in (self._hist, self._pending, self._issued, self._prev_state):
                for uuid in [u for u in table if u not in live]:
                    del table[uuid]
            for s in servers:
                uuid, state = s["uuid"], s["state"]
                hist = self._hist.get(uuid)
                if hist is None:
                    hist = self._hist[uuid] = deque(
                        maxlen=HISTORY_S // MIN_POLL + 10)
                running = state in ("running", "starting", "stopping")
                hist.append((wall, s["cpu"] if running else None,
                             s["mem"] if running else None))
                while hist and hist[0][0] < wall - HISTORY_S:
                    hist.popleft()

                pending = self._pending.get(uuid)
                if pending and (state != pending[1]
                                or now - pending[2] > PENDING_TTL_S):
                    del self._pending[uuid]

                if state not in LIVE_STATES:
                    continue           # an unavailable cycle is no transition
                prev = self._prev_state.get(uuid)
                if prev == "running" and state == "offline":
                    issued = self._issued.get(uuid)
                    ours = issued is not None and now - issued < OWN_ACTION_GRACE_S
                    if not ours and cfg["notify_crash"]:
                        crashed.append(s["name"])
                self._prev_state[uuid] = state
        return crashed

    def _points(self, hist: deque | None, wall: float) -> tuple[list, list]:
        """Average the history into fixed ``BUCKET_S`` buckets (oldest first).

        ``None`` marks a bucket without a running sample, so the frame
        draws a gap instead of a fake zero.
        """
        n = HISTORY_S // BUCKET_S
        sums = [[0.0, 0.0, 0] for _ in range(n)]
        start = wall - HISTORY_S
        for ts, cpu, mem in hist or ():
            if cpu is None:
                continue
            i = int((ts - start) // BUCKET_S)
            if 0 <= i < n:
                bucket = sums[i]
                bucket[0] += cpu
                bucket[1] += mem or 0.0
                bucket[2] += 1
        cpu_pts = [round(b[0] / b[2], 1) if b[2] else None for b in sums]
        mem_pts = [int(b[1] / b[2] / (1024 * 1024)) if b[2] else None for b in sums]
        return cpu_pts, mem_pts

    def _safe_toast(self, message: str, kind: str) -> None:
        try:
            self.ctx.toast(message, kind)
        except Exception:
            log.exception("pelican-panel: toast failed")

    # ── Views ──────────────────────────────────────────────────────────────

    def _view(self, history: bool = False) -> dict[str, Any]:
        """A consistent copy of the state for the routes and providers."""
        with self._lock:
            return {
                "servers": [dict(s) for s in self._servers],
                "pending": {u: p[0] for u, p in self._pending.items()},
                "stale": self._stale,
                "error": self._error,
                "error_kind": self._error_kind,
                "last_ok": self._last_ok,
                "running": self._running,
                "hist": ({u: list(h) for u, h in self._hist.items()}
                         if history else {}),
            }

    @staticmethod
    def _summary(servers: list[dict[str, Any]]) -> dict[str, Any]:
        running = [s for s in servers if s["state"] == "running"]
        return {
            "running": len(running),
            "total": len(servers),
            "cpu": round(sum(s["cpu"] or 0 for s in running), 1),
            "mem": sum(s["mem"] or 0 for s in running),
        }

    def monitor_handler(self, **kw: Any) -> tuple[Any, int]:
        view = self._view(history=True)
        cfg = self._config_snapshot()
        wall = time.time()
        servers = []
        for s in view["servers"]:
            cpu_pts, mem_pts = self._points(view["hist"].get(s["uuid"]), wall)
            s["pending"] = view["pending"].get(s["uuid"], "")
            s["cpu_pts"] = cpu_pts
            s["mem_pts"] = mem_pts
            servers.append(s)
        last_ok = view["last_ok"]
        payload: dict[str, Any] = {
            "servers": servers,
            "summary": self._summary(view["servers"]),
            "configured": bool(cfg["base_url"] and cfg["api_key"]),
            "stale": view["stale"],
            "age_seconds": int(time.monotonic() - last_ok) if last_ok else -1,
            "notice": view["error"],
            "busy": view["running"],
            "window_s": HISTORY_S,
            "bucket_s": BUCKET_S,
        }
        task = self.ctx.get_task_status(TASK_POWER)
        if isinstance(task, dict):
            payload["task"] = {
                "status": str(task.get("status", "")),
                "message": str(task.get("message", "")),
            }
        return payload, 200

    def p_pill(self, session: Any = None) -> dict[str, Any]:
        view = self._view()
        summary = self._summary(view["servers"])
        title = view["error"] or "Pelican game servers running / total"
        if view["error_kind"] == "config":
            return {"type": "badge", "text": "Games: not set up",
                    "tone": "muted", "title": view["error"]}
        if not view["last_ok"]:
            if view["error"]:
                return {"type": "badge", "text": "Games: " + view["error"],
                        "tone": "bad", "title": title}
            return {"type": "badge", "text": "Games …", "tone": "muted",
                    "title": "waiting for the first poll"}
        text = f"Games {summary['running']}/{summary['total']}"
        if view["stale"]:
            tone = "bad" if view["error_kind"] in ("unauthorized", "unreachable") \
                else "warn"
        else:
            tone = "ok" if summary["running"] else "muted"
        return {"type": "badge", "text": text, "tone": tone, "title": title}

    def p_settings(self, session: Any = None) -> dict[str, Any]:
        view = self._view()
        cfg = self._config_snapshot()
        summary = self._summary(view["servers"])
        age = int(time.monotonic() - view["last_ok"]) if view["last_ok"] else -1
        rows: list[list[Any]] = [
            ["Panel URL", cfg["base_url"] or "(not set)"],
            ["API key", "set" if cfg["api_key"] else "(not set)"],
            ["Servers", f"{summary['running']} running of {summary['total']}"],
            ["Last poll", _fmt_age(age)],
            ["Status", view["error"] or "ok"],
        ]
        buttons: list[dict[str, Any]] = [
            {"type": "button", "label": "Refresh now", "action": "refresh",
             "style": "secondary"},
        ]
        if valid_base_url(cfg["base_url"]):
            buttons.insert(0, {"type": "link", "text": "Open panel",
                               "href": cfg["base_url"]})
        return {"type": "stack", "children": [
            {"type": "kv", "rows": rows},
            {"type": "stack", "dir": "row", "children": buttons},
            {"type": "text", "tone": "muted",
             "text": "Change URL and key in Settings → Plugins → Configure."},
        ]}

    # ── Route handlers ─────────────────────────────────────────────────────

    def refresh_handler(self, **kw: Any) -> tuple[Any, int]:
        self._kick.set()
        return {"success": True, "message": "Refreshing Pelican servers"}, 200

    def bulk_handler(self, params: dict | None = None,
                     **kw: Any) -> tuple[Any, int]:
        action = str((params or {}).get("action", ""))
        if action not in BULK_ACTIONS:
            return {"error": "unknown action"}, 404
        signal, states = BULK_ACTIONS[action]
        with self._lock:
            targets = [{"uuid": s["uuid"], "name": s["name"], "state": s["state"]}
                       for s in self._servers if s["state"] in states]
        return self._start_power(signal, targets)

    def power_handler(self, body: dict | None = None,
                      **kw: Any) -> tuple[Any, int]:
        body = body if isinstance(body, dict) else {}
        signal = str(body.get("signal", ""))
        if signal not in SIGNALS:
            return {"error": "invalid signal"}, 400
        selection = body.get("servers", "all")
        with self._lock:
            # Inert servers (installing/suspended) are never power targets.
            known = [{"uuid": s["uuid"], "name": s["name"], "state": s["state"]}
                     for s in self._servers
                     if s["state"] not in ("installing", "suspended")]
        if selection == "all":
            targets = known
        elif isinstance(selection, list):
            # Unknown / stale uuids are dropped, never forwarded.
            wanted = {str(s) for s in selection}
            targets = [t for t in known if t["uuid"] in wanted]
        else:
            return {"error": "invalid servers selector"}, 400
        return self._start_power(signal, targets)

    # ── Power task ─────────────────────────────────────────────────────────

    def _start_power(self, signal: str,
                     targets: list[dict[str, str]]) -> tuple[Any, int]:
        if signal not in SIGNALS:
            return {"error": "invalid signal"}, 400
        if not targets:
            return {"success": True, "started": False,
                    "message": f"No servers to {signal}"}, 200
        with self._lock:
            if self._running:
                return {"error": "a power action is already in progress"}, 409
            self._running = True
            now = time.monotonic()
            for t in targets:
                self._pending[t["uuid"]] = (signal, t["state"], now)
        self.ctx.set_task_status(TASK_POWER, "running", "starting")
        self.ctx.run_task(TASK_POWER, lambda: self._run_power(signal, targets))
        if len(targets) == 1:
            what = targets[0]["name"]
        else:
            what = f"{len(targets)} servers"
        return {"success": True, "started": True,
                "message": f"{SIGNAL_PROGRESS[signal].capitalize()} {what}"}, 202

    def _run_power(self, signal: str, targets: list[dict[str, str]]) -> None:
        try:
            cancel = thread_cancel()
            cfg = self._config_snapshot()
            total = len(targets)
            done = 0
            failed = 0
            for index, target in enumerate(targets, 1):
                if cancel is not None and cancel.is_set():
                    self._finish("cancelled", "power run cancelled",
                                 "Power run cancelled", "info")
                    return
                self.ctx.set_task_status(
                    TASK_POWER, "running",
                    f"{SIGNAL_PROGRESS[signal]} {target['name']} ({index}/{total})")
                with self._lock:
                    self._issued[target["uuid"]] = time.monotonic()
                try:
                    request(cfg, "POST",
                            f"/api/client/servers/{target['uuid']}/power",
                            {"signal": signal})
                    done += 1
                except PanelError as e:
                    failed += 1
                    with self._lock:
                        self._pending.pop(target["uuid"], None)
                    log.warning("pelican-panel: %s failed for %s: %s (%s)",
                                signal, target["uuid"], e.kind, e.detail)
            verb = SIGNAL_DONE[signal]
            if total == 1:
                name = targets[0]["name"]
                message = (f"{name} {verb}" if done
                           else f"Could not {signal} {name}")
            else:
                message = f"{done} server(s) {verb}"
                if failed:
                    message += f", {failed} failed"
            kind = "ok" if not failed else ("err" if not done else "info")
            self._finish("done", message, message, kind)
        except Exception as e:               # never leave single-flight wedged
            log.exception("pelican-panel: power run crashed: %s", e)
            self._finish("error", "power run failed", "Power run failed", "err")

    def _finish(self, status: str, message: str, toast: str, kind: str) -> None:
        # Clear single-flight FIRST: a failing toast must not wedge the
        # plugin at 409 until it is reloaded.
        with self._lock:
            self._running = False
        self._kick.set()
        try:
            self.ctx.set_task_status(TASK_POWER, status, message)
        except Exception:
            log.exception("pelican-panel: reporting the power result failed")
        self._safe_toast(toast, kind)
