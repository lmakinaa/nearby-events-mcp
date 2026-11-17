# API notes: how the event listings are fetched

> Notes on the public web endpoints this project reads, recorded so contributors can fix things when they change. This project is not affiliated with or endorsed by Eventbrite or Luma; see the disclaimer in the README. Sections 1–10 cover Eventbrite, section 11 covers Luma.

Explored on `www.eventbrite.sg` (Singapore) on 2026-09-30 from a live browser session. These are the **same-origin endpoints the website itself uses** — they are unofficial, undocumented and can change without notice. Everything below was tested; items marked **(untested)** or **(uncertain)** were not confirmed. Use of these endpoints may be restricted by Eventbrite's Terms of Service; the official, supported route is the public API at `eventbriteapi.com` (which no longer offers a public event-search endpoint, which is why sites like this one use the internal one).

Base URL: `https://www.eventbrite.<tld>` (`.sg`, `.com`, `.co.uk` … all worked the same way on `.sg`).

---

## 1. How the site loads data

| Mechanism | Detail |
|---|---|
| SSR bootstrap | Listing pages such as `/d/singapore--singapore/events--today/networking-events/` embed the first result page as `window.__SERVER_DATA__` (keys include `search_data`, `placeId`, `latitude`, `longitude`, `search_id`, `page_number`, `page_count`). `search_data.event_search` is exactly the request object you send to the search API, and `search_data.events` is exactly its response. |
| Client search | Filters / pagination call **`POST /api/v3/destination/search/`** (below). |
| Detail data | Plain **GET** calls to `/api/v3/events/{id}/…` (below). |
| Analytics noise | `/switchyard/{uuid}`, `/metrics/…`, GTM, Heap, Pixel etc. — ignore. |

## 2. Authentication / headers

| Endpoint family | Requirement |
|---|---|
| `POST /api/v3/destination/search/` | Cookie `csrftoken` **and** header `X-CSRFToken: <same value>`. Without it → `401`. Omitting cookies (`credentials: omit`) → `401`. |
| `GET /api/v3/events/*`, `/venues/*`, `/organizers/*`, `/categories/`, `/formats/` | No login, no CSRF; worked with cookies omitted. |
| `GET /api/v3/destination/events/` | Worked with session cookies (no-cookie behaviour untested). |

Recommended headers: `Content-Type: application/json`, `X-Requested-With: XMLHttpRequest`, `Referer: https://www.eventbrite.sg/`, a realistic `User-Agent`.

Bootstrapping the CSRF token outside a browser: load any HTML page (e.g. `GET /`) and read the `csrftoken` value from `Set-Cookie`. **(untested from a non-browser client — bot protection may interfere; a headless browser is the fallback.)**

---

## 3. `POST /api/v3/destination/search/`

The main endpoint. Returns event cards plus (unused here) `profiles`, `articles`, etc.

### 3.1 Request

```jsonc
{
  "event_search": {                // required, STRICT: any unknown key => 400
    "dates": "today",
    "point_radius": { "latitude": 1.3521, "longitude": 103.8198, "radius": "5km" },
    "price": "free",
    "tags": ["EventbriteCategory/101"],
    "sort": "distance",
    "page": 1,
    "page_size": 50,
    "dedup": true
  },
  // optional: which sub-objects to inline in each result
  "expand.destination_event": [
    "primary_venue", "image", "ticket_availability",
    "event_sales_status", "primary_organizer", "public_collections"
  ]
}
```

### 3.2 `event_search` fields

