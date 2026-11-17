from datetime import date, datetime, timezone

import httpx

import pytest

from conftest import ORIGIN, FakeLuma, FakeSite, make_event, make_luma_entry
from nearby_events_mcp import server
from nearby_events_mcp.client import EventsError
from nearby_events_mcp.constants import split_categories
from nearby_events_mcp.luma import (decode_cursor, fetch_window, get_event_details, local_midnight_utc, make_cursor,
                                    normalize_luma_event)
from nearby_events_mcp.normalize import prosemirror_to_text
from nearby_events_mcp.search import (SearchParams, passes, plan_sources, preset_range, search_luma,
                                      search_sources)

NOW = datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc)  # 08:00 on 1 Jan 2099 in Singapore
ids = lambda r: [e["id"] for e in r["events"]]  # noqa: E731


def _cursors(fake):
    return [decode_cursor(d["pagination_cursor"]) for d in fake.discovers if "pagination_cursor" in d]


# ---------------------------------------------------------------------------------------- normalise
def test_normalize_utc_to_local_price_and_place():
    e = make_luma_entry(1, "Talk", start="2099-01-02T19:30", cents=2500, max_cents=4000, guests=42,
                        approval=True)
    assert e["event"]["start_at"] == "2099-01-02T11:30:00.000Z"  # stored as UTC
    ev = normalize_luma_event(e, ORIGIN, NOW)
    assert ev["source"] == "luma" and ev["id"] == "evt-1"
    assert ev["start"] == "2099-01-02T19:30" and ev["end"] == "2099-01-02T21:30"  # back to local
    assert ev["timezone"] == "Asia/Singapore"
    assert (ev["price_min"], ev["price_max"], ev["currency"]) == (25.0, 40.0, "SGD")
    assert ev["price"] == "SGD 25.00–40.00" and not ev["is_free"]
    assert ev["attendees"] == 42 and ev["organizer"] == "Cal" and ev["organizer_followers"] is None
    assert ev["url"] == "https://luma.com/slug-1" and ev["requires_approval"] is True
    assert ev["venue"] == "Hall 1" and ev["address"].startswith("Hall 1, 1 Road")
    assert 5 < ev["distance_km"] < 7 and not ev["_ended"]

    free = normalize_luma_event(make_luma_entry(2, free=True), ORIGIN, NOW)
    assert free["is_free"] and free["price"] == "Free" and free["price_min"] == 0.0
    single = normalize_luma_event(make_luma_entry(3, cents=1000), ORIGIN, NOW)
    assert single["price_max"] == 10.0 and single["price"] == "SGD 10.00"


def test_normalize_sold_out_online_obfuscated_ended():
    assert normalize_luma_event(make_luma_entry(1, cents=500, sold_out=True), ORIGIN, NOW)["sold_out"]
    online = normalize_luma_event(make_luma_entry(2, free=True, online=True), ORIGIN, NOW)
    assert online["is_online"] and online["distance_km"] is None
    personal = make_luma_entry(6, free=True, calendar="Personal", hosts=("Jane Host",))
    personal["calendar"]["is_personal"] = True
    assert normalize_luma_event(personal, ORIGIN, NOW)["organizer"] == "Jane Host"
    hidden = normalize_luma_event(make_luma_entry(3, free=True, obfuscated=True), ORIGIN, NOW)
    assert hidden["venue"] == "Ang Mo Kio" and hidden["_approx"]
    assert hidden["address"] == "Ang Mo Kio, Singapore (exact address for guests only)"
    old = normalize_luma_event(make_luma_entry(4, free=True, start="2000-01-01T10:00"), ORIGIN, NOW)
    assert old["_ended"]
    other_tz = normalize_luma_event(make_luma_entry(5, free=True, start="2099-01-02T09:00",
                                                    tz="America/New_York"), ORIGIN, NOW)
    assert other_tz["start"] == "2099-01-02T09:00"


