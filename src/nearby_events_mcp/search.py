"""Search orchestration: build the API request, page through results, apply client-side filters.

`search_eventbrite` (alias `run_search`) and `search_luma` search one source each; `search_sources`
runs the selected sources concurrently and merges them.
"""

from __future__ import annotations

import asyncio
import calendar
import dataclasses
import logging
import re
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Callable

from .client import EventsClient, EventsError
from .constants import (DATE_PRESETS, LUMA_CATEGORIES, LUMA_MAX_SLUGS, LUMA_SIZE_BUCKETS, PAGE_SIZE,
                        SIZE_BUCKETS, SOURCES, resolve_categories, resolve_formats, split_categories)
from .luma import LumaClient, fetch_window, local_today
from .normalize import capacity_from_ticket_classes, normalize_event

log = logging.getLogger("nearby_events_mcp.search")

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
MAX_ENRICH = 40


@dataclass
class SearchParams:
    date: str = "today"  # preset or YYYY-MM-DD
    date_to: str | None = None  # inclusive range end (YYYY-MM-DD)
    query: str | None = None
    radius_km: float = 10
    price: str = "any"  # any | free | paid
    categories: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    min_price: float | None = None  # most expensive ticket must be >= this
    max_price: float | None = None  # cheapest ticket must be <= this (free counts as 0)
    size: str | None = None  # small | medium | large (organizer-follower proxy)
    min_followers: int | None = None
    max_followers: int | None = None
    include_capacity: bool = False
    min_capacity: int | None = None
    max_capacity: int | None = None
    start_after: str | None = None  # HH:MM local
    start_before: str | None = None  # HH:MM local
    include_online: bool = False
    available_only: bool = False
    include_ended: bool = False
    sort: str = "distance"  # distance | date | price | popularity
    max_results: int = 20
    max_pages: int = 5


def validate(p: SearchParams) -> None:
    if p.date not in DATE_PRESETS:
        try:
            _date.fromisoformat(p.date)
        except ValueError:
            raise ValueError(
                f"date must be one of {sorted(DATE_PRESETS)} or YYYY-MM-DD, got '{p.date}'") from None
    if p.date_to:
        try:
            end = _date.fromisoformat(p.date_to)
            start = _date.fromisoformat(p.date)
        except ValueError:
            raise ValueError("date_to requires date and date_to both in YYYY-MM-DD format") from None
        if end < start:
            raise ValueError("date_to must be on or after date")
    for name in ("start_after", "start_before"):
        v = getattr(p, name)
        if v and not _HHMM.match(v):
            raise ValueError(f"{name} must be HH:MM (24h), got '{v}'")
    if p.price not in {"any", "free", "paid"}:
        raise ValueError("price must be any, free or paid")
    if p.size and p.size not in SIZE_BUCKETS:
        raise ValueError(f"size must be one of {sorted(SIZE_BUCKETS)}")
    if p.sort not in {"distance", "date", "price", "popularity"}:
        raise ValueError("sort must be distance, date, price or popularity")


def build_event_search(p: SearchParams, origin: tuple[float, float], page: int) -> dict:
    es: dict = {"page": page, "page_size": PAGE_SIZE, "dedup": True}
    if p.date in DATE_PRESETS and not p.date_to:
        es["dates"] = p.date
    elif p.date in DATE_PRESETS:
        raise ValueError("date_to can only be combined with a YYYY-MM-DD start date")
    else:
        es["date_range"] = {"from": p.date, "to": p.date_to or p.date}
    # Server only accepts an integer km string ("5km"); "5.0km" is rejected with 400.
    es["point_radius"] = {
        "latitude": origin[0],
        "longitude": origin[1],
        "radius": f"{max(1, round(p.radius_km))}km",
    }
    if p.query:
        es["q"] = p.query
    if p.price in {"free", "paid"}:
        es["price"] = p.price
    cats, fmts = resolve_categories(p.categories), resolve_formats(p.formats)
    # Multiple tags are OR-ed server-side. Categories and formats are different dimensions, so if both
    # are given only categories go to the server and formats are enforced client-side.
    server_tags = cats or fmts
    if server_tags:
        es["tags"] = server_tags
    # Only "date" and "distance" are valid server sorts.
    es["sort"] = "distance" if p.sort == "distance" else "date"
    return es


