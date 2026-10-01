# Concept: Pi Hub Plugin — Pelican Panel

> Written for 1.x (Plugin API v1). 2.0.0 moved to API v2: frame tab,
> header pill, Settings card, Configure dialog — see README and CHANGELOG.
> The API facts below still hold.

**Status:** v2 — revised after Claude Code Opus 5 cross-check (22 findings,
all resolved; see §10). Ready for implementation.

## 1. Goal

A Pi Hub plugin that gives the dashboard a **Pelican Panel** (game-server
panel, Pterodactyl fork) tab with:

- a live table of all servers the configured API key can see
  (name, power state, CPU %, RAM used/limit, disk, primary IP:port)
- **start / stop / restart / kill** actions for the servers
- per-server IPs (primary allocation, incl. alias/port)

Target: `Valli-2020/pi-hub-plugin-pelican-panel`, installable through the
Pi Hub Plugin Store.

## 2. Verified API facts (pelican-dev/panel, main branch, checked 2026-08-19)

All control-plane endpoints live in the **Client API**, not the Application
API (`routes/api-application.php` has no power route — only
suspend/unsuspend/reinstall/transfer; verified in source).

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/client` | GET | Server list for the key's user; includes `relationships.allocations` per server (default include, permission-gated `allocation.read`) |
| `/api/client/servers/{uuid}` | GET | Server detail (fallback for missing allocations) |
| `/api/client/servers/{uuid}/resources` | GET | Power state + resources |
| `/api/client/servers/{uuid}/power` | POST | body `{"signal": "start\|stop\|restart\|kill"}`, 204 on success |

Auth: `Authorization: Bearer <client API key>` (Sanctum token; middleware
`RequireClientApiKey` rejects application keys). The key is created in the
Pelican user account (Account → API Credentials), NOT the admin
application keys.

**Every response is JSON:API-wrapped** — list =
`{"object":"list","data":[{"object":"server","attributes":{…}}],"meta":{"pagination":{…}}}`
and resources = `{"object":"stats","attributes":{…}}`. The plugin unwraps
`.data[].attributes` (and `relationships`) everywhere.

Required headers on every request: `Accept: application/json` (otherwise
Laravel returns HTML error pages), `Content-Type: application/json` on
POSTs.

Server-identifier route binding is **uuid or short id**; the plugin always
uses the full `uuid` from the list.

Important semantics:

- **Power state comes ONLY from `/resources`** (`current_state`:
  `starting/running/stopping/offline`; corrected 2026-10-01 from
  `stopped`, which Pelican's `ContainerStatus` enum does not have — see
  `app/Enums/ContainerStatus.php`; 1.x showed stopped servers as
  unavailable because of this). The list's `status` field is the
  *install/suspend* state (`null`, `installing`, `suspended`) — do NOT
  confuse the two. The list's `is_suspended`/`is_installing` flags gate
  what we show/allow.
- **`/resources` is cached ~20 s panel-side** (Pterodactyl heritage;
  `Cache::remember` TTL — re-verify against the live panel). After a
  power action the tab may show the old state for up to one cache window;
  document this in the README, it is not a bug.
- **`/resources` shape** (already unwrapped from `attributes`):
  `current_state`, `is_suspended`, `resources.{memory_bytes,
  cpu_absolute, disk_bytes, network_rx_bytes, network_tx_bytes, uptime}`.
  `cpu_absolute` is percent where **100 % = one core**.
- **Units**: `/resources` gives **bytes**; the list's `limits.{memory,
  disk, cpu}` are **MiB / percent-cap** with **`0` = unlimited**. Render
  used/limit with a `"–"` (or "unlimited") for 0 — never "1.2 / 0".
- **Allocations come in the server list** (`relationships.allocations`),
  `AllocationTransformer` shape: `id, ip, ip_alias, port, notes,
  is_default`. NO per-server detail fan-out needed. Fall back to
  `GET /servers/{uuid}` only when a server lacks the relationship
  (e.g. subuser key without `allocation.read`).
- **Pagination**: `/api/client` defaults to 50/page. Request
  `?per_page=100` and loop `meta.pagination.total_pages`. `?type=admin-all`
  lists ALL panel servers for a root-admin key, but resolves to an EMPTY
  list for ordinary user keys (no fallback to owned servers) — the
  plugin requests admin-all first and retries once without `type` when
  page 1 comes back empty (self-healing, Opus review F2).

## 3. Plugin architecture

Single plugin `pelican-panel` (stdlib only, like the reference plugin
`proxmox-update-all`):

```
pi_hub_plugins/pelican-panel/
├── __init__.py     # Plugin subclass: routes, UI, HTTP client, refresher
└── config.json     # created at load: {"base_url", "api_key", ...}
```

- `name = "pelican-panel"`, `min_core_version = "7.3.2"`,
  `capabilities = []` — the plugin only talks to the Pelican HTTP API via
  `urllib`; it needs **no** core capability (no SSH, no hosts, no
  proxmox). Least privilege.
- **Config** (`ctx.get_config()` / `save_config()`, atomic 0600 writes by
  the core — verified `PluginContext.get_config/save_config` in
  `pi_hub/plugins/base.py`):
  - `base_url` — e.g. `https://panel.example.com` (validated, see §6)
  - `api_key` — Pelican **client** API key (write-only: never echoed in
    payloads/logs; status payload shows `api_key: "set"`)
  - `timeout` (default 8 s), `verify_ssl` (default true), `poll_interval`
    (default 10 s, auto-scaled up with server count, see §4)
