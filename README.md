# Pi Hub Plugin: Pelican Panel

Your Pelican game servers inside the Pi Hub dashboard: a small live
monitor with CPU/RAM sparklines and per-server start / stop / restart /
kill, a header pill and a Settings card.

## What it does

- **Game servers tab** (a sandboxed frame): one card per server with a
  status dot, node, `ip:port`, uptime, **CPU and RAM sparklines for the
  last hour** (30 s buckets, gaps while the server is off), a disk bar,
  network in/out and the buttons that fit its state:
  - offline: **Start**
  - running: **Stop**, **Restart**, **Kill**
  - starting: **Stop**, **Kill**; stopping: **Kill**
  - installing / suspended: greyed out, no buttons

  Stop, Restart and Kill need a second click within 3 s (the sandbox
  has no confirm dialog). Above the cards: running count, total CPU and
  RAM, and the bulk actions **Start stopped**, **Stop running**,
  **Restart running** and **Refresh**.
- **Header pill** `Games 2/3` (running / total), next to the health
  chips: green when servers run, grey when none run or nothing is set
  up, amber or red when the panel cannot be polled.
- **Settings card** "Pelican Panel": panel URL, key set yes/no, server
  count, last poll, status, **Open panel** and **Refresh now**.
- **Toast when a server stops without Pi Hub** (crash, in-game
  shutdown, someone used the panel): running → offline without a stop,
  restart or kill sent from Pi Hub in the last 3 minutes. Can be
  switched off.
- Rate-limit aware: the poll interval scales with the server count, 429
  backs off, failures keep the last good data and mark it stale.

## Install

1. Open **Settings → Plugins** in Pi Hub (admin account).
2. Add the repository `https://github.com/Valli-2020/pi-hub-plugin-pelican-panel`,
   click **Scan**, then **Install** and **Enable**.
3. Approve the permissions: `ui.frame` (run the tab's own JavaScript in a
   sandboxed frame, flagged high-risk by Pi Hub), `ui.header` (the pill)
   and `ui.settings` (the card). The plugin needs no system capability:
   no SSH, no hosts, no Proxmox.
4. **Configure** (Settings → Plugins → pelican-panel): panel URL and API key.

> Requires Pi Hub **8.0+** (Plugin API v2). Upgrading from 1.x keeps the
> existing `config.json`; Pi Hub asks once to approve the new permissions.

## API key

Use a **client API key**: in the panel, **Account → API Keys**. Client keys
have no read/write scopes, they act as your user. A root-admin account's
key sees every server (`?type=admin-all`); a normal account's key sees
the servers that account can access. Set *Allowed IPs* on the key to the
address the Pi reaches the panel from.

An **application** key (Admin → API Keys, per-resource read / read-write)
is *not* used: power control only exists in the client API, and the
panel rejects application keys there.

## Configuration

| Field | Default | Meaning |
|---|---|---|
| Panel URL | — | `http(s)://host[:port]`, no path, no IPv6 literal |
| Client API key | — | write-only; left blank on save keeps the current key |
| Verify SSL | on | off for self-signed panel certificates (per-request SSL context only) |
| Timeout (s) | 8 | per panel request, 1–120 |
| Poll interval (s) | 10 | 5–3600, scaled up with the server count |
| Toast when a server stops without Pi Hub | on | see above |

## Notes

- The panel caches `/resources` for ~20 s, so a power action shows up a
  little late. Until the state changes (at most 60 s) the card says
  `starting…`, `stopping…` and so on.
- Power actions run as a single background task: a second action while
  one runs gets `409`. Each run ends with a toast.
- `kill` is SIGKILL: no graceful shutdown, nothing saved.
- With `/?noframes=1` the tab stays empty (frames are off); the pill and
  the Settings card keep working.
- Everything is admin-only. Viewers see "Admins only" in the tab and no pill.

## API (for scripting)

All routes are admin-only; the API key is never returned.

- `GET /api/plugin/pelican-panel/monitor` — servers (raw numbers: bytes,
  CPU %, seconds; `null` = unknown), sparkline points `cpu_pts` (%) and
  `mem_pts` (MiB), `summary`, `stale`, `age_seconds`, `notice`, `task`.
- `POST /api/plugin/pelican-panel/power` —
  `{"signal": "start"|"stop"|"restart"|"kill", "servers": ["uuid", ...] | "all"}`.
- `POST /api/plugin/pelican-panel/bulk/<action>` — `start-stopped`,
  `stop-running`, `restart-running`.
- `POST /api/plugin/pelican-panel/refresh` — poll now.

## Limitations

- No IPv6 literals or sub-path installs (`https://host/pelican`) in the
  panel URL.
- Redirects from the panel are errors and never followed (the key must
  not be replayed against another host).
- History lives in memory: it starts empty after a Pi Hub restart.

## Development

```bash
PYTHONPATH=/path/to/pi-hub python3 tests/test_fake_panel.py
```

Runs a fake Pelican panel and checks the snapshot, `/monitor`, power,
bulk, single-flight, stop detection, error paths, and the pill / card
nodes through Pi Hub's own validator.

## License

MIT