def _cheapest(ev: dict) -> float | None:
    if ev["is_free"]:
        return 0.0
    return ev["price_min"]


def passes(ev: dict, p: SearchParams, format_tags: list[str]) -> bool:
    if ev["is_online"] and not p.include_online:
        return False
    if ev["_ended"] and not p.include_ended:
        return False
    if p.available_only and (ev["sold_out"] or ev["sales_status"] == "sales_ended"):
        return False

    cheapest = _cheapest(ev)
    if p.max_price is not None and cheapest is not None and cheapest > p.max_price:
        return False
    top = 0.0 if ev["is_free"] else ev["price_max"]
    if p.min_price is not None and top is not None and top < p.min_price:
        return False

    if ev.get("source") == "luma":
        # Real RSVP counts with their own buckets; follower limits are an Eventbrite-only signal.
        lo, hi = LUMA_SIZE_BUCKETS[p.size] if p.size else (None, None)
        signal = ev.get("attendees")
    else:
        lo, hi = p.min_followers, p.max_followers
        if p.size:
            blo, bhi = SIZE_BUCKETS[p.size]
            lo = blo if lo is None else lo
            hi = bhi if hi is None else hi
        signal = ev["organizer_followers"]
    if lo is not None or hi is not None:
        f = signal
        if f is None:
            return False
        if lo is not None and f < lo:
            return False
        if hi is not None and f >= hi:
            return False

    if format_tags and not any(t in ev["_tags"] for t in format_tags):
        return False

    start = ev["start"] or ""
    hhmm = start[11:16] if len(start) >= 16 else None
    if hhmm:
        if p.start_after and hhmm < p.start_after.zfill(5):
            return False
        if p.start_before and hhmm > p.start_before.zfill(5):
            return False
    return True


def popularity(ev: dict) -> int:
    """Each source's own size signal: RSVPs on Luma, organizer followers on Eventbrite."""
    value = ev.get("attendees") if ev.get("source") == "luma" else ev.get("organizer_followers")
    return int(value or 0)


def _sort_key(p: SearchParams):
    if p.sort == "distance":
        return lambda e: (e["distance_km"] is None, e["distance_km"] or 0, e["start"] or "")
    if p.sort == "price":
        return lambda e: (_cheapest(e) is None, _cheapest(e) or 0, e["start"] or "")
    if p.sort == "popularity":
        return lambda e: (-popularity(e), e["start"] or "")
    return lambda e: (e["start"] or "", e["distance_km"] or 0)


async def enrich_capacity(client: EventsClient, events: list[dict]) -> int:
    """Fetch ticket classes and sum their capacities. Returns number of events with a known capacity."""
    sem = asyncio.Semaphore(4)

    async def one(ev: dict) -> None:
        async with sem:
            try:
                ev["capacity"] = capacity_from_ticket_classes(await client.get_ticket_classes(ev["id"]))
            except EventsError:
                ev["capacity"] = None

    await asyncio.gather(*(one(e) for e in events[:MAX_ENRICH] if e.get("source") != "luma"))
    return sum(1 for e in events if e["capacity"] is not None)


