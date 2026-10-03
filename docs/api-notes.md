# API notes: how the event listings are fetched

> Notes on the public web endpoints this project reads, recorded so contributors can fix things when they change. This project is not affiliated with or endorsed by Eventbrite; see the disclaimer in the README.

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
