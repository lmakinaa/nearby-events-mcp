"""Async client for Eventbrite's internal web API (see docs/api-notes.md)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .constants import DEFAULT_EXPAND

log = logging.getLogger("nearby_events_mcp.client")


class EventsError(RuntimeError):
    """Raised for any failure talking to Eventbrite; message is safe to show to the model."""


class EventsClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._host = urlparse(settings.base_url).hostname or "www.eventbrite.sg"
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=settings.timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._lock = asyncio.Lock()
        self._boot_lock = asyncio.Lock()
        self._last = 0.0
        self._token: str | None = settings.csrf_token
        self._cache: dict[str, Any] = {}
        if settings.cookies:
            for part in settings.cookies.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    self._http.cookies.set(k.strip(), v.strip(), domain=self._host)
        if settings.csrf_token:
            self._http.cookies.set("csrftoken", settings.csrf_token, domain=self._host)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth bootstrap
    def _cookie(self, name: str) -> str | None:
        for c in self._http.cookies.jar:
            if c.name == name:
                return c.value
        return None

    async def _bootstrap(self, force: bool = False) -> str:
        async with self._boot_lock:
            if self._token and not force:
                return self._token
            if force and not self.s.use_browser:
                self._http.cookies.clear()
            token = None
            if not self.s.use_browser:
                await self._throttle()
                try:
                    resp = await self._http.get("/", headers={"Accept": "text/html"})
                    log.info("bootstrap GET / -> %s", resp.status_code)
                    token = self._cookie("csrftoken")
                except httpx.HTTPError as exc:
                    log.warning("bootstrap GET / failed: %s", exc)
            if not token:
                token = await self._bootstrap_browser()
            if not token:
                raise EventsError(
                    "Could not obtain an Eventbrite CSRF token (the site may be blocking plain HTTP "
                    "clients). Fix: set NEARBY_EVENTS_USE_BROWSER=1 (pip install 'nearby-events-mcp[browser]' "
                    "&& playwright install chromium), or copy the `csrftoken` cookie from your browser "
                    "into NEARBY_EVENTS_CSRF_TOKEN (and optionally the whole cookie header into "
                    "NEARBY_EVENTS_COOKIES)."
                )
            self._token = token
            return token

    async def _bootstrap_browser(self) -> str | None:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return None
        log.info("bootstrapping cookies with headless Chromium")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(user_agent=self.s.user_agent, locale="en-US")
                page = await ctx.new_page()
                await page.goto(self.s.base_url + "/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1500)
                cookies = await ctx.cookies()
            finally:
                await browser.close()
        for c in cookies:
            self._http.cookies.set(c["name"], c["value"], domain=c.get("domain") or self._host,
                                   path=c.get("path", "/"))
        return self._cookie("csrftoken")

    # ------------------------------------------------------------------ transport
    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self.s.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def _request(self, method: str, path: str, *, json: Any = None,
                       params: dict | None = None, csrf: bool = False) -> Any:
        refreshed = False
        last_error = "unknown error"
        for attempt in range(4):
            headers = {"Accept": "application/json"}
            if csrf:
                token = await self._bootstrap()
                headers.update({
                    "X-CSRFToken": token,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.s.base_url + "/",
                    "Origin": self.s.base_url,
                })
            await self._throttle()
            try:
                resp = await self._http.request(method, path, json=json, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code in (401, 403) and csrf and not refreshed:
                refreshed = True
                self._token = None
                log.info("%s %s -> %s, refreshing CSRF token", method, path, resp.status_code)
                await self._bootstrap(force=True)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2.0 ** attempt
                last_error = f"HTTP {resp.status_code}"
                await asyncio.sleep(min(delay, 20))
                continue
            if resp.status_code >= 400:
                raise EventsError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError:
                raise EventsError(
                    f"{method} {path} returned non-JSON (possible bot challenge): {resp.text[:200]}"
                ) from None
        raise EventsError(f"{method} {path} failed after retries: {last_error}")

    # ------------------------------------------------------------------ endpoints
    async def search(self, event_search: dict, expand: list[str] | None = None) -> dict:
        """POST /api/v3/destination/search/ (needs CSRF)."""
        payload = {
            "event_search": event_search,
            "expand.destination_event": list(expand if expand is not None else DEFAULT_EXPAND),
        }
        return await self._request("POST", "/api/v3/destination/search/", json=payload, csrf=True)

    async def get_event(self, event_id: str) -> dict:
        return await self._request("GET", f"/api/v3/events/{event_id}/")

    async def get_event_description(self, event_id: str) -> dict:
        return await self._request("GET", f"/api/v3/events/{event_id}/description/")

    async def get_ticket_classes(self, event_id: str) -> dict:
        return await self._request("GET", f"/api/v3/events/{event_id}/ticket_classes/")

    async def get_venue(self, venue_id: str) -> dict:
        return await self._request("GET", f"/api/v3/venues/{venue_id}/")

    async def get_organizer(self, organizer_id: str) -> dict:
        return await self._request("GET", f"/api/v3/organizers/{organizer_id}/")

    async def get_categories(self) -> dict:
        if "categories" not in self._cache:
            self._cache["categories"] = await self._request("GET", "/api/v3/categories/")
        return self._cache["categories"]

    async def get_formats(self) -> dict:
        if "formats" not in self._cache:
            self._cache["formats"] = await self._request("GET", "/api/v3/formats/")
        return self._cache["formats"]