- **Routes** (all `caps=["admin"]`; namespace `/api/plugin/pelican-panel/`):
  - `GET /status` — returns the **current cached snapshot immediately**
    (never blocks on panel I/O): `{servers: [...], base_url, api_key:
    "set", verify_ssl, stale, age_seconds, notice?, task?}` — see §4/§5
    for the flat-payload contract. The failure hint is named `notice`,
    NOT `error`: the generic renderer short-circuits on a top-level
    `error` key and blanks the whole tab, contradicting "old table plus
    hint" (Opus review F1).
  - `POST /start-stopped` — bodyless; starts every stopped server
  - `POST /stop-running` — bodyless; stops every running server
  - `POST /restart-running` — bodyless; restarts every running server
  - `POST /kill-running` — bodyless; SIGKILL every running server
  - `POST /power` — **extra non-button route** (API/CLI/future UI):
    body `{"signal": "start"|"stop"|"restart"|"kill",
    "servers": ["uuid", ...] | "all"}` — same code path as the buttons
  - `POST /config` — persist config fields; `api_key` accepted but never
    returned (no core plugin-config UI exists — `CardUIDef` is not
    rendered by the core frontend; verified. README documents
    "edit config.json, then disable/enable the plugin" as the fallback)
- **UI** — one `TabUIDef` (id `pelican-panel`, label "Pelican servers"):
  - `poll_endpoint = /api/plugin/pelican-panel/status`
  - **actions** (each maps 1:1 to a bodyless route above):
    `start-stopped` ("Start stopped"), `stop-running` ("Stop running"),
    `restart-running` ("Restart running"), `kill-running` ("Kill running",
    style secondary). The generic renderer's action buttons POST to
    `/api/plugin/<name>/<action-id>` without a body — hence the
    bodyless routes in §3.

## 4. Poll behaviour: background refresher

The frontend polls `/status` every 10 s; the handler must never do panel
I/O itself (multi-tab browsers would multiply the fan-out, and a slow
panel would block the poll loop).

- **One background refresher thread** (daemon, `threading.Timer`-style
  loop, started in `load()`, stopped in `unload()`) runs every
  `poll_interval` seconds (default 10 s, **auto-scaled**:
  `poll_interval = max(10, ceil(servers / 5) * 10)` so ~35 servers →
  60 s and the ~240 req/min client-API rate limit is never saturated;
  ~6 req per poll = 1 list + N resources).
- Each refresh cycle:
  1. `GET /api/client?per_page=100&type=admin-all` (loop pages) — fresh
     list each cycle; gives add/remove/rename instantly.
  2. Parallel per-server `GET .../resources` via ONE long-lived
     `ThreadPoolExecutor` (created in `load()`, `max_workers =
     min(8, len(servers))`; never per-poll).
  3. Build the new snapshot and swap it atomically under a lock.
- **Failure handling**:
  - List fails → keep serving the last good snapshot with `stale: true`
    + `error` (generic message); the tab shows the old table plus hint.
  - 401/403 on the list → snapshot `error: "API key invalid"` (whole-tab
    hint) — the key is bad or expired.
  - Per-server resources fail/timeout → row values `"–"`, row status
    `"unavailable"`; never a failed poll.
  - **409** (installing/suspended server or unreachable node daemon) →
    row status `"unavailable"` (distinct from timeout).
  - **429** rate limit → honor `Retry-After` (or back off 60 s), surface
    `error: "rate limited"`, keep the old snapshot.
  - 3xx anywhere → treated as an error (no redirects, see §6).
