from __future__ import annotations

import re
from collections import deque
from typing import Any
from urllib.parse import unquote, urlparse

from .constants import (
    BINDING_REASON_CODES,
    BINDING_STATUS_VALUES,
    DUPLICATE_EXACT_PAGE_REASON_CODE,
    EMBEDDED_ABSOLUTE_URL_RE,
    EMBEDDED_PLACE_PATH_RE,
    EMBEDDED_URL_PARAM_NAMES,
    GOOGLE_HOST_RE,
    PLACE_HREF_RE,
    PLACE_ID_PATTERNS,
    VALIDATED_PLACE_ID_PATTERNS,
)

def normalize_place_url(raw_url: str) -> str:
    """Normalize Google Maps place URLs for deduplication and navigation."""
    for candidate in iter_google_maps_url_candidates(raw_url):
        parsed = urlparse(candidate)
        if not parsed.scheme or not parsed.netloc:
            continue
        if not is_google_maps_host(parsed.netloc):
            continue

        path = decode_google_maps_url_component(parsed.path)
        if "/maps/place/" not in path:
            continue

        return f"https://www.google.com{path}"
    return ""

def extract_google_place_id(raw_url: str) -> str:
    direct_value = validate_google_place_token(raw_url)
    if direct_value:
        return direct_value

    for candidate in iter_google_maps_url_candidates(raw_url):
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc and not is_google_maps_host(parsed.netloc):
            continue

        for pattern in PLACE_ID_PATTERNS:
            for match in pattern.finditer(candidate):
                validated_value = validate_google_place_token(match.group(1))
                if validated_value:
                    return validated_value
    return ""

def decode_google_maps_url_component(raw_value: Any) -> str:
    candidate = str(raw_value or "").strip().strip("\"'")
    if not candidate:
        return ""

    for _ in range(4):
        updated = (
            candidate.replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u003f", "?")
            .replace("\\u002f", "/")
            .replace("\\/", "/")
            .replace("&amp;", "&")
        )
        updated = unquote(updated).strip().strip("\"'")
        if updated == candidate:
            break
        candidate = updated
    return candidate

def is_google_maps_host(host: str) -> bool:
    normalized_host = str(host or "").strip().strip(".").lower()
    if not normalized_host:
        return False
    if normalized_host == "maps.app.goo.gl":
        return True
    return bool(GOOGLE_HOST_RE.search(normalized_host))

def iter_google_maps_url_candidates(raw_url: Any):
    initial_candidate = decode_google_maps_url_component(raw_url)
    if not initial_candidate:
        return

    pending: deque[str] = deque([initial_candidate])
    seen: set[str] = set()

    while pending:
        current = decode_google_maps_url_component(pending.popleft())
        if not current or current in seen:
            continue

        seen.add(current)

        if current.startswith("//"):
            current = f"https:{current}"
        elif current.startswith("/"):
            current = f"https://www.google.com{current}"
        elif re.match(r"^(?:www\.)?google\.[A-Za-z.]+/", current, re.IGNORECASE):
            current = f"https://{current}"
        elif current.startswith("maps.app.goo.gl/"):
            current = f"https://{current}"

        yield current

        parsed = urlparse(current)
        is_google_host = bool(
            parsed.scheme and parsed.netloc and is_google_maps_host(parsed.netloc)
        )

        if is_google_host:
            for chunk in parsed.query.split("&"):
                if not chunk or "=" not in chunk:
                    continue

                param_name, raw_value = chunk.split("=", 1)
                if param_name not in EMBEDDED_URL_PARAM_NAMES or not raw_value:
                    continue
                pending.append(raw_value)

            for match in EMBEDDED_PLACE_PATH_RE.findall(current):
                if match != current:
                    pending.append(match)

        for match in EMBEDDED_ABSOLUTE_URL_RE.findall(current):
            if match != current:
                pending.append(match)

