# nearby-events-mcp

[![tests](https://github.com/lmakinaa/nearby-events-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/lmakinaa/nearby-events-mcp/actions/workflows/tests.yml)
![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

Ask Claude what's happening near you, and get real answers.

```
You:    Any free networking events within 5 km tonight, after 6pm?
Claude: 3 events. Closest is 1.2 km away at 19:00, free, organizer has 4.6k followers…
```

An [MCP](https://modelcontextprotocol.io) server that gives Claude Desktop (or any MCP client) live, local event search: by day or date range, distance, price, category, format, time of day and a "size" signal. It reads the public event listings on **Eventbrite** and **Luma** (where much of the tech, AI, startup and community scene lists its events) and merges them; every result says which `source` it came from.

<img width="800" height="993" alt="Claude Desktop answering a nearby-events question with a ranked list of events" src="https://github.com/user-attachments/assets/82db6fd7-5f0e-4d2d-9683-7d5ffc210a33" />

## Why it's useful

Finding the right event usually means checking several sites, each with its own filters
and none covering everything, then cross-checking distance and time by hand. Luma, where
much of the tech, AI and startup scene posts events, has no date or radius search at all.
This server lets you ask one question in plain language and get a merged, filtered answer
from both sources, together with what each source couldn't tell you.

It is also a compact example of building a reliable agent tool on top of sources you
don't control:

- **Two sources, one schema.** Eventbrite and Luma listings normalise to the same compact record, and every result carries its `source`.
- **Filters the sites can't do are done server-side:** price ranges, time-of-day windows, radius and dates on Luma, and a per-source "size" signal.
- **Graceful degradation.** If one source is down or blocks the session, the other still answers, and the response says which one failed.
- **Honest data.** Approximate addresses, hidden guest lists and incomplete keyword coverage are reported to the model instead of hidden.
- **Polite by design.** Requests are throttled, nothing is stored, and CI runs fully mocked tests on every push.

> ## ⚠️ Disclaimer — read before using
>
> - **Unofficial.** This project is independent and is **not affiliated with, endorsed by or sponsored by Eventbrite or Luma**. "Eventbrite" and "Luma" are trademarks of their owners and are used here only to describe where the listings come from.
> - **It uses undocumented website endpoints, not an official API.** Eventbrite's [Terms of Service](https://www.eventbrite.com/help/en-us/articles/251210/eventbrite-terms-of-service/) prohibit scraping and automated data extraction from their sites. **Using this software may breach those terms.** You are responsible for how you use it and for complying with the terms and laws that apply to you.
> - **The same goes for Luma.** The Luma results come from the undocumented endpoints behind Luma's own discover pages (`api.lu.ma`), not from Luma's official API (which needs an API key and covers the calendars you manage, not event discovery). **Using this software may breach Luma's [Terms of Service](https://luma.com/terms).** Turn Luma off with `NEARBY_EVENTS_SOURCES=eventbrite` if you'd rather not use it.
> - **Personal, low-volume use only.** It is built for one person asking a few questions a day: requests are throttled, results are not stored, and nothing is redistributed. Don't use it for bulk collection, resale or commercial data products.
> - **It can break at any time.** The endpoints are not a public contract and may change or be blocked without notice. If one source breaks, the other keeps working and the answer says which one was unavailable.
> - **Rights holders:** if you'd like this project changed or taken down, please open an issue and it will be addressed promptly.
>
> Provided "as is", without warranty (see [LICENSE](LICENSE)).

## What it can do

| Tool | What it does |
|---|---|
| `search_events` | Events from Eventbrite and Luma around a place or lat/lng for `today`, `tomorrow`, `this_weekend`, a date or a date range. Filters: radius, keywords, free/paid, min/max price, categories, formats, start time window, size, online, sold-out, and `sources` (one or both). Sort by distance, date, price or popularity. |
| `daily_events` | A day-by-day agenda for up to 14 days with the same filters. |
| `get_event` | Full details for one event (Eventbrite or Luma id): description, ticket tiers and prices, venue, organizer or hosts. |
| `list_categories` | Valid category and format names, plus Luma's categories. |
| `resolve_location` | Turn a place name into coordinates (OpenStreetMap). |

Try asking:

- "What free networking events are near me today?"
- "Plan my weekend: 3 things within 5 km, under $30, after 6pm."
- "Small, indie food & drink events this week."
- "AI meetups near me tomorrow evening." (AI is a Luma category)
- "Tell me more about the second one."

## Install (Claude Desktop)

Needs Python 3.10+ (if you only have an older one, the installer fetches a private 3.12 for you via `uv`).

```bash
git clone https://github.com/lmakinaa/nearby-events-mcp.git
cd nearby-events-mcp
python3 scripts/install_claude_desktop.py --location "Boon Keng, Singapore" --radius-km 8
```

The installer creates `.venv`, installs the package, checks the server starts, and adds a `nearby-events` entry to your `claude_desktop_config.json` (backing up the old one). **Fully quit and reopen Claude Desktop.**

Options: `--lat 1.3197 --lng 103.8617` (exact point instead of a place name), `--base-url https://www.eventbrite.com` (other countries: `.co.uk`, `.ca`, `.com.au`…), `--sources eventbrite,luma` (which sources to search; default both), `--use-browser` (see Troubleshooting), `--print-only`, `--uninstall`.

## Use it with other AI apps (Cursor, VS Code, Claude Code…)

It's a standard MCP server, so any app that can launch local (stdio) MCP servers can use it. Only Claude Desktop is tested and set up by the installer; for the others, install once and paste a snippet.

**1. Install** (Python 3.10+):

```bash
git clone https://github.com/lmakinaa/nearby-events-mcp.git
cd nearby-events-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
echo "$PWD/.venv/bin/python"   # copy this path for the snippets below
```

On Windows: `py -m venv .venv` then `.venv\Scripts\pip install -e .`, and the path is `...\nearby-events-mcp\.venv\Scripts\python.exe`.

**2. Add it to your app.** Replace `/ABSOLUTE/PATH/...` with the path printed above and set your own location.

**Cursor**: `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{
  "mcpServers": {
    "nearby-events": {
      "command": "/ABSOLUTE/PATH/nearby-events-mcp/.venv/bin/python",
      "args": ["-m", "nearby_events_mcp"],
      "env": { "NEARBY_EVENTS_DEFAULT_LOCATION": "Boon Keng, Singapore" }
    }
  }
}
```

**VS Code (GitHub Copilot agent mode)**: `.vscode/mcp.json` in your workspace, or run **MCP: Open User Configuration** for all workspaces. Note the top-level key is `servers`:

```json
{
  "servers": {
    "nearby-events": {
      "type": "stdio",
      "command": "/ABSOLUTE/PATH/nearby-events-mcp/.venv/bin/python",
      "args": ["-m", "nearby_events_mcp"],
      "env": { "NEARBY_EVENTS_DEFAULT_LOCATION": "Boon Keng, Singapore" }
    }
  }
}
```

**Claude Code**: one command (`--scope user` makes it available in every project):

```bash
claude mcp add --transport stdio --scope user \
  --env NEARBY_EVENTS_DEFAULT_LOCATION="Boon Keng, Singapore" \
  nearby-events -- /ABSOLUTE/PATH/nearby-events-mcp/.venv/bin/python -m nearby_events_mcp
```

**Anything else** (Windsurf, Zed, Cline, Continue…): use the same three values in that app's MCP settings: command = the python path, args = `-m nearby_events_mcp`, env = your location.

Not supported yet: apps that only connect to MCP servers hosted on the internet (remote/HTTP), such as ChatGPT. This server runs locally on your machine.

<details>
<summary>Claude Desktop manual setup (without the installer)</summary>

Point Claude Desktop at the launcher, which creates `.venv` on first run:

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
| `NEARBY_EVENTS_SOURCES` | Sources searched by default, comma separated: `eventbrite`, `luma` (default both). |
| `NEARBY_EVENTS_BASE_URL` | Eventbrite site for your country, default `https://www.eventbrite.sg`. Luma needs no setting. |
| `NEARBY_EVENTS_MIN_INTERVAL` | Seconds between requests, default 0.5. Please don't lower it. |
| `NEARBY_EVENTS_USE_BROWSER` | `1` = start the session with headless Chromium (`pip install -e ".[browser]"` + `playwright install chromium`). |
| `NEARBY_EVENTS_CSRF_TOKEN`, `NEARBY_EVENTS_COOKIES` | Manual session fallback (see Troubleshooting). |

## How "size" works

Eventbrite doesn't publish attendance, and capacity is almost always hidden. So on Eventbrite `size` uses the **organizer's follower count** as a proxy: small < 200, medium 200–2,000, large > 2,000. `min_capacity` / `max_capacity` use ticket capacity when an organizer publishes it, and keep events where it's unknown.

Luma does publish the **number of people registered** (`attendees` in results), so on Luma `size` uses that: small < 30, medium 30–150, large 150+. `min_followers` / `max_followers` only apply to Eventbrite. Sorting by popularity ranks each source by its own signal and interleaves them.

## Luma specifics

- Luma's feed has no date filter, so the server works out the dates (e.g. `this_weekend` = Fri–Sun in the search area's timezone, as on Eventbrite), starts Luma's feed at the first day and stops once events pass the last day. Free/paid is filtered by Luma; price ranges, radius and time of day are checked by the server.
- Keyword (`query`) searches on Luma only see Luma's top 50 matches worldwide, so Luma results for dates further ahead can be incomplete (the results say so).
- Hosts can hide their guest list; Luma then reports 0 guests, so those events have no `attendees` count and are left out when filtering by `size`.
- Some Luma hosts hide the exact address until you register: those events show only the area (e.g. "Ang Mo Kio, Singapore (exact address for guests only)") and their distance is approximate.
- Luma has its own categories (Tech, AI, Crypto, Climate, Food & Drink, Arts & Culture, Fitness, Running, Wellness, Family, Books, Games). Eventbrite names map to the closest one (Science & Tech → Tech); a name only one source knows (Music, AI) searches that source only. `formats` is Eventbrite-only.

## Troubleshooting

- **"Could not obtain a CSRF token"**: the site refused the plain HTTP session. Re-run the installer with `--use-browser`, or copy the `csrftoken` cookie from your browser into `--csrf-token`.
- **"No location found"**: use a shorter place ("Boon Keng, Singapore") or pass `--lat/--lng`.
- **Nothing shows up in Claude**: check the logs in `~/Library/Logs/Claude/mcp-server-nearby-events.log` (macOS) or `%APPDATA%\Claude\logs\` (Windows).

## How it works

`client.py` opens a normal web session on Eventbrite and `luma.py` reads Luma's discover feed, each calling the same JSON endpoints the site's own pages use, one request at a time with a delay between requests. `search.py` turns your filters into a query per source, runs the sources side by side, pages through results, applies the filters the sites can't do themselves (price range, time window, size, and dates on Luma) and merges the results. `normalize.py` and `luma.py` turn each listing into the same compact record for the model. Endpoint notes for contributors are in [docs/api-notes.md](docs/api-notes.md).

## Development

```bash
pip install -e ".[dev]"
pytest                    # no network needed: both sites are mocked
npx @modelcontextprotocol/inspector python -m nearby_events_mcp
```

Issues and PRs welcome, especially for official event APIs as extra sources, other countries, and better size signals.

## License

[MIT](LICENSE) © Ismail Jaija