def test_cursor_format_and_prosemirror():
    c = make_cursor(local_midnight_utc(date(2026, 11, 28), "Asia/Singapore"))
    assert "=" not in c and decode_cursor(c) == {"sv": "2026-11-27 16:00:00+00", "fb": ""}
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Hi "},
                                          {"type": "text", "text": "there", "marks": [{"type": "bold"}]}]},
        {"type": "bullet_list", "content": [{"type": "list_item", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "a"}]}]}]}]}
    assert prosemirror_to_text(doc) == "Hi there\n\n• a"
    assert prosemirror_to_text(None) == "" and prosemirror_to_text(doc, 5).endswith("…")


def test_preset_ranges():
    tue = date(2026, 11, 17)
    assert preset_range("today", tue) == (tue, tue)
    assert preset_range("tomorrow", tue) == (date(2026, 11, 18),) * 2
    # Friday-Sunday, matching Eventbrite's server-side preset
    assert preset_range("this_weekend", tue) == (date(2026, 11, 20), date(2026, 11, 22))
    assert preset_range("this_weekend", date(2026, 11, 21)) == (date(2026, 11, 21), date(2026, 11, 22))
    assert preset_range("this_weekend", date(2026, 11, 22)) == (date(2026, 11, 22),) * 2
    assert preset_range("this_week", tue) == (tue, date(2026, 11, 22))
    assert preset_range("this_month", tue) == (tue, date(2026, 11, 30))
    assert preset_range("current_future", tue) == (tue, None)


# ---------------------------------------------------------------------------------------- date window
def _days(n, **kw):
    return [make_luma_entry(f"{d:02d}", f"Day {d}", start=f"2099-01-{d:02d}T19:00", cents=1000, **kw)
            for d in range(1, n + 1)]


async def test_window_stops_paging_past_last_day(make_luma):
    fake = FakeLuma(_days(10), page_size=2, now=NOW)
    out = await fetch_window(make_luma(fake), ORIGIN, (date(2099, 1, 1), date(2099, 1, 3)), now=NOW)
    assert [e["id"] for e in out.events] == ["evt-01", "evt-02", "evt-03"]
    assert len(fake.discovers) == 2 and not out.more  # page 2 (days 3-4) passed day 3: stop
    assert _cursors(fake)[0]["fb"] == "evt-02"  # the feed's own cursor, no jump needed


async def test_window_jumps_with_crafted_cursor(make_luma):
    fake = FakeLuma(_days(10), page_size=2, now=NOW)
    out = await fetch_window(make_luma(fake), ORIGIN, (date(2099, 1, 8), date(2099, 1, 8)), now=NOW)
    assert [e["id"] for e in out.events] == ["evt-08"]
    # page 1 (days 1-2) is before the window, so jump to local midnight of 8 Jan (16:00Z on the 7th)
    assert _cursors(fake)[0] == {"sv": "2099-01-07 16:00:00+00", "fb": ""}
    assert len(fake.discovers) == 2 and out.notes == []


async def test_window_falls_back_when_cursor_rejected(make_luma):
    fake = FakeLuma(_days(10), page_size=2, now=NOW, reject_crafted=True)
    out = await fetch_window(make_luma(fake), ORIGIN, (date(2099, 1, 6), date(2099, 1, 6)), now=NOW)
    assert [e["id"] for e in out.events] == ["evt-06"]  # found by paging forward instead
    assert _cursors(fake)[0]["fb"] == "" and all(c["fb"] for c in _cursors(fake)[1:])
    assert any("rejected the date jump" in n for n in out.notes)


async def test_window_page_cap_reports_more(make_luma):
    fake = FakeLuma(_days(10), page_size=2, now=NOW)
    out = await fetch_window(make_luma(fake), ORIGIN, (date(2099, 1, 1), None), max_pages=2, now=NOW)
    assert len(out.events) == 4 and out.more and len(fake.discovers) == 2


async def test_tomorrow_resolved_in_area_timezone(make_luma):
    # 23:30 UTC on 1 Jan is already 2 Jan in Singapore, so "tomorrow" there is 3 Jan.
    now = datetime(2099, 1, 1, 23, 30, tzinfo=timezone.utc)
    fake = FakeLuma(_days(5), page_size=50, now=now)
    res = await search_luma(make_luma(fake), SearchParams(date="tomorrow", sort="date"), ORIGIN, now=now)
    assert ids(res) == ["evt-03"]