| Field | Type | Values / behaviour | Verified |
|---|---|---|---|
| `dates` | string \| string[] | `today`, `tomorrow`, `this_week`, `this_weekend`, `this_month`, `current_future`. Array form is accepted (`["current_future","today"]` is what the site sends; it gave a different count than `"today"` alone, so treat arrays as combined/AND-like). | yes |
| `date_range` | `{from, to}` | ISO dates `YYYY-MM-DD`. `{from:"2026-10-01",to:"2026-10-07"}` returned 821 events for SG. Flat `start_date`/`end_date` keys are rejected. | yes |
| `places` | string[] | Eventbrite place IDs (WOEID-style), e.g. `"102032341"` = Singapore (locality). IDs appear in `locations[]` of every event and in `__SERVER_DATA__.placeId`. Omitting both `places` and geo returned ~2,941 events (wide/global-ish default). | yes |
| `point_radius` | `{latitude:number, longitude:number, radius:string}` | Best option for "near me". `radius` must be a string with unit: `"5km"` and `"5mi"` accepted (SG today: 5 km → 15 events, 5 mi → 94). Bare numbers / `"5000m"` / `"5.0km"` → 400 `INVALID`. Top-level `latitude`/`longitude`/`radius` keys are **not** valid. | yes |
| `bbox` | string | `"west,south,east,north"` in degrees (`"103.6,1.2,104.0,1.5"` → 202 hits; a tiny box → 8). | yes (axis order inferred) |
| `q` | string | Free-text query. Widens the result set (returned 244 vs 203 without it), so it is relevance-based, not a strict filter. | yes |
| `price` | `"free"` \| `"paid"` | Exact lowercase. `"Free"` → 400. SG today: free 82, paid 121. | yes |
| `tags` | string[] | Category / subcategory / format tags. **Multiple tags = OR** (cat 101 → 22, cat 102 → 3, both → 25). See §6 for IDs. | yes |
| `currencies` | string[] | e.g. `["SGD"]` (count dropped 203 → 155). | yes |
| `languages` | string[] | e.g. `["en"]` (accepted; no count change in test). | partly |
| `sort` | string | Only `date` and `distance` accepted. `best`, `relevance`, `popularity`, `trending`, `-date`, `starts_at`, `newest` → 400. | yes |
| `page` | int ≥ 1 | 1-based. | yes |
| `page_size` | int | **Max 50** (100/200 silently capped to 50). `0` returns zero results. | yes |
| `dedup` | bool | `true` (site default) collapses series/duplicates, so a page can hold fewer than `page_size` rows (e.g. 18/20). | yes |
| `online_events_only` | bool | Accepted but **had no effect** (203 → 203, 0 online rows). Filter client-side on `is_online_event`. **(uncertain)** | yes |
| `aggs` | string[] | Site sends `["places_borough","places_neighborhood"]`; my other agg names were rejected. Returned buckets were empty in SG. | partly |
| `continuation` | string | Accepted, but had no visible effect vs `page`; use `page`. | partly |

Rejected/unknown keys (all 400): `start_date`, `end_date`, `organizers`, `event_ids`, `start_time`, `location`, `geo`, `latlng`, top-level `latitude`/`longitude`/`radius`.

**No native filter for event size (attendee count/capacity)** — see §8.

### 3.3 Response (200)

```jsonc
{
  "events": {
    "pagination": {
      "object_count": 203,       // approximate — can jump around on deep pages
      "page_count": 11,
      "page_number": 1,
      "page_size": 20,
      "continuation": "eyJwYWdlIjoyfQ"   // base64 {"page":2}; null on last page
    },
    "results": [ /* DestinationEvent, see below */ ],
    "aggs": { "places_neighborhood": {"buckets": []}, "places_borough": {"buckets": []} },
    "promoted_results": []
  },
  "event_search": { /* echo of your query */ },
  "search_id": "bcc3a1dc-…",     // uuid
  "es": [],
  "articles": {}, "profiles": {}, "profile_search": {}, "article_search": {},
  "suggested_categories": [],
  "current_user_id": "…", "is_staff": false
}
```

**Paginate** by incrementing `page` until `results` is empty or `page > page_count`. Do not trust `object_count` on deep pages (page 13 reported 240, page 99 reported 1,960 while returning nothing).

### 3.4 `DestinationEvent` (each item in `events.results[]`)

Fields marked † only appear when the matching `expand.destination_event` value is requested (the site requests all of them).

