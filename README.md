# nearby-events-mcp

[![tests](https://github.com/lmakinaa/nearby-events-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/lmakinaa/nearby-events-mcp/actions/workflows/tests.yml)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

Ask Claude what's happening near you, and get real answers.

```
You:    Any free networking events within 5 km tonight, after 6pm?
Claude: 3 events. Closest is 1.2 km away at 19:00, free, organizer has 4.6k followers…
```

An [MCP](https://modelcontextprotocol.io) server that gives Claude Desktop (or any MCP client) live, local event search: by day or date range, distance, price, category, format, time of day and a rough "size" signal. It reads the public event listings on Eventbrite.

> ## ⚠️ Disclaimer — read before using
>
> - **Unofficial.** This project is independent and is **not affiliated with, endorsed by or sponsored by Eventbrite**. "Eventbrite" is a trademark of its owner and is used here only to describe where the listings come from.
> - **It uses undocumented website endpoints, not an official API.** Eventbrite's [Terms of Service](https://www.eventbrite.com/help/en-us/articles/251210/eventbrite-terms-of-service/) prohibit scraping and automated data extraction from their sites. **Using this software may breach those terms.** You are responsible for how you use it and for complying with the terms and laws that apply to you.
> - **Personal, low-volume use only.** It is built for one person asking a few questions a day: requests are throttled, results are not stored, and nothing is redistributed. Don't use it for bulk collection, resale or commercial data products.
> - **It can break at any time.** The endpoints are not a public contract and may change or be blocked without notice.
> - **Rights holders:** if you'd like this project changed or taken down, please open an issue and it will be addressed promptly.
>
> Provided "as is", without warranty (see [LICENSE](LICENSE)).

## What it can do

| Tool | What it does |
|---|---|
| `search_events` | Events around a place or lat/lng for `today`, `tomorrow`, `this_weekend`, a date or a date range. Filters: radius, keywords, free/paid, min/max price, categories, formats, start time window, size, online, sold-out. Sort by distance, date, price or popularity. |
| `daily_events` | A day-by-day agenda for up to 14 days with the same filters. |
| `get_event` | Full details for one event: description, ticket tiers and prices, venue, organizer. |
| `list_categories` | Valid category and format names. |
| `resolve_location` | Turn a place name into coordinates (OpenStreetMap). |

Try asking:

- "What free networking events are near me today?"
- "Plan my weekend: 3 things within 5 km, under $30, after 6pm."
- "Small, indie food & drink events this week."
- "Tell me more about the second one."

## Install (Claude Desktop)

Needs Python 3.10+ (if you only have an older one, the installer fetches a private 3.12 for you via `uv`).

```bash
git clone https://github.com/lmakinaa/nearby-events-mcp.git
cd nearby-events-mcp
python3 scripts/install_claude_desktop.py --location "Boon Keng, Singapore" --radius-km 8
```

The installer creates `.venv`, installs the package, checks the server starts, and adds a `nearby-events` entry to your `claude_desktop_config.json` (backing up the old one). **Fully quit and reopen Claude Desktop.**

Options: `--lat 1.3197 --lng 103.8617` (exact point instead of a place name), `--base-url https://www.eventbrite.com` (other countries: `.co.uk`, `.ca`, `.com.au`…), `--use-browser` (see Troubleshooting), `--print-only`, `--uninstall`.

<details>
<summary>Manual setup / other MCP clients</summary>

Point your client at the launcher, which creates `.venv` on first run:

```json
{
  "mcpServers": {
    "nearby-events": {
      "command": "/ABSOLUTE/PATH/nearby-events-mcp/run_server.sh",
      "env": { "NEARBY_EVENTS_DEFAULT_LOCATION": "Boon Keng, Singapore" }
    }
  }
}
```

On Windows use `run_server.bat`. Always use absolute paths: desktop apps don't inherit your shell's `PATH`.
</details>

## Configuration

| Environment variable | Meaning |
|---|---|
| `NEARBY_EVENTS_DEFAULT_LOCATION` | Place used when you say "near me" (looked up once). |
| `NEARBY_EVENTS_DEFAULT_LAT` / `_LNG` | Exact default point (takes priority). |
| `NEARBY_EVENTS_DEFAULT_RADIUS_KM` | Default 10. |
| `NEARBY_EVENTS_BASE_URL` | Listing site for your country, default `https://www.eventbrite.sg`. |
| `NEARBY_EVENTS_MIN_INTERVAL` | Seconds between requests, default 0.5. Please don't lower it. |
| `NEARBY_EVENTS_USE_BROWSER` | `1` = start the session with headless Chromium (`pip install -e ".[browser]"` + `playwright install chromium`). |
| `NEARBY_EVENTS_CSRF_TOKEN`, `NEARBY_EVENTS_COOKIES` | Manual session fallback (see Troubleshooting). |

## How "size" works

Listings don't publish attendance, and capacity is almost always hidden. So `size` uses the **organizer's follower count** as a proxy: small < 200, medium 200–2,000, large > 2,000. `min_capacity` / `max_capacity` use ticket capacity when an organizer publishes it, and keep events where it's unknown.

## Troubleshooting

- **"Could not obtain a CSRF token"**: the site refused the plain HTTP session. Re-run the installer with `--use-browser`, or copy the `csrftoken` cookie from your browser into `--csrf-token`.
- **"No location found"**: use a shorter place ("Boon Keng, Singapore") or pass `--lat/--lng`.
- **Nothing shows up in Claude**: check the logs in `~/Library/Logs/Claude/mcp-server-nearby-events.log` (macOS) or `%APPDATA%\Claude\logs\` (Windows).

## How it works

`client.py` opens a normal web session, then calls the same JSON endpoints the site's own search page uses, one request at a time with a delay between requests. `search.py` turns your filters into a query, pages through results and applies the filters the site can't do itself (price range, time window, size). `normalize.py` turns each listing into a compact record for the model. Endpoint notes for contributors are in [docs/api-notes.md](docs/api-notes.md).

## Development

```bash
pip install -e ".[dev]"
pytest                    # no network needed: the site is mocked
npx @modelcontextprotocol/inspector python -m nearby_events_mcp
```

Issues and PRs welcome, especially for official event APIs as extra sources, other countries, and better size signals.

## License

[MIT](LICENSE) © Ismail Jaija
