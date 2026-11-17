"""Static lookup tables.

Eventbrite tables are verified against /api/v3/categories/ and /api/v3/formats/; Luma's against
/discover/category/list-categories (see docs/api-notes.md).
"""

from __future__ import annotations

CATEGORIES: dict[int, str] = {
    101: "Business", 102: "Science & Tech", 103: "Music", 104: "Film & Media", 105: "Arts",
    106: "Fashion", 107: "Health", 108: "Sports & Fitness", 109: "Travel & Outdoor",
    110: "Food & Drink", 111: "Charity & Causes", 112: "Government", 113: "Community",
    114: "Spirituality", 115: "Family & Education", 116: "Holiday", 117: "Home & Lifestyle",
    118: "Auto, Boat & Air", 119: "Hobbies", 120: "School Activities", 199: "Other",
}

FORMATS: dict[int, str] = {
    1: "Conference", 2: "Seminar", 3: "Expo", 4: "Convention", 5: "Festival", 6: "Performance",
    7: "Screening", 8: "Gala", 9: "Class", 10: "Networking", 11: "Party", 12: "Rally",
    13: "Tournament", 14: "Game", 15: "Race", 16: "Tour", 17: "Attraction", 18: "Retreat",
    19: "Appearance", 100: "Other",
}

CATEGORY_ALIASES: dict[str, int] = {
    "tech": 102, "technology": 102, "science": 102, "food": 110, "drink": 110, "drinks": 110,
    "sports": 108, "sport": 108, "fitness": 108, "travel": 109, "outdoor": 109, "outdoors": 109,
    "charity": 111, "family": 115, "education": 115, "film": 104, "media": 104, "art": 105,
    "professional": 101, "business & professional": 101, "wellness": 107,
}

FORMAT_ALIASES: dict[str, int] = {
    "talk": 2, "workshop": 9, "course": 9, "meetup": 10, "mixer": 10, "concert": 6,
}

DATE_PRESETS = {"today", "tomorrow", "this_week", "this_weekend", "this_month", "current_future"}

# Organizer follower counts are the only size signal Eventbrite exposes reliably
# (event/ticket/venue `capacity` is null for almost every event). Thresholds are heuristics.
SIZE_BUCKETS: dict[str, tuple[int, int | None]] = {
    "small": (0, 200),
    "medium": (200, 2000),
    "large": (2000, None),
}

DEFAULT_EXPAND = [
    "primary_venue",
    "image",
    "ticket_availability",
    "event_sales_status",
    "primary_organizer",
]

PAGE_SIZE = 50  # server maximum

SOURCES = ("eventbrite", "luma")

# Luma discover categories (`slug` filter), from GET /discover/category/list-categories.
LUMA_CATEGORIES: dict[str, str] = {
    "tech": "Tech", "ai": "AI", "crypto": "Crypto", "climate": "Climate", "food": "Food & Drink",
    "arts": "Arts & Culture", "fitness": "Fitness", "running": "Running", "wellness": "Wellness",
    "family": "Family", "books": "Books", "games": "Games",
}

LUMA_ALIASES: dict[str, str] = {
    "technology": "tech", "artificial intelligence": "ai", "machine learning": "ai", "web3": "crypto",
    "blockchain": "crypto", "sustainability": "climate", "drink": "food", "drinks": "food", "art": "arts",
    "culture": "arts", "run": "running", "health": "wellness", "book": "books", "reading": "books",
    "game": "games", "gaming": "games", "kids": "family",
}

# Eventbrite category id -> closest Luma slug (and back, for Luma names given to Eventbrite).
EVENTBRITE_TO_LUMA: dict[int, str] = {
    102: "tech", 110: "food", 105: "arts", 108: "fitness", 107: "wellness", 115: "family", 119: "games",
}
LUMA_TO_EVENTBRITE: dict[str, int] = {v: k for k, v in EVENTBRITE_TO_LUMA.items()}