```jsonc
{
  "_type": "destination_event",
  "id": "1999977769346",              // == eventbrite_event_id; use for /api/v3/events/{id}/
  "eid": "…", "eventbrite_event_id": "1999977769346",
  "name": "Riverwalk - eat & heal",
  "summary": "…",
  "full_description": null,
  "url": "https://www.eventbrite.sg/e/…-tickets-1999977769346",
  "tickets_url": "https://www.eventbrite.com/checkout-external…",
  "tickets_by": "Eventbrite",
  "checkout_flow": "…",
  "language": "en-gb",
  "timezone": "Asia/Singapore",
  "start_date": "2026-09-30", "start_time": "18:30",     // local time
  "end_date":   "2026-09-30", "end_time":   "20:00",
  "hide_start_date": false, "hide_end_date": false,
  "published": "2026-…",              // ISO timestamp
  "is_online_event": false,
  "is_cancelled": null,
  "is_protected_event": false,
  "series_id": null, "parent_url": null, "num_children": 1,
  "dedup": { "count": 1, "hash": "…" },
  "urgency_signals": { "messages": ["salesEndSoon"], "categories": [] },
  "image_id": "1193240353",
  "image": {                           // †
    "url": "https://img.evbuc.com/…", "id": "…", "aspect_ratio": "1.775",
    "original": {"url": "…", "width": 1280, "height": 720},
    "image_sizes": {"small": "…", "medium": "…", "large": "…"},
    "focal_point": {"x": 0.5, "y": 0.5}, "edge_color": "#ffffff"
  },
  "locations": [                       // hierarchy: continent > country > region > locality
    {"type": "locality", "id": "102032341", "name": "Singapore"}
  ],
  "tags": [
    {"prefix": "EventbriteCategory", "tag": "EventbriteCategory/101",
     "display_name": "Business & Professional"},
    {"prefix": "EventbriteSubCategory", "tag": "EventbriteSubCategory/1001", "display_name": "…"},
    {"prefix": "EventbriteFormat", "tag": "EventbriteFormat/2", "display_name": "Seminar or Talk"},
    {"prefix": "OrganizerTag", "tag": "OrganizerTag/Businessnetworking", "display_name": "…"}
  ],
  "primary_venue_id": "298739734",
  "primary_venue": {                   // †
    "_type": "destination_venue", "id": "…", "name": "Zion Riverside Food Centre",
    "venue_profile_id": null, "venue_profile_url": "…",
    "address": {
      "address_1": "…", "city": "Singapore", "region": "…", "country": "SG", "postal_code": "…",
      "latitude": "1.2929", "longitude": "103.8318",      // NOTE: strings
      "localized_address_display": "…", "localized_area_display": "…",
      "localized_multi_line_address_display": ["…", "…"]
    }
  },
  "primary_organizer_id": "59996354093",
  "primary_organizer": {               // †
    "_type": "destination_profile", "id": "…", "name": "…", "url": "…",
    "profile_type": "organizer", "summary": "…", "image_id": "…",
    "num_followers": 119,              // <- useful popularity/size proxy
    "num_upcoming_events": null, "num_saves": null, "num_collections": null, "num_following": null,
    "followed_by_you": false, "website_url": "…", "facebook": "…", "twitter": null
  },
  "ticket_availability": {             // †  <- price filter data lives here
    "is_free": false, "is_sold_out": false,
    "has_available_tickets": true, "has_bogo_tickets": false,
    "minimum_ticket_price": {"currency": "SGD", "value": 800, "major_value": "8.00", "display": "$8.00"},
    "maximum_ticket_price": {"currency": "SGD", "value": 2000, "major_value": "20.00", "display": "$20.00"}
    // value = minor units (cents)
  },
  "event_sales_status": {              // †
    "sales_status": "on_sale",         // observed: "on_sale" | "sales_ended"
    "currency": "SGD",
    "start_sales_date": {"timezone": "…", "local": "…", "utc": "…"},
    "end_sales_date":   {"timezone": "…", "local": "…", "utc": "…"},
    "message": null, "message_code": null, "message_type": null, "default_message": null
  },
  "saves": {"saved_by_you": false},
  "public_collections": {"creator_collections": {"object_count": 0, "…": "…"}},  // †
  "debug_info": {}
}
```

Note: `is_free` at event level is under `ticket_availability.is_free`; the price filter is applied server-side, but price *range* filtering (e.g. under $30) has to be done client-side on `minimum_ticket_price.value`.

---

## 4. `GET /api/v3/destination/events/`

Batch-hydrate events by ID with the same `DestinationEvent` shape (used for saved/likes lists, etc.).

```
GET /api/v3/destination/events/?event_ids=1999977769346,2001684045865
    &expand=event_sales_status,primary_venue,ticket_availability,primary_organizer
    &page_size=20
```

Response: `{ "pagination": {object_count, continuation, page_count, page_size, has_more_items, page_number}, "events": [DestinationEvent, …] }`. Verified with one ID; multiple comma-separated IDs **(untested)**.

---

## 5. Classic v3 REST endpoints (public GETs, no auth needed)