async def test_client_side_window_drops_out_of_range(make_luma):
    # A Luma that ignores the crafted cursor (serves from the start again) still yields only the window.
    class Ignoring(FakeLuma):
        def __call__(self, request):
            if "pagination_cursor" in request.url.params:
                cur = decode_cursor(request.url.params["pagination_cursor"])
                if cur["fb"] == "":
                    request.url = request.url.copy_remove_param("pagination_cursor")
            return super().__call__(request)

    fake = Ignoring(_days(10), page_size=3, now=NOW)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-05", sort="date"), ORIGIN, now=NOW)
    assert ids(res) == ["evt-05"]


# ---------------------------------------------------------------------------------------- price
async def test_price_free_and_paid_sent_server_side(make_luma):
    entries = [make_luma_entry(1, "Free", free=True), make_luma_entry(2, "Cheap", cents=2500),
               make_luma_entry(3, "Pricey", cents=6000)]
    fake = FakeLuma(entries, page_size=50)
    c = make_luma(fake)
    p = dict(date="2099-01-01", sort="date")
    assert ids(await search_luma(c, SearchParams(price="free", **p), ORIGIN, now=NOW)) == ["evt-1"]
    assert fake.discovers[-1]["price"] == "free"
    assert ids(await search_luma(c, SearchParams(min_price=50, **p), ORIGIN, now=NOW)) == ["evt-3"]
    assert fake.discovers[-1]["price"] == "paid"  # narrowed on the server, 25.00 dropped on the client
    assert ids(await search_luma(c, SearchParams(max_price=10, **p), ORIGIN, now=NOW)) == ["evt-1"]
    assert "price" not in fake.discovers[-1]


async def test_client_side_filters(make_luma):
    entries = [
        make_luma_entry(1, "Near", free=True, lat=1.355, lng=103.82, guests=10),
        make_luma_entry(2, "Far", free=True, lat=1.45, lng=104.0, guests=10),  # ~22 km away
        make_luma_entry(3, "Late big", free=True, start="2099-01-01T21:00", guests=300),
        make_luma_entry(4, "Mid", free=True, guests=80),
        make_luma_entry(5, "Gone", cents=100, sold_out=True),
        make_luma_entry(6, "Online", free=True, online=True),
    ]
    fake = FakeLuma(entries, page_size=50)
    c = make_luma(fake)
    run = lambda **kw: search_luma(c, SearchParams(date="2099-01-01", sort="date", **kw), ORIGIN, now=NOW)  # noqa: E731
    assert ids(await run()) == ["evt-1", "evt-4", "evt-5", "evt-3"]  # far + online dropped
    assert "evt-2" in ids(await run(radius_km=30))
    assert ids(await run(start_after="20:00")) == ["evt-3"]
    assert ids(await run(size="large")) == ["evt-3"]
    assert ids(await run(size="medium")) == ["evt-4"]
    assert ids(await run(min_followers=1000)) == ["evt-1", "evt-4", "evt-5", "evt-3"]  # EB-only filter
    assert ids(await run(available_only=True)) == ["evt-1", "evt-4", "evt-3"]
    assert "evt-6" in ids(await run(include_online=True))
    pop = await search_luma(c, SearchParams(date="2099-01-01", sort="popularity"), ORIGIN, now=NOW)
    assert ids(pop)[0] == "evt-3"
    assert any("RSVP" in n for n in (await run(size="small"))["notes"])


def test_passes_picks_size_signal_by_source():
    lu = normalize_luma_event(make_luma_entry(1, free=True, guests=200), ORIGIN, NOW)
    assert passes(lu, SearchParams(size="large"), []) and not passes(lu, SearchParams(size="small"), [])


