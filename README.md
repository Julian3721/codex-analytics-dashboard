# Codex Analytics Dashboard

Local-first analytics dashboard for Codex session logs. It reads your own Codex data from your machine, reconstructs daily token usage, sessions, message events, model mix, output ratio, heatmaps, top sessions, top projects, and what-if API cost estimates, then renders a self-contained HTML dashboard.

No data is uploaded. The dashboard is generated locally from your local Codex files.

## Requirements

- Node.js 18 or newer for the `npx` launcher.
- Python 3.10 or newer for the dashboard generator.
- Local Codex session data in `~/.codex`, or a custom path passed with `--codex-home`.

## Quick Start

```bash
npx codex-analytics-dashboard@latest
```

The `npx` launcher starts a localhost dashboard server, opens the dashboard in your browser, and writes generated files to a user-local application data directory. Browser refresh regenerates the dashboard from the latest local logs while the server is running.

Install it globally if you prefer a reusable command:

```bash
npm install -g codex-analytics-dashboard
codex-analytics-dashboard
```

You can also run the Python generator directly:

```bash
python3 codex_usage_dashboard.py --serve
```

## Data Sources

By default, the dashboard reads:

- `~/.codex/sessions`
- `~/.codex/archived_sessions`
- `~/.codex/state_5.sqlite`

Override the Codex data directory when needed:

```bash
npx codex-analytics-dashboard@latest -- --codex-home ~/.codex
python3 codex_usage_dashboard.py --codex-home ~/.codex --serve
```

## Useful Options

```bash
npx codex-analytics-dashboard@latest -- --timezone Europe/Berlin
npx codex-analytics-dashboard@latest -- --no-open
npx codex-analytics-dashboard@latest -- --redact
npx codex-analytics-dashboard@latest -- --snapshot-dir ~/Dropbox --device-name "Work MacBook"
python3 codex_usage_dashboard.py --out ~/Desktop/codex_analytics_dashboard.html
python3 codex_usage_dashboard.py --serve --port 8765
python3 codex_usage_dashboard.py --serve --no-open
python3 codex_usage_dashboard.py --no-json
```

`--redact` also works as `--privacy`. It masks session titles, thread IDs, local paths, and source metadata in the generated dashboard output. Use it when creating screenshots or a shareable local export.

## Multi-Device Snapshots

Do not put your full `~/.codex` directory in Dropbox, iCloud, Syncthing, or any other shared folder. It can contain private prompts, responses, local paths, auth/config files, and raw session logs.

Use a synced parent directory instead. The dashboard creates a `Codex Analytics` folder inside it automatically:

```bash
npx codex-analytics-dashboard@latest -- --snapshot-dir ~/Dropbox --device-name "Work Windows"
```

Run the same setup once on each device, pointing all devices at the same synced folder and giving each device a clear name:

```bash
npx codex-analytics-dashboard@latest -- --snapshot-dir ~/Dropbox --device-name "Personal MacBook"
```

The snapshot path is saved in the user-local app config, so future dashboard launches update the same folder automatically. Each launch or localhost refresh reads the local `~/.codex`, writes a reduced snapshot for the current device, then aggregates all snapshots found in:

```text
Dropbox/
  Codex Analytics/
    work-windows/
      device.json
      snapshot.json
    personal-macbook/
      device.json
      snapshot.json
```

If you pass a folder already named `Codex Analytics`, `CodexAnalytics`, or `codex-analytics`, that folder is used directly instead of creating another nested folder.

Snapshots include dashboard-level analytics only: token/time series, model usage, cost estimates, message/session counts, session titles, and project names. They do not include prompts, responses, tool output, raw rollout logs, SQLite databases, source metadata, full filesystem paths, or auth/config files. Project names are reduced to the final folder name, such as `codex-analytics-dashboard`.

The dashboard defaults to **All devices** and includes a device selector in the header so you can filter the full view down to one device.

## Outputs

The Python defaults write to the current directory:

- `codex_analytics_dashboard.html` - interactive dashboard.
- `codex_analytics_data.json` - machine-readable export.

The `npx` launcher writes to a user-local application data directory:

- macOS: `~/Library/Application Support/Codex Analytics Dashboard`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/codex-analytics-dashboard`
- Windows: `%LOCALAPPDATA%/Codex Analytics Dashboard`

Opening `codex_analytics_dashboard.html` directly as a `file://` page shows a static snapshot. Use `--serve` when you want browser refresh to update data.

## Privacy

Generated dashboard files can contain private local usage data: session titles, thread IDs, model usage, timestamps, local filesystem paths, and project names. Keep generated HTML/JSON files out of commits, issues, screenshots, and public releases unless you intentionally generated them with `--redact` and reviewed the result.

This project is a local analysis tool, not a billing mirror. The cost view is a what-if estimate using public OpenAI API token prices. It is not ChatGPT or Codex subscription billing.

## Development

```bash
python3 -m py_compile codex_usage_dashboard.py
python3 -m unittest
node --check bin/codex-analytics-dashboard.js
npm pack --dry-run
```

To verify empty-state behavior:

```bash
python3 codex_usage_dashboard.py --codex-home /tmp/empty-codex-home --out /tmp/codex_analytics_dashboard_empty.html --json-out /tmp/codex_analytics_data_empty.json --timezone UTC
```

## License

MIT
