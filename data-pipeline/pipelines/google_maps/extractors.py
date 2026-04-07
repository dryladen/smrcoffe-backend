from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .constants import (
    ABOUT_ATTRIBUTE_KEYS,
    DAY_NAME_RE,
    DAY_ORDER,
    HOURS_NOISE_RE,
    PATH_COORD_RE,
    PLACE_HREF_RE,
    TIME_TOKEN_RE,
)
from .identity import normalize_place_url

def extract_place_urls_from_links(result: Any) -> set[str]:
    urls: set[str] = set()
    links = getattr(result, "links", {}) or {}
    for group in ("internal", "external"):
        for link in links.get(group, []):
            href = (
                link.get("href", "")
                if isinstance(link, dict)
                else getattr(link, "href", "")
            )
            normalized = normalize_place_url(href)
            if normalized:
                urls.add(normalized)
    return urls

def extract_place_urls_from_html(html: str) -> set[str]:
    urls: set[str] = set()
    if not html:
        return urls

    for match in PLACE_HREF_RE.findall(html):
        normalized = normalize_place_url(match)
        if normalized:
            urls.add(normalized)
    return urls

def extract_base_url(full_url: str) -> str:
    try:
        if full_url.startswith("/url?"):
            full_url = f"https://www.google.com{full_url}"

        parsed = urlparse(full_url)
        if parsed.netloc in ("www.google.com", "google.com") and parsed.path == "/url":
            query = parse_qs(parsed.query)
            target = query.get("q", [""])[0] or query.get("url", [""])[0]
            if target:
                return extract_base_url(unquote(target))
            return ""

        if not parsed.scheme and parsed.path and "." in parsed.path:
            parsed = urlparse(f"https://{parsed.path}")

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""

def extract_coordinates_from_url(url: str) -> tuple[float | None, float | None]:
    try:
        parsed = urlparse(url)
        center = parse_qs(parsed.query).get("center", [""])[0]
        if center:
            lat_str, lng_str = center.split(",", 1)
            return float(lat_str), float(lng_str)

        path_match = PATH_COORD_RE.search(url)
        if path_match:
            return float(path_match.group(1)), float(path_match.group(2))
    except (TypeError, ValueError):
        return None, None

    return None, None

def upgrade_photo_url_to_hd(url: str) -> str:
    """Upgrade a Google photo URL to high-definition by modifying size parameters."""
    if not url or "googleusercontent.com" not in url:
        return url
    # Pattern: =wWIDTH-hHEIGHT-suffix
    hd = re.sub(r"=w\d+-h\d+[^=]*$", "=w1920-h1080", url)
    if hd != url:
        return hd
    # Pattern: =sSIZE-suffix
    hd = re.sub(r"=s\d+[^=]*$", "=s1920", url)
    if hd != url:
        return hd
    # Pattern: =wWIDTH-suffix
    hd = re.sub(r"=w\d+[^=]*$", "=w1920", url)
    if hd != url:
        return hd
    return url

def extract_opening_days(hours: list[str]) -> list[str]:
    days: set[str] = set()
    for item in hours:
        if ":" in item:
            candidate = item.split(":", 1)[0].strip()
            if DAY_NAME_RE.fullmatch(candidate):
                days.add(candidate)
        day_match = DAY_NAME_RE.search(item)
        if day_match:
            days.add(day_match.group(1).title())
    return sorted(days, key=lambda d: DAY_ORDER.index(d) if d in DAY_ORDER else 99)

def normalize_opening_hours(hours: list[str]) -> list[str]:
    cleaned_hours: list[str] = []
    seen: set[str] = set()
    for item in hours:
        normalized = re.sub(r"\s+", " ", str(item or "")).strip()
        if not normalized:
            continue
        normalized = re.sub(
            r",?\s*Copy open hours\b", "", normalized, flags=re.IGNORECASE
        ).strip(" ,")
        if not normalized or HOURS_NOISE_RE.fullmatch(normalized):
            continue
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        cleaned_hours.append(normalized)
    return cleaned_hours

def has_structured_opening_hours(hours: list[str]) -> bool:
    for item in hours:
        if DAY_NAME_RE.search(item) and TIME_TOKEN_RE.search(item):
            return True
    return False

def _normalize_string_list(values: Any, *, lowercase: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if lowercase:
            text = text.lower()
        dedupe_key = text.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(text)
    return normalized

def sanitize_about_attributes(details: dict[str, Any]) -> None:
    planning = _normalize_string_list(details.get("planning"))
    recycling = _normalize_string_list(details.get("recycling"), lowercase=True)

    if planning:
        planning_clean: list[str] = []
        moved_from_planning: list[str] = []
        found_recycling_marker = False

        for index, item in enumerate(planning):
            if item.strip().lower() == "recycling":
                found_recycling_marker = True
                moved_from_planning = planning[index + 1 :]
                break
            planning_clean.append(item)

        if found_recycling_marker:
            planning = planning_clean
            recycling = _normalize_string_list(
                recycling + moved_from_planning, lowercase=True
            )

    if planning:
        details["planning"] = planning
    elif "planning" in details:
        details.pop("planning", None)

    if recycling:
        details["recycling"] = recycling
    elif "recycling" in details:
        details.pop("recycling", None)

def expand_opening_hours_to_full_week(hours: list[str]) -> tuple[list[str], list[str]]:
    normalized_hours = normalize_opening_hours(hours)
    opening_days = extract_opening_days(normalized_hours)

    if len(normalized_hours) == 1 and len(opening_days) == 1:
        single = normalized_hours[0]
        match = re.match(
            r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[,:]\s*(.+)$",
            single,
            flags=re.IGNORECASE,
        )
        if match:
            schedule = re.sub(r"\s+", " ", match.group(2)).strip()
            if schedule and TIME_TOKEN_RE.search(schedule):
                return normalized_hours, DAY_ORDER.copy()

    return normalized_hours, opening_days

def needs_short_share_url(gmaps_url: str) -> bool:
    return (
        not str(gmaps_url or "").strip().lower().startswith("https://maps.app.goo.gl/")
    )

def should_extract_hd_photos(photos: list[Any]) -> bool:
    if not isinstance(photos, list):
        return True
    cleaned = [str(photo or "").strip() for photo in photos if str(photo or "").strip()]
    if len(cleaned) < 12:
        return True
    for photo in cleaned:
        if "googleusercontent.com" not in photo:
            continue
        if "=w1920-h1080" in photo or "=s1920" in photo or "=w1920" in photo:
            continue
        return True
    return False

def needs_about_attributes(details: dict[str, Any]) -> bool:
    for key in ABOUT_ATTRIBUTE_KEYS:
        value = details.get(key)
        if isinstance(value, list) and value:
            continue
        return True
    return False