### `GET /api/v3/events/{id}/`
Full event record. Keys observed:
`name{text,html}, description{text,html}, url, start{timezone,local,utc}, end{…}, organization_id, created, changed, published, capacity, capacity_is_custom, status ("live"), currency, listed, shareable, online_event, tx_time_limit, hide_start_date, hide_end_date, locale, is_locked, privacy_setting, is_series, is_series_parent, inventory_type, is_reserved_seating, show_pick_a_seat, source, is_free, version, summary, facebook_event_id, logo_id, logo{…}, organizer_id, venue_id, category_id, subcategory_id, format_id, id, resource_uri, is_externally_ticketed`.

`capacity` was **`null` for all 10 events sampled** (see §8).

### `GET /api/v3/events/{id}/ticket_classes/`
`{ pagination, ticket_classes: [ … ] }`; each ticket class has:
`id, name, display_name, description, free (bool), donation (bool), cost{currency,value,major_value,display}, fee{…}, tax{…}, actual_cost, actual_fee, capacity, minimum_quantity, maximum_quantity, maximum_quantity_per_order, on_sale_status ("AVAILABLE" …), hidden_currently, include_fee, sales_end_relative, hide_sale_dates, sorting, category, delivery_methods, sales_channels, payment_constraints, event_id, …`.
`capacity` was `null` in every sampled event.

### `GET /api/v3/events/{id}/description/`
`{ "description": "<html>" }` — full HTML description.

### `GET /api/v3/events/{id}/structured_content/`
Structured page modules (agenda, etc.); returns `resource_uris` pointing at `eventbriteapi.com`.

### `GET /api/v3/venues/{id}/`
`{ id, name, address{…}, latitude, longitude, capacity, age_restriction, resource_uri }` (`capacity` null in samples).

### `GET /api/v3/organizers/{id}/`
`{ id, name, url, vanity_url, description, long_description, website, facebook, num_past_events, num_future_events, follow_status, organization_id, logo_id, logo, … }`.

### `GET /api/v3/categories/`  and  `GET /api/v3/formats/`
Static lookup tables; see §6.

---

## 6. Reference: IDs

**Categories** (`tags: ["EventbriteCategory/<id>"]`): 101 Business · 102 Science & Tech · 103 Music · 104 Film & Media · 105 Arts · 106 Fashion · 107 Health · 108 Sports & Fitness · 109 Travel & Outdoor · 110 Food & Drink · 111 Charity & Causes · 112 Government · 113 Community · 114 Spirituality · 115 Family & Education · 116 Holiday · 117 Home & Lifestyle · 118 Auto, Boat & Air · 119 Hobbies · 120 School Activities · 199 Other

**Formats** (`tags: ["EventbriteFormat/<id>"]`): 1 Conference · 2 Seminar · 3 Expo · 4 Convention · 5 Festival · 6 Performance · 7 Screening · 8 Gala · 9 Class · 10 Networking · 11 Party · 12 Rally · 13 Tournament · 14 Game · 15 Race · 16 Tour · 17 Attraction · 18 Retreat · 19 Appearance · 100 Other

Subcategories use `EventbriteSubCategory/<4-digit id>` (e.g. `1001` = Startups & Small Business); the full list was not enumerated.

**Places:** no place-autocomplete endpoint was found (guesses such as `/api/v3/destination/places/` return 404; the UI uses a `location-autocomplete` component whose API call I did not capture). For "near me" use `point_radius`, so you don't need place IDs. To resolve a city → place ID, fetch `/d/<country>--<city>/events--today/` and read `__SERVER_DATA__.placeId`, or read `locations[].id` from any result.

**Listing URL pattern (SSR):** `/d/{country}--{city}/{filters}/{query-slug}/` e.g. `/d/singapore--singapore/events--today/networking-events/`.

---

## 7. Example: "free events today within 5 km, nearest first"

```js
// browser / same-origin
const csrf = document.cookie.match(/csrftoken=([^;]+)/)[1];
const res = await fetch('/api/v3/destination/search/', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf, 'X-Requested-With': 'XMLHttpRequest'},
  body: JSON.stringify({
    event_search: {
      dates: 'today',
      point_radius: {latitude: 1.3521, longitude: 103.8198, radius: '5km'},
      price: 'free', sort: 'distance', page: 1, page_size: 50, dedup: true
    },
    'expand.destination_event': ['primary_venue','image','ticket_availability','event_sales_status','primary_organizer']
  })
});
const data = await res.json();          // verified: 6 events, all ticket_availability.is_free === true
```

---

## 8. Filtering by "size" — what exists

There is **no server-side size filter** and `capacity` is null in practice (event, ticket classes and venue: 10/10 sampled events). Options, in order of usefulness:

