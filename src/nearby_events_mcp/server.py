"""FastMCP server exposing Eventbrite event discovery to Claude Desktop (stdio transport)."""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .client import EventsClient, EventsError
from .config import Settings
from .constants import CATEGORIES, FORMATS
from .geo import geocode
from .normalize import html_to_text, normalize_ticket_class
from .search import SearchParams, run_search

log = logging.getLogger("nearby_events_mcp")

mcp = FastMCP(
    "nearby-events",
    instructions=(
        "Find Eventbrite events near a location. Use search_events for a single day/range, "
        "daily_events for a per-day agenda, get_event for full details of one event. Location comes from "
        "lat/lng or a place name, else the user's configured default. Event size is only available as "
        "an organizer-followers proxy; say so when the user filters by size."
    ),
)

_settings: Settings | None = None
_client: EventsClient | None = None


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


@mcp.tool()
async def search_events(
    date: Annotated[str, Field(description="today, tomorrow, this_week, this_weekend, this_month or YYYY-MM-DD")] = "today",
    date_to: Annotated[str | None, Field(description="Inclusive range end YYYY-MM-DD (then `date` must be YYYY-MM-DD)")] = None,
    location: Annotated[str | None, Field(description="Place name to search around, e.g. 'Marina Bay, Singapore'. Ignored if lat/lng given")] = None,
    lat: Annotated[float | None, Field(description="Latitude of search centre")] = None,
    lng: Annotated[float | None, Field(description="Longitude of search centre")] = None,
    radius_km: Annotated[float, Field(description="Search radius in km (rounded to whole km, min 1)", ge=1, le=200)] = 10,
    query: Annotated[str | None, Field(description="Free-text keywords, e.g. 'startup networking'")] = None,
    price: Annotated[Price, Field(description="any, free or paid")] = "any",
    max_price: Annotated[float | None, Field(description="Cheapest ticket must cost at most this (event currency; free events pass)")] = None,
    min_price: Annotated[float | None, Field(description="Most expensive ticket must cost at least this")] = None,
    categories: Annotated[list[str] | None, Field(description="Category names or ids, OR-ed. e.g. ['Business','Music']. See list_categories")] = None,
    formats: Annotated[list[str] | None, Field(description="Format names or ids, OR-ed. e.g. ['Networking','Conference']")] = None,
    size: Annotated[Size | None, Field(description="PROXY: organizer followers small<200, medium 200-2000, large>2000")] = None,
    min_followers: Annotated[int | None, Field(description="Minimum organizer followers (size proxy)")] = None,
    max_followers: Annotated[int | None, Field(description="Maximum organizer followers (size proxy)")] = None,
    min_capacity: Annotated[int | None, Field(description="Min total ticket capacity; only known for a minority of events, unknown are kept. Triggers extra requests")] = None,
    max_capacity: Annotated[int | None, Field(description="Max total ticket capacity; see min_capacity")] = None,
    include_capacity: Annotated[bool, Field(description="Look up ticket capacity for each result (slower)")] = False,
    start_after: Annotated[str | None, Field(description="Local start time not before HH:MM (24h)")] = None,
    start_before: Annotated[str | None, Field(description="Local start time not after HH:MM (24h)")] = None,
    include_online: Annotated[bool, Field(description="Include online events")] = False,
    available_only: Annotated[bool, Field(description="Hide sold-out / sales-ended events (default on)")] = True,
    include_ended: Annotated[bool, Field(description="Include events that already finished today")] = False,
    sort: Annotated[Sort, Field(description="distance, date, price or popularity")] = "distance",
    max_results: Annotated[int, Field(description="Max events returned", ge=1, le=100)] = 20,
) -> dict:
    """Search Eventbrite events around a location with filters for date, price, category, format,
    time of day and (proxy) size. Returns compact JSON: events with venue, distance, price, organizer."""
    la, ln, label = await resolve_origin(lat, lng, location)
    p = SearchParams(
        date=date, date_to=date_to, query=query, radius_km=radius_km, price=price,
        categories=categories or [], formats=formats or [], min_price=min_price, max_price=max_price,
        size=size, min_followers=min_followers, max_followers=max_followers,
        include_capacity=include_capacity, min_capacity=min_capacity, max_capacity=max_capacity,
        start_after=start_after, start_before=start_before, include_online=include_online,
        available_only=available_only, include_ended=include_ended, sort=sort, max_results=max_results,
    )
    result = await run_search(client(), p, (la, ln))
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
    size: Size | None = None,
    start_after: str | None = None,
    start_before: str | None = None,
    include_online: bool = False,
    available_only: bool = True,
    max_per_day: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Day-by-day agenda of nearby events (one search per day), same filters as search_events.
    Results per day are sorted by start time."""
    la, ln, label = await resolve_origin(lat, lng, location)
    first = date.fromisoformat(start_date) if start_date else date.today()
    agenda: dict[str, dict] = {}
    for i in range(days):
        day = (first + timedelta(days=i)).isoformat()
        p = SearchParams(
            date=day, query=query, radius_km=radius_km, price=price, max_price=max_price,
            categories=categories or [], formats=formats or [], size=size, start_after=start_after,
            start_before=start_before, include_online=include_online, available_only=available_only,
            sort="date", max_results=max_per_day,
        )
        res = await run_search(client(), p, (la, ln))
        agenda[day] = {"count": res["count"], "more_available": res["more_available"],
                       "events": res["events"]}
    return {"origin": {"lat": la, "lng": ln, "label": label, "radius_km": round(radius_km)},
            "days": agenda}


@mcp.tool()
async def get_event(
    event_id: Annotated[str, Field(description="Eventbrite event id (the `id` field from search results)")],
    include_description: bool = True,
    include_tickets: bool = True,
) -> dict:
    """Full details for one event: times, description text, ticket tiers with prices and capacity."""
    import asyncio

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
    """Valid category and format names/ids for the `categories` and `formats` filters."""
    return {
        "categories": {str(k): v for k, v in CATEGORIES.items()},
        "formats": {str(k): v for k, v in FORMATS.items()},
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