async def search_eventbrite(client: EventsClient, p: SearchParams, origin: tuple[float, float]) -> dict:
    validate(p)
    format_tags = resolve_formats(p.formats) if p.categories else []
    server_sorted = p.sort in {"distance", "date"}
    kept: list[dict] = []
    seen: set[str] = set()
    scanned = 0
    more = False

    for page in range(1, max(1, p.max_pages) + 1):
        data = await client.search(build_event_search(p, origin, page))
        block = data.get("events") or {}
        raw_events = block.get("results") or []
        if not raw_events:
            more = False
            break
        for raw in raw_events:
            ev = normalize_event(raw, origin)
            if not ev["id"] or ev["id"] in seen:
                continue
            seen.add(ev["id"])
            scanned += 1
            if passes(ev, p, format_tags):
                kept.append(ev)
        page_count = (block.get("pagination") or {}).get("page_count") or page
        more = page < page_count
        if not more:
            break
        if server_sorted and len(kept) >= p.max_results and p.min_capacity is None and p.max_capacity is None:
            break

    notes: list[str] = []
    if p.include_capacity or p.min_capacity is not None or p.max_capacity is not None:
        kept.sort(key=_sort_key(p))
        known = await enrich_capacity(client, kept)
        if p.min_capacity is not None or p.max_capacity is not None:
            def cap_ok(e: dict) -> bool:
                c = e["capacity"]
                if c is None:
                    return True  # unknown -> keep
                return (p.min_capacity is None or c >= p.min_capacity) and \
                       (p.max_capacity is None or c <= p.max_capacity)
            kept = [e for e in kept if cap_ok(e)]
        notes.append(
            f"Capacity is only published by some organizers: known for {known} of "
            f"{min(len(kept), MAX_ENRICH)} checked events; events with unknown capacity are kept.")
    if p.size or p.min_followers is not None or p.max_followers is not None:
        notes.append("Size filter uses organizer follower count as a proxy (Eventbrite does not expose "
                     "attendance). small<200, medium 200-2000, large>2000 followers.")

    return _result(kept, p, scanned, more, notes)


run_search = search_eventbrite  # original single-source name


def clean_event(e: dict) -> dict:
    for k in ("_ended", "_tags", "_approx"):
        e.pop(k, None)
    return e


def _result(kept: list[dict], p: SearchParams, scanned: int, more: bool, notes: list[str]) -> dict:
    kept.sort(key=_sort_key(p))
    total_kept = len(kept)
    out = [clean_event(e) for e in kept[: max(1, p.max_results)]]
    return {
        "count": len(out),
        "matched": total_kept,
        "scanned": scanned,
        "more_available": more or total_kept > len(out),
        "notes": notes,
        "events": out,
    }


# ---------------------------------------------------------------------------------------- dates
def preset_range(preset: str, today: _date) -> tuple[_date, _date | None]:
    """A date preset as an inclusive (first, last) local-date window; last None = open-ended."""
    if preset == "today":
        return today, today
    if preset == "tomorrow":
        d = today + timedelta(days=1)
        return d, d
    if preset == "this_week":  # through Sunday
        return today, today + timedelta(days=6 - today.weekday())
    if preset == "this_weekend":  # Friday-Sunday like Eventbrite's preset (from today once it has begun)
        sun = today + timedelta(days=6 - today.weekday())
        return max(today, sun - timedelta(days=2)), sun
    if preset == "this_month":
        return today, today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if preset == "current_future":
        return today, None
    raise ValueError(f"unknown date preset '{preset}'")


def date_window(p: SearchParams, today: _date) -> tuple[_date, _date | None]:
    if p.date in DATE_PRESETS:
        return preset_range(p.date, today)
    first = _date.fromisoformat(p.date)
    return first, _date.fromisoformat(p.date_to) if p.date_to else first


# ---------------------------------------------------------------------------------------- Luma
def luma_price(p: SearchParams) -> str | None:
    """Luma's server-side price filter: free/paid, and paid when a positive min_price is asked for."""
    if p.price in {"free", "paid"}:
        return p.price
    if p.min_price is not None and p.min_price > 0:
        return "paid"
    return None


def luma_keep(p: SearchParams) -> Callable[[dict], bool]:
    """The client-side filters for Luma events: radius plus passes()."""
    radius = max(1, round(p.radius_km))

    def keep(ev: dict) -> bool:
        if not ev["is_online"]:
            # Keyword results are global; an in-person event without coordinates can be anywhere.
            if ev["distance_km"] is None or ev["distance_km"] > radius:
                return False
        return passes(ev, p, [])
    return keep