1. **Organizer popularity** – `primary_organizer.num_followers` (free with the expand; e.g. 7 → 4,658 in a 10-event sample). Good proxy for "big/small event".
2. **Venue** – name/address (a convention centre vs. a café); `primary_venue` has no capacity.
3. **Ticket capacity** – when an organizer does set `ticket_classes[].capacity` (or `event.capacity`), sum it; treat `null` as unknown.
4. **Signals** – `urgency_signals.messages` (`salesEndSoon`, …), `ticket_availability.is_sold_out`, `event_sales_status.sales_status`.
5. **Series** – `num_children` > 1 means a multi-date series.

Client-side price filters: use `ticket_availability.minimum_ticket_price.value` / `maximum_ticket_price.value` (minor units) with `is_free`.

---

## 9. Suggested MCP surface

| Tool | Args | Implementation |
|---|---|---|
| `search_events` | `lat, lng, radius_km, date` (`today`/`tomorrow`/`YYYY-MM-DD` or range), `price` (`free`/`paid`/`any`), `max_price`, `categories[]`, `formats[]`, `q`, `min_organizer_followers`, `max_organizer_followers`, `include_online`, `sort`, `limit` | Loop `POST /destination/search/` with `page_size=50` and `expand.destination_event`; apply price-range, size-proxy and online filters client-side; normalise each event to `{id,name,url,start,end,venue,lat,lng,distance_km,is_free,min_price,max_price,currency,category,format,organizer,followers,sold_out}`. |
| `get_event` | `event_id` | `GET /api/v3/events/{id}/` + `/description/` + `/ticket_classes/` (+ venue, organizer). |
| `list_categories` | – | `GET /api/v3/categories/` + `/formats/` (cache). |
| `daily_events` | same filters, `days` | One `search_events` per day; dedupe by `id`. |

Implementation notes: obtain and cache the CSRF token (refresh on `401`/`403`); throttle (1–2 req/s) and cache detail calls by event ID; compute distance yourself with haversine from `primary_venue.address.latitude/longitude` (strings → floats); treat all schema as unstable and validate responses (e.g. with zod/pydantic) so breakage is loud.

## 10. Known gaps

- Bootstrapping the CSRF token from a non-browser client and any anti-bot behaviour/rate limits were not tested.
- Place autocomplete endpoint not found.
- Meaning of the array form of `dates`, `languages`, `continuation`, and `online_events_only` only partly confirmed.
- Only Singapore was tested; other markets may expose different fields/filters.

---

## 11. Luma (`api.lu.ma`)

Probed live on 2026-11-17 from Singapore with plain HTTP GETs (browser User-Agent, **no auth, no cookies**). These are the undocumented endpoints behind Luma's own discover pages, not Luma's official API (which needs a key and covers your own calendars). They can change without notice; use may be restricted by Luma's Terms of Service. Implementation: `src/nearby_events_mcp/luma.py`.

### 11.1 `GET /discover/get-paginated-events`

```
GET https://api.lu.ma/discover/get-paginated-events
    ?latitude=1.3521&longitude=103.8198&pagination_limit=50
    [&pagination_cursor=…][&query=…][&slug=<category>][&price=free|paid]
→ { "entries": [ … ], "has_more": true, "next_cursor": "eyJzdiI6…" }
```

