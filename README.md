# Pi Hub Plugin: Pelican Panel

Live Pelican Panel server table (power state, CPU, RAM, disk, IP) with
start / stop / restart / kill actions — straight from the Pi Hub
dashboard.

## What it does

- Lists every server the configured API key can see, refreshed in the
  background: **power state** (`running` / `stopped` / `starting` /
  `stopping`), **CPU** (`cpu_absolute`, 100 % = one core), **RAM** and
  **disk** used/limit (bytes vs. MiB converted; `0` limit renders as
  `∞`), **primary IP:port** (prefers the allocation alias), and uptime.
- **Start stopped** / **Stop running** / **Restart running** / **Kill
  running** buttons — global actions over the matching servers.
- Installing/suspended servers are shown but never targeted by power
  actions.
- Rate-limit aware: the refresh interval auto-scales with server count,
  429s back off and surface as an error, failures never blank the table
  (last good snapshot + `stale` marker).

## Install

1. Open **Settings → Plugins** in Pi Hub (needs an admin account).
2. Add the repository:

   ```
   https://github.com/Valli-2020/pi-hub-plugin-pelican-panel
   ```

3. Click **Scan** — the plugin appears in the list.
4. Click **Install**, then **Enable**.

The sidebar now shows a **Pelican servers** tab.

> Requires Pi Hub **7.3.2+** (plugin-tab UI renderer).

## Requirements

- A **Pelican client API key** created in the *user* account
  (Account → API Credentials) — NOT an admin Application API key. The
  panel rejects application keys on the client API. The key sees exactly
  the servers the account can access; a root-admin key with
  `?type=admin-all` sees all panel servers.
- The panel reachable from the Pi (HTTPS by default; self-signed certs
  work via `verify_ssl = false`).

## Configuration

Options are persisted in the plugin's `config.json` (via `POST /config`
or by editing the file — the Pi Hub core has no plugin config form yet,
so after editing the file manually, disable and re-enable the plugin):

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `""` | Panel origin, e.g. `https://panel.example.com` — `http(s)://host[:port]` only (no path, no IPv6 literal) |
| `api_key` | `""` | Pelican **client** API key (write-only; never shown again) |
| `timeout` | `8` | Seconds per panel request (1–120) |
| `verify_ssl` | `true` | Set `false` for self-signed panel certificates (per-request SSL context only) |
| `poll_interval` | `10` | Base refresh seconds (5–3600; auto-scaled up with server count) |

Example via curl on the Pi:

```bash
curl -X POST http://raspberrypi:8898/api/plugin/pelican-panel/config \
  -H "Authorization: Bearer <pi-hub-token>" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "https://panel.example.com", "api_key": "plc_...", "verify_ssl": false}'
```

## Usage

| Action | Effect |
|---|---|
| **Start stopped** | Sends `start` to every stopped server |
| **Stop running** | Sends `stop` to every running server |
| **Restart running** | Sends `restart` to every running server |
| **Kill running** | Sends `kill` (SIGKILL) to every running server |

Power actions run as a background task (single-flight — a second action
while one runs is refused with `409`) and end with a toast. The tab is
polled every 10 s; the table may lag a power action by one panel-side
cache window — the Pelican panel caches `/resources` for ~20 s, so a
just-started server can still show `stopped` for a few seconds. That is
the panel's cache, not a bug.

## API (for scripting)

- `GET /api/plugin/pelican-panel/status` — current snapshot
  (`servers[]`, flat scalars, `stale`, `age_seconds`, `notice` on
  transient failure; the table and buttons stay visible).
- `POST /api/plugin/pelican-panel/power` — body
  `{"signal": "start"|"stop"|"restart"|"kill", "servers": ["uuid", ...] | "all"}`.
- `POST /api/plugin/pelican-panel/config` — set config fields.

All routes are admin-only. The `api_key` is never returned by any
endpoint.

## Limitations

- **Global actions only.** The Pi Hub generic plugin-tab renderer has no
  per-row buttons, so power actions address all matching servers. The
  `/power` route accepts explicit server uuids for future UIs / scripts.
- `base_url` does not accept IPv6 literals or sub-path installs
  (`https://host/pelican`) — reverse-proxy to the root path or use a
  dedicated hostname.
- `kill` is SIGKILL — no graceful shutdown, no save games flushed.
- Redirects (3xx) from the panel are treated as errors and never
  followed (the Authorization header must not be replayed against
  another host).

## License

MIT