async def luma_events(client: LumaClient, p: SearchParams, origin: tuple[float, float],
                      slugs: list[str] | None = None, now: datetime | None = None,
                      early_stop: bool = True,
                      window: Callable[[str | None], tuple[_date, _date | None]] | None = None,
                      ) -> tuple[list[dict], int, bool, list[str]]:
    """Filtered Luma events in p's date window: (kept, scanned, more_available, notes). Unsorted, uncut.

    `window` overrides p's dates with a function of the area's timezone name.
    """
    now = now or datetime.now(timezone.utc)
    notes: list[str] = []
    slugs = list(dict.fromkeys(slugs or []))
    if len(slugs) > LUMA_MAX_SLUGS:
        notes.append(f"Luma: only the first {LUMA_MAX_SLUGS} categories were searched "
                     f"({', '.join(LUMA_CATEGORIES.get(s, s) for s in slugs[:LUMA_MAX_SLUGS])}).")
        slugs = slugs[:LUMA_MAX_SLUGS]
    # Presets ("today", "this_weekend") are resolved in the search area's timezone, which the first
    # feed page reveals; explicit YYYY-MM-DD dates are taken as local dates there.
    def p_window(tz_name: str | None):
        return date_window(p, local_today(now, tz_name))

    keep = luma_keep(p)
    # Only date-sorted searches can stop early, counting events that pass every filter.
    enough = p.max_results if (early_stop and p.sort == "date") else None

    chains = await asyncio.gather(*(
        fetch_window(client, origin, window or p_window, query=p.query, slug=slug, price=luma_price(p),
                     max_pages=p.max_pages, enough=enough, keep=keep, now=now)
        for slug in (slugs or [None])))

    by_id: dict[str, dict] = {}
    scanned = 0
    more = False
    for chain in chains:
        more = more or chain.more
        for n in chain.notes:
            if n not in notes:
                notes.append(n)
        for ev in chain.events:
            if ev["id"] in by_id:  # same event under two categories
                merged = by_id[ev["id"]]["categories"]
                merged.extend(c for c in ev["categories"] if c not in merged)
                continue
            by_id[ev["id"]] = ev
            scanned += 1

    kept = [ev for ev in by_id.values() if keep(ev)]
    if p.query:
        notes.append("Luma keyword search only returns Luma's top 50 matches worldwide, so Luma results "
                     "for dates further ahead may be incomplete.")
    if any(e.get("_approx") for e in kept):
        notes.append("Some Luma events only show the area until you register; their address and "
                     "distance are approximate.")
    return kept, scanned, more, notes


async def search_luma(client: LumaClient, p: SearchParams, origin: tuple[float, float],
                      slugs: list[str] | None = None, now: datetime | None = None) -> dict:
    validate(p)
    kept, scanned, more, notes = await luma_events(client, p, origin, slugs, now)
    if p.size:
        lo, hi = LUMA_SIZE_BUCKETS[p.size]
        notes.append(f"On Luma, size uses the real RSVP count: {p.size} = {lo}"
                     f"{'+' if hi is None else f'-{hi}'} guests. Events whose host hides the guest "
                     f"list have no count and are left out.")
    return _result(kept, p, scanned, more, notes)


# ---------------------------------------------------------------------------------------- multi-source
def plan_sources(p: SearchParams, sources: list[str] | tuple[str, ...]) -> tuple[dict[str, dict], list[str]]:
    """Per source: kwargs to run it with ({"p": ..., "slugs": ...}), plus notes for skipped sources.

    Category names resolve per source; a source that knows none of the requested categories (or, for
    Luma, any `formats` filter) is skipped with a note instead of erroring.
    """
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        raise ValueError(f"Unknown source(s) {unknown}; valid: {list(SOURCES)}")
    eb_tags, slugs = split_categories(p.categories)
    if p.formats:
        resolve_formats(p.formats)  # raise early on a bad format name
    plans: dict[str, dict] = {}
    notes: list[str] = []
    for src in dict.fromkeys(sources):
        if src == "eventbrite":
            if p.categories and not eb_tags:
                notes.append(f"Eventbrite skipped: it has no category matching {p.categories}.")
                continue
            plans[src] = {"p": dataclasses.replace(p, categories=eb_tags)}
        elif src == "luma":
            if p.formats:
                notes.append("Luma skipped: it has no event formats, so the `formats` filter cannot apply.")
                continue
            if p.categories and not slugs:
                notes.append(f"Luma skipped: it has no category matching {p.categories} "
                             f"(Luma categories: {', '.join(LUMA_CATEGORIES.values())}).")
                continue
            plans[src] = {"p": p, "slugs": slugs}
    return plans, notes


