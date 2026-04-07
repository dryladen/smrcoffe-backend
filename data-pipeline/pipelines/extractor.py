"""Extraction and normalization pipeline for scraped cafe data.

This module reads raw JSON envelopes from ``data/raw/<date>/<source>/`` and
transforms them into normalized JSON datasets under
``data/normalized/<date>/``. It is the transform-only stage between scraping
and future database loading.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.schemas import (  # type: ignore[reportMissingImports]
    CafePhotoRecord,
    CafeRecord,
    CafeTagRecord,
    InfluencerReviewRecord,
    QuarantineRecord,
)
from pipeline_utils import (  # type: ignore[reportMissingImports]
    generate_slug,
    get_normalized_dir,
    normalize_phone,
    normalize_text,
    normalize_url,
    quarantine_record,
    save_json,
)

logger = logging.getLogger(__name__)

_GENERIC_HASHTAGS = {
    "cafe",
    "cafes",
    "coffee",
    "coffeeshop",
    "coffeeshops",
    "fyp",
    "foryou",
    "foryoupage",
    "kuliner",
    "nongkrong",
    "rekomendasi",
    "review",
    "samarinda",
    "tiktok",
    "viral",
}

_CAFE_FIELD_ORDER = [
    "status",
    "is_featured",
    "name",
    "slug",
    "price_range",
    "description",
    "image",
    "district",
    "address",
    "opening_time",
    "closing_time",
    "opening_days",
    "service_options",
    "highlights",
    "popular_for",
    "offerings",
    "dining_options",
    "amenities",
    "atmosphere",
    "crowd",
    "planning",
    "payments",
    "children",
    "parking",
    "gmaps_url",
    "latitude",
    "longitude",
    "ig_url",
    "tiktok_url",
]

_ABOUT_LIST_FIELDS = (
    "service_options",
    "highlights",
    "popular_for",
    "offerings",
    "dining_options",
    "amenities",
    "atmosphere",
    "crowd",
    "planning",
    "payments",
    "children",
    "parking",
)

_RAW_RECORD_METADATA_FIELDS = {
    "source_platform",
    "run_id",
    "scraped_at",
    "query",
    "source_record_id",
    "_ingest_source_path",
    "_ingest_record_index",
}


def _resolve_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_raw_base_dir(raw_dir: str) -> Path:
    raw_path = Path(raw_dir)
    if os.path.isabs(raw_dir):
        return raw_path
    return _resolve_project_root() / raw_path


def _resolve_normalized_output_dir(normalized_dir: str, run_date: str) -> Path:
    normalized_path = Path(normalized_dir)
    if os.path.isabs(normalized_dir):
        output_dir = normalized_path / run_date
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    normalized_posix = normalized_path.as_posix().strip("./")
    if normalized_posix == "data/normalized":
        return Path(get_normalized_dir(str(_resolve_project_root()), run_date))

    output_dir = _resolve_project_root() / normalized_path / run_date
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_coordinates_from_url(url: str) -> tuple[float | None, float | None]:
    for pattern in (
        r"@(-?\d+\.\d+),(-?\d+\.\d+)",
        r"[?&]center=(-?\d+\.\d+),(-?\d+\.\d+)",
    ):
        match = re.search(pattern, normalize_text(url))
        if not match:
            continue
        latitude = _as_float(match.group(1))
        longitude = _as_float(match.group(2))
        if latitude is not None and longitude is not None:
            return latitude, longitude
    return None, None


def _normalize_clock_time(value: str) -> str:
    cleaned = normalize_text(value).upper().replace(".", ":")
    if not cleaned:
        return ""

    for parser in ("%H:%M", "%H", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(cleaned, parser).strftime("%H:%M")
        except ValueError:
            continue
    return ""


def _extract_opening_details(raw: dict[str, Any]) -> tuple[str, str, list[str]]:
    raw_hours = raw.get("opening_hours")
    opening_hours = (
        [item for item in raw_hours if isinstance(item, str)]
        if isinstance(raw_hours, list)
        else []
    )

    opening_days: list[str] = []
    seen_days: set[str] = set()
    day_pattern = re.compile(
        r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        flags=re.IGNORECASE,
    )

    for item in opening_hours:
        day_match = day_pattern.search(item)
        if not day_match:
            continue
        normalized_day = normalize_text(day_match.group(1)).title()
        if normalized_day and normalized_day not in seen_days:
            seen_days.add(normalized_day)
            opening_days.append(normalized_day)

    raw_days = raw.get("opening_days")
    if isinstance(raw_days, list):
        for item in raw_days:
            day_match = day_pattern.search(normalize_text(str(item)))
            normalized_day = (
                normalize_text(day_match.group(1)).title() if day_match else ""
            )
            if normalized_day and normalized_day not in seen_days:
                seen_days.add(normalized_day)
                opening_days.append(normalized_day)

    time_pattern = re.compile(
        r"(\d{1,2}(?::\d{2})?(?:\s?[APap][Mm])?)\s*(?:-|–|—|to)\s*(\d{1,2}(?::\d{2})?(?:\s?[APap][Mm])?)"
    )
    opening_time = ""
    closing_time = ""
    for item in opening_hours:
        match = time_pattern.search(item)
        if not match:
            continue
        opening_time = _normalize_clock_time(match.group(1))
        closing_time = _normalize_clock_time(match.group(2))
        if opening_time and closing_time:
            break

    return opening_time, closing_time, opening_days


def _normalize_reason_code(reason_code: str) -> str:
    reason_code = normalize_text(reason_code).lower()
    return reason_code or "schema_mismatch"


def _extract_raw_payload(raw_record: dict[str, Any]) -> dict[str, Any]:
    nested = raw_record.get("raw")
    if isinstance(nested, dict):
        return nested

    return {
        key: value
        for key, value in raw_record.items()
        if key not in _RAW_RECORD_METADATA_FIELDS and not key.startswith("_ingest_")
    }


def _build_quarantine_record(
    reason_code: str,
    source_platform: str,
    raw_data: dict[str, Any],
    run_id: str | None = None,
    source_record_id: str | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    payload = quarantine_record(
        reason_code=_normalize_reason_code(reason_code),
        source_platform=source_platform,
        raw_data=raw_data,
        run_id=run_id,
        source_record_id=source_record_id,
        validation_errors=validation_errors,
    )

    try:
        return QuarantineRecord(**payload).to_json_dict()
    except ValidationError as exc:
        logger.warning("Failed to validate quarantine record: %s", exc)
        return payload


def _normalize_tag(tag: str) -> str:
    cleaned = normalize_text(tag).lower().replace("#", " ")
    cleaned = re.sub(r"[^a-z0-9\s-]", " ", cleaned)
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned


def _extract_hashtags(raw: dict[str, Any]) -> list[str]:
    hashtags: list[str] = []
    raw_hashtags = raw.get("hashtags", [])

    if isinstance(raw_hashtags, list):
        for item in raw_hashtags:
            if isinstance(item, str):
                hashtags.append(item)
            elif isinstance(item, dict):
                for key in ("name", "tag", "hashtag", "text"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        hashtags.append(value)
                        break
    elif isinstance(raw_hashtags, str):
        hashtags.extend(re.findall(r"#?[A-Za-z0-9_]+", raw_hashtags))

    caption = raw.get("caption") or raw.get("desc") or raw.get("description") or ""
    if isinstance(caption, str):
        hashtags.extend(re.findall(r"#[A-Za-z0-9_]+", caption))

    normalized: list[str] = []
    seen: set[str] = set()
    for hashtag in hashtags:
        tag = _normalize_tag(hashtag)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized


def _extract_video_id(raw_record: dict[str, Any], raw: dict[str, Any]) -> str:
    for value in (
        raw.get("video_id"),
        raw_record.get("source_record_id"),
        raw.get("aweme_id"),
    ):
        if isinstance(value, str) and value.strip():
            return normalize_text(value)
        if isinstance(value, int):
            return str(value)

    video_url = str(raw.get("video_url", ""))
    match = re.search(r"/video/(\d+)", video_url)
    if match:
        return match.group(1)

    return "unknown"


def _normalize_review_date(value: Any) -> str:
    if not value:
        return ""

    if isinstance(value, datetime):
        return value.date().isoformat()

    raw_value = normalize_text(str(value))
    if not raw_value:
        return ""

    for parser in (
        lambda item: (
            datetime.fromisoformat(item.replace("Z", "+00:00")).date().isoformat()
        ),
        lambda item: datetime.strptime(item, "%Y-%m-%d").date().isoformat(),
        lambda item: datetime.strptime(item, "%Y/%m/%d").date().isoformat(),
    ):
        try:
            return parser(raw_value)
        except ValueError:
            continue

    return ""


def _parse_review_date(value: Any) -> tuple[str, str | None]:
    normalized = _normalize_review_date(value)
    if normalized:
        return normalized, None

    if value in (None, ""):
        return "", None

    raw_value = normalize_text(str(value))
    if not raw_value:
        return "", None
    return "", f"review_date must be a valid YYYY-MM-DD date: {raw_value}"


def _is_google_maps_short_link(url: str) -> bool:
    normalized = normalize_text(url).lower()
    return normalized.startswith("https://maps.app.goo.gl/")


def _resolve_google_exact_page_identity(
    raw_record: dict[str, Any], raw: dict[str, Any] | None = None
) -> dict[str, str]:
    raw_payload = raw if isinstance(raw, dict) else {}

    canonical_place_key = normalize_text(
        str(raw_payload.get("canonical_place_key") or "")
    )
    canonical_place_url = normalize_url(
        str(raw_payload.get("canonical_place_url") or "")
    )
    validated_google_place_id = normalize_text(
        str(raw_payload.get("validated_google_place_id") or "")
    )
    maps_link = normalize_url(str(raw_payload.get("maps_link") or ""))
    gmaps_url = normalize_url(str(raw_payload.get("gmaps_url") or ""))
    source_record_id = normalize_text(str(raw_record.get("source_record_id") or ""))
    source_record_id_url = normalize_url(source_record_id)

    source_record_id_exact_page_key = ""
    if source_record_id in {canonical_place_key, canonical_place_url, validated_google_place_id}:
        source_record_id_exact_page_key = source_record_id
    elif source_record_id_url and source_record_id_url in {
        canonical_place_url,
        maps_link,
        gmaps_url,
    }:
        source_record_id_exact_page_key = source_record_id_url
    elif source_record_id.startswith("/maps/place/"):
        source_record_id_exact_page_key = source_record_id

    exact_page_key = ""
    exact_page_source = ""
    for source_name, candidate in (
        ("canonical_place_key", canonical_place_key),
        ("canonical_place_url", canonical_place_url),
        ("validated_google_place_id", validated_google_place_id),
        ("maps_link", maps_link),
        ("gmaps_url", gmaps_url),
        ("source_record_id", source_record_id_exact_page_key),
    ):
        if candidate:
            exact_page_key = candidate
            exact_page_source = source_name
            break

    exact_page_url = ""
    for candidate in (canonical_place_url, maps_link, gmaps_url):
        if candidate:
            exact_page_url = candidate
            break

    return {
        "exact_page_key": exact_page_key,
        "exact_page_source": exact_page_source,
        "exact_page_url": exact_page_url,
        "canonical_place_key": canonical_place_key,
        "canonical_place_url": canonical_place_url,
        "validated_google_place_id": validated_google_place_id,
        "maps_link": maps_link,
        "gmaps_url": gmaps_url,
        "source_record_id": source_record_id,
        "source_record_id_exact_page_key": source_record_id_exact_page_key,
    }


def _normalize_google_exact_page_key_for_dedup(exact_page_key: str) -> str:
    normalized_key = normalize_text(exact_page_key)
    if not normalized_key:
        return ""

    if normalized_key.startswith("/maps/place/"):
        return normalized_key

    normalized_url = normalize_url(normalized_key)
    google_place_prefix = "https://www.google.com/maps/place/"
    if normalized_url.startswith(google_place_prefix):
        return normalized_url.removeprefix("https://www.google.com")

    return normalized_key


def _merge_cafe_records(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    coarse_price_labels = {"budget", "mid", "premium"}
    day_order = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    day_index = {day: idx for idx, day in enumerate(day_order)}

    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "opening_days":
            current_days = [
                normalize_text(str(item)).title()
                for item in (current if isinstance(current, list) else [])
                if normalize_text(str(item))
            ]
            incoming_days = [
                normalize_text(str(item)).title()
                for item in (value if isinstance(value, list) else [])
                if normalize_text(str(item))
            ]
            combined = list(dict.fromkeys([*current_days, *incoming_days]))
            merged[key] = sorted(combined, key=lambda day: day_index.get(day, 99))
        elif key in _ABOUT_LIST_FIELDS:
            current_values = _normalize_string_list(current)
            incoming_values = _normalize_string_list(value)
            merged[key] = list(dict.fromkeys([*current_values, *incoming_values]))
        elif key == "price_range":
            current_price = normalize_text(str(current or "")).lower()
            incoming_price = normalize_text(str(value or "")).lower()
            if not current_price and incoming_price:
                merged[key] = value
            elif (
                current_price in coarse_price_labels
                and incoming_price
                and incoming_price not in coarse_price_labels
            ):
                merged[key] = value
        elif key == "gmaps_url":
            current_url = normalize_text(str(current or ""))
            incoming_url = normalize_text(str(value or ""))
            if not current_url and incoming_url:
                merged[key] = value
            elif _is_google_maps_short_link(incoming_url):
                merged[key] = value
        elif current in (None, "", []):
            merged[key] = value
    return merged


def _normalize_google_business_name(value: Any) -> str:
    cleaned = normalize_text(str(value or "")).lower().replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return normalize_text(cleaned)


def _normalize_google_business_address(value: Any) -> str:
    cleaned = normalize_text(str(value or "")).lower()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return normalize_text(cleaned)


def _build_google_business_identity(
    cafe: dict[str, Any],
    raw: dict[str, Any],
    google_identity: dict[str, str],
) -> dict[str, Any]:
    exact_page_keys = {
        normalize_text(candidate)
        for candidate in (
            google_identity.get("exact_page_key", ""),
            google_identity.get("canonical_place_key", ""),
            google_identity.get("canonical_place_url", ""),
            google_identity.get("validated_google_place_id", ""),
        )
        if normalize_text(candidate)
    }
    exact_page_urls = {
        normalize_url(candidate)
        for candidate in (
            google_identity.get("exact_page_url", ""),
            google_identity.get("maps_link", ""),
            google_identity.get("gmaps_url", ""),
        )
        if normalize_url(candidate)
    }

    return {
        "slug": normalize_text(str(cafe.get("slug") or "")),
        "name": normalize_text(str(cafe.get("name") or "")),
        "address": normalize_text(str(cafe.get("address") or "")),
        "phone": normalize_phone(str(raw.get("phone") or "")),
        "normalized_name": _normalize_google_business_name(cafe.get("name")),
        "normalized_address": _normalize_google_business_address(cafe.get("address")),
        "exact_page_keys": exact_page_keys,
        "exact_page_urls": exact_page_urls,
    }


def _initialize_google_business_entity(
    cafe: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    return {
        "cafe": cafe,
        "identity": {
            "slug": identity["slug"],
            "names": {identity["normalized_name"]} if identity["normalized_name"] else set(),
            "addresses": (
                {identity["normalized_address"]}
                if identity["normalized_address"]
                else set()
            ),
            "phones": {identity["phone"]} if identity["phone"] else set(),
            "exact_page_keys": set(identity["exact_page_keys"]),
            "exact_page_urls": set(identity["exact_page_urls"]),
        },
    }


def _merge_google_business_entity_identity(
    target: dict[str, Any], incoming: dict[str, Any]
) -> None:
    if incoming["normalized_name"]:
        target["names"].add(incoming["normalized_name"])
    if incoming["normalized_address"]:
        target["addresses"].add(incoming["normalized_address"])
    if incoming["phone"]:
        target["phones"].add(incoming["phone"])

    target["exact_page_keys"].update(incoming["exact_page_keys"])
    target["exact_page_urls"].update(incoming["exact_page_urls"])


def _google_business_value_conflicts(
    existing_values: set[str], incoming_value: str
) -> bool:
    return bool(incoming_value and existing_values and incoming_value not in existing_values)


def _build_google_business_conflict_message(
    existing_cafe: dict[str, Any],
    incoming_cafe: dict[str, Any],
    *,
    reason: str,
    conflict_fields: list[str] | None = None,
) -> str:
    fields = ", ".join(conflict_fields or []) or "business fields"
    return (
        f"google business dedup conflict via {reason} on {fields}: "
        f"existing '{existing_cafe.get('name', '')}' @ '{existing_cafe.get('address', '')}' "
        f"vs incoming '{incoming_cafe.get('name', '')}' @ '{incoming_cafe.get('address', '')}'"
    )


def _resolve_google_business_match(
    existing_entity: dict[str, Any], incoming_identity: dict[str, Any]
) -> dict[str, Any]:
    existing_identity = existing_entity["identity"]
    shared_exact_page = bool(
        existing_identity["exact_page_keys"] & incoming_identity["exact_page_keys"]
        or existing_identity["exact_page_urls"] & incoming_identity["exact_page_urls"]
    )
    shared_name = bool(
        incoming_identity["normalized_name"]
        and incoming_identity["normalized_name"] in existing_identity["names"]
    )
    shared_address = bool(
        incoming_identity["normalized_address"]
        and incoming_identity["normalized_address"] in existing_identity["addresses"]
    )
    shared_phone = bool(
        incoming_identity["phone"] and incoming_identity["phone"] in existing_identity["phones"]
    )

    name_conflict = _google_business_value_conflicts(
        existing_identity["names"], incoming_identity["normalized_name"]
    )
    address_conflict = _google_business_value_conflicts(
        existing_identity["addresses"], incoming_identity["normalized_address"]
    )
    phone_conflict = _google_business_value_conflicts(
        existing_identity["phones"], incoming_identity["phone"]
    )

    if shared_exact_page:
        conflict_fields = [
            field
            for field, has_conflict in (
                ("name", name_conflict),
                ("address", address_conflict),
                ("phone", phone_conflict),
            )
            if has_conflict
        ]
        if conflict_fields:
            return {
                "status": "conflict",
                "reason": "exact_page_key",
                "conflict_fields": conflict_fields,
            }
        return {"status": "match", "reason": "exact_page_key"}

    if shared_phone and shared_name:
        if address_conflict:
            return {
                "status": "conflict",
                "reason": "phone_and_name",
                "conflict_fields": ["address"],
            }
        return {"status": "match", "reason": "phone_and_name"}

    if shared_phone and shared_address:
        if name_conflict:
            return {
                "status": "conflict",
                "reason": "phone_and_address",
                "conflict_fields": ["name"],
            }
        return {"status": "match", "reason": "phone_and_address"}

    if shared_name and shared_address:
        if phone_conflict:
            return {
                "status": "conflict",
                "reason": "name_and_address",
                "conflict_fields": ["phone"],
            }
        return {"status": "match", "reason": "name_and_address"}

    return {"status": "none", "reason": ""}


def _retarget_related_records_to_cafe_slug(
    records: list[dict[str, Any]], cafe_slug: str
) -> list[dict[str, Any]]:
    if not cafe_slug:
        return records

    retargeted: list[dict[str, Any]] = []
    for record in records:
        updated = dict(record)
        updated["cafe_slug"] = cafe_slug
        retargeted.append(updated)
    return retargeted


def _build_known_slug_aliases(cafe_records: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for cafe in cafe_records:
        slug = str(cafe.get("slug", "")).strip()
        if not slug:
            continue

        name = normalize_text(str(cafe.get("name", ""))).lower()
        if name:
            aliases[name] = slug

        slug_text = slug.replace("-", " ")
        aliases[slug] = slug
        aliases[slug_text] = slug

        no_space_name = name.replace(" ", "") if name else ""
        if no_space_name and no_space_name not in aliases:
            aliases[no_space_name] = slug

        slug_parts = [part for part in slug.split("-") if part]
        if len(slug_parts) >= 3:
            short_slug = "-".join(slug_parts[:-1])
            short_slug_text = short_slug.replace("-", " ")
            aliases[short_slug] = slug
            aliases[short_slug_text] = slug

            short_no_space = short_slug_text.replace(" ", "")
            if short_no_space and short_no_space not in aliases:
                aliases[short_no_space] = slug
    return aliases


def _extract_cafe_name_hint(caption: str, hashtags: list[str]) -> str | None:
    caption = caption.strip()
    if caption:
        patterns = [
            r"\b(?:at|di|dari)\s+([A-Z][\w&'’.-]*(?:\s+[A-Z0-9][\w&'’.-]*){0,5})",
            r"\b(?:cafe|coffee shop|kedai kopi|kopi)\s+([A-Z0-9][\w&'’.-]*(?:\s+[A-Z0-9][\w&'’.-]*){0,4})",
        ]
        for pattern in patterns:
            match = re.search(pattern, caption)
            if match:
                candidate = normalize_text(match.group(1))
                candidate_lower = candidate.lower()
                if len(candidate.split()) < 2 and not any(
                    keyword in candidate_lower
                    for keyword in ("cafe", "coffee", "kopi", "kedai")
                ):
                    continue
                return candidate

    for hashtag in hashtags:
        if hashtag in _GENERIC_HASHTAGS:
            continue
        if any(keyword in hashtag for keyword in ("cafe", "coffee", "kopi", "kedai")):
            return normalize_text(hashtag.replace("-", " "))
    return None


def _append_validated_tag(
    target: list[dict[str, Any]],
    cafe_slug: str,
    tag: str,
    raw_record: dict[str, Any],
    quarantine: list[dict[str, Any]],
) -> None:
    normalized_tag = _normalize_tag(tag)
    if not cafe_slug or not normalized_tag:
        return

    tag_payload = {
        "cafe_slug": cafe_slug,
        "tag": normalized_tag,
    }
    try:
        target.append(CafeTagRecord(**tag_payload).to_json_dict())
    except ValidationError as exc:
        quarantine.append(
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=str(raw_record.get("source_platform", ""))
                or "google_maps",
                raw_data=raw_record.get("raw", raw_record)
                if isinstance(raw_record, dict)
                else {},
                run_id=raw_record.get("run_id")
                if isinstance(raw_record, dict)
                else None,
                source_record_id=(
                    raw_record.get("source_record_id")
                    if isinstance(raw_record, dict)
                    else None
                ),
                validation_errors=[str(exc)],
            )
        )


def _append_validated_photo(
    target: list[dict[str, Any]],
    cafe_slug: str,
    photo: Any,
    order: int,
    raw_record: dict[str, Any],
    quarantine: list[dict[str, Any]],
) -> None:
    url = ""
    caption = ""
    if isinstance(photo, str):
        url = normalize_url(photo)
    elif isinstance(photo, dict):
        for key in ("url", "src", "href"):
            candidate = photo.get(key)
            if isinstance(candidate, str) and candidate.strip():
                url = normalize_url(candidate)
                break
        for key in ("caption", "title", "alt", "description"):
            candidate = photo.get(key)
            if isinstance(candidate, str) and candidate.strip():
                caption = normalize_text(candidate)
                break

    if not cafe_slug or not url:
        return

    try:
        target.append(
            CafePhotoRecord(
                cafe_slug=cafe_slug,
                url=url,
                caption=caption,
                order=order,
            ).to_json_dict()
        )
    except ValidationError as exc:
        quarantine.append(
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=str(raw_record.get("source_platform", ""))
                or "google_maps",
                raw_data=raw_record.get("raw", raw_record)
                if isinstance(raw_record, dict)
                else {},
                run_id=raw_record.get("run_id")
                if isinstance(raw_record, dict)
                else None,
                source_record_id=(
                    raw_record.get("source_record_id")
                    if isinstance(raw_record, dict)
                    else None
                ),
                validation_errors=[str(exc)],
            )
        )


def _build_default_quarantine_buckets() -> dict[str, list[dict[str, Any]]]:
    return {
        "quarantine": [],
        "quarantine_cafes": [],
        "quarantine_cafe_photos": [],
        "quarantine_cafe_tags": [],
        "quarantine_influencer_reviews": [],
    }


def _append_bucketed_quarantine(
    buckets: dict[str, list[dict[str, Any]]],
    bucket_key: str,
    record: dict[str, Any],
) -> None:
    buckets["quarantine"].append(record)
    buckets[bucket_key].append(record)


def _extract_city_from_query(query: str) -> str:
    """Extract city name from search query like 'coffee in Samarinda'."""
    query = normalize_text(query)
    if not query:
        return ""

    match = re.search(r"\bin\s+(.+)$", query, flags=re.IGNORECASE)
    if match:
        return normalize_text(match.group(1))
    return ""


def _extract_district_from_address(address: str) -> str:
    """Extract district from address — the part immediately before 'Kec.'."""
    address = normalize_text(address)
    if not address:
        return ""
    match = re.search(r",\s*([^,]+?)\s*,\s*Kec\.", address, flags=re.IGNORECASE)
    if match:
        return normalize_text(match.group(1))
    return ""


def _normalize_price_range(value: str) -> str:
    raw = normalize_text(value)
    if not raw:
        return ""
    cleaned = re.sub(r"\s*per\s+person\s*", "", raw, flags=re.IGNORECASE).strip()
    cleaned = cleaned.replace(",", ".")
    return cleaned


def _extract_first_photo_url(photos: Any) -> str:
    if not isinstance(photos, list) or not photos:
        return ""
    first = photos[0]
    if isinstance(first, str):
        return normalize_url(first)
    if isinstance(first, dict):
        for key in ("url", "src", "href"):
            candidate = first.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return normalize_url(candidate)
    return ""


def _normalize_string_list(values: Any, *, lowercase: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = normalize_text(str(value))
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


def _category_to_tags(category: str) -> list[str]:
    """Convert raw category string to normalized tags."""
    normalized_category = _normalize_tag(category)
    tags: list[str] = []

    if normalized_category:
        tags.append(normalized_category)

    category_lower = normalize_text(category).lower()
    if any(keyword in category_lower for keyword in ("coffee", "cafe", "kopi")):
        tags.append("coffee-shop")

    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return deduped


def _find_cafe_mention_in_caption(
    caption: str, known_slugs: dict[str, str]
) -> str | None:
    """Try to match caption text to a known cafe slug."""
    haystack = normalize_text(caption).lower()
    if not haystack:
        return None

    for alias, slug in sorted(
        known_slugs.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if alias and alias in haystack:
            return slug
    return None


def _order_cafe_fields(cafe: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in _CAFE_FIELD_ORDER:
        if key in cafe:
            ordered[key] = cafe[key]

    for key, value in cafe.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def normalize_google_maps_record(
    raw_record: dict,
    run_id: str | None = None,
) -> dict[str, list[dict]]:
    """Transform a single raw Google Maps record into normalized records.

    Returns dict with keys:
        - "cafes": list of cafe dicts
        - "cafe_tags": list of tag dicts
        - "quarantine": list of quarantine dicts (for invalid records)
    """

    results: dict[str, list[dict]] = {
        "cafes": [],
        "cafe_photos": [],
        "cafe_tags": [],
        **_build_default_quarantine_buckets(),
    }

    raw = _extract_raw_payload(raw_record)
    if not isinstance(raw, dict):
        _append_bucketed_quarantine(
            results,
            "quarantine_cafes",
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=str(raw_record.get("source_platform", "google_maps"))
                or "google_maps",
                raw_data={},
                run_id=run_id or raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=["raw payload must be a dictionary"],
            ),
        )
        return results

    effective_run_id = run_id or raw_record.get("run_id")
    query = normalize_text(str(raw_record.get("query", "")))
    city_from_query = _extract_city_from_query(query)
    name = normalize_text(str(raw.get("name", "")))
    address = normalize_text(str(raw.get("address", "")))
    google_identity = _resolve_google_exact_page_identity(raw_record, raw)
    raw_gmaps_url = normalize_url(str(raw.get("gmaps_url") or ""))
    gmaps_url = raw_gmaps_url or google_identity["exact_page_url"]
    latitude = _as_float(raw.get("latitude"))
    longitude = _as_float(raw.get("longitude"))
    if latitude is None or longitude is None:
        fallback_latitude, fallback_longitude = _extract_coordinates_from_url(gmaps_url)
        latitude = fallback_latitude if latitude is None else latitude
        longitude = fallback_longitude if longitude is None else longitude
    opening_time, closing_time, opening_days = _extract_opening_details(raw)

    missing_fields = [
        field
        for field, value in {
            "name": name,
            "address": address,
            "latitude": latitude,
            "longitude": longitude,
        }.items()
        if value in (None, "")
    ]
    if missing_fields:
        _append_bucketed_quarantine(
            results,
            "quarantine_cafes",
            _build_quarantine_record(
                reason_code="missing_required_field",
                source_platform=str(raw_record.get("source_platform", "google_maps"))
                or "google_maps",
                raw_data=raw,
                run_id=effective_run_id,
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[
                    f"missing required field: {field}" for field in missing_fields
                ],
            ),
        )
        return results

    price_range_raw = str(raw.get("price_range", "") or "")
    price_range_match = re.match(r"(Rp[\s\d.,]+\s*[–\-]\s*[\d.,]+)", price_range_raw)
    price_range = (
        normalize_text(price_range_match.group(1))
        if price_range_match
        else price_range_raw
    )

    raw_photos = raw.get("photos")
    image_urls: list[str] = []
    if isinstance(raw_photos, list):
        for photo in raw_photos:
            if isinstance(photo, str) and photo.strip():
                url = normalize_url(photo)
                if url:
                    image_urls.append(url)
            elif isinstance(photo, dict):
                for key in ("url", "src", "href"):
                    candidate = photo.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        url = normalize_url(candidate)
                        if url:
                            image_urls.append(url)
                        break

    cafe_payload = {
        "name": name,
        "slug": generate_slug(name, city_from_query),
        "address": address,
        "district": _extract_district_from_address(address),
        "gmaps_url": gmaps_url,
        "latitude": latitude,
        "longitude": longitude,
        "price_range": price_range,
        "opening_time": opening_time,
        "closing_time": closing_time,
        "opening_days": opening_days,
        **{
            field: _normalize_string_list(raw.get(field))
            for field in _ABOUT_LIST_FIELDS
        },
        "ig_url": normalize_url(str(raw.get("ig_url", "") or "")),
        "tiktok_url": normalize_url(str(raw.get("tiktok_url", "") or "")),
        "image": image_urls,
        "description": "<p>lorem ipsum</p>",
        "is_featured": False,
        "status": "Draft",
    }

    try:
        validated_cafe = CafeRecord(**cafe_payload).to_json_dict()
        results["cafes"].append(validated_cafe)
    except ValidationError as exc:
        _append_bucketed_quarantine(
            results,
            "quarantine_cafes",
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=str(raw_record.get("source_platform", "google_maps"))
                or "google_maps",
                raw_data=raw,
                run_id=effective_run_id,
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[str(exc)],
            ),
        )
        return results

    raw_photos = raw.get("photos")
    if isinstance(raw_photos, list):
        for order, photo in enumerate(raw_photos):
            before_count = len(results["quarantine_cafe_photos"])
            _append_validated_photo(
                target=results["cafe_photos"],
                cafe_slug=validated_cafe["slug"],
                photo=photo,
                order=order,
                raw_record=raw_record,
                quarantine=results["quarantine_cafe_photos"],
            )
            if len(results["quarantine_cafe_photos"]) > before_count:
                results["quarantine"].append(results["quarantine_cafe_photos"][-1])

    for tag in _category_to_tags(str(raw.get("category", "") or "")):
        before_count = len(results["quarantine_cafe_tags"])
        _append_validated_tag(
            target=results["cafe_tags"],
            cafe_slug=validated_cafe["slug"],
            tag=tag,
            raw_record=raw_record,
            quarantine=results["quarantine_cafe_tags"],
        )
        if len(results["quarantine_cafe_tags"]) > before_count:
            results["quarantine"].append(results["quarantine_cafe_tags"][-1])

    return results


def normalize_tiktok_record(
    raw_record: dict,
    cafe_slugs: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Transform a single raw TikTok record into normalized records.

    Returns dict with keys:
        - "influencer_reviews": list of review dicts
        - "cafes": list of NEW cafe candidate dicts (if cafe mention found)
        - "cafe_tags": list of tag dicts from hashtags
        - "quarantine": list of quarantine dicts
    """

    results: dict[str, list[dict]] = {
        "influencer_reviews": [],
        "cafes": [],
        "cafe_tags": [],
        **_build_default_quarantine_buckets(),
    }

    raw = _extract_raw_payload(raw_record)
    if not isinstance(raw, dict):
        _append_bucketed_quarantine(
            results,
            "quarantine_influencer_reviews",
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=str(raw_record.get("source_platform", "tiktok"))
                or "tiktok",
                raw_data={},
                run_id=raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=["raw payload must be a dictionary"],
            ),
        )
        return results

    caption = normalize_text(
        str(raw.get("caption") or raw.get("desc") or raw.get("description") or "")
    )
    hashtags = _extract_hashtags(raw)
    known_records = [
        {"slug": slug, "name": slug.replace("-", " ")} for slug in (cafe_slugs or set())
    ]
    known_aliases = _build_known_slug_aliases(known_records)

    matched_slug = _find_cafe_mention_in_caption(caption, known_aliases)
    if not matched_slug and hashtags:
        matched_slug = _find_cafe_mention_in_caption(" ".join(hashtags), known_aliases)

    if not matched_slug:
        cafe_name_hint = _extract_cafe_name_hint(caption, hashtags)
        _append_bucketed_quarantine(
            results,
            "quarantine_influencer_reviews",
            _build_quarantine_record(
                reason_code="missing_required_field",
                source_platform=str(raw_record.get("source_platform", "tiktok"))
                or "tiktok",
                raw_data=raw,
                run_id=raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[
                    "missing required field: cafe_slug",
                    (
                        f"unable to resolve canonical cafe for mention: {cafe_name_hint}"
                        if cafe_name_hint
                        else "unable to resolve canonical cafe from caption or hashtags"
                    ),
                ],
            ),
        )
        return results

    video_id = _extract_video_id(raw_record, raw)
    review_cafe_slug = matched_slug or f"unmatched-{video_id}"
    influencer_name = normalize_text(str(raw.get("author_handle", "") or ""))
    post_url = normalize_url(str(raw.get("video_url", "") or ""))
    review_date, review_date_error = _parse_review_date(raw.get("post_date", ""))

    if review_date_error:
        _append_bucketed_quarantine(
            results,
            "quarantine_influencer_reviews",
            _build_quarantine_record(
                reason_code="invalid_timestamp",
                source_platform=str(raw_record.get("source_platform", "tiktok"))
                or "tiktok",
                raw_data=raw,
                run_id=raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[review_date_error],
            ),
        )
        return results

    missing_fields = [
        field
        for field, value in {
            "cafe_slug": matched_slug,
            "influencer_name": influencer_name,
            "post_url": post_url,
        }.items()
        if not value
    ]
    if missing_fields:
        _append_bucketed_quarantine(
            results,
            "quarantine_influencer_reviews",
            _build_quarantine_record(
                reason_code="missing_required_field",
                source_platform=str(raw_record.get("source_platform", "tiktok"))
                or "tiktok",
                raw_data=raw,
                run_id=raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[
                    f"missing required field: {field}" for field in missing_fields
                ],
            ),
        )
        return results

    review_payload = {
        "cafe_slug": review_cafe_slug,
        "influencer_name": influencer_name,
        "platform": "tiktok",
        "post_url": post_url,
        "embed_code": normalize_text(
            str(raw.get("embed_code") or raw.get("embed_html") or "")
        ),
        "review_date": review_date,
        "is_approved": False,
    }

    try:
        results["influencer_reviews"].append(
            InfluencerReviewRecord(**review_payload).to_json_dict()
        )
    except ValidationError as exc:
        reason_code = "schema_mismatch"
        lower_error = str(exc).lower()
        if "post_url" in lower_error and "valid http(s) url" in lower_error:
            reason_code = "invalid_url"
        elif "review_date" in lower_error:
            reason_code = "invalid_timestamp"

        _append_bucketed_quarantine(
            results,
            "quarantine_influencer_reviews",
            _build_quarantine_record(
                reason_code=reason_code,
                source_platform=str(raw_record.get("source_platform", "tiktok"))
                or "tiktok",
                raw_data=raw,
                run_id=raw_record.get("run_id"),
                source_record_id=raw_record.get("source_record_id"),
                validation_errors=[str(exc)],
            ),
        )
        return results

    if matched_slug:
        for hashtag in hashtags:
            before_count = len(results["quarantine_cafe_tags"])
            _append_validated_tag(
                target=results["cafe_tags"],
                cafe_slug=matched_slug,
                tag=hashtag,
                raw_record=raw_record,
                quarantine=results["quarantine_cafe_tags"],
            )
            if len(results["quarantine_cafe_tags"]) > before_count:
                results["quarantine"].append(results["quarantine_cafe_tags"][-1])

    return results


