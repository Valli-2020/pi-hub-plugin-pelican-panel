"""Pelican Panel plugin — live server table plus power actions.

Talks to the Pelican **client** API (`/api/client/...`) over plain
``urllib``; the plugin needs no core capability at all (no SSH, no hosts,
no Proxmox) — least privilege.

Two moving parts:

* a **background refresher thread** that rebuilds a snapshot every
  ``poll_interval`` seconds (server list once, per-server ``/resources``
  in parallel through one long-lived executor) and swaps it under a lock,
  so ``GET /status`` never does panel I/O and multi-tab browsers cannot
  multiply the fan-out;
* a **single-flight power task** that walks the selected servers and
  POSTs ``{"signal": ...}`` to each one.

Semantics worth remembering when reading this file:

* Power state comes ONLY from ``/resources`` (``current_state``).  The
  list's ``status`` field is the *install/suspend* state.
* ``/resources`` reports bytes; the list's ``limits`` are MiB with ``0``
  meaning unlimited.  ``cpu_absolute`` is a percentage where 100 % is one
  core.  Wings reports ``uptime`` in milliseconds.
* The panel caches ``/resources`` for ~20 s, so a power action may not
  show up in the table until the next cache window — documented in the
  README, not a bug.
"""

from __future__ import annotations

import json
import logging
import math
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pi_hub.plugins.base import (
    ActionDef,
    Plugin,
    PluginContext,
    RouteDef,
    TabUIDef,
    thread_cancel,
)

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

#: Which power states a bodyless action targets.
ACTION_TARGETS = {
    "start-stopped": ("start", ("stopped",)),
    "stop-running": ("stop", ("running",)),
    "restart-running": ("restart", ("running",)),
    "kill-running": ("kill", ("running",)),
}

#: Placeholder for every value we could not determine.  Never ``None`` —
#: the generic renderer would print the literal string "None".
DASH = "–"
INFINITY = "∞"

#: Scheme + host + optional port, nothing else.  ``fullmatch`` (not a
#: ``$``-anchored search, which would accept a trailing newline) plus an
#: explicit CR/LF check.  IPv6 literals and sub-path installs are
#: deliberately unsupported — see README "Limitations".
_BASE_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?")

#: Every row carries exactly these keys, in this order — a missing key
#: would shift the generic renderer's table columns.
ROW_KEYS = ("name", "status", "cpu", "ram", "disk", "ip", "uptime")

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "",
    "api_key": "",
    "timeout": 8,
    "verify_ssl": True,
    "poll_interval": 10,
}

MIN_TIMEOUT, MAX_TIMEOUT = 1, 120
MIN_POLL, MAX_POLL = 5, 3600
RATE_LIMIT_BACKOFF_S = 60
MAX_PAGES = 50           # page-loop guard against a misbehaving panel
MAX_WORKERS = 8


class PanelError(Exception):
    """A failed panel request, reduced to a user-safe ``kind``.

    ``kind`` is one of ``unauthorized`` / ``unavailable`` / ``rate
    limited`` / ``unreachable`` / ``error``.  The detailed cause stays in
    ``detail`` and only ever reaches the server log.
    """

    def __init__(self, kind: str, detail: str = "", retry_after: float = 0.0):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    urllib's default handler would replay the ``Authorization`` header
    against the redirect target — a one-hop API key exfiltration.
    Returning ``None`` makes urllib raise the 3xx as an ``HTTPError``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def valid_base_url(url: str) -> bool:
    """True when *url* is a bare ``scheme://host[:port]`` with no CR/LF."""
    if not url or "\r" in url or "\n" in url:
        return False
    return _BASE_URL_RE.fullmatch(url) is not None


def _gib(value: float) -> str:
    """Format a GiB amount: one decimal below 10, whole numbers above."""
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def _fmt_size(used_bytes: Any, limit_mib: Any) -> str:
    """Render ``used / limit`` in GiB; a limit of 0 means unlimited."""
    try:
        used = float(used_bytes) / (1024 ** 3)
        limit = float(limit_mib) / 1024
    except (TypeError, ValueError):
        return DASH
    if limit <= 0:
        return f"{_gib(used)} GiB / {INFINITY}"
    return f"{_gib(used)} / {_gib(limit)} GiB"


def _fmt_cpu(value: Any) -> str:
    """Render ``cpu_absolute`` (100 % = one core) as a whole percentage."""
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return DASH