- `age_seconds` = time since last successful refresh; `stale` = refresh
  failing.

## 5. `/status` payload contract (generic renderer)

The core renderer maps **flat scalar fields → rows** and **arrays of
dicts → tables**. Nested dicts render as junk. Contract:

- Top level: flat scalars only — `base_url`, `api_key` (`"set"`),
  `verify_ssl`, `stale`, `age_seconds`, `error` (optional), `task`
  (optional flat), `running` (bool, from the power task).
- `servers`: array of dicts. **Every row has the IDENTICAL key set**
  (missing keys shift columns), all values **pre-formatted strings or
  numbers** — `name`, `status` (`running/stopped/starting/stopping/
 unavailable/installing/suspended`), `cpu` (`"12%"`), `ram`
 (`"1.2 / 4.0 GiB"`, `"–"` on failure, `"1.2 GiB / ∞"` for unlimited
 limit), `disk` (same scheme), `ip` (`"10.0.0.5:25565"` or `ip_alias:
 port`), `uptime`. Failures render `"–"`, **never Python `None`**
 (renders as the literal string "None").
- The renderer escapes everything; the plugin supplies flat JSON only.

## 6. Security

- `api_key` lives only in the plugin `config.json` (core writes 0600,
  atomic tmp+fsync+replace — verified in `base.py`) — never in logs,
  never in status payloads (always `"set"`).
- `base_url` validated with `re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?")`
  — fullmatch (NOT `$`-anchored search, which would let a trailing
  newline through) and an explicit `\r`/`\n` rejection. **IPv6 literals
  (`https://[fd00::1]:8443`) and sub-path installs
  (`https://host/pelican`) are deliberately NOT supported** — rejected by
  the regex, documented in the README as known limitations (a panel
  behind a reverse proxy usually lives on the root path; IPv6 can be
  added later if needed).
- `signal` whitelisted (`start|stop|restart|kill`) — never interpolated.
- `servers` values matched against the freshly fetched uuid list; a
  stale/foreign uuid is dropped, never passed through.
- **Redirects are forbidden**: `urllib`'s default redirect handler would
  carry the `Authorization` header to the redirect target (key
  exfiltration). Install a `HTTPRedirectHandler` subclass whose
  `redirect_request` returns `None`; treat any 3xx as an error.
- `verify_ssl` opt-out uses a **per-request `ssl.SSLContext`** passed to
  `urlopen(context=…)`. NEVER mutate `ssl._create_default_https_context`
  — that would disable verification process-wide for the whole Pi Hub
  core.
- All HTTP via `urllib` with hard timeouts; errors masked in payloads
  (generic message), full detail only to the server log.
- Routes admin-only (`caps=["admin"]`); `verify_ssl` is the only
  deliberate downgrade, off by default.
- No HTML/JS in any payload — flat JSON only.

## 7. Task / single-flight

Power actions run in a background task (`ctx.run_task`, name `power`),
status pollable via `GET /api/task/plugin:pelican-panel:power` and
surfaced in the tab payload. Second power request while one runs → `409`.
Cancellation: check `thread_cancel()` between servers.

Core API usage verified against the local pi_hub core (7.6.0,
2026-08-19): `PluginContext.run_task`, `set_task_status`,
`get_task_status`, `toast`, `thread_cancel()` all exist in
`pi_hub/plugins/base.py`; task names auto-namespaced
`plugin:<name>:<task>`; `RouteDef.caps=["admin"]` semantics match the
reference plugin `proxmox-update-all`; `min_core_version = "7.3.2"` is
the plugin-tab-renderer floor used by the reference plugin.

## 8. Deliverables / repo layout

```
pi-hub-plugin-pelican-panel/          (new public repo, Valli-2020)
├── pi_hub_plugins/pelican-panel/
│   ├── __init__.py
│   └── config.json
├── pihub-plugin.json                 # store manifest (release asset)
├── README.md                         # what/why/install/usage/config
├── CHANGELOG.md                      # changelog-mandatory convention
├── LICENSE                           # MIT
├── .gitignore                        # __pycache__, *.pyc, *.tar.gz
└── CONCEPT.md                        # this document (kept for reference)
```

Release `v1.0.0` with both assets (`pihub-plugin.json`,
`pi-hub-plugin-pelican-panel-1.0.0.tar.gz`), changelog-mandatory release
notes, repo description + topics.

