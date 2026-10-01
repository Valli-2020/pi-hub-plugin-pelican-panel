# Changelog

All notable changes to this plugin are documented here. Releases follow
the repo-tag = version convention; each GitHub release ships the two
assets `pihub-plugin.json` and the versioned tarball.

## [Unreleased]

## [2.0.0] - 2026-10-01

Rewritten for Pi Hub's Plugin API v2. **Needs Pi Hub 8.0+**; Pi Hub asks
once to approve the new permissions (`ui.frame`, `ui.header`,
`ui.settings`). The existing `config.json` is kept.

### Added

- **Per-server control**: Start / Stop / Restart / Kill on each server,
  only the buttons that fit its state; Stop, Restart and Kill need a
  second click.
- **Live monitor**: the tab is a sandboxed frame with one card per
  server: CPU and RAM sparklines for the last hour, disk bar, network
  in/out, uptime, node and `ip:port`, plus running count and total
  CPU/RAM.
- **Header pill** `Games 2/3` with the panel's health as its colour.
- **Settings card** with connection status, Open panel and Refresh now.
- **Toast when a server stops without Pi Hub** (crash, in-game shutdown,
  panel UI); option `notify_crash`, on by default.
- Servers show `starting…` / `stopping…` until the panel's ~20 s cache
  catches up with a power action.
- `POST /refresh` and `POST /bulk/<action>` routes; `GET /monitor`
  returns raw numbers and sparkline points.

### Changed

- Settings live in Pi Hub's own Configure dialog (Settings → Plugins);
  the API key is a masked field, left blank keeps the stored key.
- The HTTP client moved to `client.py`; behaviour is unchanged (no
  redirects, per-request SSL context, masked errors).
- Bulk actions: Start stopped, Stop running, Restart running. Bulk
  **Kill running** is gone; Kill is per server only.

### Fixed

- **Stopped servers were shown as "unavailable" and "Start stopped" did
  nothing.** Pelican reports a stopped server as `offline` (its
  `ContainerStatus` has no `stopped`); 1.x only knew `stopped`. Rarer
  states (`exited`, `missing`, `restarting`, …) are mapped too.

### Removed

- `GET /status`, `POST /config` and the four `…-running` / `start-stopped`
  top-level routes (replaced by `/monitor`, the Configure dialog and
  `/bulk/<action>`).

## [1.2.0] - 2026-08-19

### Added

- **Configure… button opens a form dialog** on Pi Hub **7.7+** (core
  `ActionDef.fields`): edit `base_url`, `api_key`, `timeout`,
  `verify_ssl` and `poll_interval` directly in the dialog and save —
  values POST as JSON to the existing `/config` route. On older cores
  the button falls back to the v1.1.0 summary toast; the plugin keeps
  `min_core_version 7.3.2`.

## [1.1.0] - 2026-08-19

### Added

- **Configure… button** on the Pelican servers tab: shows the current
  configuration (base_url, api_key state, timeout, verify_ssl,
  poll_interval) in a toast; the same values are now rendered as rows in
  the tab payload so the active configuration is always visible.
- `POST /config` accepts bodyless requests (the tab button) and returns
  a summary `message` for the toast; with a body it persists fields as
  before (validation + clamping unchanged).

## [1.0.0] - 2026-08-19

### Added

- **Pelican servers tab**: live table with power state, CPU, RAM,
  disk (used/limit, `∞` for unlimited) and primary IP:port per server.
- **Power actions**: `start-stopped`, `stop-running`, `restart-running`
  and `kill-running` buttons; body-capable `POST /power` route for
  scripting (explicit server uuids or `all`).
- **Background refresher**: server list + parallel per-server resources
  through one long-lived executor; `/status` always returns the last
  snapshot immediately (`stale` / `age_seconds` markers).
- **Failure resilience**: 401/403 → "API key invalid" hint, 409 →
  row `unavailable`, 429 → `Retry-After` backoff, per-server failures →
  `–` values, list failure → stale snapshot + error.
- **Rate-limit aware polling**: refresh interval auto-scales with
  server count (never saturates the ~240 req/min client-API limit).
- **Config** (`POST /config` + `config.json`): `base_url`, `api_key`
  (write-only), `timeout`, `verify_ssl`, `poll_interval`.
- **Security**: redirects refused (no Authorization replay), base_url
  fullmatch validation (no CR/LF, no path/IPv6), per-request SSL context
  for `verify_ssl = false`, server uuids matched against the live list,
  `api_key` never echoed.
- **Tests**: `tests/test_fake_panel.py` — fake Pelican panel + fake
  plugin context covering snapshot build, row contract, power flow,
  single-flight, 401 and validation paths (32 checks).