def dedup_key(ev: dict) -> tuple[str, str]:
    return (re.sub(r"\s+", " ", (ev.get("name") or "").strip().lower()), (ev.get("start") or "")[:16])


def merge_events(lists: list[list[dict]], p: SearchParams) -> tuple[list[dict], int]:
    """Union of per-source results: drop cross-source duplicates (same name + start), then sort.

    Only an event matching one from *another* source is dropped; same-name events within one source
    (different ids, e.g. two venues) are distinct.

    `popularity` interleaves sources by percentile within their own signal, because RSVP counts
    and follower counts are not comparable. Returns (events, duplicates dropped).
    """
    seen: dict[tuple[str, str], set] = {}
    merged: list[dict] = []
    dropped = 0
    for events in lists:
        for ev in events:
            key = dedup_key(ev)
            sources = seen.setdefault(key, set())
            if key[0] and sources - {ev.get("source")}:
                dropped += 1
                continue
            sources.add(ev.get("source"))
            merged.append(ev)
    if p.sort == "popularity":
        rank: dict[int, float] = {}
        for src in {e.get("source") for e in merged}:
            group = sorted((e for e in merged if e.get("source") == src), key=lambda e: -popularity(e))
            for i, e in enumerate(group):
                rank[id(e)] = i / len(group)
        merged.sort(key=lambda e: (rank[id(e)], -popularity(e), e["start"] or ""))
    else:
        merged.sort(key=_sort_key(p))
    return merged, dropped


async def search_sources(clients: dict[str, object], p: SearchParams, origin: tuple[float, float],
                         sources: list[str] | tuple[str, ...] | None = None,
                         now: datetime | None = None) -> dict:
    """Search every selected source concurrently and merge. A failing source becomes a note."""
    validate(p)
    sources = list(sources or clients)
    plans, notes = plan_sources(p, sources)

    async def run(src: str, plan: dict) -> dict:
        if src == "luma":
            return await search_luma(clients["luma"], plan["p"], origin, plan.get("slugs"), now)  # type: ignore[arg-type]
        return await search_eventbrite(clients["eventbrite"], plan["p"], origin)  # type: ignore[arg-type]

    names = list(plans)
    results = await asyncio.gather(*(run(n, plans[n]) for n in names), return_exceptions=True)
    ok: dict[str, dict] = {}
    failures: list[str] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            if isinstance(res, ValueError):
                raise res
            if not isinstance(res, Exception):
                raise res
            log.warning("%s search failed: %s", name, res)
            failures.append(f"{name} unavailable: {res}")
            continue
        ok[name] = res
    if names and not ok:
        raise EventsError("All event sources failed. " + " | ".join(failures))
    notes.extend(failures)
    for res in ok.values():
        notes.extend(n for n in res["notes"] if n not in notes)

    events, dropped = merge_events([ok[n]["events"] for n in ok], p)
    if dropped:
        notes.append(f"{dropped} event(s) listed on both Eventbrite and Luma were shown once.")
    if p.sort == "popularity" and len(ok) > 1:
        notes.append("Popularity ranks each source by its own signal (Luma: RSVP count, Eventbrite: "
                     "organizer followers) and interleaves them.")
    cap = max(1, p.max_results)
    out = events[:cap]
    matched = sum(r["matched"] for r in ok.values()) - dropped
    return {
        "count": len(out),
        "matched": matched,
        "scanned": sum(r["scanned"] for r in ok.values()),
        "more_available": any(r["more_available"] for r in ok.values()) or len(events) > len(out),
        "by_source": {src: sum(1 for e in out if e.get("source") == src) for src in sources if src in ok},
        "notes": notes,
        "events": out,
    }