# ---------------------------------------------------------------------------------------- categories
def test_category_mapping():
    assert split_categories(["Science & Tech"]) == (["EventbriteCategory/102"], ["tech"])
    assert split_categories(["AI"]) == ([], ["ai"])  # never "Auto, Boat & Air"
    assert split_categories(["Music", "crypto"]) == (["EventbriteCategory/103"], ["crypto"])
    with pytest.raises(ValueError, match="Valid options"):
        split_categories(["nonsense"])
    plans, notes = plan_sources(SearchParams(categories=["AI"]), ["eventbrite", "luma"])
    assert list(plans) == ["luma"] and "Eventbrite skipped" in notes[0]
    plans, notes = plan_sources(SearchParams(formats=["Networking"]), ["eventbrite", "luma"])
    assert list(plans) == ["eventbrite"] and "formats" in notes[0]


async def test_category_slugs_one_chain_each(make_luma):
    entries = [make_luma_entry(1, "Tech talk", free=True, slugs=["tech"]),
               make_luma_entry(2, "AI night", free=True, slugs=["ai", "tech"]),
               make_luma_entry(3, "Run club", free=True, slugs=["running"])]
    fake = FakeLuma(entries, page_size=50)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-01", sort="date"), ORIGIN,
                            slugs=["tech", "ai"], now=NOW)
    assert sorted(ids(res)) == ["evt-1", "evt-2"] and res["scanned"] == 2
    assert sorted(d["slug"] for d in fake.discovers) == ["ai", "tech"]
    assert sorted(next(e for e in res["events"] if e["id"] == "evt-2")["categories"]) == ["AI", "Tech"]


# ---------------------------------------------------------------------------------------- multi-source
async def test_search_sources_merges_sorts_and_dedups(make_client, make_luma):
    eb = FakeSite([make_event(1, "Shared Meetup", free=True, start="2099-01-01T18:30", followers=900),
                   make_event(2, "EB only", pmin=5, pmax=5, start="2099-01-01T20:00", followers=5)],
                  page_size=50)
    lu = FakeLuma([make_luma_entry(9, "shared meetup", free=True, start="2099-01-01T18:30"),
                   make_luma_entry(8, "Luma only", free=True, start="2099-01-01T19:00", guests=500)],
                  page_size=50)
    clients = {"eventbrite": make_client(eb), "luma": make_luma(lu)}
    res = await search_sources(clients, SearchParams(date="2099-01-01", sort="date"), ORIGIN, now=NOW)
    assert ids(res) == ["1", "evt-8", "2"]
    assert [e["source"] for e in res["events"]] == ["eventbrite", "luma", "eventbrite"]
    assert res["by_source"] == {"eventbrite": 2, "luma": 1}
    assert any("both Eventbrite and Luma" in n for n in res["notes"])
    assert "_approx" not in res["events"][1]
    pop = await search_sources(clients, SearchParams(date="2099-01-01", sort="popularity"), ORIGIN, now=NOW)
    assert ids(pop)[:2] in (["1", "evt-8"], ["evt-8", "1"])  # each source's top first
    assert any("own signal" in n for n in pop["notes"])


async def test_failing_source_becomes_note(make_client, make_luma):
    eb = FakeSite([make_event(1, "A", free=True)], page_size=50)
    clients = {"eventbrite": make_client(eb), "luma": make_luma(FakeLuma([], fail_with=403))}
    res = await search_sources(clients, SearchParams(date="2099-01-01"), ORIGIN, now=NOW)
    assert ids(res) == ["1"] and any(n.startswith("luma unavailable") for n in res["notes"])
    clients["eventbrite"] = make_client(FakeSite([], fail_first_search_with=404))
    with pytest.raises(EventsError, match="All event sources failed"):
        await search_sources(clients, SearchParams(date="2099-01-01"), ORIGIN, now=NOW)


# ---------------------------------------------------------------------------------------- tools
@pytest.fixture
def wired_both(make_client, make_luma, monkeypatch):
    eb = FakeSite([make_event(1, "EB", free=True, start="2099-01-02T10:00")], page_size=50)
    lu = FakeLuma([make_luma_entry(f"d{d}", f"Luma {d}", free=True, start=f"2099-01-0{d}T19:00")
                   for d in (1, 2, 3, 4, 5)], page_size=2)
    c = make_client(eb, default_lat=1.3521, default_lng=103.8198)
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_settings", c.s)
    monkeypatch.setattr(server, "_luma", make_luma(lu))
    return eb, lu


