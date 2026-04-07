from __future__ import annotations

import json
import logging
import os
from typing import Any

from .constants import (
    CROSS_RUN_SAME_DATE_SKIP_REASON_CODE,
    DUPLICATE_EXACT_PAGE_REASON_CODE,
    PERSISTED_RAW_EXCLUDED_FIELDS,
    RAW_RECORD_METADATA_FIELDS,
    SOURCE_PLATFORM,
)
from .identity import (
    _index_google_record_context,
    _resolve_google_identity_match,
    build_google_maps_record_identity_snapshot,
    build_google_maps_exact_page_key,
    build_google_maps_history_record_context,
    extract_google_maps_place_label,
    extract_validated_google_place_token,
    normalize_place_url,
    normalize_google_binding_text,
    stabilize_google_accepted_record_identity,
)

logger = logging.getLogger(__name__)

def _resolve_project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _resolve_normalized_history_dir(output_dir: str) -> str:
    normalized_suffix = os.path.join("data", "normalized")
    stripped_output_dir = str(output_dir or "").strip()
    if not stripped_output_dir:
        return os.path.join(_resolve_project_root(), normalized_suffix)

    if os.path.isabs(stripped_output_dir):
        parent = os.path.dirname(os.path.normpath(stripped_output_dir))
        return os.path.join(parent, "normalized")

    normalized_dir = stripped_output_dir.replace("\\", "/").strip("/")
    if normalized_dir == "data/raw":
        return os.path.join(_resolve_project_root(), normalized_suffix)
    if normalized_dir.endswith("/raw"):
        return os.path.join(
            _resolve_project_root(),
            normalized_dir[: -len("raw")] + "normalized",
        )
    return os.path.join(_resolve_project_root(), normalized_suffix)