def _source_quarantine_key(source: str) -> str:
    return (
        "quarantine_cafes"
        if source == "google_maps"
        else "quarantine_influencer_reviews"
    )


def _load_raw_file_records(
    filepath: Path,
    source: str,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    quarantines = _build_default_quarantine_buckets()
    quarantine_key = _source_quarantine_key(source)

    try:
        raw_payload = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        _append_bucketed_quarantine(
            quarantines,
            quarantine_key,
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=source,
                raw_data={"file": str(filepath)},
                validation_errors=[f"failed to read raw file: {exc}"],
            ),
        )
        return [], quarantines

    try:
        loaded: Any = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        _append_bucketed_quarantine(
            quarantines,
            quarantine_key,
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=source,
                raw_data={"file": str(filepath)},
                validation_errors=[
                    f"invalid json: {exc.msg} at line {exc.lineno} column {exc.colno}"
                ],
            ),
        )
        return [], quarantines

    if isinstance(loaded, dict):
        items = [loaded]
    elif isinstance(loaded, list):
        items = loaded
    else:
        _append_bucketed_quarantine(
            quarantines,
            quarantine_key,
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=source,
                raw_data={"file": str(filepath), "value_type": type(loaded).__name__},
                validation_errors=[
                    "top-level raw JSON must be an object or array of objects"
                ],
            ),
        )
        return [], quarantines

    records: list[dict[str, Any]] = []
    for item_index, item in enumerate(items):
        if isinstance(item, dict):
            record = dict(item)
            record.setdefault("_ingest_source_path", filepath.as_posix())
            record.setdefault("_ingest_record_index", item_index)
            records.append(record)
            continue
        _append_bucketed_quarantine(
            quarantines,
            quarantine_key,
            _build_quarantine_record(
                reason_code="schema_mismatch",
                source_platform=source,
                raw_data={"file": str(filepath), "invalid_item": item},
                validation_errors=["raw file item must be an object"],
            ),
        )

    return records, quarantines