def _payload(result):
    import json
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_daily_events_one_luma_chain_for_range(wired_both):
    eb, lu = wired_both
    out = _payload(await server.mcp.call_tool("daily_events", {"days": 3, "start_date": "2099-01-01"}))
    luma_ids = {d: [e["id"] for e in v["events"] if e["source"] == "luma"] for d, v in out["days"].items()}
    assert luma_ids == {"2099-01-01": ["evt-d1"], "2099-01-02": ["evt-d2"], "2099-01-03": ["evt-d3"]}
    assert [e["source"] for e in out["days"]["2099-01-02"]["events"]] == ["eventbrite", "luma"]  # by time
    assert len(eb.searches) == 3  # Eventbrite: one search per day
    assert len(lu.discovers) == 2  # Luma: a single chain (days 1-2, then 3-4 passes the range)


async def test_search_events_sources_arg(wired_both):
    eb, lu = wired_both
    out = _payload(await server.mcp.call_tool("search_events", {"date": "2099-01-01", "sources": ["luma"]}))
    assert ids(out) == ["evt-d1"] and not eb.searches and out["by_source"] == {"luma": 1}


async def test_get_event_dispatches_luma_ids(wired_both):
    eb, lu = wired_both
    out = _payload(await server.mcp.call_tool("get_event", {"event_id": "evt-d2"}))
    assert out["source"] == "luma" and out["name"] == "Luma 2" and out["start"] == "2099-01-02T19:00"
    assert out["description"] == "About\n\nHello world\nline two\n\n• one\n• two"
    assert [t["name"] for t in out["ticket_classes"]] == ["General"]  # hidden tier dropped
    assert out["ticket_classes"][0]["free"] and out["ticket_classes"][0]["capacity"] == 40
    assert out["ticket_classes"][0]["price"] == "Free" and out["currency"] is None  # free tiers: null currency
    assert set(out["ticket_classes"][0]) == {"name", "free", "donation", "price", "currency", "capacity",
                                             "on_sale_status", "min_per_order", "max_per_order"}
    assert lu.requests[-1].url.params["event_api_id"] == "evt-d2" and not eb.requests


async def test_list_categories_includes_luma():
    out = _payload(await server.mcp.call_tool("list_categories", {}))
    assert out["luma_categories"]["ai"] == "AI"


def test_sources_setting(monkeypatch):
    from nearby_events_mcp.config import Settings
    monkeypatch.setenv("NEARBY_EVENTS_SOURCES", "luma, bogus")
    assert Settings.from_env().sources == ("luma",)
    monkeypatch.setenv("NEARBY_EVENTS_SOURCES", "")
    assert Settings.from_env().sources == ("eventbrite", "luma")


def test_installer_sources_flag():
    import json
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "scripts/install_claude_desktop.py"
    r = subprocess.run([sys.executable, str(script), "--print-only", "--python", sys.executable,
                        "--location", "Singapore", "--sources", "luma"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["mcpServers"]["nearby-events"]["env"]["NEARBY_EVENTS_SOURCES"] == "luma"
    bad = subprocess.run([sys.executable, str(script), "--print-only", "--sources", "meetup"],
                         capture_output=True, text=True)
    assert bad.returncode != 0


# ---------------------------------------------------------------------------------------- review regressions
async def test_date_sort_early_stop_counts_only_filtered_events(make_luma):
    # Four daytime events fill page 1; the evening ones asked for are on page 2.
    day = [make_luma_entry(f"a{i}", f"Day {i}", free=True, start=f"2099-01-01T1{i}:00") for i in range(4)]
    eve = [make_luma_entry(f"b{i}", f"Eve {i}", free=True, start=f"2099-01-01T19:{i}0") for i in range(4)]
    fake = FakeLuma(day + eve, page_size=4)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-01", sort="date", max_results=4,
                                                          start_after="18:00"), ORIGIN, now=NOW)
    assert ids(res) == ["evt-b0", "evt-b1", "evt-b2", "evt-b3"]
    # Events outside the radius ahead of nearby ones must not count toward the early stop either.
    far = [make_luma_entry(f"f{i}", f"Far {i}", free=True, start=f"2099-01-01T1{i}:00", lat=1.6, lng=104.1)
           for i in range(6)]
    near = [make_luma_entry(f"n{i}", f"Near {i}", free=True, start=f"2099-01-01T19:{i}0", lat=1.352,
                            lng=103.82) for i in range(3)]
    fake = FakeLuma(far + near, page_size=5)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-01", sort="date", max_results=3,
                                                          radius_km=5), ORIGIN, now=NOW)
    assert ids(res) == ["evt-n0", "evt-n1", "evt-n2"] and len(fake.discovers) == 2


