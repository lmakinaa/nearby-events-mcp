"""FastMCP server exposing Eventbrite and Luma event discovery to Claude Desktop (stdio transport)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import EventsClient, EventsError
from .config import Settings
from .constants import CATEGORIES, FORMATS, LUMA_CATEGORIES
from .geo import geocode
from .luma import LumaClient, get_event_details, local_today
from .normalize import html_to_text, normalize_ticket_class
from .search import (SearchParams, clean_event, luma_events, merge_events, plan_sources, search_eventbrite,
                     search_sources, validate)

log = logging.getLogger("nearby_events_mcp")

mcp = FastMCP(
    "nearby-events",
    instructions=(
        "Find events near a location from Eventbrite and Luma (each result has a `source`). Use "
        "search_events for a single day/range, daily_events for a per-day agenda, get_event for full "
        "details of one event (Eventbrite numeric id or Luma evt-... id). Location comes from lat/lng or "
        "a place name, else the user's configured default. Size on Eventbrite is only an "
        "organizer-followers proxy, on Luma it is the real RSVP count; say so when the user filters by "
        "size. Some Luma addresses are approximate until you register. If a source is unavailable the "
        "result says so in `notes`; mention it."
    ),
)

_settings: Settings | None = None
_client: EventsClient | None = None
_luma: LumaClient | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def client() -> EventsClient:
    global _client
    if _client is None:
        _client = EventsClient(settings())
    return _client


def luma_client() -> LumaClient:
    global _luma
    if _luma is None:
        _luma = LumaClient(settings())
    return _luma


def clients() -> dict[str, object]:
    return {"eventbrite": client(), "luma": luma_client()}


def _utcnow() -> datetime:  # patched in tests
    return datetime.now(timezone.utc)


def pick_sources(sources: list[str] | None) -> list[str]:
    return list(dict.fromkeys(sources)) if sources else list(settings().sources)


async def resolve_origin(lat: float | None, lng: float | None, location: str | None) -> tuple[float, float, str]:
    s = settings()
    if lat is not None and lng is not None:
        return lat, lng, f"{lat:.4f},{lng:.4f}"
    if location:
        return await geocode(location)
    if s.default_lat is not None and s.default_lng is not None:
        return s.default_lat, s.default_lng, "default location"
    if s.default_location:
        return await geocode(s.default_location)
    raise EventsError(
        "No location given. Pass lat+lng or a location name (e.g. 'Orchard Road, Singapore'), or set "
        "NEARBY_EVENTS_DEFAULT_LOCATION / NEARBY_EVENTS_DEFAULT_LAT+LNG in the server config.")


Price = Literal["any", "free", "paid"]
Sort = Literal["distance", "date", "price", "popularity"]
Size = Literal["small", "medium", "large"]
Source = Literal["eventbrite", "luma"]
SIZE_HELP = ("small, medium or large. Eventbrite: PROXY by organizer followers (<200, 200-2000, >2000). "
             "Luma: real RSVP count (<30, 30-150, 150+)")
SOURCES_HELP = "Which sources to search: eventbrite, luma (default: all configured)"


@mcp.tool()
async def search_events(
    date: Annotated[str, Field(description="today, tomorrow, this_week, this_weekend (Fri-Sun), this_month or YYYY-MM-DD")] = "today",
    date_to: Annotated[str | None, Field(description="Inclusive range end YYYY-MM-DD (then `date` must be YYYY-MM-DD)")] = None,
    location: Annotated[str | None, Field(description="Place name to search around, e.g. 'Marina Bay, Singapore'. Ignored if lat/lng given")] = None,
    lat: Annotated[float | None, Field(description="Latitude of search centre")] = None,
    lng: Annotated[float | None, Field(description="Longitude of search centre")] = None,
    radius_km: Annotated[float, Field(description="Search radius in km (rounded to whole km, min 1)", ge=1, le=200)] = 10,
    query: Annotated[str | None, Field(description="Free-text keywords, e.g. 'startup networking'")] = None,
    price: Annotated[Price, Field(description="any, free or paid")] = "any",
    max_price: Annotated[float | None, Field(description="Cheapest ticket must cost at most this (event currency; free events pass)")] = None,
    min_price: Annotated[float | None, Field(description="Most expensive ticket must cost at least this")] = None,
    categories: Annotated[list[str] | None, Field(description="Category names or ids, OR-ed. e.g. ['Business','Music','AI']. See list_categories; each source uses the ones it knows")] = None,
    formats: Annotated[list[str] | None, Field(description="Format names or ids, OR-ed. e.g. ['Networking','Conference']. Eventbrite only (Luma is skipped)")] = None,
    size: Annotated[Size | None, Field(description=SIZE_HELP)] = None,
    min_followers: Annotated[int | None, Field(description="Minimum organizer followers (Eventbrite size proxy; Luma events unaffected)")] = None,
    max_followers: Annotated[int | None, Field(description="Maximum organizer followers (Eventbrite size proxy; Luma events unaffected)")] = None,
    min_capacity: Annotated[int | None, Field(description="Min total ticket capacity (Eventbrite); only known for a minority of events, unknown are kept. Triggers extra requests")] = None,
    max_capacity: Annotated[int | None, Field(description="Max total ticket capacity; see min_capacity")] = None,
    include_capacity: Annotated[bool, Field(description="Look up ticket capacity for each result (slower)")] = False,
    start_after: Annotated[str | None, Field(description="Local start time not before HH:MM (24h)")] = None,
    start_before: Annotated[str | None, Field(description="Local start time not after HH:MM (24h)")] = None,
    include_online: Annotated[bool, Field(description="Include online events")] = False,
    available_only: Annotated[bool, Field(description="Hide sold-out / sales-ended events (default on)")] = True,
    include_ended: Annotated[bool, Field(description="Include events that already finished today")] = False,
    sort: Annotated[Sort, Field(description="distance, date, price or popularity")] = "distance",
    max_results: Annotated[int, Field(description="Max events returned", ge=1, le=100)] = 20,
    sources: Annotated[list[Source] | None, Field(description=SOURCES_HELP)] = None,
) -> dict:
    """Search Eventbrite and Luma events around a location with filters for date, price, category,
    format, time of day and size. Returns compact JSON: events with source, venue, distance, price,
    organizer (Luma events also carry `attendees`), plus `notes` and per-source counts."""
    la, ln, label = await resolve_origin(lat, lng, location)
    p = SearchParams(
        date=date, date_to=date_to, query=query, radius_km=radius_km, price=price,
        categories=categories or [], formats=formats or [], min_price=min_price, max_price=max_price,
        size=size, min_followers=min_followers, max_followers=max_followers,
        include_capacity=include_capacity, min_capacity=min_capacity, max_capacity=max_capacity,
        start_after=start_after, start_before=start_before, include_online=include_online,
        available_only=available_only, include_ended=include_ended, sort=sort, max_results=max_results,
    )
    result = await search_sources(clients(), p, (la, ln), pick_sources(sources))
    return {"origin": {"lat": la, "lng": ln, "label": label, "radius_km": round(radius_km)}, **result}


@mcp.tool()
async def daily_events(
    days: Annotated[int, Field(description="Number of consecutive days", ge=1, le=14)] = 3,
    start_date: Annotated[str | None, Field(description="First day YYYY-MM-DD (default: today)")] = None,
    location: Annotated[str | None, Field(description="Place name to search around")] = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: Annotated[float, Field(ge=1, le=200)] = 10,
    query: str | None = None,
    price: Price = "any",
    max_price: float | None = None,
    categories: list[str] | None = None,
    formats: list[str] | None = None,
    size: Annotated[Size | None, Field(description=SIZE_HELP)] = None,
    start_after: str | None = None,
    start_before: str | None = None,
    include_online: bool = False,
    available_only: bool = True,
    max_per_day: Annotated[int, Field(ge=1, le=50)] = 10,
    sources: Annotated[list[Source] | None, Field(description=SOURCES_HELP)] = None,
) -> dict:
    """Day-by-day agenda of nearby events, same filters as search_events. Eventbrite is searched once
    per day; Luma once for the whole range. Results per day are sorted by start time. Without
    start_date the first day is today in the search area's timezone."""
    la, ln, label = await resolve_origin(lat, lng, location)
    base = SearchParams(
        date=start_date or date.today().isoformat(), query=query, radius_km=radius_km, price=price, max_price=max_price,
        categories=categories or [], formats=formats or [], size=size, start_after=start_after,
        start_before=start_before, include_online=include_online, available_only=available_only,
        sort="date", max_results=max_per_day,
    )
    validate(base)
    plans, notes = plan_sources(base, pick_sources(sources))

    # The first day: start_date, else "today" where the search is. Luma's first page reveals the
    # area's timezone, so Eventbrite waits for it (or for Luma to fail) before building its days.
    first_day: asyncio.Future[date] = asyncio.get_running_loop().create_future()
    if start_date:
        first_day.set_result(date.fromisoformat(start_date))
    elif "luma" not in plans:
        first_day.set_result(date.today())
    now = _utcnow()

    def span(first: date) -> list[str]:
        return [(first + timedelta(days=i)).isoformat() for i in range(days)]

    async def eventbrite_days() -> dict[str, dict]:
        out: dict[str, dict] = {}
        for day in span(await first_day):
            out[day] = await search_eventbrite(client(), dataclasses.replace(plans["eventbrite"]["p"], date=day),
                                               (la, ln))
        return out

    def luma_window(tz_name: str | None) -> tuple[date, date]:
        first = date.fromisoformat(start_date) if start_date else local_today(now, tz_name)
        if not first_day.done():
            first_day.set_result(first)
        return first, first + timedelta(days=days - 1)

    async def luma_range() -> tuple[list[dict], bool, list[str]]:
        lp = dataclasses.replace(plans["luma"]["p"], max_results=max_per_day * days,
                                 max_pages=min(15, 4 + days))
        try:
            kept, _, more, lnotes = await luma_events(luma_client(), lp, (la, ln), plans["luma"]["slugs"],
                                                      now=now, early_stop=False, window=luma_window)
        finally:
            if not first_day.done():  # Luma failed before its first page
                first_day.set_result(date.today())
        return kept, more, lnotes

    jobs = {}
    if "eventbrite" in plans:
        jobs["eventbrite"] = eventbrite_days()
    if "luma" in plans:
        jobs["luma"] = luma_range()
    results = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
    failed = {k: v for k, v in results.items() if isinstance(v, Exception)}
    for err in failed.values():
        if isinstance(err, ValueError):
            raise err
    if jobs and len(failed) == len(jobs):
        raise EventsError("All event sources failed. " + " | ".join(f"{k} unavailable: {v}" for k, v in failed.items()))
    notes.extend(f"{k} unavailable: {v}" for k, v in failed.items())

    ok = {k: v for k, v in results.items() if k not in failed}
    eb_days: dict[str, dict] = ok.get("eventbrite") or {}  # type: ignore[assignment]
    luma_kept, luma_more, luma_notes = ok.get("luma") or ([], False, [])  # type: ignore[misc]
    notes.extend(n for n in luma_notes if n not in notes)
    for res in eb_days.values():
        notes.extend(n for n in res["notes"] if n not in notes)
    buckets: dict[str, list[dict]] = {}
    day_list = span(first_day.result())
    for ev in luma_kept:
        # Multi-day events already running on the first day go under that day.
        buckets.setdefault(max((ev["start"] or "")[:10], day_list[0]), []).append(clean_event(ev))
    last_luma_day = max(buckets) if buckets else day_list[0]

    agenda: dict[str, dict] = {}
    for day in day_list:
        eb = eb_days.get(day) or {"events": [], "more_available": False}
        merged, _ = merge_events([eb["events"], buckets.get(day, [])], base)
        out = merged[:max_per_day]
        more = eb["more_available"] or len(merged) > len(out) or (luma_more and day >= last_luma_day)
        agenda[day] = {"count": len(out), "more_available": more, "events": out}
    if luma_more:
        notes.append(f"Luma results stop at {last_luma_day}; later days may be missing Luma events.")
    return {"origin": {"lat": la, "lng": ln, "label": label, "radius_km": round(radius_km)},
            "notes": notes, "days": agenda}