def _google_raw_record_keep_sort_key(record: dict[str, Any]) -> tuple[str, int, str, str, str]:
    raw_payload = _extract_raw_payload(record)
    source_path = normalize_text(str(record.get("_ingest_source_path") or ""))
    source_record_id = normalize_text(str(record.get("source_record_id") or ""))
    raw_name = ""
    raw_address = ""
    if isinstance(raw_payload, dict):
        raw_name = normalize_text(str(raw_payload.get("name") or "")).lower()
        raw_address = normalize_text(str(raw_payload.get("address") or "")).lower()

    raw_index_value = record.get("_ingest_record_index")
    if isinstance(raw_index_value, int):
        raw_index = raw_index_value
    elif isinstance(raw_index_value, str):
        try:
            raw_index = int(raw_index_value)
        except ValueError:
            raw_index = sys.maxsize
    else:
        raw_index = sys.maxsize

    return (source_path, raw_index, source_record_id, raw_name, raw_address)


def _dedupe_google_raw_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped_records: list[dict[str, Any]] = []
    seen_exact_page_keys: set[str] = set()

    for record in sorted(records, key=_google_raw_record_keep_sort_key):
        raw_payload = record.get("raw")
        google_identity = _resolve_google_exact_page_identity(
            record, raw_payload if isinstance(raw_payload, dict) else None
        )
        exact_page_key = google_identity["exact_page_key"]
        normalized_exact_page_key = _normalize_google_exact_page_key_for_dedup(
            exact_page_key
        )
        if not exact_page_key:
            deduped_records.append(record)
            continue

        if normalized_exact_page_key in seen_exact_page_keys:
            logger.debug(
                "Skipping duplicate Google raw exact page %s from %s[%s]",
                exact_page_key,
                record.get("_ingest_source_path"),
                record.get("_ingest_record_index"),
            )
            continue

        seen_exact_page_keys.add(normalized_exact_page_key)
        deduped_records.append(record)

    return deduped_records