def _fmt_uptime(uptime_ms: Any) -> str:
    """Render Wings' millisecond uptime as ``1d 2h 3m``."""
    try:
        seconds = int(float(uptime_ms) / 1000)
    except (TypeError, ValueError):
        return DASH
    if seconds <= 0:
        return DASH
    if seconds < 60:
        return f"{seconds}s"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _primary_allocation(allocations: list[dict[str, Any]]) -> str:
    """Render the default (or first) allocation as ``host:port``."""
    for alloc in allocations or []:
        if alloc.get("is_default"):
            chosen = alloc
            break
    else:
        chosen = (allocations or [None])[0]
    if not isinstance(chosen, dict):
        return DASH
    host = str(chosen.get("ip_alias") or chosen.get("ip") or "").strip()
    port = chosen.get("port")
    if not host or port is None:
        return DASH
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


class PelicanPanelPlugin(Plugin):
    name = "pelican-panel"
    version = "1.2.0"
    description = (
        "Live Pelican Panel server table (state, CPU, RAM, disk, IP) with "
        "start / stop / restart / kill actions"
    )
    min_core_version = "7.3.2"
    # No core capability: everything happens over the Pelican HTTP API.
    capabilities: list[str] = []

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        self._targets: list[dict[str, str]] = []   # uuid/name/status, internal
        self._last_ok = 0.0
        self._stale = True
        self._error = ""
        self._running = False
        self._backoff_until = 0.0
        self._stop = threading.Event()
        self._executor: ThreadPoolExecutor | None = None

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
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None

    # ── Descriptors ────────────────────────────────────────────────────────

    def get_routes(self) -> list[RouteDef]:
        return [
            RouteDef("GET", "/status", self.status_handler, caps=["admin"]),
            RouteDef("POST", "/start-stopped", self.start_stopped_handler,
                     caps=["admin"]),
            RouteDef("POST", "/stop-running", self.stop_running_handler,
                     caps=["admin"]),
            RouteDef("POST", "/restart-running", self.restart_running_handler,
                     caps=["admin"]),
            RouteDef("POST", "/kill-running", self.kill_running_handler,
                     caps=["admin"]),
            RouteDef("POST", "/power", self.power_handler, caps=["admin"]),
            RouteDef("POST", "/config", self.config_handler, caps=["admin"]),
        ]

    def get_ui(self) -> list[Any]:
        return [
            TabUIDef(
                id="pelican-panel",
                label="Pelican servers",
                icon_svg=(
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                    '<rect x="3" y="4" width="18" height="7" rx="2"/>'
                    '<rect x="3" y="13" width="18" height="7" rx="2"/>'
                    '<path d="M7 7.5h.01M7 16.5h.01"/></svg>'
                ),
                position=10,
                poll_endpoint="/api/plugin/pelican-panel/status",
                actions=[
                    ActionDef("start-stopped", "Start stopped",
                              style="primary", caps=["admin"]),
                    ActionDef("stop-running", "Stop running",
                              style="secondary", caps=["admin"]),
                    ActionDef("restart-running", "Restart running",
                              style="secondary", caps=["admin"]),
                    ActionDef("kill-running", "Kill running",
                              style="secondary", caps=["admin"]),
                    ActionDef("config", "Configure…",
                              style="secondary", caps=["admin"],
                              # Core 7.7+ renders a form dialog from this
                              # schema; older cores POST bodyless and get
                              # the summary toast instead.
                              fields=[
                                  {"name": "base_url", "label": "Panel URL",
                                   "type": "text",
                                   "placeholder": "https://panel.example.com"},
                                  {"name": "api_key", "label": "API key",
                                   "type": "password"},
                                  {"name": "timeout", "label": "Timeout (s)",
                                   "type": "number", "default": 8},
                                  {"name": "verify_ssl",
                                   "label": "Verify SSL",
                                   "type": "checkbox", "default": True},
                                  {"name": "poll_interval",
                                   "label": "Poll interval (s)",
                                   "type": "number", "default": 10},
                              ]),
                ],
            ),
        ]

    # ── HTTP client ────────────────────────────────────────────────────────

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
        }

    @staticmethod
    def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return fallback

    def _request(self, cfg: dict[str, Any], method: str, path: str,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform one panel request and return the decoded JSON body.

        Raises :class:`PanelError` for every failure; the caller only ever
        sees the masked ``kind``.
        """
        if not valid_base_url(cfg["base_url"]):
            raise PanelError("error", "base_url invalid")
        if not cfg["api_key"]:
            raise PanelError("unauthorized", "api_key not configured")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            cfg["base_url"] + path, data=data, headers=headers, method=method)

        # Per-request SSL context when verification is off — mutating the
        # module-level default would disable verification for the whole
        # Pi Hub process, not just this plugin.
        handlers: list[Any] = [_NoRedirect()]
        if not cfg["verify_ssl"]:
            insecure = ssl.create_default_context()
            insecure.check_hostname = False
            insecure.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=insecure))
        opener = urllib.request.build_opener(*handlers)

        try:
            with opener.open(req, timeout=cfg["timeout"]) as resp:
                raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise self._http_error(e, method, path) from None
        except urllib.error.URLError as e:
            log.warning("pelican-panel: %s %s unreachable: %s", method, path,
                        e.reason)
            raise PanelError("unreachable", str(e.reason)) from None
        except (OSError, ValueError) as e:
            log.warning("pelican-panel: %s %s failed: %s", method, path, e)
            raise PanelError("unreachable", str(e)) from None

    @staticmethod
    def _http_error(e: urllib.error.HTTPError, method: str,
                    path: str) -> PanelError:
        """Map an HTTP status onto a masked :class:`PanelError`."""
        log.warning("pelican-panel: %s %s returned HTTP %s", method, path, e.code)
        if e.code in (401, 403):
            return PanelError("unauthorized", f"HTTP {e.code}")
        if e.code == 409:
            return PanelError("unavailable", "HTTP 409")
        if e.code == 429:
            retry_after = RATE_LIMIT_BACKOFF_S
            header = e.headers.get("Retry-After") if e.headers else None
            try:
                if header is not None:
                    retry_after = max(1.0, float(str(header).strip()))
            except ValueError:
                pass
            return PanelError("rate limited", "HTTP 429", retry_after)
        return PanelError("error", f"HTTP {e.code}")

    # ── Refresher ──────────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self._refresh_once()
            except Exception as e:            # a crashed refresher is worse
                log.exception("pelican-panel: refresh cycle crashed: %s", e)
                delay = float(DEFAULT_CONFIG["poll_interval"])
            if self._stop.wait(delay):
                return

    def _refresh_once(self) -> float:
        """Rebuild the snapshot.  Returns the seconds to sleep afterwards."""
        cfg = self._config_snapshot()
        interval = float(cfg["poll_interval"])

        remaining = self._backoff_until - time.monotonic()
        if remaining > 0:
            return min(remaining, interval)

        if not cfg["base_url"] or not cfg["api_key"]:
            self._mark_failed("not configured — set base_url and api_key")
            return interval
        if not valid_base_url(cfg["base_url"]):
            self._mark_failed("base_url invalid")
            return interval

        try:
            entries = self._fetch_servers(cfg)
        except PanelError as e:
            if e.kind == "unauthorized":
                self._mark_failed("API key invalid or expired")
            elif e.kind == "rate limited":
                self._backoff_until = time.monotonic() + e.retry_after
                self._mark_failed("rate limited")
                return min(e.retry_after, interval)
            else:
                message = ("panel unreachable" if e.kind == "unreachable"
                           else "panel error")
                self._mark_failed(message)
            return interval

        rows, targets = self._build_snapshot(cfg, entries)
        with self._lock:
            self._rows = rows
            self._targets = targets
            # monotonic: age_seconds must never jump on an NTP step.
            self._last_ok = time.monotonic()
            self._stale = False
            self._error = ""
        # Auto-scale: ~1 + N requests per cycle, so a large panel polls
        # slower and never saturates the client API's rate limit.
        scaled = math.ceil(len(rows) / 5) * 10
        return float(max(cfg["poll_interval"], scaled))

    def _mark_failed(self, message: str) -> None:
        """Keep the last good rows, flag the snapshot stale, set the error."""
        with self._lock:
            self._stale = True
            self._error = message

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
            body = self._request(cfg, "GET", query)
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

    def _build_snapshot(
        self, cfg: dict[str, Any], entries: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        pollable = [e for e in entries if not self._inert_state(e)]
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

        rows: list[dict[str, Any]] = []
        targets: list[dict[str, str]] = []
        for entry in entries:
            uuid = str(entry["uuid"])
            row = self._build_row(cfg, entry, results.get(uuid),
                                  ips.get(uuid))
            rows.append(row)
            # Inert servers (installing/suspended) are never power
            # targets — every power path then needs no re-filtering.
            if not self._inert_state(entry):
                targets.append({"uuid": uuid, "name": row["name"],
                                "status": row["status"]})
        return rows, targets

    def _fetch_resources(self, cfg: dict[str, Any], uuid: str) -> dict[str, Any]:
        body = self._request(cfg, "GET", f"/api/client/servers/{uuid}/resources")
        attributes = body.get("attributes")
        return attributes if isinstance(attributes, dict) else {}

    def _fetch_ip(self, cfg: dict[str, Any], uuid: str) -> str:
        """Fallback primary allocation via the per-server detail call."""
        body = self._request(cfg, "GET", f"/api/client/servers/{uuid}")
        attributes = body.get("attributes")
        if not isinstance(attributes, dict):
            return ""
        return _primary_allocation(_allocations_of(attributes))

    @staticmethod
    def _inert_state(entry: dict[str, Any]) -> str:
        """Return ``installing``/``suspended`` when the server is not live."""
        status = str(entry.get("status") or "")
        if entry.get("is_suspended") or status == "suspended":
            return "suspended"
        if entry.get("is_installing") or status == "installing":
            return "installing"
        return ""

    def _build_row(self, cfg: dict[str, Any], entry: dict[str, Any],
                   resources: dict[str, Any] | None,
                   ip: str = "") -> dict[str, Any]:
        limits = entry.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        row: dict[str, Any] = {
            "name": str(entry.get("name") or entry.get("uuid") or DASH),
            "status": "unavailable",
            "cpu": DASH,
            "ram": DASH,
            "disk": DASH,
            "ip": ip or _primary_allocation(_allocations_of(entry)) or DASH,
            "uptime": DASH,
        }
        inert = self._inert_state(entry)
        if inert:
            row["status"] = inert
            return row
        if not resources:
            return row
        state = str(resources.get("current_state") or "")
        row["status"] = state if state in (
            "running", "stopped", "starting", "stopping") else "unavailable"
        usage = resources.get("resources")
        if not isinstance(usage, dict):
            return row
        row["cpu"] = _fmt_cpu(usage.get("cpu_absolute"))
        row["ram"] = _fmt_size(usage.get("memory_bytes"), limits.get("memory", 0))
        row["disk"] = _fmt_size(usage.get("disk_bytes"), limits.get("disk", 0))
        row["uptime"] = _fmt_uptime(usage.get("uptime"))
        # Enforce the identical-key-set invariant: a missing key would
        # shift the generic renderer's table columns.  KeyError here is a
        # programming error and crashes the cycle loudly (caught upstream).
        return {key: row[key] for key in ROW_KEYS}

    # ── Route handlers ─────────────────────────────────────────────────────

    def status_handler(self, **kw: Any) -> tuple[Any, int]:
        cfg = self._config_snapshot()
        with self._lock:
            rows = [dict(r) for r in self._rows]
            stale = self._stale
            error = self._error
            last_ok = self._last_ok
            running = self._running
        payload: dict[str, Any] = {
            "servers": rows,
            "base_url": cfg["base_url"],
            "api_key": "set" if cfg["api_key"] else "",
            "verify_ssl": cfg["verify_ssl"],
            "timeout": cfg["timeout"],
            "poll_interval": cfg["poll_interval"],
            "stale": stale,
            "age_seconds": int(time.monotonic() - last_ok) if last_ok else -1,
            "running": running,
        }
        if error:
            # "notice", NOT "error": the generic renderer short-circuits
            # on a top-level `error` key and blanks the whole tab
            # (index.html renderPluginTabData) — the table and action
            # buttons must stay visible next to the hint.
            payload["notice"] = error
        task = self.ctx.get_task_status(TASK_POWER)
        if isinstance(task, dict):
            payload["task"] = {
                "status": str(task.get("status", "")),
                "message": str(task.get("message", "")),
            }
        return payload, 200

    def start_stopped_handler(self, **kw: Any) -> tuple[Any, int]:
        return self._action("start-stopped")

    def stop_running_handler(self, **kw: Any) -> tuple[Any, int]:
        return self._action("stop-running")

    def restart_running_handler(self, **kw: Any) -> tuple[Any, int]:
        return self._action("restart-running")

    def kill_running_handler(self, **kw: Any) -> tuple[Any, int]:
        return self._action("kill-running")

    def _action(self, action_id: str) -> tuple[Any, int]:
        signal, states = ACTION_TARGETS[action_id]
        with self._lock:
            targets = [dict(t) for t in self._targets if t["status"] in states]
        return self._start_power(signal, targets)

    def power_handler(self, **kw: Any) -> tuple[Any, int]:
        body = kw.get("body") or {}
        signal = str(body.get("signal", ""))
        if signal not in SIGNALS:
            return {"error": "invalid signal"}, 400
        selection = body.get("servers", "all")
        with self._lock:
            known = [dict(t) for t in self._targets]
        if selection == "all":
            targets = known
        elif isinstance(selection, list):
            # Unknown / stale uuids are dropped, never forwarded.
            wanted = {str(s) for s in selection}
            targets = [t for t in known if t["uuid"] in wanted]
        else:
            return {"error": "invalid servers selector"}, 400
        return self._start_power(signal, targets)

    def config_handler(self, **kw: Any) -> tuple[Any, int]:
        body = kw.get("body") or {}
        if "base_url" in body:
            candidate = str(body.get("base_url") or "").strip().rstrip("/")
            if candidate and not valid_base_url(candidate):
                return {"error": "base_url must be http(s)://host[:port] "
                                 "(no path, no IPv6 literal)"}, 400
        with self._lock:
            cfg = self.ctx.get_config()
            changed = False
            if "base_url" in body:
                cfg["base_url"] = str(body.get("base_url") or "").strip().rstrip("/")
                changed = True
            if "api_key" in body:
                cfg["api_key"] = str(body.get("api_key") or "")
                changed = True
            if "verify_ssl" in body:
                cfg["verify_ssl"] = bool(body["verify_ssl"])
                changed = True
            if "timeout" in body:
                cfg["timeout"] = self._clamp(body["timeout"], MIN_TIMEOUT,
                                             MAX_TIMEOUT, DEFAULT_CONFIG["timeout"])
                changed = True
            if "poll_interval" in body:
                cfg["poll_interval"] = self._clamp(
                    body["poll_interval"], MIN_POLL, MAX_POLL,
                    DEFAULT_CONFIG["poll_interval"])
                changed = True
            if changed:
                self.ctx.save_config()
        out = self._config_snapshot()
        # The key is write-only: accepted above, never echoed back.
        out["api_key"] = "set" if out["api_key"] else ""
        result: dict[str, Any] = {"config": out}
        # Bodyless POST (the Configure… tab button): the core renderer
        # toasts `message` from the top level, so summarize the current
        # values there.
        if not body:
            result["message"] = (
                f"Config: base_url={out['base_url'] or '(empty)'}, "
                f"api_key={out['api_key']}, timeout={out['timeout']}s, "
                f"verify_ssl={out['verify_ssl']}, "
                f"poll_interval={out['poll_interval']}s — change via "
                f"POST /api/plugin/pelican-panel/config (see README)")
        return result, 200

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
        self.ctx.set_task_status(TASK_POWER, "running", "starting")
        self.ctx.run_task(TASK_POWER, lambda: self._run_power(signal, targets))
        return {"success": True, "started": True,
                "message": f"{SIGNAL_PROGRESS[signal].capitalize()} "
                           f"{len(targets)} server(s)"}, 202

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
                try:
                    self._request(
                        cfg, "POST",
                        f"/api/client/servers/{target['uuid']}/power",
                        {"signal": signal})
                    done += 1
                except PanelError as e:
                    failed += 1
                    log.warning("pelican-panel: %s failed for %s: %s (%s)",
                                signal, target["uuid"], e.kind, e.detail)
            verb = SIGNAL_DONE[signal]
            message = f"{done} server(s) {verb}"
            if failed:
                message += f", {failed} failed"
            kind = "success" if not failed else ("error" if not done else "warn")
            self._finish("done", message, message, kind)
        except Exception as e:               # never leave single-flight wedged
            log.exception("pelican-panel: power run crashed: %s", e)
            self._finish("error", "power run failed", "Power run failed", "error")

    def _finish(self, status: str, message: str, toast: str, kind: str) -> None:
        # Clear single-flight FIRST: a failing toast must not wedge the
        # plugin at 409 until it is reloaded.
        with self._lock:
            self._running = False
        try:
            self.ctx.set_task_status(TASK_POWER, status, message)
            self.ctx.toast(toast, kind)
        except Exception:
            log.exception("pelican-panel: reporting the power result failed")