# Luma publishes a real RSVP count (`guest_count`), so its size buckets use attendees directly.
LUMA_SIZE_BUCKETS: dict[str, tuple[int, int | None]] = {
    "small": (0, 30),
    "medium": (30, 150),
    "large": (150, None),
}

LUMA_MAX_SLUGS = 3


def _match(v: str, table: dict[int, str], aliases: dict[str, int], prefix: str,
           substring: bool = True) -> str | None:
    """One name/id/alias -> "<prefix>/<id>" tag, or None."""
    if v.startswith(prefix + "/"):
        return v
    if v.isdigit() and int(v) in table:
        return f"{prefix}/{int(v)}"
    low = v.lower()
    match = aliases.get(low)
    if match is None:
        for k, name in table.items():
            if name.lower() == low:
                match = k
                break
    if match is None and substring:
        for k, name in table.items():
            if low in name.lower():
                match = k
                break
    return None if match is None else f"{prefix}/{match}"


def _resolve(values: list[str] | None, table: dict[int, str], aliases: dict[str, int],
             prefix: str, kind: str) -> list[str]:
    tags: list[str] = []
    for raw in values or []:
        v = str(raw).strip()
        if not v:
            continue
        tag = _match(v, table, aliases, prefix)
        if tag is None:
            options = ", ".join(f"{n} ({k})" for k, n in table.items())
            raise ValueError(f"Unknown {kind} '{raw}'. Valid options: {options}")
        tags.append(tag)
    # de-dup, keep order
    return list(dict.fromkeys(tags))


def resolve_categories(values: list[str] | None) -> list[str]:
    return _resolve(values, CATEGORIES, CATEGORY_ALIASES, "EventbriteCategory", "category")


def resolve_formats(values: list[str] | None) -> list[str]:
    return _resolve(values, FORMATS, FORMAT_ALIASES, "EventbriteFormat", "format")


def _match_luma(v: str, substring: bool = True) -> str | None:
    low = v.lower().strip()
    if low.startswith("luma/"):
        low = low[5:]
    if low in LUMA_CATEGORIES:
        return low
    if low in LUMA_ALIASES:
        return LUMA_ALIASES[low]
    for slug, name in LUMA_CATEGORIES.items():
        if name.lower() == low:
            return slug
    if substring:
        for slug, name in LUMA_CATEGORIES.items():
            if low in name.lower():
                return slug
    return None


def split_categories(values: list[str] | None) -> tuple[list[str], list[str]]:
    """Resolve category names for every source at once: (Eventbrite tags, Luma slugs).

    A name only one source knows ("AI" is Luma-only, "Music" Eventbrite-only) resolves for that source
    alone; exact matches win over substring matches so "AI" never becomes "Auto, Boat & Air". Raises
    ValueError only for a name neither source knows.
    """
    eb: list[str] = []
    lu: list[str] = []
    for raw in values or []:
        v = str(raw).strip()
        if not v:
            continue
        tag = _match(v, CATEGORIES, CATEGORY_ALIASES, "EventbriteCategory", substring=False)
        slug = _match_luma(v, substring=False)
        if tag is None and slug is None:
            tag = _match(v, CATEGORIES, CATEGORY_ALIASES, "EventbriteCategory")
            slug = _match_luma(v)
        if tag is None and slug is None:
            eb_opts = ", ".join(f"{n} ({k})" for k, n in CATEGORIES.items())
            lu_opts = ", ".join(LUMA_CATEGORIES.values())
            raise ValueError(f"Unknown category '{raw}'. Valid options: {eb_opts}; Luma: {lu_opts}")
        if tag is not None and slug is None:
            slug = EVENTBRITE_TO_LUMA.get(int(tag.split("/")[1])) if tag.split("/")[1].isdigit() else None
        if slug is not None and tag is None and slug in LUMA_TO_EVENTBRITE:
            tag = f"EventbriteCategory/{LUMA_TO_EVENTBRITE[slug]}"
        if tag:
            eb.append(tag)
        if slug:
            lu.append(slug)
    return list(dict.fromkeys(eb)), list(dict.fromkeys(lu))
