"""Optional place-name -> coordinates lookup via OpenStreetMap Nominatim (cached per place)."""

from __future__ import annotations

import re

import httpx

from .client import EventsError

_CACHE: dict[str, tuple[float, float, str]] = {}
_FILLER = re.compile(r"^(near|nearby|next to|beside|opposite|around|close to|by)\s+", re.I)


def candidate_queries(query: str) -> list[str]:
    """Progressively simpler versions of a free-text address.

    Nominatim is strict: "34 Whampoa West, near Boon Keng MRT station, Singapore" finds nothing,
    while "34 Whampoa West, Singapore" or "Boon Keng MRT station, Singapore" do.
    """
    parts = [p.strip() for p in query.split(",") if p.strip()]
    out: list[str] = [query.strip()]
    if len(parts) > 1:
        tail = parts[-1]
        plain = [p for p in parts[:-1] if not _FILLER.match(p)]
        landmarks = [_FILLER.sub("", p) for p in parts[:-1] if _FILLER.match(p)]
        if plain:
            out.append(", ".join(plain + [tail]))
        for lm in landmarks:
            out.append(f"{lm}, {tail}")
        for p in plain:
            out.append(f"{p}, {tail}")
        out.append(tail)
    else:
        out.append(_FILLER.sub("", query.strip()))
    return list(dict.fromkeys(q for q in out if q))


async def geocode(query: str, user_agent: str = "nearby-events-mcp/0.1 (open-source MCP server)",
                  transport: httpx.AsyncBaseTransport | None = None) -> tuple[float, float, str]:
    key = query.strip().lower()
    if key in _CACHE:
        return _CACHE[key]
    async with httpx.AsyncClient(timeout=15, transport=transport,
                                 headers={"User-Agent": user_agent}) as http:
        for q in candidate_queries(query):
            try:
                resp = await http.get("https://nominatim.openstreetmap.org/search",
                                      params={"q": q, "format": "jsonv2", "limit": 1})
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise EventsError(f"Could not geocode '{query}': {exc}. Pass lat/lng instead.") from exc
            if data:
                hit = data[0]
                result = (float(hit["lat"]), float(hit["lon"]), hit.get("display_name", q))
                _CACHE[key] = result
                return result
    raise EventsError(f"No location found for '{query}'. Try a shorter place name "
                      "(e.g. 'Boon Keng, Singapore') or pass lat/lng.")
