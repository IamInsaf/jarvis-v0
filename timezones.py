"""Time in other places, worked out by Python instead of the LLM (a 7B model gets timezone maths wrong).
Uses the IANA timezone database (the `tzdata` package on Windows), so it works offline."""
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, available_timezones

# countries and common names that aren't a city in the timezone database (or need picking for)
_ALIASES = {
    "uk": "Europe/London", "england": "Europe/London", "britain": "Europe/London", "scotland": "Europe/London",
    "ireland": "Europe/Dublin", "usa": "America/New_York", "us": "America/New_York", "america": "America/New_York",
    "the us": "America/New_York", "the states": "America/New_York", "new york city": "America/New_York",
    "nyc": "America/New_York", "washington": "America/New_York", "california": "America/Los_Angeles",
    "la": "America/Los_Angeles", "san francisco": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "texas": "America/Chicago", "houston": "America/Chicago", "dallas": "America/Chicago", "canada": "America/Toronto",
    "india": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "chennai": "Asia/Kolkata", "sri lanka": "Asia/Colombo", "pakistan": "Asia/Karachi",
    "bangladesh": "Asia/Dhaka", "nepal": "Asia/Kathmandu", "maldives": "Indian/Maldives", "japan": "Asia/Tokyo",
    "china": "Asia/Shanghai", "beijing": "Asia/Shanghai", "korea": "Asia/Seoul", "south korea": "Asia/Seoul",
    "uae": "Asia/Dubai", "abu dhabi": "Asia/Dubai", "saudi arabia": "Asia/Riyadh", "saudi": "Asia/Riyadh",
    "mecca": "Asia/Riyadh", "makkah": "Asia/Riyadh", "medina": "Asia/Riyadh", "qatar": "Asia/Qatar",
    "doha": "Asia/Qatar", "kuwait": "Asia/Kuwait", "oman": "Asia/Muscat", "bahrain": "Asia/Bahrain",
    "turkey": "Europe/Istanbul", "egypt": "Africa/Cairo", "germany": "Europe/Berlin", "france": "Europe/Paris",
    "italy": "Europe/Rome", "spain": "Europe/Madrid", "netherlands": "Europe/Amsterdam", "russia": "Europe/Moscow",
    "australia": "Australia/Sydney", "new zealand": "Pacific/Auckland", "brazil": "America/Sao_Paulo",
    "mexico": "America/Mexico_City", "malaysia": "Asia/Kuala_Lumpur", "indonesia": "Asia/Jakarta",
    "thailand": "Asia/Bangkok", "vietnam": "Asia/Ho_Chi_Minh", "philippines": "Asia/Manila", "manila": "Asia/Manila",
    "south africa": "Africa/Johannesburg", "nigeria": "Africa/Lagos", "kenya": "Africa/Nairobi",
}


@lru_cache(maxsize=1)
def _cities() -> dict:
    """"tokyo" -> "Asia/Tokyo", "new york" -> "America/New_York", ... from the timezone database."""
    return {key.rsplit("/", 1)[-1].replace("_", " ").lower(): key
            for key in available_timezones() if "/" in key and not key.startswith(("Etc/", "SystemV/"))}


def resolve(place: str) -> str | None:
    """The timezone key for a place the user named, or None if we don't know it."""
    p = place.lower().strip(" ?.!,")
    p = p[4:] if p.startswith("the ") and p not in _ALIASES else p
    return _ALIASES.get(p) or _cities().get(p)


def time_in(place: str) -> str:
    key = resolve(place)
    if not key:
        return f"I don't know the timezone for {place}."
    there = datetime.now(ZoneInfo(key))
    here = datetime.now().astimezone()
    day = "" if there.date() == here.date() else f" on {there:%A}"
    name = place.strip(" ?.!,")
    name = name.title() if name.islower() else name  # "new york" -> "New York", but keep "the UK"
    return f"It's {there:%I:%M %p}".replace(" 0", " ", 1) + f"{day} in {name}."