def validate_google_place_token(candidate: Any) -> str:
    value = str(candidate or "").strip()
    if not value:
        return ""

    value = unquote(value)
    if any(token in value for token in (" ", "+", "/", "?", "&", "=")):
        return ""

    for pattern in VALIDATED_PLACE_ID_PATTERNS:
        if pattern.fullmatch(value):
            return value
    return ""

def extract_validated_google_place_token(*candidates: Any) -> str:
    for candidate in candidates:
        direct_value = validate_google_place_token(candidate)
        if direct_value:
            return direct_value

        extracted_value = validate_google_place_token(
            extract_google_place_id(str(candidate or ""))
        )
        if extracted_value:
            return extracted_value
    return ""

def merge_discovered_place_targets(
    discovered_places: list[dict[str, str]],
    diagnostics: dict[str, Any],
    raw_urls: list[str] | set[str],
    *,
    source_name: str,
) -> int:
    source_stats = diagnostics.setdefault(
        "sources",
        {},
    ).setdefault(
        source_name,
        {
            "raw_candidates": 0,
            "accepted": 0,
            "duplicates_skipped": 0,
            "invalid_skipped": 0,
        },
    )
    seen_exact_page_keys = diagnostics.setdefault("seen_exact_page_keys", set())
    place_index_by_key = diagnostics.setdefault("place_index_by_key", {})
    duplicate_examples = diagnostics.setdefault("duplicate_examples", [])

    added = 0
    for raw_url in raw_urls:
        source_stats["raw_candidates"] += 1
        diagnostics["raw_candidates"] += 1

        place_url = normalize_place_url(raw_url)
        exact_page_key = build_google_maps_exact_page_key(
            raw_url
        ) or build_google_maps_exact_page_key(place_url)
        if not place_url or not exact_page_key:
            source_stats["invalid_skipped"] += 1
            diagnostics["invalid_skipped"] += 1
            continue

        place_id = extract_google_place_id(raw_url) or extract_google_place_id(
            place_url
        )
        existing_index = place_index_by_key.get(exact_page_key)
        if existing_index is not None:
            source_stats["duplicates_skipped"] += 1
            diagnostics["duplicates_skipped"] += 1
            existing = discovered_places[existing_index]
            if not existing.get("place_id") and place_id:
                existing["place_id"] = place_id
            if len(duplicate_examples) < 10:
                duplicate_examples.append(
                    {
                        "reason_code": DUPLICATE_EXACT_PAGE_REASON_CODE,
                        "source": source_name,
                        "exact_page_key": exact_page_key,
                        "kept_place_url": existing.get("place_url", ""),
                        "skipped_place_url": place_url,
                    }
                )
            continue

        place_index_by_key[exact_page_key] = len(discovered_places)
        seen_exact_page_keys.add(exact_page_key)
        discovered_places.append(
            {
                "place_url": place_url,
                "place_id": place_id,
            }
        )
        source_stats["accepted"] += 1
        diagnostics["unique_candidates"] += 1
        added += 1

    diagnostics["unique_exact_page_keys"] = len(seen_exact_page_keys)
    return added

def build_google_maps_exact_page_key(place_url: str) -> str:
    normalized_url = normalize_place_url(place_url)
    if not normalized_url:
        return ""

    parsed = urlparse(normalized_url)
    return parsed.path or ""

