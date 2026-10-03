import pytest

from conftest import ORIGIN, FakeSite, make_event
from nearby_events_mcp.constants import resolve_categories, resolve_formats
from nearby_events_mcp.normalize import haversine_km, html_to_text, normalize_event
from nearby_events_mcp.search import SearchParams, build_event_search, run_search


def test_resolve_names_ids_and_errors():
    assert resolve_categories(["Business", "music", "102", "tech"]) == [
        "EventbriteCategory/101", "EventbriteCategory/103", "EventbriteCategory/102"]
    assert resolve_formats(["networking"]) == ["EventbriteFormat/10"]
    with pytest.raises(ValueError, match="Valid options"):
        resolve_categories(["nonsense"])


def test_haversine_and_html():
    assert haversine_km(1.3521, 103.8198, 1.3521, 103.8198) == 0
    assert 110 < haversine_km(0, 0, 1, 0) < 112
    assert html_to_text("<p>Hi&nbsp;<b>there</b></p><br>x", 100).startswith("Hi")


def test_normalize_prices_distance():
    ev = normalize_event(make_event(1, pmin=8, pmax=20, followers=12), ORIGIN)
    assert ev["price_min"] == 8.0 and ev["price_max"] == 20.0 and ev["price"] == "SGD 8.00–20.00"
    assert ev["distance_km"] and ev["organizer_followers"] == 12 and not ev["is_free"]
    assert normalize_event(make_event(2, free=True), ORIGIN)["price"] == "Free"


def test_build_event_search_matches_verified_api_shape():
    p = SearchParams(date="today", radius_km=5.0, price="free", categories=["Business"], sort="popularity")
    es = build_event_search(p, ORIGIN, 1)
    assert es["point_radius"] == {"latitude": 1.3521, "longitude": 103.8198, "radius": "5km"}  # never "5.0km"
    assert es["dates"] == "today" and es["price"] == "free" and es["page_size"] == 50
    assert es["tags"] == ["EventbriteCategory/101"] and es["sort"] == "date"  # popularity is client-side
    rng = build_event_search(SearchParams(date="2026-10-01", date_to="2026-10-03", radius_km=0.4), ORIGIN, 2)
    assert rng["date_range"] == {"from": "2026-10-01", "to": "2026-10-03"} and "dates" not in rng
    assert rng["point_radius"]["radius"] == "1km" and rng["page"] == 2
    assert build_event_search(SearchParams(sort="distance"), ORIGIN, 1)["sort"] == "distance"


def test_only_server_valid_keys_sent():
    allowed = {"dates", "date_range", "point_radius", "q", "price", "tags", "sort", "page", "page_size", "dedup"}
    p = SearchParams(query="x", price="paid", categories=["Music"], formats=["Party"])
    assert set(build_event_search(p, ORIGIN, 1)) <= allowed


@pytest.mark.parametrize("bad", [dict(date="soon"), dict(start_after="6pm"), dict(price="cheap"),
                                 dict(size="huge"), dict(sort="best"), dict(date="today", date_to="2026-01-01")])
async def test_validation(make_client, bad):
    c = make_client(FakeSite([]))
    with pytest.raises(ValueError):
        await run_search(c, SearchParams(**bad), ORIGIN)


async def test_search_pagination_csrf_and_filters(make_client):
    events = [
        make_event(1, "Free big", free=True, followers=5000, lat="1.353", lng="103.82"),
        make_event(2, "Cheap small", pmin=10, pmax=10, followers=20),
        make_event(3, "Pricey mid", pmin=80, pmax=120, followers=500),
        make_event(4, "Online", free=True, online=True),
        make_event(5, "Sold out", pmin=5, pmax=5, sold_out=True),
        make_event(6, "Late", pmin=15, pmax=15, start="2099-01-01T23:15", followers=10),
        make_event(7, "Old", pmin=1, pmax=1, start="2000-01-01T10:00", ended_date="2000-01-01"),
    ]
    fake = FakeSite(events, page_size=2)
    c = make_client(fake)

    res = await run_search(c, SearchParams(sort="date", max_results=50), ORIGIN)
    assert [e["id"] for e in res["events"]] == ["1", "2", "3", "5", "6"]  # online + ended dropped
    assert len(fake.searches) == 4  # walked all pages
    assert "_ended" not in res["events"][0] and "_tags" not in res["events"][0]
    assert sum(1 for r in fake.requests if r.url.path == "/") == 1  # token bootstrapped once

    ids = lambda r: [e["id"] for e in r["events"]]  # noqa: E731
    assert ids(await run_search(c, SearchParams(max_price=10, sort="date", max_results=50), ORIGIN)) == ["1", "2", "5"]
    assert ids(await run_search(c, SearchParams(size="small", sort="date", max_results=50), ORIGIN)) == ["2", "5", "6"]
    assert ids(await run_search(c, SearchParams(size="large", max_results=50), ORIGIN)) == ["1"]
    assert ids(await run_search(c, SearchParams(min_price=100, max_results=50), ORIGIN)) == ["3"]
    assert ids(await run_search(c, SearchParams(available_only=True, sort="date", max_results=50), ORIGIN)) == ["1", "2", "3", "6"]
    assert ids(await run_search(c, SearchParams(start_after="19:00", sort="date", max_results=50), ORIGIN)) == ["6"]
    assert ids(await run_search(c, SearchParams(start_before="19:00", sort="date", max_results=50), ORIGIN)) == ["1", "2", "3", "5"]
    assert ids(await run_search(c, SearchParams(include_online=True, sort="date", max_results=50), ORIGIN))[:4] == ["1", "2", "3", "4"]
    assert ids(await run_search(c, SearchParams(sort="price", max_results=50), ORIGIN)) == ["1", "5", "2", "6", "3"]
    assert ids(await run_search(c, SearchParams(sort="popularity", max_results=3), ORIGIN)) == ["1", "3", "5"]  # 5000, 500, 50 followers


