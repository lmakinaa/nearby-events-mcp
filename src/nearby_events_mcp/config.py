"""Runtime settings, read from environment variables (set them in claude_desktop_config.json)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Which Eventbrite domain to talk to. Any regional domain works (.sg, .com, .co.uk ...).
    base_url: str = "https://www.eventbrite.sg"
    user_agent: str = DEFAULT_UA
    timeout: float = 25.0
    # Minimum seconds between requests (politeness / rate-limit protection).
    min_interval: float = 0.5

    # "Near me" defaults. Claude cannot know your location, so set one of these.
    default_lat: float | None = None
    default_lng: float | None = None
    default_location: str | None = None  # free text, geocoded once via OpenStreetMap Nominatim
    default_radius_km: int = 10

    # Auth bootstrap overrides.
    csrf_token: str | None = None  # value of the `csrftoken` cookie copied from your browser
    cookies: str | None = None  # raw "a=b; c=d" cookie string copied from your browser
    use_browser: bool = False  # bootstrap cookies with headless Chromium (needs [browser] extra)

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        radius = _float("NEARBY_EVENTS_DEFAULT_RADIUS_KM")
        return cls(
            base_url=os.environ.get("NEARBY_EVENTS_BASE_URL", cls.base_url).rstrip("/"),
            user_agent=os.environ.get("NEARBY_EVENTS_USER_AGENT", DEFAULT_UA),
            timeout=_float("NEARBY_EVENTS_TIMEOUT") or cls.timeout,
            min_interval=_float("NEARBY_EVENTS_MIN_INTERVAL") or cls.min_interval,
            default_lat=_float("NEARBY_EVENTS_DEFAULT_LAT"),
            default_lng=_float("NEARBY_EVENTS_DEFAULT_LNG"),
            default_location=os.environ.get("NEARBY_EVENTS_DEFAULT_LOCATION", "").strip() or None,
            default_radius_km=int(radius) if radius else cls.default_radius_km,
            csrf_token=os.environ.get("NEARBY_EVENTS_CSRF_TOKEN", "").strip() or None,
            cookies=os.environ.get("NEARBY_EVENTS_COOKIES", "").strip() or None,
            use_browser=_bool("NEARBY_EVENTS_USE_BROWSER"),
        )
