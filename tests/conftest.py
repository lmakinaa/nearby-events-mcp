import json

import httpx
import pytest

from nearby_events_mcp.client import EventsClient
from nearby_events_mcp.config import Settings

ORIGIN = (1.3521, 103.8198)


def make_event(eid, name="Event", lat="1.30", lng="103.85", pmin=None, pmax=None, free=False,
               followers=50, start="2099-01-01T18:30", online=False, sold_out=False,
               sales="on_sale", tags=None, ended_date=None):
    date, time_ = start.split("T")
    return {
        "id": str(eid), "name": name, "url": f"https://www.eventbrite.sg/e/{eid}",
        "summary": "<p>Some <b>summary</b></p>", "timezone": "Asia/Singapore",
        "start_date": date, "start_time": time_, "end_date": ended_date or date, "end_time": "23:00",
        "is_online_event": online, "num_children": 1,
        "primary_venue": {"name": "Venue", "address": {
            "latitude": lat, "longitude": lng, "localized_address_display": "1 Road, Singapore"}},
        "primary_organizer": {"name": "Org", "num_followers": followers},
        "ticket_availability": {
            "is_free": free, "is_sold_out": sold_out, "has_available_tickets": not sold_out,
            "minimum_ticket_price": None if free else {"currency": "SGD", "value": int(pmin * 100), "major_value": f"{pmin:.2f}", "display": f"${pmin:.2f}"},
            "maximum_ticket_price": None if free else {"currency": "SGD", "value": int(pmax * 100), "major_value": f"{pmax:.2f}", "display": f"${pmax:.2f}"},
        },
        "event_sales_status": {"sales_status": sales, "currency": "SGD"},
        "urgency_signals": {"messages": []},
        "tags": [{"prefix": t.split("/")[0], "tag": t, "display_name": t} for t in (tags or [])],
    }


class FakeSite:
    """MockTransport handler emulating the bits of Eventbrite the client uses."""

    def __init__(self, events, page_size=2, capacities=None, fail_first_search_with=None):
        self.events = events
        self.page_size = page_size
        self.capacities = capacities or {}
        self.requests: list[httpx.Request] = []
        self.searches: list[dict] = []
        self.fail_first = fail_first_search_with
        self.token = "tok1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/":
            self.token = f"tok{sum(1 for r in self.requests if r.url.path == '/')}"
            return httpx.Response(200, text="<html/>", headers={"set-cookie": f"csrftoken={self.token}; Path=/"})
        if path == "/api/v3/destination/search/":
            if self.fail_first and not self.searches:
                self.searches.append({})
                return httpx.Response(self.fail_first, text="nope")
            if request.headers.get("x-csrftoken") != self.token:
                return httpx.Response(401, text="csrf")
            body = json.loads(request.content)
            self.searches.append(body)
            es = body["event_search"]
            page, size = es["page"], self.page_size
            chunk = self.events[(page - 1) * size: page * size]
            pages = max(1, -(-len(self.events) // size))
            return httpx.Response(200, json={"events": {
                "pagination": {"object_count": len(self.events), "page_count": pages, "page_number": page,
                               "page_size": size},
                "results": chunk}})
        if path.endswith("/ticket_classes/"):
            eid = path.split("/")[-3]
            cap = self.capacities.get(eid)
            return httpx.Response(200, json={"ticket_classes": [{"name": "GA", "capacity": cap, "cost": {"display": "$5"}}]})
        return httpx.Response(404, text="not found")


@pytest.fixture
def make_client():
    clients = []

    def _make(fake, **settings):
        s = Settings(min_interval=0, **settings)
        c = EventsClient(s, transport=httpx.MockTransport(fake))
        clients.append(c)
        return c

    yield _make