@mcp.tool()
async def get_event(
    event_id: Annotated[str, Field(description="The `id` from search results: Eventbrite numeric id or Luma evt-... id")],
    include_description: bool = True,
    include_tickets: bool = True,
) -> dict:
    """Full details for one event: times, description text, ticket tiers with prices and capacity."""
    if event_id.strip().startswith("evt-"):
        return await get_event_details(luma_client(), event_id.strip(), include_description, include_tickets)

    c = client()
    ev = await c.get_event(event_id)
    tasks = {}
    if include_description:
        tasks["desc"] = c.get_event_description(event_id)
    if include_tickets:
        tasks["tickets"] = c.get_ticket_classes(event_id)
    if ev.get("venue_id"):
        tasks["venue"] = c.get_venue(ev["venue_id"])
    if ev.get("organizer_id"):
        tasks["org"] = c.get_organizer(ev["organizer_id"])
    results = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True)))
    ok = {k: v for k, v in results.items() if not isinstance(v, Exception)}

    venue = ok.get("venue") or {}
    org = ok.get("org") or {}
    out = {
        "source": "eventbrite",
        "id": ev.get("id"),
        "name": (ev.get("name") or {}).get("text"),
        "url": ev.get("url"),
        "status": ev.get("status"),
        "start": (ev.get("start") or {}).get("local"),
        "end": (ev.get("end") or {}).get("local"),
        "timezone": (ev.get("start") or {}).get("timezone"),
        "online": ev.get("online_event"),
        "is_free": ev.get("is_free"),
        "currency": ev.get("currency"),
        "capacity": ev.get("capacity"),
        "summary": ev.get("summary"),
        "venue": {
            "name": venue.get("name"),
            "address": (venue.get("address") or {}).get("localized_address_display"),
            "lat": venue.get("latitude"), "lng": venue.get("longitude"),
            "capacity": venue.get("capacity"),
        } if venue else None,
        "organizer": {
            "name": org.get("name"), "url": org.get("url"), "website": org.get("website"),
            "past_events": org.get("num_past_events"), "future_events": org.get("num_future_events"),
        } if org else None,
    }
    if "desc" in ok:
        out["description"] = html_to_text((ok["desc"] or {}).get("description"), 4000)
    if "tickets" in ok:
        out["ticket_classes"] = [normalize_ticket_class(t) for t in ok["tickets"].get("ticket_classes", [])]
    failed = [k for k, v in results.items() if isinstance(v, Exception)]
    if failed:
        out["warnings"] = f"Could not load: {', '.join(failed)}"
    return out


@mcp.tool()
async def list_categories() -> dict:
    """Valid category and format names/ids for the `categories` and `formats` filters.
    Eventbrite categories map to the closest Luma one where there is one (e.g. Science & Tech -> Tech);
    Luma-only names such as AI or Crypto search Luma only. Formats are Eventbrite-only."""
    return {
        "categories": {str(k): v for k, v in CATEGORIES.items()},
        "formats": {str(k): v for k, v in FORMATS.items()},
        "luma_categories": dict(LUMA_CATEGORIES),
    }


@mcp.tool()
async def resolve_location(
    query: Annotated[str, Field(description="Place name or address, e.g. 'Orchard Road, Singapore'")],
) -> dict:
    """Convert a place name to coordinates (OpenStreetMap Nominatim) to use as lat/lng."""
    la, ln, label = await geocode(query)
    return {"lat": la, "lng": ln, "label": label}


def main() -> None:
    # stdout is the MCP protocol channel: all logging must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
