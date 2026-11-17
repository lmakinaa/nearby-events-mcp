"""Luma (luma.com) as a second event source: client, normaliser and date-window fetcher.

Luma's public discover feed (see docs/api-notes.md, "Luma") is geo-scoped and sorted by start time
from *now*. It filters `query`, `slug` (category) and `price=free|paid` on the server, but has no date,
price-range or radius parameters, so those are handled here:

- dates: the feed cursor is base64url JSON {"sv": "<UTC start>", "fb": "<event id>"}. A cursor crafted
  with sv = local midnight of the first wanted day and fb = "" starts the feed at that day. It is
  undocumented, so a 4xx on it falls back to plain paging, paging stops once events pass the last day,
  and every event is checked against the window on the client anyway.
- with `query` the feed is different: a global, relevance-ranked top 50 with no further pages, so it
  is read as one unsorted page (no date jump, no stop rule).
- price range, radius, time of day, size: client-side (search.passes plus the radius check there).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

from .client import EventsError
from .config import Settings
from .constants import LUMA_CATEGORIES
from .normalize import clip, haversine_km, prosemirror_to_text, to_float

try:  # zoneinfo needs tzdata on Windows; degrade gracefully
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

log = logging.getLogger("nearby_events_mcp.luma")

LUMA_API = "https://api.lu.ma"
LUMA_SITE = "https://luma.com"
LUMA_PAGE_SIZE = 50


class LumaHTTPError(EventsError):
    """A 4xx from Luma (kept apart so a rejected crafted cursor can fall back to plain paging)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------------- client
class LumaClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._http = httpx.AsyncClient(
            base_url=LUMA_API,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=settings.timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self.s.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        last_error = "unknown error"
        for attempt in range(4):
            await self._throttle()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                last_error = f"HTTP {resp.status_code}"
                await asyncio.sleep(min(delay, 20))
                continue
            if resp.status_code >= 400:
                raise LumaHTTPError(f"Luma GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                                    resp.status_code)
            try:
                return resp.json()
            except ValueError:
                raise EventsError(f"Luma GET {path} returned non-JSON: {resp.text[:200]}") from None
        raise EventsError(f"Luma GET {path} failed after retries: {last_error}")

    async def discover(self, lat: float, lng: float, cursor: str | None = None, query: str | None = None,
                       slug: str | None = None, price: str | None = None,
                       limit: int = LUMA_PAGE_SIZE) -> dict:
        params: dict[str, Any] = {"latitude": lat, "longitude": lng, "pagination_limit": limit}
        if cursor:
            params["pagination_cursor"] = cursor
        if query:
            params["query"] = query
        if slug:
            params["slug"] = slug
        if price in {"free", "paid"}:
            params["price"] = price
        return await self._get("/discover/get-paginated-events", params)

    async def get_event(self, api_id: str) -> dict:
        return await self._get("/event/get", {"event_api_id": api_id})

    async def categories(self) -> dict:
        if "categories" not in self._cache:
            self._cache["categories"] = await self._get("/discover/category/list-categories")
        return self._cache["categories"]


# ---------------------------------------------------------------------------------------- time
def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _zone(name: str | None):
    if name and ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def to_local(value: str | None, tz_name: str | None) -> datetime | None:
    dt = parse_utc(value)
    return dt.astimezone(_zone(tz_name)) if dt else None


def local_midnight_utc(day: date, tz_name: str | None) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=_zone(tz_name)).astimezone(timezone.utc)


def make_cursor(start_utc: datetime, after_id: str = "") -> str:
    """The feed cursor format: base64url (unpadded) of {"sv": "YYYY-MM-DD HH:MM:SS+00", "fb": id}."""
    sv = start_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")
    raw = json.dumps({"sv": sv, "fb": after_id}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict:
    pad = "=" * (-len(cursor) % 4)
    return json.loads(base64.urlsafe_b64decode(cursor + pad))


# ---------------------------------------------------------------------------------------- normalise
def _price(block: dict | None) -> tuple[float | None, str | None]:
    if not block:
        return None, None
    cents = to_float(block.get("cents"))
    cur = block.get("currency")
    return (None if cents is None else cents / 100.0), (cur.upper() if isinstance(cur, str) else None)


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def place_text(geo: dict) -> tuple[str | None, str | None, bool]:
    """(venue, address, approximate) from geo_address_info; obfuscated events only show the area."""
    area = ", ".join(x for x in (geo.get("sublocality"), geo.get("city") or geo.get("city_state"))
                     if x) or geo.get("country")
    if geo.get("mode") == "obfuscated" or not (geo.get("full_address") or geo.get("address")):
        address = f"{area} (exact address for guests only)" if area else None
        return geo.get("sublocality") or geo.get("city"), address, True
    return geo.get("address") or geo.get("sublocality"), geo.get("full_address") or geo.get("address"), False


def organizer_name(calendar: dict, hosts: list[dict]) -> str | None:
    """The calendar's name, or the first host for personal calendars (whose name is just "Personal")."""
    if calendar.get("name") and not calendar.get("is_personal"):
        return calendar["name"]
    return next((h["name"] for h in hosts if h.get("name")), None) or calendar.get("name")


def guest_count(entry: dict) -> int | None:
    """The RSVP count, or None when the host hides the guest list (Luma then reports 0)."""
    ev = entry.get("event") or {}
    count = entry.get("guest_count")
    shown = ev.get("show_guest_list")
    if not isinstance(count, (int, float)) or shown is False or (shown is None and count == 0):
        return None
    return int(count)


def event_timezone(entry: dict, default: str | None = None) -> str | None:
    ev = entry.get("event") or {}
    return ev.get("timezone") or (entry.get("calendar") or {}).get("timezone") or default


def normalize_luma_event(entry: dict, origin: tuple[float, float] | None = None,
                         now: datetime | None = None, default_tz: str | None = None) -> dict:
    """Map one discover entry to the same keys as normalize.normalize_event (plus source/attendees).

    Local times use the event's timezone, else its calendar's, else `default_tz` (the search area's).
    """
    ev = entry.get("event") or {}
    ti = entry.get("ticket_info") or {}
    geo = ev.get("geo_address_info") or {}
    coord = ev.get("coordinate") or geo.get("place_coordinate") or {}
    cal = entry.get("calendar") or {}
    hosts = entry.get("hosts") or []
    tz_name = event_timezone(entry, default_tz)

    start_at = ev.get("start_at") or entry.get("start_at")
    start, end = to_local(start_at, tz_name), to_local(ev.get("end_at"), tz_name)

    lat, lng = to_float(coord.get("latitude")), to_float(coord.get("longitude"))
    is_online = (ev.get("location_type") or "offline") != "offline"
    distance = None
    if origin and lat is not None and lng is not None and not is_online:
        distance = round(haversine_km(origin[0], origin[1], lat, lng), 2)

    price_min, currency = _price(ti.get("price"))
    price_max, cur2 = _price(ti.get("max_price"))
    currency = currency or cur2
    is_free = bool(ti.get("is_free"))
    if is_free:
        price_min = price_max = 0.0 if price_min is None else price_min
    if price_max is None:
        price_max = price_min
    if is_free and not price_max:
        display = "Free"
    elif price_min is not None and price_max is not None and price_min != price_max:
        display = f"{currency or ''} {price_min:.2f}–{price_max:.2f}".strip()
    elif price_min is not None:
        display = f"{currency or ''} {price_min:.2f}".strip()
    else:
        display = None

    venue, address, approx = place_text(geo) if not is_online else ("Online", None, False)
    availability = entry.get("registration_availability")
    sold_out = bool(ti.get("is_sold_out")) or availability == "sold-out"
    ended_at = parse_utc(ev.get("end_at")) or parse_utc(start_at)
    now = now or datetime.now(timezone.utc)
    url_slug = ev.get("url")
    urgency = []
    if ti.get("is_near_capacity"):
        urgency.append("nearCapacity")
    if availability == "waitlist":
        urgency.append("waitlist")

    return {
        "source": "luma",
        "id": ev.get("api_id") or entry.get("api_id"),
        "name": ev.get("name"),
        "url": f"{LUMA_SITE}/{url_slug}" if url_slug else None,
        "summary": "",
        "start": _fmt(start),
        "end": _fmt(end),
        "timezone": tz_name,
        "venue": venue,
        "address": address,
        "lat": lat,
        "lng": lng,
        "distance_km": distance,
        "is_online": is_online,
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "currency": currency,
        "price": display,
        "categories": [],  # filled in when the feed was filtered by category
        "subcategories": [],
        "formats": [],
        "organizer": organizer_name(cal, hosts),
        "organizer_followers": None,
        "attendees": guest_count(entry),
        "requires_approval": ti.get("require_approval"),
        "sales_status": availability,
        "sold_out": sold_out,
        "has_tickets": not sold_out,
        "urgency": urgency,
        "is_series": bool(ev.get("recurrence_id")),
        "capacity": None,
        "_ended": bool(ended_at and ended_at < now),
        "_tags": [],
        "_approx": approx,
    }


# ---------------------------------------------------------------------------------------- fetching
@dataclass
class LumaFetch:
    events: list[dict] = field(default_factory=list)  # normalised, inside the date window, deduped
    requests: int = 0
    more: bool = False  # stopped by the page cap while the window still had events
    notes: list[str] = field(default_factory=list)


Window = tuple[date, Optional[date]]  # inclusive local dates; None = open-ended


def local_today(now: datetime, tz_name: str | None) -> date:
    return now.astimezone(_zone(tz_name)).date() if tz_name else now.astimezone().date()


def area_timezone(entries: list[dict], origin: tuple[float, float]) -> str | None:
    """The search area's timezone: that of the in-person event nearest the origin.

    Not simply the first entry's: keyword results are global and can start with another country.
    """
    best: tuple[float, str] | None = None
    for e in entries:
        ev = e.get("event") or {}
        tz = event_timezone(e)
        coord = ev.get("coordinate") or {}
        lat, lng = to_float(coord.get("latitude")), to_float(coord.get("longitude"))
        if not tz or lat is None or lng is None or (ev.get("location_type") or "offline") != "offline":
            continue
        d = haversine_km(origin[0], origin[1], lat, lng)
        if best is None or d < best[0]:
            best = (d, tz)
    return best[1] if best else next((event_timezone(e) for e in entries if event_timezone(e)), None)


def in_window(norm: dict, day_from: date | None, day_to: date | None) -> bool:
    """Starts on or before the last day and has not ended before the first (like Eventbrite's dates)."""
    try:
        start = date.fromisoformat((norm["start"] or "")[:10])
    except ValueError:
        return False
    try:
        end = date.fromisoformat((norm["end"] or "")[:10])
    except ValueError:
        end = start
    return not ((day_to and start > day_to) or (day_from and max(start, end) < day_from))


async def fetch_window(client: LumaClient, origin: tuple[float, float],
                       window: Window | Callable[[str | None], Window], *, query: str | None = None,
                       slug: str | None = None, price: str | None = None, max_pages: int = 5,
                       enough: int | None = None, keep: Callable[[dict], bool] | None = None,
                       now: datetime | None = None) -> LumaFetch:
    """Every feed event in the window [first, last] of local dates (last None = open-ended).

    `window` may be a function of the area's timezone name, so presets like "tomorrow" are resolved
    in the search area's local time: page 1 is the plain feed, and the in-person event nearest the
    origin gives the timezone. Paging decisions use UTC instants of the area's local midnights, so a
    stray event from another timezone cannot end paging early. If page 1 ends before the window, jump
    to the window's first midnight with a crafted cursor; on a 4xx keep paging with the feed's own
    cursor instead. Stop once a page ends past the last day, when the feed ends, after `max_pages`,
    or (date-sorted searches) once `enough` events that also pass `keep` were collected.
    With `query` the feed is relevance-ranked, not date-sorted: no jump and no stop rule apply.
    """
    now = now or datetime.now(timezone.utc)
    out = LumaFetch()
    seen: set[str] = set()
    cursor: str | None = None
    tz_name: str | None = None
    resolved: Window | None = None
    start_utc = end_utc = None
    date_sorted = not query
    jumped = False
    accepted = 0
    fallback_cursor: str | None = None  # the feed's own next cursor, kept while trying a crafted one
    pages = max(1, max_pages)

    while out.requests < pages:
        try:
            data = await client.discover(origin[0], origin[1], cursor=cursor, query=query, slug=slug,
                                         price=price)
        except LumaHTTPError as exc:
            if fallback_cursor is not None and exc.status < 500:
                log.info("crafted Luma cursor rejected (%s); paging forward instead", exc.status)
                out.notes.append("Luma rejected the date jump, so its feed was paged forward from today; "
                                 "dates far ahead may be incomplete.")
                cursor, fallback_cursor = fallback_cursor, None
                out.requests += 1
                continue
            raise
        out.requests += 1
        entries = data.get("entries") or []
        if resolved is None:
            tz_name = area_timezone(entries, origin)
            resolved = window(tz_name) if callable(window) else window
            day_from, day_to = resolved
            start_utc = local_midnight_utc(day_from, tz_name) if day_from else None
            end_utc = local_midnight_utc(day_to + timedelta(days=1), tz_name) if day_to else None
        last_utc: datetime | None = None
        for entry in entries:
            ev = entry.get("event") or {}
            started = parse_utc(ev.get("start_at") or entry.get("start_at"))
            if started is None:
                continue
            last_utc = started
            norm = normalize_luma_event(entry, origin, now, default_tz=tz_name)
            if not norm["id"] or norm["id"] in seen or not in_window(norm, day_from, day_to):
                continue
            seen.add(norm["id"])
            if slug:
                norm["categories"] = [LUMA_CATEGORIES.get(slug, slug)]
            out.events.append(norm)
            if keep is None or keep(norm):
                accepted += 1

        has_more = bool(data.get("has_more")) and bool(data.get("next_cursor")) and bool(entries)
        if not has_more or (date_sorted and end_utc and last_utc and last_utc >= end_utc):
            out.more = False
            break
        if date_sorted and enough is not None and accepted >= enough:
            out.more = True
            break
        fallback_cursor = None
        if date_sorted and not jumped and start_utc and last_utc and last_utc < start_utc:
            jumped = True
            fallback_cursor = data["next_cursor"]
            cursor = make_cursor(start_utc)
        else:
            cursor = data["next_cursor"]
        out.more = True
    return out


async def get_event_details(client: LumaClient, api_id: str, include_description: bool = True,
                            include_tickets: bool = True) -> dict:
    """get_event output for a Luma event, keyed like the Eventbrite version."""
    data = await client.get_event(api_id)
    ev = data.get("event") or {}
    cal = data.get("calendar") or {}
    hosts = data.get("hosts") or []
    tz_name = ev.get("timezone")
    geo = ev.get("geo_address_info") or {}
    coord = ev.get("coordinate") or geo.get("place_coordinate") or {}
    ti = data.get("ticket_info") or {}
    is_online = (ev.get("location_type") or "offline") != "offline"
    venue, address, approx = place_text(geo) if not is_online else ("Online", None, False)
    types = [t for t in (data.get("ticket_types") or []) if not t.get("is_hidden")]
    caps = [t.get("max_capacity") for t in types]
    _, currency = _price(ti.get("price"))
    url_slug = ev.get("url")
    out: dict[str, Any] = {
        "source": "luma",
        "id": ev.get("api_id") or api_id,
        "name": ev.get("name"),
        "url": f"{LUMA_SITE}/{url_slug}" if url_slug else None,
        "status": data.get("registration_availability"),
        "start": _fmt(to_local(ev.get("start_at"), tz_name)),
        "end": _fmt(to_local(ev.get("end_at"), tz_name)),
        "timezone": tz_name,
        "online": is_online,
        "is_free": ti.get("is_free"),
        "currency": currency or next((str(t["currency"]).upper() for t in types if t.get("currency")), None),
        "capacity": int(sum(caps)) if caps and all(isinstance(c, (int, float)) for c in caps) else None,
        "attendees": guest_count(data),
        "requires_approval": ti.get("require_approval"),
        "sold_out": bool(ti.get("is_sold_out") or data.get("sold_out")),
        "spots_remaining": ti.get("spots_remaining"),
        "summary": None,
        "venue": {
            "name": venue, "address": address,
            "lat": to_float(coord.get("latitude")), "lng": to_float(coord.get("longitude")),
            "approximate": approx, "capacity": None,
        } if not is_online else None,
        "organizer": {
            "name": organizer_name(cal, hosts),
            "url": f"{LUMA_SITE}/{cal['slug']}" if cal.get("slug") else None,
            "website": cal.get("website"),
            "hosts": [h.get("name") for h in hosts if h.get("name")],
        },
        "categories": [c.get("name") for c in (data.get("categories") or []) if isinstance(c, dict) and c.get("name")],
    }
    if include_description:
        out["description"] = prosemirror_to_text(data.get("description_mirror"), 4000)
    if include_tickets:
        out["ticket_classes"] = [luma_ticket_class(t) for t in types]
    return out


def luma_ticket_class(t: dict) -> dict:
    """A Luma ticket type with the keys of normalize.normalize_ticket_class."""
    cents = to_float(t.get("cents")) or 0.0
    currency = (t.get("currency") or "").upper() or None
    free = t.get("type") == "free" or cents == 0
    if t.get("is_disabled"):
        status = "UNAVAILABLE"
    elif t.get("spots_remaining") == 0:
        status = "SOLD_OUT"
    else:
        status = "AVAILABLE"
    return {
        "name": clip(str(t.get("name") or ""), 120) or None,
        "free": free,
        "donation": bool(t.get("is_flexible")),
        "price": "Free" if free else f"{currency or ''} {cents / 100:.2f}".strip(),
        "currency": currency,
        "capacity": t.get("max_capacity"),
        "on_sale_status": status,
        "min_per_order": None,
        "max_per_order": t.get("max_per_guest"),
    }