async def test_early_stop_and_more_available(make_client):
    fake = FakeSite([make_event(i, pmin=1, pmax=1) for i in range(1, 11)], page_size=2)
    res = await run_search(make_client(fake), SearchParams(sort="date", max_results=3), ORIGIN)
    assert res["count"] == 3 and res["more_available"] and len(fake.searches) == 2


async def test_category_plus_format_filters_client_side(make_client):
    events = [make_event(1, tags=["EventbriteCategory/101", "EventbriteFormat/10"], free=True),
              make_event(2, tags=["EventbriteCategory/101", "EventbriteFormat/2"], free=True)]
    fake = FakeSite(events, page_size=50)
    res = await run_search(make_client(fake), SearchParams(categories=["Business"], formats=["Networking"]), ORIGIN)
    assert [e["id"] for e in res["events"]] == ["1"]
    assert fake.searches[0]["event_search"]["tags"] == ["EventbriteCategory/101"]


async def test_capacity_enrichment_and_filter(make_client):
    events = [make_event(1, free=True), make_event(2, free=True), make_event(3, free=True)]
    fake = FakeSite(events, page_size=50, capacities={"1": 30, "2": 500})
    res = await run_search(make_client(fake), SearchParams(max_capacity=100, sort="date"), ORIGIN)
    assert [e["id"] for e in res["events"]] == ["1", "3"]  # 500 dropped, unknown (3) kept
    assert res["events"][0]["capacity"] == 30 and res["events"][1]["capacity"] is None
    assert any("Capacity" in n for n in res["notes"])


async def test_csrf_refresh_on_401(make_client):
    fake = FakeSite([make_event(1, free=True)], page_size=50)
    c = make_client(fake)
    await c.search({"page": 1})
    fake.token = "rotated"  # server invalidates our token
    fake.requests.clear()
    data = await c.search({"page": 1})
    assert data["events"]["results"]
    assert sum(1 for r in fake.requests if r.url.path == "/") == 1  # re-bootstrapped once


async def test_retry_on_5xx_then_success(make_client):
    fake = FakeSite([make_event(1, free=True)], page_size=50, fail_first_search_with=503)
    data = await make_client(fake).search({"page": 1})
    assert data["events"]["results"]


def test_geocode_candidates_simplify_verbose_addresses():
    from nearby_events_mcp.geo import candidate_queries
    c = candidate_queries("34 Whampoa West, near Boon Keng MRT station, Singapore")
    assert c[0] == "34 Whampoa West, near Boon Keng MRT station, Singapore"
    assert "34 Whampoa West, Singapore" in c and "Boon Keng MRT station, Singapore" in c
    assert c[-1] == "Singapore"
    assert candidate_queries("near Orchard") == ["near Orchard", "Orchard"]


async def test_geocode_falls_back_to_simpler_query():
    import httpx
    from nearby_events_mcp import geo

    seen = []

    def handler(req):
        q = req.url.params["q"]
        seen.append(q)
        if q == "34 Whampoa West, Singapore":
            return httpx.Response(200, json=[{"lat": "1.32", "lon": "103.86", "display_name": "Whampoa"}])
        return httpx.Response(200, json=[])

    geo._CACHE.clear()
    lat, lng, _ = await geo.geocode("34 Whampoa West, near Boon Keng MRT station, Singapore",
                                    transport=httpx.MockTransport(handler))
    assert (lat, lng) == (1.32, 103.86) and len(seen) == 2