def normalize_google_maps_exact_page_key(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("/maps/place/"):
        parsed = urlparse(f"https://www.google.com{candidate}")
        return parsed.path or candidate.split("?", 1)[0]
    return build_google_maps_exact_page_key(candidate)

def extract_google_maps_place_label(raw_url: Any) -> str:
    for candidate in iter_google_maps_url_candidates(raw_url):
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc and not is_google_maps_host(parsed.netloc):
            continue

        path = decode_google_maps_url_component(parsed.path)
        if "/maps/place/" not in path:
            continue

        slug = path.split("/maps/place/", 1)[1].split("/", 1)[0]
        label = unquote(slug).replace("+", " ")
        label = re.sub(r"\s+", " ", label).strip(" /")
        if label:
            return label
    return ""

def normalize_google_binding_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _build_google_maps_base_record_context(
    *,
    place_url: Any = "",
    source_record_id: Any = "",
    name: Any = "",
    address: Any = "",
) -> dict[str, str]:
    normalized_place_url = normalize_place_url(place_url)
    return {
        "place_url": normalized_place_url,
        "source_record_id": str(source_record_id or "").strip(),
        "name": str(name or "").strip(),
        "address": str(address or "").strip(),
        "gmaps_url": normalized_place_url,
        "canonical_place_key": build_google_maps_exact_page_key(normalized_place_url),
        "validated_google_place_id": extract_validated_google_place_token(
            normalized_place_url
        ),
        "normalized_name": normalize_google_binding_text(name),
    }

def _index_google_record_context(
    *,
    record_context: dict[str, str],
    by_place_id: dict[str, dict[str, str]],
    by_exact_page_key: dict[str, dict[str, str]],
    by_gmaps_url: dict[str, dict[str, str]],
    by_name: dict[str, dict[str, str]],
    diagnostics: dict[str, Any],
    counter_keys: dict[str, str],
    include_normalized_name_in_name_context: bool = False,
) -> bool:
    identity_loaded = False

    validated_google_place_id = str(
        record_context.get("validated_google_place_id") or ""
    ).strip()
    if validated_google_place_id and validated_google_place_id not in by_place_id:
        by_place_id[validated_google_place_id] = dict(record_context)
        diagnostics[counter_keys["place_id"]] += 1
        identity_loaded = True

    canonical_place_key = str(record_context.get("canonical_place_key") or "").strip()
    if canonical_place_key and canonical_place_key not in by_exact_page_key:
        by_exact_page_key[canonical_place_key] = dict(record_context)
        diagnostics[counter_keys["exact_page_key"]] += 1
        identity_loaded = True

    normalized_gmaps_url = str(record_context.get("gmaps_url") or "").strip()
    if normalized_gmaps_url and normalized_gmaps_url not in by_gmaps_url:
        by_gmaps_url[normalized_gmaps_url] = dict(record_context)
        diagnostics[counter_keys["gmaps_url"]] += 1
        identity_loaded = True

    normalized_name = str(record_context.get("normalized_name") or "").strip()
    if normalized_name and normalized_name not in by_name:
        name_record_context = dict(record_context)
        if include_normalized_name_in_name_context:
            name_record_context["normalized_name"] = normalized_name
        by_name[normalized_name] = name_record_context
        diagnostics[counter_keys["name"]] += 1
        identity_loaded = True

    return identity_loaded

def _resolve_google_identity_match(
    *,
    validated_google_place_id: str,
    canonical_place_key: str,
    gmaps_url: str,
    normalized_name: str,
    by_place_id: dict[str, dict[str, str]],
    by_exact_page_key: dict[str, dict[str, str]],
    by_gmaps_url: dict[str, dict[str, str]],
    by_name: dict[str, dict[str, str]],
    exact_page_match_name: str = "canonical_place_key",
) -> tuple[dict[str, str] | None, str, str]:
    if validated_google_place_id:
        kept_record = by_place_id.get(validated_google_place_id)
        if kept_record:
            return kept_record, "validated_google_place_id", validated_google_place_id

    if canonical_place_key:
        kept_record = by_exact_page_key.get(canonical_place_key)
        if kept_record:
            return kept_record, exact_page_match_name, canonical_place_key

    if gmaps_url:
        kept_record = by_gmaps_url.get(gmaps_url)
        if kept_record:
            return kept_record, "gmaps_url", gmaps_url

    if normalized_name:
        kept_record = by_name.get(normalized_name)
        if kept_record:
            return kept_record, "name", normalized_name

    return None, "", ""

def build_google_maps_history_record_context(
    *,
    name: Any = "",
    address: Any = "",
    gmaps_url: Any = "",
    source_record_id: Any = "",
    slug: Any = "",
) -> dict[str, str]:
    context = _build_google_maps_base_record_context(
        place_url=gmaps_url,
        source_record_id=source_record_id,
        name=name,
        address=address,
    )
    context["slug"] = str(slug or "").strip()
    return context

def build_google_maps_record_identity_snapshot(
    details: dict[str, Any],
    *,
    place_url: str = "",
    source_record_id: str = "",
) -> dict[str, str]:
    base_context = _build_google_maps_base_record_context(
        place_url=place_url,
        source_record_id=source_record_id,
        name=details.get("name"),
        address=details.get("address"),
    )
    return {
        "place_url": base_context["place_url"],
        "source_record_id": base_context["source_record_id"],
        "name": base_context["name"],
        "address": base_context["address"],
        "discovery_url": str(details.get("discovery_url") or "").strip(),
        "final_page_url": str(details.get("final_page_url") or "").strip(),
        "canonical_place_url": str(details.get("canonical_place_url") or "").strip(),
        "canonical_place_key": str(details.get("canonical_place_key") or "").strip(),
        "validated_google_place_id": str(
            details.get("validated_google_place_id") or ""
        ).strip(),
        "binding_status": str(details.get("binding_status") or "").strip(),
    }

def build_google_maps_binding_diagnostics(
    *,
    discovery_url: str,
    final_page_url: str,
    canonical_place_url: str,
    validated_google_place_id: str,
    discovery_place_id: str = "",
    name: str = "",
    address: str = "",
) -> dict[str, Any]:
    discovery_place_key = build_google_maps_exact_page_key(discovery_url)
    final_place_key = build_google_maps_exact_page_key(
        final_page_url
    ) or build_google_maps_exact_page_key(canonical_place_url)
    discovery_place_token = extract_validated_google_place_token(
        discovery_place_id,
        discovery_url,
    )
    final_place_token = extract_validated_google_place_token(
        validated_google_place_id,
        final_page_url,
        canonical_place_url,
    )
    discovery_place_label = extract_google_maps_place_label(discovery_url)
    final_place_label = extract_google_maps_place_label(
        final_page_url
    ) or extract_google_maps_place_label(canonical_place_url)

    discovery_label_norm = normalize_google_binding_text(discovery_place_label)
    final_label_norm = normalize_google_binding_text(final_place_label)
    page_name_norm = normalize_google_binding_text(name)

    matched_on: list[str] = []
    mismatch_reasons: list[str] = []
    drift_indicators: list[str] = []

    token_matched = False
    label_matched = bool(
        discovery_label_norm
        and final_label_norm
        and discovery_label_norm == final_label_norm
    )

    if discovery_place_key and final_place_key:
        if discovery_place_key == final_place_key:
            matched_on.append("canonical_place_key")
        elif token_matched or label_matched:
            drift_indicators.append("canonical_place_key_drift")
        else:
            mismatch_reasons.append("canonical_place_key_mismatch")

    if discovery_place_token and final_place_token:
        if discovery_place_token == final_place_token:
            token_matched = True
            matched_on.append("validated_google_place_id")
        else:
            mismatch_reasons.append("validated_google_place_id_mismatch")

    if label_matched:
        matched_on.append("url_place_label")
    elif discovery_label_norm and final_label_norm:
        drift_indicators.append("url_place_label_drift")

    if (
        discovery_place_key
        and final_place_key
        and discovery_place_key != final_place_key
        and (token_matched or label_matched)
        and "canonical_place_key_drift" not in drift_indicators
    ):
        drift_indicators.append("canonical_place_key_drift")
        mismatch_reasons = [
            reason
            for reason in mismatch_reasons
            if reason != "canonical_place_key_mismatch"
        ]

    if (
        discovery_label_norm
        and page_name_norm
        and discovery_label_norm != page_name_norm
    ):
        drift_indicators.append("discovery_name_drift")

    if final_label_norm and page_name_norm and final_label_norm != page_name_norm:
        drift_indicators.append("final_name_drift")

    if mismatch_reasons:
        binding_status = "mismatch"
    elif matched_on:
        binding_status = "matched"
    elif (
        discovery_label_norm
        and page_name_norm
        and discovery_label_norm == page_name_norm
    ):
        binding_status = "matched"
        matched_on.append("discovery_place_label")
    else:
        binding_status = "unverified"

    reason_code = BINDING_REASON_CODES[binding_status]
    reason_codes = [reason_code]
    for mismatch_reason in mismatch_reasons:
        if mismatch_reason not in reason_codes:
            reason_codes.append(mismatch_reason)

    identity_context = {
        "discovery_url": str(discovery_url or "").strip(),
        "final_page_url": str(final_page_url or "").strip(),
        "canonical_place_url": str(canonical_place_url or "").strip(),
        "discovery_place_key": discovery_place_key,
        "final_place_key": final_place_key,
        "discovery_place_token": discovery_place_token,
        "final_place_token": final_place_token,
        "discovery_place_label": discovery_place_label,
        "final_place_label": final_place_label,
        "page_name": str(name or "").strip(),
        "page_address": str(address or "").strip(),
    }

    diagnostics: dict[str, Any] = {
        "status": binding_status,
        "reason_code": reason_code,
        "reason_codes": reason_codes,
        "matched_on": matched_on,
        "mismatch_reasons": mismatch_reasons,
        "drift_indicators": drift_indicators,
        "discovery_place_key": discovery_place_key,
        "final_place_key": final_place_key,
        "discovery_place_token": discovery_place_token,
        "final_place_token": final_place_token,
        "discovery_place_label": discovery_place_label,
        "final_place_label": final_place_label,
        "page_name": str(name or "").strip(),
        "page_address": str(address or "").strip(),
        "identity_context": identity_context,
    }
    return diagnostics

def build_google_maps_identity_contract(
    *,
    discovery_url: str,
    final_page_url: str,
    share_url: str = "",
    place_token: str = "",
) -> dict[str, str]:
    discovery_url = normalize_place_url(discovery_url)
    final_page_url = str(final_page_url or "").strip()
    canonical_place_url = normalize_place_url(final_page_url) or discovery_url
    canonical_place_key = build_google_maps_exact_page_key(canonical_place_url)
    validated_google_place_id = extract_validated_google_place_token(
        final_page_url,
        canonical_place_url,
        place_token,
        discovery_url,
    )

    return {
        "discovery_url": discovery_url,
        "final_page_url": final_page_url,
        "canonical_place_url": canonical_place_url,
        "canonical_place_key": canonical_place_key,
        "validated_google_place_id": validated_google_place_id,
        "share_url": str(share_url or "").strip(),
    }

def resolve_google_source_record_id(details: dict[str, Any]) -> str:
    for field_name in (
        "canonical_place_key",
        "canonical_place_url",
        "final_page_url",
        "discovery_url",
        "maps_link",
        "gmaps_url",
    ):
        value = str(details.get(field_name) or "").strip()
        if value:
            return value
    return ""

def stabilize_google_accepted_record_identity(
    details: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    stabilized = dict(details)

    canonical_place_key = normalize_google_maps_exact_page_key(
        stabilized.get("canonical_place_key")
    )
    canonical_place_url = normalize_place_url(
        stabilized.get("canonical_place_url") or ""
    )

    if not canonical_place_url:
        for field_name in (
            "maps_link",
            "final_page_url",
            "discovery_url",
            "gmaps_url",
        ):
            canonical_place_url = normalize_place_url(stabilized.get(field_name) or "")
            if canonical_place_url:
                break

    if not canonical_place_key:
        canonical_place_key = build_google_maps_exact_page_key(canonical_place_url)

    if canonical_place_key and not canonical_place_url:
        canonical_place_url = f"https://www.google.com{canonical_place_key}"

    if canonical_place_key:
        stabilized["canonical_place_key"] = canonical_place_key
    if canonical_place_url:
        stabilized["canonical_place_url"] = canonical_place_url
        stabilized["maps_link"] = canonical_place_url

    return stabilized, canonical_place_key, resolve_google_source_record_id(stabilized)

def validate_google_maps_identity_contract(details: dict[str, Any]) -> dict[str, str]:
    identity = {
        "discovery_url": str(details.get("discovery_url") or "").strip(),
        "final_page_url": str(details.get("final_page_url") or "").strip(),
        "canonical_place_url": str(details.get("canonical_place_url") or "").strip(),
        "canonical_place_key": str(details.get("canonical_place_key") or "").strip(),
        "validated_google_place_id": str(
            details.get("validated_google_place_id") or ""
        ).strip(),
    }
    if not any(identity.values()):
        raise RuntimeError(
            "Google Maps raw identity contract is missing all identity fields"
        )
    if identity["canonical_place_url"] and not identity["canonical_place_key"]:
        raise RuntimeError(
            "Google Maps canonical_place_key is required when canonical_place_url exists"
        )
    if identity["canonical_place_key"] and not identity[
        "canonical_place_key"
    ].startswith("/maps/place/"):
        raise RuntimeError(
            "Google Maps canonical_place_key must be a normalized /maps/place/ path"
        )
    expected_place_token = extract_validated_google_place_token(
        identity["final_page_url"],
        identity["canonical_place_url"],
        identity["discovery_url"],
    )
    if (
        expected_place_token
        and identity["validated_google_place_id"] != expected_place_token
    ):
        raise RuntimeError(
            "Google Maps validated_google_place_id must match the validated token recoverable from identity URLs"
        )
    if identity["validated_google_place_id"] and not validate_google_place_token(
        identity["validated_google_place_id"]
    ):
        raise RuntimeError(
            "Google Maps validated_google_place_id must contain a validated place token"
        )
    binding_status = str(details.get("binding_status") or "").strip()
    if binding_status not in BINDING_STATUS_VALUES:
        raise RuntimeError(
            "Google Maps binding_status must be matched, mismatch, or unverified"
        )
    binding_diagnostics = details.get("binding_diagnostics")
    if not isinstance(binding_diagnostics, dict):
        raise RuntimeError("Google Maps binding_diagnostics must be a dictionary")
    if str(binding_diagnostics.get("status") or "").strip() != binding_status:
        raise RuntimeError(
            "Google Maps binding diagnostics status must match binding_status"
        )
    reason_code = str(binding_diagnostics.get("reason_code") or "").strip()
    if reason_code != BINDING_REASON_CODES[binding_status]:
        raise RuntimeError(
            "Google Maps binding diagnostics reason_code must match binding_status"
        )
    reason_codes = binding_diagnostics.get("reason_codes")
    if (
        not isinstance(reason_codes, list)
        or not reason_codes
        or not all(isinstance(value, str) and value.strip() for value in reason_codes)
    ):
        raise RuntimeError(
            "Google Maps binding diagnostics reason_codes must be a non-empty string list"
        )
    if reason_code not in reason_codes:
        raise RuntimeError(
            "Google Maps binding diagnostics reason_codes must include reason_code"
        )
    if binding_status == "mismatch" and "binding_mismatch" not in reason_codes:
        raise RuntimeError(
            "Google Maps mismatch diagnostics must include binding_mismatch reason code"
        )
    identity_context = binding_diagnostics.get("identity_context")
    if not isinstance(identity_context, dict):
        raise RuntimeError(
            "Google Maps binding diagnostics identity_context must be a dictionary"
        )
    for field_name in ("discovery_url", "final_page_url", "canonical_place_url"):
        if str(identity_context.get(field_name) or "").strip() != identity[field_name]:
            raise RuntimeError(
                f"Google Maps binding diagnostics identity_context.{field_name} must match the identity contract"
            )
    return identity
