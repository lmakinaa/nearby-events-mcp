"""Static lookup tables verified against /api/v3/categories/ and /api/v3/formats/."""

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


def _resolve(values: list[str] | None, table: dict[int, str], aliases: dict[str, int],
             prefix: str, kind: str) -> list[str]:
    tags: list[str] = []
    for raw in values or []:
        v = str(raw).strip()
        if not v:
            continue
        if v.startswith(prefix + "/"):
            tags.append(v)
            continue
        if v.isdigit() and int(v) in table:
            tags.append(f"{prefix}/{int(v)}")
            continue
        low = v.lower()
        match = aliases.get(low)
        if match is None:
            for k, name in table.items():
                if name.lower() == low:
                    match = k
                    break
        if match is None:
            for k, name in table.items():
                if low in name.lower():
                    match = k
                    break
        if match is None:
            options = ", ".join(f"{n} ({k})" for k, n in table.items())
            raise ValueError(f"Unknown {kind} '{raw}'. Valid options: {options}")
        tags.append(f"{prefix}/{match}")
    # de-dup, keep order
    return list(dict.fromkeys(tags))


def resolve_categories(values: list[str] | None) -> list[str]:
    return _resolve(values, CATEGORIES, CATEGORY_ALIASES, "EventbriteCategory", "category")


def resolve_formats(values: list[str] | None) -> list[str]:
    return _resolve(values, FORMATS, FORMAT_ALIASES, "EventbriteFormat", "format")
