"""Assistant tools carried over from the original HELENA: weather, markets,
reminders, and durable memory. All keyless and free.

Sources: Open-Meteo (forecast + geocoding), a chain of free IP-geolocation
providers, and Yahoo Finance's public chart/search endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from .. import memory as memory_store
from .. import profile as profile_store
from ..permissions import Action
from .base import Tool, ToolContext, ToolError, ToolResult

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
    48: "depositing rime fog", 51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain", 66: "freezing rain",
    67: "heavy freezing rain", 71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains", 80: "slight rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

YAHOO_HEADERS = {
    # Yahoo 403s anything without a browser-shaped User-Agent.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

INDEXES = [("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq Composite")]


async def geocode(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    async def lookup(name: str):
        res = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            timeout=15.0,
        )
        results = res.json().get("results") if res.status_code == 200 else None
        return results[0] if results else None

    hit = await lookup(query)
    if not hit:
        # Open-Meteo matches on city name only, so "bend oregon" fails where
        # "bend" succeeds. Retry with the first segment before giving up.
        first = query.split(",")[0].strip() if "," in query else query.split(" ")[0].strip()
        if first and first.lower() != query.strip().lower():
            hit = await lookup(first)
    if not hit:
        raise ToolError(f'No location found matching "{query}". Try just the city name.')
    return {
        "label": ", ".join(p for p in (hit.get("name"), hit.get("admin1"), hit.get("country")) if p),
        "lat": hit["latitude"],
        "lon": hit["longitude"],
        "timezone": hit.get("timezone"),
    }


async def geolocate_by_ip(client: httpx.AsyncClient) -> dict[str, Any]:
    """Free IP-geolocation services rate-limit hard, so try a few in turn."""
    attempts = [
        ("https://ipapi.co/json/", lambda d: (
            ", ".join(p for p in (d.get("city"), d.get("region"), d.get("country_name")) if p),
            d.get("latitude"), d.get("longitude"), d.get("timezone"))),
        ("https://ipwho.is/", lambda d: (
            ", ".join(p for p in (d.get("city"), d.get("region"), d.get("country")) if p),
            d.get("latitude"), d.get("longitude"), (d.get("timezone") or {}).get("id"))),
        ("http://ip-api.com/json/", lambda d: (
            ", ".join(p for p in (d.get("city"), d.get("regionName"), d.get("country")) if p),
            d.get("lat"), d.get("lon"), d.get("timezone"))),
    ]
    errors = []
    for url, extract in attempts:
        try:
            res = await client.get(url, timeout=10.0)
            if res.status_code != 200:
                errors.append(f"{url}: HTTP {res.status_code}")
                continue
            label, lat, lon, tz = extract(res.json())
            if lat is None or lon is None:
                errors.append(f"{url}: no coordinates")
                continue
            return {"label": label or "your area", "lat": lat, "lon": lon, "timezone": tz}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append(f"{url}: {exc}")
    raise ToolError(
        "Every IP-geolocation provider failed or is rate-limited "
        f"({'; '.join(errors)}). Ask the user for a city instead."
    )


class WeatherTool(Tool):
    name = "get_weather"
    description = """
    Current conditions and today's forecast for a city, or for the user's saved
    location if no city is given. Use this for weather questions — never web search.
    """
    action = Action.NETWORK
    read_only = True
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "Optional. Omit to use the saved/detected location."}},
    }

    def preview(self, args: dict[str, Any]) -> str:
        return f"Check the weather{' in ' + args['city'] if args.get('city') else ''}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        profile = profile_store.load_profile()
        units = profile.get("units", "imperial")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            if args.get("city"):
                loc = await geocode(client, args["city"])
            elif profile["location"].get("lat") is not None:
                loc = profile["location"]
            else:
                loc = await geolocate_by_ip(client)
                profile_store.set_location(loc["label"], loc["lat"], loc["lon"], loc.get("timezone"))

            res = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": loc["lat"], "longitude": loc["lon"],
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                    "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
                    "wind_speed_unit": "mph" if units == "imperial" else "kmh",
                    "timezone": "auto",
                },
                timeout=20.0,
            )
            if res.status_code != 200:
                raise ToolError(f"Open-Meteo returned HTTP {res.status_code}.")
            data = res.json()

        cur, daily = data["current"], data["daily"]
        unit = "F" if units == "imperial" else "C"
        wind_unit = "mph" if units == "imperial" else "km/h"
        text = (
            f"Weather for {loc['label']}\n"
            f"Now: {cur['temperature_2m']}°{unit}, {WEATHER_CODES.get(cur['weather_code'], 'unknown')} "
            f"(feels like {cur['apparent_temperature']}°{unit})\n"
            f"Humidity {cur['relative_humidity_2m']}% · wind {cur['wind_speed_10m']} {wind_unit} · "
            f"precipitation {cur['precipitation']}\n"
            f"Today: {daily['temperature_2m_min'][0]}–{daily['temperature_2m_max'][0]}°{unit}, "
            f"{WEATHER_CODES.get(daily['weather_code'][0], 'unknown')}"
        )
        return ToolResult(ok=True, content=text, display=f"{cur['temperature_2m']}°{unit} in {loc['label']}")


async def _chart_meta(client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
    res = await client.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "1d", "interval": "1d"}, headers=YAHOO_HEADERS, timeout=20.0,
    )
    if res.status_code != 200:
        raise ToolError(f"Yahoo Finance returned HTTP {res.status_code} for {symbol}.")
    result = (res.json().get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ToolError(f"No market data returned for {symbol}.")
    return result.get("meta", {})


class StockTool(Tool):
    name = "get_stock"
    description = """
    Look up one stock by company name or ticker ("Apple", "AAPL"), or pass
    market_snapshot: true for the major indices. Use this for market questions —
    never web search. Yahoo's free feed can run ~15 minutes behind during live
    trading; outside market hours it shows the last actual trade.
    """
    action = Action.NETWORK
    read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Company name or ticker."},
            "market_snapshot": {"type": "boolean", "description": "Return S&P 500 / Dow / Nasdaq instead."},
        },
    }

    def preview(self, args: dict[str, Any]) -> str:
        if args.get("market_snapshot") or not args.get("query"):
            return "Check the markets"
        return f"Look up {args['query']}"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            if args.get("market_snapshot") or not args.get("query"):
                lines = ["Market snapshot"]
                for symbol, label in INDEXES:
                    try:
                        meta = await _chart_meta(client, symbol)
                    except ToolError as exc:
                        lines.append(f"{label}: unavailable ({exc})")
                        continue
                    price = meta.get("regularMarketPrice")
                    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
                    pct = ((price - prev) / prev * 100) if price and prev else None
                    arrow = "" if pct is None else ("▲" if pct >= 0 else "▼")
                    lines.append(
                        f"{label}: {price:,.2f}" + (f" {arrow} {abs(pct):.2f}% today" if pct is not None else "")
                    )
                state = meta.get("marketState") if "meta" in dir() else None
                lines.append(f"Source: Yahoo Finance{f' · market {state}' if state else ''}")
                return ToolResult(ok=True, content="\n".join(lines), display="indices")

            query = args["query"]
            search = await client.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": 5, "newsCount": 0},
                headers=YAHOO_HEADERS, timeout=20.0,
            )
            quotes = [q for q in (search.json().get("quotes") or []) if q.get("symbol")]
            if not quotes:
                raise ToolError(f'No ticker found matching "{query}".')
            exact = next((q for q in quotes if q["symbol"].upper() == query.strip().upper()), None)
            best = exact or next((q for q in quotes if q.get("quoteType") == "EQUITY"), quotes[0])
            meta = await _chart_meta(client, best["symbol"])

        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        change = (price - prev) if price and prev else None
        pct = (change / prev * 100) if change and prev else None
        name = meta.get("longName") or best.get("longname") or best.get("shortname") or best["symbol"]
        lines = [
            f"{name} ({meta.get('symbol', best['symbol'])})",
            f"Price: {price:,.2f} {meta.get('currency', '')}"
            + (f"  {'+' if change >= 0 else ''}{change:,.2f} ({pct:+.2f}%) today" if change is not None else ""),
            f"Day range: {meta.get('regularMarketDayLow', '—')} – {meta.get('regularMarketDayHigh', '—')}",
            f"52-week range: {meta.get('fiftyTwoWeekLow', '—')} – {meta.get('fiftyTwoWeekHigh', '—')}",
        ]
        for key, label in (("postMarketPrice", "After-hours"), ("preMarketPrice", "Pre-market")):
            if meta.get(key) and meta.get("marketState") != "REGULAR":
                lines.append(f"{label}: {meta[key]:,.2f}")
                break
        lines.append(f"Market state: {meta.get('marketState', 'unknown')} · source: Yahoo Finance")
        return ToolResult(
            ok=True, content="\n".join(lines),
            display=f"{best['symbol']} {price:,.2f}" if price else best["symbol"],
        )


class RemindTool(Tool):
    name = "add_reminder"
    description = """
    Schedule a reminder that really fires — the harness checks every 15 seconds
    and interrupts to tell the user. Always call this when asked to be reminded
    of something; saying "I'll remind you" without calling it creates nothing.
    Understands "3pm", "in 20 minutes", "tomorrow at 9am", "2026-08-12 09:00".
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind the user about."},
            "when": {"type": "string", "description": "When it should fire."},
        },
        "required": ["text", "when"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return "reminder"

    def preview(self, args: dict[str, Any]) -> str:
        return f'Remind "{args.get("text", "?")}" at {args.get("when", "?")}'

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        text, when_text = args.get("text", ""), args.get("when", "")
        when = profile_store.parse_when(when_text)
        profile_store.add_reminder(text, when_text, when)
        if not when:
            return ToolResult(
                ok=False,
                content=(
                    f'Saved "{text}" as a note, but "{when_text}" could not be parsed as a time, '
                    "so nothing will fire. Ask the user for a clearer time "
                    '("3pm", "in 20 minutes", "tomorrow at 9am").'
                ),
                display="unparsed time",
            )
        return ToolResult(
            ok=True,
            content=f'Reminder set: "{text}" at {when.strftime("%a %d %b, %H:%M")}.',
            display=when.strftime("%a %H:%M"),
        )


class RememberTool(Tool):
    name = "remember"
    description = """
    Save a durable fact, preference, or pattern about the user (their stack,
    working style, standing preferences, ongoing projects, recurring
    frustrations, who they are). It's kept in every future session in this
    project, and also embedded into a cross-project memory store that gets
    searched and recalled before you answer in *any* project — this is what
    lets you know the user beyond just the current folder. Use it when the
    user says to remember something, or tells you something clearly
    long-lived. Don't use it for one-off task details.
    """
    action = Action.WRITE
    read_only = False
    parameters = {
        "type": "object",
        "properties": {"fact": {"type": "string"}},
        "required": ["fact"],
    }

    def permission_key(self, args: dict[str, Any]) -> str:
        return "memory"

    def preview(self, args: dict[str, Any]) -> str:
        return f'Remember: "{args.get("fact", "?")}"'

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        fact = (args.get("fact") or "").strip()
        if not fact:
            raise ToolError("`fact` is required.")
        profile_store.add_fact(fact)
        if ctx.config.memory_enabled:
            await memory_store.remember(ctx.client, ctx.config.embed_model, ctx.workspace.name, fact)
        return ToolResult(ok=True, content=f"Saved: {fact}", display="noted")


class TimeTool(Tool):
    name = "get_time"
    description = "Current local date and time. Use it instead of guessing what today is."
    action = Action.NONE
    read_only = True
    parameters = {"type": "object", "properties": {}}

    def preview(self, args: dict[str, Any]) -> str:
        return "Check the time"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        now = datetime.now().astimezone()
        return ToolResult(
            ok=True,
            content=now.strftime("%A, %d %B %Y, %H:%M:%S %Z"),
            display=now.strftime("%H:%M"),
        )
