# Changelog

All notable changes to this plugin are documented here. Releases follow
the repo-tag = version convention; each GitHub release ships the two
assets `pihub-plugin.json` and the versioned tarball.

## [Unreleased]

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