def load_all_historical_normalized_google_cafe_identities(
    normalized_history_dir: str,
) -> dict[str, Any]:
    prior_record_context_by_place_id: dict[str, dict[str, str]] = {}
    prior_record_context_by_exact_page_key: dict[str, dict[str, str]] = {}
    prior_record_context_by_gmaps_url: dict[str, dict[str, str]] = {}
    prior_record_context_by_name: dict[str, dict[str, str]] = {}
    diagnostics: dict[str, int] = {
        "normalized_dates_scanned": 0,
        "normalized_files_scanned": 0,
        "normalized_records_scanned": 0,
        "normalized_identity_records_loaded": 0,
        "normalized_invalid_records_skipped": 0,
        "normalized_validated_google_place_ids": 0,
        "normalized_canonical_place_keys": 0,
        "normalized_gmaps_urls": 0,
        "normalized_names": 0,
    }

    if not normalized_history_dir or not os.path.isdir(normalized_history_dir):
        return {
            "by_place_id": prior_record_context_by_place_id,
            "by_exact_page_key": prior_record_context_by_exact_page_key,
            "by_gmaps_url": prior_record_context_by_gmaps_url,
            "by_name": prior_record_context_by_name,
            "diagnostics": diagnostics,
        }

    for date_name in sorted(os.listdir(normalized_history_dir)):
        date_dir = os.path.join(normalized_history_dir, date_name)
        cafes_path = os.path.join(date_dir, "cafes.json")
        if not os.path.isdir(date_dir) or not os.path.isfile(cafes_path):
            continue
        diagnostics["normalized_dates_scanned"] += 1
        diagnostics["normalized_files_scanned"] += 1

        try:
            with open(cafes_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning(
                "Skipping historical normalized cafe identity load for %s: %s",
                cafes_path,
                exc,
            )
            continue

        if not isinstance(payload, list):
            logger.warning(
                "Skipping historical normalized cafe identity load for %s: expected list payload",
                cafes_path,
            )
            continue

        for cafe in payload:
            diagnostics["normalized_records_scanned"] += 1
            if not isinstance(cafe, dict):
                diagnostics["normalized_invalid_records_skipped"] += 1
                continue

            record_context = build_google_maps_history_record_context(
                name=cafe.get("name"),
                address=cafe.get("address"),
                gmaps_url=cafe.get("gmaps_url"),
                source_record_id=cafe.get("gmaps_url")
                or cafe.get("slug")
                or cafe.get("name"),
                slug=cafe.get("slug"),
            )
            if _index_google_record_context(
                record_context=record_context,
                by_place_id=prior_record_context_by_place_id,
                by_exact_page_key=prior_record_context_by_exact_page_key,
                by_gmaps_url=prior_record_context_by_gmaps_url,
                by_name=prior_record_context_by_name,
                diagnostics=diagnostics,
                counter_keys={
                    "place_id": "normalized_validated_google_place_ids",
                    "exact_page_key": "normalized_canonical_place_keys",
                    "gmaps_url": "normalized_gmaps_urls",
                    "name": "normalized_names",
                },
            ):
                diagnostics["normalized_identity_records_loaded"] += 1

    return {
        "by_place_id": prior_record_context_by_place_id,
        "by_exact_page_key": prior_record_context_by_exact_page_key,
        "by_gmaps_url": prior_record_context_by_gmaps_url,
        "by_name": prior_record_context_by_name,
        "diagnostics": diagnostics,
    }

def _prune_nested_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, nested_value in value.items():
            pruned = _prune_nested_empty_values(nested_value)
            if pruned in (None, "", [], {}):
                continue
            cleaned[key] = pruned
        return cleaned
    if isinstance(value, list):
        cleaned_list = [
            pruned
            for item in value
            if (pruned := _prune_nested_empty_values(item)) not in (None, "", [], {})
        ]
        return cleaned_list
    return value

def build_persisted_google_raw_payload(details: dict[str, Any]) -> dict[str, Any]:
    persisted = {
        key: value
        for key, value in dict(details or {}).items()
        if key not in PERSISTED_RAW_EXCLUDED_FIELDS
    }
    return _prune_nested_empty_values(persisted)

def extract_google_raw_record_details(raw_record: dict[str, Any]) -> dict[str, Any]:
    nested_details = raw_record.get("raw")
    if isinstance(nested_details, dict) and nested_details:
        return dict(nested_details)
    return {
        key: value
        for key, value in raw_record.items()
        if key not in RAW_RECORD_METADATA_FIELDS
    }

def build_google_maps_duplicate_skip_diagnostic(
    *,
    accepted_exact_page_key: str,
    skipped_record: dict[str, str],
    kept_record: dict[str, str] | None = None,
    matched_on: str = "exact_page_key",
    matched_value: str = "",
) -> dict[str, Any]:
    return {
        "reason_code": DUPLICATE_EXACT_PAGE_REASON_CODE,
        "status": "skipped",
        "matched_on": str(matched_on or "exact_page_key").strip(),
        "matched_value": str(matched_value or accepted_exact_page_key or "").strip(),
        "exact_page_key": str(accepted_exact_page_key or "").strip(),
        "identity_context": dict(skipped_record or {}),
        "kept_record": dict(kept_record or {}),
    }

def append_google_maps_diagnostic_example(
    examples: list[dict[str, Any]],
    diagnostic: dict[str, Any],
    *,
    limit: int = 10,
) -> None:
    if len(examples) < limit:
        examples.append(diagnostic)

def build_google_maps_cross_run_skip_diagnostic(
    *,
    skipped_place_url: str,
    skipped_place_id: str = "",
    matched_on: str,
    matched_value: str,
    kept_record: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "reason_code": CROSS_RUN_SAME_DATE_SKIP_REASON_CODE,
        "status": "skipped",
        "matched_on": str(matched_on or "").strip(),
        "matched_value": str(matched_value or "").strip(),
        "identity_context": {
            "place_url": str(skipped_place_url or "").strip(),
            "canonical_place_key": build_google_maps_exact_page_key(skipped_place_url),
            "validated_google_place_id": extract_validated_google_place_token(
                skipped_place_id,
                skipped_place_url,
            ),
        },
        "kept_record": dict(kept_record or {}),
    }

def load_same_date_successful_google_raw_identities(
    target_dir: str,
    *,
    current_output_path: str = "",
    normalized_history_dir: str = "",
) -> dict[str, Any]:
    prior_record_context_by_place_id: dict[str, dict[str, str]] = {}
    prior_record_context_by_exact_page_key: dict[str, dict[str, str]] = {}
    prior_record_context_by_gmaps_url: dict[str, dict[str, str]] = {}
    prior_record_context_by_name: dict[str, dict[str, str]] = {}
    diagnostics: dict[str, Any] = {
        "raw_files_scanned": 0,
        "raw_records_scanned": 0,
        "identity_records_loaded": 0,
        "invalid_records_skipped": 0,
        "validated_google_place_ids": 0,
        "canonical_place_keys": 0,
        "gmaps_urls": 0,
        "names": 0,
    }

    normalized_identities = load_all_historical_normalized_google_cafe_identities(
        normalized_history_dir
    )
    prior_record_context_by_place_id.update(normalized_identities["by_place_id"])
    prior_record_context_by_exact_page_key.update(
        normalized_identities["by_exact_page_key"]
    )
    prior_record_context_by_gmaps_url.update(normalized_identities["by_gmaps_url"])
    prior_record_context_by_name.update(normalized_identities["by_name"])
    diagnostics.update(normalized_identities["diagnostics"])

    if not target_dir or not os.path.isdir(target_dir):
        return {
            "by_place_id": prior_record_context_by_place_id,
            "by_exact_page_key": prior_record_context_by_exact_page_key,
            "by_gmaps_url": prior_record_context_by_gmaps_url,
            "by_name": prior_record_context_by_name,
            "diagnostics": diagnostics,
        }

    current_output_name = os.path.basename(str(current_output_path or "").strip())

    for filename in sorted(os.listdir(target_dir)):
        if not filename.startswith(f"{SOURCE_PLATFORM}_raw_") or not filename.endswith(
            ".json"
        ):
            continue
        if current_output_name and filename == current_output_name:
            continue

        file_path = os.path.join(target_dir, filename)
        diagnostics["raw_files_scanned"] += 1

        try:
            with open(file_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning(
                "Skipping same-date Google Maps raw identity load for %s: %s",
                file_path,
                exc,
            )
            continue

        if not isinstance(payload, list):
            logger.warning(
                "Skipping same-date Google Maps raw identity load for %s: expected list payload",
                file_path,
            )
            continue

        for raw_record in payload:
            diagnostics["raw_records_scanned"] += 1
            if not isinstance(raw_record, dict):
                diagnostics["invalid_records_skipped"] += 1
                continue

            details = extract_google_raw_record_details(raw_record)
            if not isinstance(details, dict) or not details:
                diagnostics["invalid_records_skipped"] += 1
                continue

            stabilized_details, canonical_place_key, source_record_id = (
                stabilize_google_accepted_record_identity(details)
            )
            validated_google_place_id = extract_validated_google_place_token(
                stabilized_details.get("validated_google_place_id"),
                stabilized_details.get("canonical_place_url"),
                stabilized_details.get("final_page_url"),
                stabilized_details.get("discovery_url"),
                stabilized_details.get("maps_link"),
                stabilized_details.get("gmaps_url"),
                stabilized_details.get("google_place_id"),
            )
            if validated_google_place_id:
                stabilized_details["validated_google_place_id"] = (
                    validated_google_place_id
                )

            record_context = build_google_maps_record_identity_snapshot(
                stabilized_details,
                place_url=str(
                    stabilized_details.get("canonical_place_url")
                    or stabilized_details.get("maps_link")
                    or stabilized_details.get("gmaps_url")
                    or ""
                ).strip(),
                source_record_id=str(
                    source_record_id or raw_record.get("source_record_id") or ""
                ).strip(),
            )

            record_context["gmaps_url"] = normalize_place_url(
                stabilized_details.get("gmaps_url")
                or stabilized_details.get("maps_link")
                or stabilized_details.get("canonical_place_url")
                or ""
            )
            record_context["normalized_name"] = normalize_google_binding_text(
                stabilized_details.get("name")
            )

            if _index_google_record_context(
                record_context=record_context,
                by_place_id=prior_record_context_by_place_id,
                by_exact_page_key=prior_record_context_by_exact_page_key,
                by_gmaps_url=prior_record_context_by_gmaps_url,
                by_name=prior_record_context_by_name,
                diagnostics=diagnostics,
                counter_keys={
                    "place_id": "validated_google_place_ids",
                    "exact_page_key": "canonical_place_keys",
                    "gmaps_url": "gmaps_urls",
                    "name": "names",
                },
                include_normalized_name_in_name_context=True,
            ):
                diagnostics["identity_records_loaded"] += 1

    return {
        "by_place_id": prior_record_context_by_place_id,
        "by_exact_page_key": prior_record_context_by_exact_page_key,
        "by_gmaps_url": prior_record_context_by_gmaps_url,
        "by_name": prior_record_context_by_name,
        "diagnostics": diagnostics,
    }

def apply_same_date_cross_run_google_dedup(
    discovered_places: list[dict[str, str]],
    *,
    target_dir: str,
    current_output_path: str = "",
    normalized_history_dir: str = "",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    prior_identities = load_same_date_successful_google_raw_identities(
        target_dir,
        current_output_path=current_output_path,
        normalized_history_dir=normalized_history_dir,
    )
    prior_by_place_id = prior_identities["by_place_id"]
    prior_by_exact_page_key = prior_identities["by_exact_page_key"]
    prior_by_gmaps_url = prior_identities["by_gmaps_url"]
    prior_by_name = prior_identities["by_name"]
    filtered_places: list[dict[str, str]] = []
    skip_diagnostics: dict[str, Any] = {
        "reason_code": CROSS_RUN_SAME_DATE_SKIP_REASON_CODE,
        "count": 0,
        "examples": [],
        "load_diagnostics": dict(prior_identities["diagnostics"]),
    }

    for place in discovered_places:
        if not isinstance(place, dict):
            continue

        place_url = normalize_place_url(str(place.get("place_url") or ""))
        place_id = str(place.get("place_id") or "").strip()
        validated_google_place_id = extract_validated_google_place_token(
            place_id,
            place_url,
        )
        canonical_place_key = build_google_maps_exact_page_key(place_url)
        normalized_place_name = normalize_google_binding_text(
            extract_google_maps_place_label(place_url)
        )

        kept_record, matched_on, matched_value = _resolve_google_identity_match(
            validated_google_place_id=validated_google_place_id,
            canonical_place_key=canonical_place_key,
            gmaps_url=place_url,
            normalized_name=normalized_place_name,
            by_place_id=prior_by_place_id,
            by_exact_page_key=prior_by_exact_page_key,
            by_gmaps_url=prior_by_gmaps_url,
            by_name=prior_by_name,
            exact_page_match_name="canonical_place_key",
        )

        if kept_record:
            skip_diagnostics["count"] += 1
            append_google_maps_diagnostic_example(
                skip_diagnostics["examples"],
                build_google_maps_cross_run_skip_diagnostic(
                    skipped_place_url=place_url,
                    skipped_place_id=place_id,
                    matched_on=matched_on,
                    matched_value=matched_value,
                    kept_record=kept_record,
                ),
            )
            continue

        filtered_places.append(
            {
                "place_url": place_url,
                "place_id": place_id,
            }
        )

    skip_diagnostics["retained_candidates"] = len(filtered_places)
    return filtered_places, skip_diagnostics