def _cafes_conflict(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    existing_address = normalize_text(str(existing.get("address", ""))).lower()
    incoming_address = normalize_text(str(incoming.get("address", ""))).lower()
    return bool(
        existing_address and incoming_address and existing_address != incoming_address
    )


def run_extraction(
    raw_dir: str = "data/raw",
    normalized_dir: str = "data/normalized",
    run_date: str | None = None,
    run_id: str | None = None,
) -> dict:
    """Run the full extraction pipeline.

    Reads all raw files for the given date, normalizes them,
    and writes normalized JSON files.

    Returns summary dict with counts.
    """

    resolved_run_date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_base_dir = _resolve_raw_base_dir(raw_dir)
    raw_run_dir = raw_base_dir / resolved_run_date
    output_dir = _resolve_normalized_output_dir(normalized_dir, resolved_run_date)

    grouped_records: dict[str, list[dict[str, Any]]] = {"google_maps": [], "tiktok": []}
    total_raw_records = 0
    file_quarantines = _build_default_quarantine_buckets()

    if raw_run_dir.exists():
        for filepath in sorted(raw_run_dir.rglob("*.json")):
            if filepath.name.startswith("errors_"):
                continue

            source = filepath.parent.name
            if source not in grouped_records:
                logger.debug("Skipping unsupported raw source file: %s", filepath)
                continue

            records, quarantines = _load_raw_file_records(filepath, source)
            total_raw_records += len(records)
            grouped_records[source].extend(records)
            for key in file_quarantines:
                file_quarantines[key].extend(quarantines[key])
    else:
        logger.warning("Raw run directory does not exist: %s", raw_run_dir)

    all_cafes: list[dict[str, Any]] = []
    all_cafe_photos: list[dict[str, Any]] = []
    all_cafe_tags: list[dict[str, Any]] = []
    all_reviews: list[dict[str, Any]] = []
    all_quarantine = list(file_quarantines["quarantine"])
    quarantine_cafes = list(file_quarantines["quarantine_cafes"])
    quarantine_cafe_photos = list(file_quarantines["quarantine_cafe_photos"])
    quarantine_cafe_tags = list(file_quarantines["quarantine_cafe_tags"])
    quarantine_influencer_reviews = list(
        file_quarantines["quarantine_influencer_reviews"]
    )

    google_entities: list[dict[str, Any]] = []
    google_raw_records = _dedupe_google_raw_records(grouped_records["google_maps"])
    for record in google_raw_records:
        raw_payload = record.get("raw")
        raw = raw_payload if isinstance(raw_payload, dict) else {}
        google_identity = _resolve_google_exact_page_identity(
            record, raw
        )
        normalized = normalize_google_maps_record(record, run_id=record.get("run_id"))
        all_quarantine.extend(normalized["quarantine"])
        quarantine_cafes.extend(normalized["quarantine_cafes"])
        quarantine_cafe_photos.extend(normalized["quarantine_cafe_photos"])
        quarantine_cafe_tags.extend(normalized["quarantine_cafe_tags"])

        if not normalized["cafes"]:
            continue

        cafe = normalized["cafes"][0]
        slug = str(cafe.get("slug", ""))
        if not slug:
            continue

        business_identity = _build_google_business_identity(cafe, raw, google_identity)
        matched_entities: list[tuple[int, dict[str, Any]]] = []
        conflict_entities: list[tuple[int, dict[str, Any]]] = []
        for entity_index, entity in enumerate(google_entities):
            match_result = _resolve_google_business_match(entity, business_identity)
            if match_result["status"] == "match":
                matched_entities.append((entity_index, match_result))
            elif match_result["status"] == "conflict":
                conflict_entities.append((entity_index, match_result))

        if len(matched_entities) > 1:
            quarantine = _build_quarantine_record(
                reason_code="dedupe_conflict",
                source_platform=str(record.get("source_platform", "google_maps"))
                or "google_maps",
                raw_data=raw,
                run_id=record.get("run_id"),
                source_record_id=record.get("source_record_id"),
                validation_errors=[
                    f"google business dedup is ambiguous for slug {slug}: matched multiple existing entities"
                ],
            )
            all_quarantine.append(quarantine)
            quarantine_cafes.append(quarantine)
            continue

        if matched_entities:
            entity_index, match_result = matched_entities[0]
            entity = google_entities[entity_index]
            entity["cafe"] = _merge_cafe_records(entity["cafe"], cafe)
            _merge_google_business_entity_identity(entity["identity"], business_identity)
            target_slug = str(entity["cafe"].get("slug", ""))
        else:
            if conflict_entities:
                conflict_index, conflict_result = conflict_entities[0]
                existing_cafe = google_entities[conflict_index]["cafe"]
                quarantine = _build_quarantine_record(
                    reason_code="dedupe_conflict",
                    source_platform=str(record.get("source_platform", "google_maps"))
                    or "google_maps",
                    raw_data=raw,
                    run_id=record.get("run_id"),
                    source_record_id=record.get("source_record_id"),
                    validation_errors=[
                        _build_google_business_conflict_message(
                            existing_cafe,
                            cafe,
                            reason=str(conflict_result.get("reason") or "business_match"),
                            conflict_fields=list(
                                conflict_result.get("conflict_fields") or []
                            ),
                        )
                    ],
                )
                all_quarantine.append(quarantine)
                quarantine_cafes.append(quarantine)
                continue

            slug_collision_entity = next(
                (
                    entity
                    for entity in google_entities
                    if str(entity["cafe"].get("slug", "")) == slug
                ),
                None,
            )
            if slug_collision_entity:
                quarantine = _build_quarantine_record(
                    reason_code="dedupe_conflict",
                    source_platform=str(record.get("source_platform", "google_maps"))
                    or "google_maps",
                    raw_data=raw,
                    run_id=record.get("run_id"),
                    source_record_id=record.get("source_record_id"),
                    validation_errors=[
                        _build_google_business_conflict_message(
                            slug_collision_entity["cafe"],
                            cafe,
                            reason="slug_collision_without_strong_business_match",
                            conflict_fields=["slug"],
                        )
                    ],
                )
                all_quarantine.append(quarantine)
                quarantine_cafes.append(quarantine)
                continue

            google_entities.append(_initialize_google_business_entity(cafe, business_identity))
            target_slug = slug

        all_cafe_tags.extend(
            _retarget_related_records_to_cafe_slug(normalized["cafe_tags"], target_slug)
        )
        all_cafe_photos.extend(
            _retarget_related_records_to_cafe_slug(normalized["cafe_photos"], target_slug)
        )

    google_cafes = [entity["cafe"] for entity in google_entities]
    all_cafes.extend(google_cafes)

    known_google_slugs = {
        str(cafe.get("slug", "")) for cafe in google_cafes if str(cafe.get("slug", ""))
    }

    for record in grouped_records["tiktok"]:
        normalized = normalize_tiktok_record(record, cafe_slugs=known_google_slugs)
        all_cafes.extend(normalized["cafes"])
        all_cafe_tags.extend(normalized["cafe_tags"])
        all_reviews.extend(normalized["influencer_reviews"])
        all_quarantine.extend(normalized["quarantine"])
        quarantine_cafes.extend(normalized["quarantine_cafes"])
        quarantine_cafe_tags.extend(normalized["quarantine_cafe_tags"])
        quarantine_influencer_reviews.extend(
            normalized["quarantine_influencer_reviews"]
        )

    deduped_cafes: dict[str, dict[str, Any]] = {
        str(cafe.get("slug", "")): cafe
        for cafe in google_cafes
        if str(cafe.get("slug", ""))
    }
    for cafe in all_cafes[len(google_cafes) :]:
        slug = str(cafe.get("slug", ""))
        if not slug:
            continue
        existing = deduped_cafes.get(slug)
        deduped_cafes[slug] = _merge_cafe_records(existing, cafe) if existing else cafe

    deduped_photos: list[dict[str, Any]] = []
    seen_photo_keys: set[tuple[str, str]] = set()
    for photo in all_cafe_photos:
        key = (
            str(photo.get("cafe_slug", "")),
            normalize_url(str(photo.get("url", ""))),
        )
        if not key[0] or not key[1] or key in seen_photo_keys:
            continue
        seen_photo_keys.add(key)
        deduped_photos.append(photo)

    deduped_tags: list[dict[str, Any]] = []
    seen_tag_keys: set[tuple[str, str]] = set()
    for tag in all_cafe_tags:
        key = (str(tag.get("cafe_slug", "")), str(tag.get("tag", "")))
        if not key[0] or not key[1] or key in seen_tag_keys:
            continue
        seen_tag_keys.add(key)
        deduped_tags.append(tag)

    deduped_reviews: list[dict[str, Any]] = []
    seen_review_keys: set[tuple[str, str]] = set()
    for review in all_reviews:
        key = (
            str(review.get("platform", "")),
            normalize_url(str(review.get("post_url", ""))),
        )
        if not key[0] or not key[1] or key in seen_review_keys:
            continue
        seen_review_keys.add(key)
        deduped_reviews.append(review)

    ordered_cafes = [_order_cafe_fields(cafe) for cafe in deduped_cafes.values()]

    save_json(str(output_dir / "cafes.json"), ordered_cafes)
    save_json(str(output_dir / "cafe_photos.json"), deduped_photos)
    save_json(str(output_dir / "cafe_tags.json"), deduped_tags)
    save_json(str(output_dir / "influencer_reviews.json"), deduped_reviews)
    save_json(str(output_dir / "events.json"), [])
    save_json(str(output_dir / "quarantine.json"), all_quarantine)
    save_json(str(output_dir / "quarantine_cafes.json"), quarantine_cafes)
    save_json(str(output_dir / "quarantine_cafe_photos.json"), quarantine_cafe_photos)
    save_json(str(output_dir / "quarantine_cafe_tags.json"), quarantine_cafe_tags)
    save_json(
        str(output_dir / "quarantine_influencer_reviews.json"),
        quarantine_influencer_reviews,
    )

    summary = {
        "run_id": run_id,
        "run_date": resolved_run_date,
        "raw_dir": str(raw_run_dir),
        "normalized_dir": str(output_dir),
        "total_raw_records": total_raw_records,
        "cafes": len(deduped_cafes),
        "cafe_photos": len(deduped_photos),
        "cafe_tags": len(deduped_tags),
        "influencer_reviews": len(deduped_reviews),
        "quarantine": len(all_quarantine),
    }
    logger.info("Extraction summary: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Extract & normalize raw scraped data")
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--normalized-dir", default="data/normalized")
    parser.add_argument(
        "--date", default=None, help="Run date YYYY-MM-DD (default: today)"
    )
    args = parser.parse_args()

    result = run_extraction(
        raw_dir=args.raw_dir,
        normalized_dir=args.normalized_dir,
        run_date=args.date,
    )
    logger.info("Extraction completed: %s", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