async def test_foreign_timezone_entry_does_not_end_paging(make_luma):
    # Page 1 ends with an online event hosted from Sydney (already 2 Jan there); the Singapore day
    # still has events on page 2, so paging must go on (decided in the area's timezone).
    entries = [make_luma_entry("sg1", start="2099-01-01T19:00", free=True),
               make_luma_entry("syd", start="2099-01-02T01:30", tz="Australia/Sydney", free=True, online=True),
               make_luma_entry("sg-late", start="2099-01-01T23:00", free=True),
               make_luma_entry("sg-late2", start="2099-01-01T23:30", free=True)]
    fake = FakeLuma(entries, page_size=2)
    out = await fetch_window(make_luma(fake), ORIGIN, (date(2099, 1, 1), date(2099, 1, 1)), now=NOW)
    assert [e["id"] for e in out.events] == ["evt-sg1", "evt-sg-late", "evt-sg-late2"]


async def test_window_uses_calendar_timezone_when_event_has_none(make_luma):
    e = make_luma_entry("cal", start="2099-01-02T07:00", free=True)  # = 23:00Z on 1 Jan
    e["event"]["timezone"] = None
    e["calendar"]["timezone"] = "Asia/Singapore"
    c = make_luma(FakeLuma([e], page_size=50))
    assert ids(await search_luma(c, SearchParams(date="2099-01-01"), ORIGIN, now=NOW)) == []
    res = await search_luma(c, SearchParams(date="2099-01-02"), ORIGIN, now=NOW)
    assert ids(res) == ["evt-cal"] and res["events"][0]["start"] == "2099-01-02T07:00"


async def test_hidden_guest_list_is_unknown_size(make_luma):
    hidden = make_luma_entry("h", "Hidden", free=True, guests=500, show_guests=False)
    assert hidden["guest_count"] == 0
    assert normalize_luma_event(hidden, ORIGIN, NOW)["attendees"] is None
    c = make_luma(FakeLuma([hidden, make_luma_entry("s", "Small", free=True, guests=5)], page_size=50))
    run = lambda **kw: search_luma(c, SearchParams(date="2099-01-01", **kw), ORIGIN, now=NOW)  # noqa: E731
    assert ids(await run(size="small")) == ["evt-s"]  # not counted as "small"
    assert ids(await run(size="large")) == []
    assert any("hides the guest list" in n for n in (await run(size="small"))["notes"])
    assert (await get_event_details(c, "evt-h"))["attendees"] is None


async def test_offline_event_without_coordinates_is_dropped(make_luma):
    nowhere = make_luma_entry("x", "Somewhere else", free=True, tz="America/New_York",
                              start="2099-01-01T05:30")  # same instant window as Singapore's day
    nowhere["event"]["coordinate"] = None
    nowhere["event"]["geo_address_info"] = {"mode": "obfuscated"}
    fake = FakeLuma([nowhere, make_luma_entry("ok", free=True)], page_size=50)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-01"), ORIGIN, now=NOW)
    assert ids(res) == ["evt-ok"]