## 9. Verification plan

1. `python3 -m py_compile` on `__init__.py`.
2. Import test against a local fake Pelican HTTP server (stdlib
   `http.server`): list (JSON:API envelope, allocations, pagination) →
   resources → power flow, error paths (401, 409, 429, timeout, 3xx).
3. Flat-payload contract check: render the snapshot with the actual core
   renderer logic (`renderPluginTabData`) headlessly or by
   JSON-shape inspection (identical keys per row, no `None`).
4. Live smoke test against a real panel if one is reachable (none found
   in the homelab as of 2026-08-19) — otherwise documented as
   untested-against-live-panel.
5. Code review by Claude Code Opus 5 (read-only).
6. Store-ready check: both assets in release, semver tag, manifest fields
   match `Plugin` class fields.

## 10. Opus 5 cross-check resolution (2026-08-19)

Verdict: CORRECTED. 22 findings. Resolutions:

- F1 (buttons 404): bodyless routes `start-stopped/stop-running/
  restart-running/kill-running` registered; `/power` kept as body-capable
  extra route. → §3
- F2 (payload contract): flat scalars + identical-key row dicts,
  pre-formatted strings, no `None`. → §5
- F3 (no config UI): core has no plugin-config form (`CardUIDef` not
  rendered); `POST /config` + README "edit config.json, re-enable"
  fallback. → §3
- F4 (allocations in list): confirmed in source — `ServerTransformer`
  `defaultIncludes = ['allocations', 'variables']`; per-server detail
  fan-out dropped, detail fetch only as fallback. → §2, §4
- F5 (list `status` ≠ power state): power state from `/resources` only;
  list flags only gate skip/label. → §2
- F6 (JSON:API envelope): documented; unwrap `.attributes`. → §2
- F7 (pagination / admin-all): `per_page=100`, page loop, `type=admin-all`.
  → §2, §4
- F8 (units): bytes vs MiB, `cpu_absolute` 100 % = 1 core, `0` = unlimited
  sentinel rendered as `∞`. → §2, §5
- F9 (route binding): uuid or short id; plugin uses uuid. → §2
- F10 (20 s cache): softened to "~20 s (heritage, re-verify)"; README
  documents post-action cache window. → §2
- F11 (regex): `re.fullmatch` + CR/LF rejection; IPv6/sub-path rejected
  deliberately and documented. → §6
- F12 (redirect token leak): redirect handler returns `None`; 3xx =
  error. → §6
- F13 (verify_ssl): per-request `SSLContext`, never global. → §6
- F14 (headers): `Accept: application/json`, `Content-Type` on POSTs. → §2
- F15 (rate limit): 429 handling + `Retry-After` backoff + interval
  auto-scale with server count. → §4
- F16 (409 path): row `"unavailable"`; 401/403 whole-tab key hint. → §4
- F17 (sync poll): background refresher thread; `/status` serves snapshot
  with `stale`/`age_seconds`; one long-lived executor. → §4
- F18 (schema consistency): `stale`/`age_seconds` in §3 schema. → §3/§5
- F19 (core API): verified against `pi_hub/plugins/base.py` + reference
  plugin. → §7
- F20 (allocation cache): dropped with F4. → §4
- F21 (kill): `kill-running` action + route added. → §3
- F22 (cosmetic filtering): kept to cut request volume, not correctness.
  → §3

### Code-review round 2 (2026-08-19, implementation review) — CORRECTED,
11 findings, all fixed:

- R1 (High, error blanks tab): status payload uses `notice` not `error`.
- R2 (High, admin-all): retry once without `type` when page 1 empty.
- R3 (Medium, executor race): `unload()` under lock; `_ensure_executor`
  refuses after stop.
- R4 (Low, dead code): ROW_KEYS enforced as row-build invariant;
  redundant inert filter dropped.
- R5 (Medium, false-positive test): single-flight tested with a blocked
  power endpoint (real concurrency).
- R6 (Medium, serialized test server): `ThreadingHTTPServer`.
- R7 (Medium, error coverage): ratelimit/redirect/conflict/slow modes +
  admin-all fallback tests (41 checks).
- R8 (Low-Med, per-server 429): rate-limited fan-out sets backoff.
- R9 (Low, units): unlimited renders `"N GiB / ∞"`.
- R10 (Low, message): non-unreachable list errors → "panel error".
- R11 (Low, hygiene): unused test vars dropped; README grammar;
  `_BRIEF.md` gitignored; `age_seconds` on monotonic clock.
