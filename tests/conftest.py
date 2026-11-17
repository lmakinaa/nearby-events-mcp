import base64
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from nearby_events_mcp.client import EventsClient
from nearby_events_mcp.config import Settings
from nearby_events_mcp.luma import LumaClient

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


# ---------------------------------------------------------------------------------------- Luma
def _luma_utc(local: str, tz: str) -> datetime:
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_luma_entry(eid, name="Luma event", start="2099-01-01T18:30", hours=2, tz="Asia/Singapore",
                    lat=1.30, lng=103.85, cents=None, max_cents=None, free=False, guests=10,
                    sold_out=False, online=False, obfuscated=False, slugs=None, availability="open",
                    approval=False, currency="sgd", calendar="Cal", hosts=("Host A",), show_guests=True):
    """A discover-feed entry as returned by api.lu.ma (start given in local time of `tz`)."""
    eid = eid if str(eid).startswith("evt-") else f"evt-{eid}"
    s_utc = _luma_utc(start, tz)
    geo = {"city": "Singapore", "country": "Singapore", "sublocality": "Ang Mo Kio",
           "mode": "obfuscated" if obfuscated else "shown"}
    if not obfuscated:
        geo.update({"address": "Hall 1", "full_address": "Hall 1, 1 Road, Singapore 123456"})
    return {
        "api_id": eid,
        "start_at": _iso_z(s_utc),
        "event": {
            "api_id": eid, "name": name, "url": f"slug-{eid[4:]}", "timezone": tz,
            "start_at": _iso_z(s_utc), "end_at": _iso_z(s_utc + timedelta(hours=hours)),
            "location_type": "online" if online else "offline",
            "coordinate": None if online else {"latitude": lat, "longitude": lng},
            "geo_address_info": {} if online else geo,
            "recurrence_id": None, "show_guest_list": show_guests,
        },
        "calendar": {"name": calendar, "slug": None},
        "hosts": [{"name": h} for h in hosts],
        "guest_count": guests if show_guests else 0,  # live: 0 when the guest list is hidden
        "ticket_info": {
            "is_free": free,
            "price": None if free or cents is None else {"cents": cents, "currency": currency},
            "max_price": None if max_cents is None else {"cents": max_cents, "currency": currency},
            "is_sold_out": sold_out, "spots_remaining": 0 if sold_out else 5,
            "is_near_capacity": False, "require_approval": approval,
        },
        "registration_availability": "sold-out" if sold_out else availability,
        "_slugs": list(slugs or []),
    }


def _parse_sv(sv: str) -> datetime:
    return datetime.strptime(sv[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _make_cursor(start_at: str, eid: str) -> str:
    sv = datetime.fromisoformat(start_at.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S+00")
    raw = json.dumps({"sv": sv, "fb": eid}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class FakeLuma:
    """MockTransport handler emulating api.lu.ma discover (cursor paging, slug/query/price) + event/get."""

    def __init__(self, entries, page_size=2, now=None, reject_crafted=False, fail_with=None):
        self.entries = sorted(entries, key=lambda e: (e["start_at"], e["api_id"]))
        self.page_size = page_size
        self.now = now  # feed starts here when no cursor is given (None = from the first entry)
        self.reject_crafted = reject_crafted
        self.fail_with = fail_with
        self.requests: list[httpx.Request] = []
        self.discovers: list[dict] = []

    @staticmethod
    def _public(entry):
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, params = request.url.path, dict(request.url.params)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="nope")
        if path == "/discover/get-paginated-events":
            self.discovers.append(params)
            rows = self.entries
            if params.get("slug"):
                rows = [e for e in rows if params["slug"] in e["_slugs"]]
            if params.get("query"):
                rows = [e for e in rows if params["query"].lower() in e["event"]["name"].lower()]
            if params.get("price") == "free":
                rows = [e for e in rows if e["ticket_info"]["is_free"]]
            elif params.get("price") == "paid":
                rows = [e for e in rows if not e["ticket_info"]["is_free"]]
            cursor = params.get("pagination_cursor")
            if cursor:
                cur = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if self.reject_crafted and cur["fb"] == "":
                    return httpx.Response(400, json={"message": "bad cursor"})
                sv = _parse_sv(cur["sv"])

                def after(e, sv=sv, fb=cur["fb"]):
                    t = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00"))
                    return t > sv or (t == sv and (not fb or e["api_id"] > fb))
                rows = [e for e in rows if after(e)]
            elif self.now is not None:
                rows = [e for e in rows
                        if datetime.fromisoformat(e["event"]["end_at"].replace("Z", "+00:00")) >= self.now]
            limit = min(self.page_size, int(params.get("pagination_limit", 50)))
            page, rest = rows[:limit], rows[limit:]
            nxt = _make_cursor(page[-1]["start_at"], page[-1]["api_id"]) if page and rest else None
            return httpx.Response(200, json={"entries": [self._public(e) for e in page],
                                             "has_more": bool(rest), "next_cursor": nxt})
        if path == "/event/get":
            eid = params.get("event_api_id")
            for e in self.entries:
                if e["api_id"] == eid:
                    return httpx.Response(200, json={
                        **self._public(e),
                        "sold_out": e["ticket_info"]["is_sold_out"],
                        "categories": [{"name": s.title()} for s in e["_slugs"]],
                        "ticket_types": [
                            {"name": "General", "cents": e["ticket_info"]["price"]["cents"] if e["ticket_info"]["price"] else 0,
                             # live data: free tiers carry currency null
                             "currency": None if e["ticket_info"]["is_free"] else "sgd",
                             "type": "free" if e["ticket_info"]["is_free"] else "fiat-price",
                             "max_capacity": 40, "spots_remaining": 5, "is_hidden": False, "is_disabled": False,
                             "max_per_guest": 2, "is_flexible": False},
                            {"name": "Hidden", "cents": 0, "currency": "sgd", "is_hidden": True},
                        ],
                        "description_mirror": {"type": "doc", "content": [
                            {"type": "heading", "content": [{"type": "text", "text": "About"}]},
                            {"type": "paragraph", "content": [
                                {"type": "text", "text": "Hello "},
                                {"type": "text", "text": "world", "marks": [{"type": "bold"}]},
                                {"type": "hard_break"}, {"type": "text", "text": "line two"}]},
                            {"type": "bullet_list", "content": [
                                {"type": "list_item", "content": [{"type": "paragraph", "content": [
                                    {"type": "text", "text": "one"}]}]},
                                {"type": "list_item", "content": [{"type": "paragraph", "content": [
                                    {"type": "text", "text": "two"}]}]}]}]},
                    })
            return httpx.Response(404, json={"message": "not found"})
        if path == "/discover/category/list-categories":
            return httpx.Response(200, json={"entries": [{"slug": "tech"}, {"slug": "ai"}]})
        return httpx.Response(404, text="not found")


@pytest.fixture
def make_luma():
    def _make(fake, **settings):
        s = Settings(min_interval=0, **settings)
        return LumaClient(s, transport=httpx.MockTransport(fake))

    return _make