class Relevance(FakeLuma):
    """Keyword search as observed live: global, relevance-ranked, not date-sorted."""

    def __call__(self, request):
        params = dict(request.url.params)
        if request.url.path != "/discover/get-paginated-events" or "query" not in params:
            return super().__call__(request)
        self.requests.append(request)
        self.discovers.append(params)
        rows = [self._public(e) for e in self.entries_in_order]
        if params.get("pagination_cursor") == "p2":
            return httpx.Response(200, json={"entries": rows[2:], "has_more": False, "next_cursor": None})
        return httpx.Response(200, json={"entries": rows[:2], "has_more": True, "next_cursor": "p2"})


async def test_keyword_search_is_read_as_unsorted(make_luma):
    # 17:00Z on 1 Jan: 2 Jan in Singapore but still 1 Jan in New York.
    now = datetime(2099, 1, 1, 17, 0, tzinfo=timezone.utc)
    order = [make_luma_entry("ny", "Fintech NYC", free=True, tz="America/New_York", start="2099-01-05T18:00",
                             lat=40.7, lng=-74.0),
             make_luma_entry("sg2", "Fintech SG 2", free=True, start="2099-01-02T19:00"),
             make_luma_entry("sg3", "Fintech SG 3", free=True, start="2099-01-03T19:00"),
             make_luma_entry("sg3b", "Fintech SG 3b", free=True, start="2099-01-03T12:00")]
    fake = Relevance(order)
    fake.entries_in_order = order
    res = await search_luma(make_luma(fake), SearchParams(date="tomorrow", query="fintech", sort="date"),
                            ORIGIN, now=now)
    # "tomorrow" is resolved in Singapore (nearest in-person event), not New York (first entry);
    # page 1 ending past the window does not stop paging, and no date jump is attempted.
    assert ids(res) == ["evt-sg3b", "evt-sg3"]
    assert len(fake.discovers) == 2 and _cursors_raw(fake) == ["p2"]
    assert any("top 50 matches" in n for n in res["notes"])


def _cursors_raw(fake):
    return [d["pagination_cursor"] for d in fake.discovers if "pagination_cursor" in d]


async def test_running_multi_day_event_overlaps_window(make_luma):
    fest = make_luma_entry("fest", "Festival", free=True, start="2099-01-01T10:00", hours=72)
    fake = FakeLuma([fest, *_days(4, free=False)], page_size=50, now=NOW)
    res = await search_luma(make_luma(fake), SearchParams(date="2099-01-02", sort="date"), ORIGIN, now=NOW)
    assert ids(res) == ["evt-fest", "evt-02"]


async def test_same_name_events_within_one_source_are_kept(make_client, make_luma):
    eb = FakeSite([make_event(1, "Yoga in the Park", free=True), make_event(2, "Yoga in the Park", free=True,
                                                                           lat="1.35", lng="103.82")],
                  page_size=50)
    lu = FakeLuma([make_luma_entry("y", "Yoga in the park", free=True)], page_size=50)
    clients = {"eventbrite": make_client(eb), "luma": make_luma(lu)}
    res = await search_sources(clients, SearchParams(date="2099-01-01"), ORIGIN, ["eventbrite"], now=NOW)
    assert sorted(ids(res)) == ["1", "2"] and res["matched"] == 2 and not res["notes"]
    both = await search_sources(clients, SearchParams(date="2099-01-01"), ORIGIN, now=NOW)
    assert sorted(ids(both)) == ["1", "2"]  # the Luma copy is the cross-source duplicate
    assert any("both Eventbrite and Luma" in n for n in both["notes"])


async def test_daily_events_first_day_is_area_today(wired_both, monkeypatch):
    eb, lu = wired_both
    # 20:00Z on 1 Jan 2099 is already 2 Jan in Singapore (and nowhere near the machine's date).
    monkeypatch.setattr(server, "_utcnow", lambda: datetime(2099, 1, 1, 20, 0, tzinfo=timezone.utc))
    out = _payload(await server.mcp.call_tool("daily_events", {"days": 2}))
    assert list(out["days"]) == ["2099-01-02", "2099-01-03"]
    assert [e["id"] for e in out["days"]["2099-01-02"]["events"]] == ["1", "evt-d2"]
    assert [s["event_search"]["date_range"]["from"] for s in eb.searches] == ["2099-01-02", "2099-01-03"]