- Without `query`: geo-scoped (roughly 50 km around the point) and **sorted by start time from now** (an event that started but hasn't ended can still appear first). There is no radius parameter: filter by distance client-side.
- **With `query` the feed is different**: a global, relevance-ranked list capped at 50 entries, `has_more: false`, no `next_cursor`, start times out of order and many countries mixed in (e.g. `query=networking` from Singapore: 19 of 50 in Asia/Singapore, the rest across 20 timezones). Keyword results are therefore read as one unsorted page (no date jump, no stop rule), and can miss local events a few days out; results carry a note saying so. Some in-person entries there have `coordinate: null` and an obfuscated address; they are dropped, since their location is unknown.
- Server-side filters that work (checked against the same request without them):

| Param | Behaviour | Verified |
|---|---|---|
| `query` | Keyword search | yes |
| `slug` | Category slug (§11.3); one per request | yes |
| `price` | `free` returned only free events, `paid` only paid ones | yes |
| `pagination_limit` | Page size; 50 used | yes |

- **No date parameter.** Ignored: `start_date`, `end_date`, `date`, `start`, `from`, `after`, `before`, `starts_after`, `start_after`. `period=` → HTTP 400 here (it exists only on per-calendar `/calendar/get-items`). Other maintained users of this endpoint also filter dates client-side.
- **No price-range parameter.** Ignored: `price_min`, `min_price`, `max_price`, `price_max`, `min_price_cents`, `max_price_cents`. Filter on `ticket_info.price.cents` / `max_price.cents` client-side (after `price=paid` narrows the feed).
- **Cursor = date jump.** `next_cursor` is unpadded base64url of compact JSON `{"sv":"YYYY-MM-DD HH:MM:SS+00","fb":"<last evt id>"}` (sv = UTC start of the last entry). A crafted cursor with `sv` = a later instant and `"fb":""` starts the feed at that instant (tested with 2026-11-28). Undocumented, so this project:
  1. fetches page 1 without a cursor (the in-person event nearest the search point gives the area's timezone, used to resolve `today`/`this_weekend`; `this_weekend` is Friday–Sunday, like Eventbrite's preset);
  2. if page 1 ends before the window, jumps to the window's local midnight with a crafted cursor; on a 4xx it falls back to the feed's own cursor and pages forward (with a note);
  3. stops paging once a page ends past midnight after the last day in the area's timezone (or `has_more` is false, or 5 pages; date-sorted searches also stop once enough events pass every filter);
  4. always drops events outside the window: kept are events starting on or before the last day (local time of the event, else its calendar, else the area) and not ended before the first. Like Eventbrite, a multi-day event already running is included, but only when the feed reaches it: a date jump starts at the first day's midnight, so events that began earlier are skipped then.

### 11.2 Entry fields used

```jsonc
{
  "api_id": "evt-…",
  "event": {
    "api_id": "evt-…", "name": "…", "url": "fpsm8em6",          // page: https://luma.com/{url} (lu.ma 301s there)
    "start_at": "2026-11-18T11:00:00.000Z", "end_at": "…Z",     // UTC
    "timezone": "Asia/Singapore",                               // convert start/end to this for local times
    "location_type": "offline",                                 // anything else = online
    "coordinate": {"latitude": 1.3153, "longitude": 103.8809},
    "geo_address_info": {
      "mode": "shown" | "obfuscated",                           // obfuscated: exact address for guests only
      "address": "Untangled Minds", "full_address": "…",       // only when shown
      "sublocality": "Geylang", "city": "Singapore", "country": "Singapore"
    },
    "recurrence_id": null
  },
  "calendar": {"name": "…", "slug": null, "is_personal": false}, // organizer; personal calendars are named "Personal", so use hosts[0]
  "hosts": [{"name": "…"}],
  "guest_count": 27,                                            // real RSVP count (size signal); 0 when hidden
  // event.show_guest_list: false <=> guest_count 0 (all 143 of 340 sampled entries); treated as unknown
  "ticket_info": {
    "is_free": false,
    "price": {"cents": 2500, "currency": "sgd"},                // cheapest available tier
    "max_price": {"cents": 4000, "currency": "sgd"} | null,     // null = single price
    "is_sold_out": true, "spots_remaining": 0, "is_near_capacity": false, "require_approval": true
  },
  "registration_availability": "open" | "waitlist" | "sold-out"
}
```

Currency codes are lowercase (`sgd`); they are upper-cased to match Eventbrite.

### 11.3 Categories: `GET /discover/category/list-categories`

12 slugs: `family, books, games, tech, food, ai, running, arts, climate, fitness, wellness, crypto`. Mapped from Eventbrite: Science & Tech→tech, Food & Drink→food, Arts→arts, Sports & Fitness→fitness, Health→wellness, Family & Education→family, Hobbies→games. Luma has no formats.

### 11.4 Details: `GET /event/get?event_api_id=evt-…`

Same `event`, `calendar`, `hosts`, `guest_count`, `ticket_info` as above, plus `ticket_types[]` (`name, cents, currency, type ("free"|"fiat-price"), max_capacity, spots_remaining, is_hidden, is_disabled, max_per_guest, is_flexible`), `description_mirror` (ProseMirror JSON: flattened to text), `categories[]`, `sold_out`. Observed: `ticket_info.price` is the cheapest tier *still available*; `guest_count` is 0 when the host hides the guest list (`event.show_guest_list: false`), so it is treated as unknown (`attendees: null`, left out by `size`); `description_mirror` can be `null`; free tiers have `currency: null`.
