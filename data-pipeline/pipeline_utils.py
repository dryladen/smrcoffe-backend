"""Shared utility helpers for the cafe scraping data pipeline."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

try:
    import orjson  # type: ignore[reportMissingImports]

    _HAS_ORJSON = True
except ImportError:
    import json as orjson

    _HAS_ORJSON = False

__all__ = [
    "extract_base_url",
    "generate_run_id",
    "generate_slug",
    "get_errors_filepath",
    "get_normalized_dir",
    "get_raw_dir",
    "get_raw_filepath",
    "get_run_date",
    "get_timestamp",
    "load_json",
    "normalize_phone",
    "normalize_text",
    "normalize_url",
    "quarantine_record",
    "save_json",
    "write_quarantine",
]

_QUARANTINE_FIELDS = [
    "reason_code",
    "source_platform",
    "run_id",
    "source_record_id",
    "raw_data",
    "validation_errors",
]

_ORJSON_INDENT_2 = getattr(orjson, "OPT_INDENT_2", 0)


def generate_slug(name: str, location: str = "") -> str:
    """Generate a URL-safe, lowercase slug from cafe name + optional location."""
    parts = [part.replace("&", "and") for part in (name, location) if part and part.strip()]
    slug = "-".join(parts).lower()
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def save_json(filepath: str, data: list[dict] | dict, indent: int = 2) -> None:
    """Save data as JSON to filepath, creating parent directories as needed."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(filepath, "wb") as f:
        if _HAS_ORJSON and indent == 2:
            option = _ORJSON_INDENT_2
            payload = cast(bytes, cast(Any, orjson).dumps(data, option=option))
        else:
            payload = json.dumps(data, indent=indent, ensure_ascii=False).encode("utf-8")
        f.write(payload)


def load_json(filepath: str) -> list[dict] | dict:
    """Load JSON from filepath, returning an empty list on missing/invalid data."""
    if not os.path.exists(filepath):
        return []

    try:
        with open(filepath, "rb") as f:
            raw = f.read()
            if _HAS_ORJSON:
                data = cast(Any, orjson).loads(raw)
            else:
                data = cast(Any, orjson).loads(raw.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []

    if isinstance(data, (list, dict)):
        return data
    return []


def get_run_date() -> str:
    """Return today's date as YYYY-MM-DD string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_timestamp() -> str:
    """Return current ISO-8601 timestamp with timezone."""
    return datetime.now(timezone.utc).isoformat()


def generate_run_id() -> str:
    """Generate a unique run ID: 'run_YYYYMMDD_HHMMSS_microseconds'."""
    return datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S_%f")


def get_raw_dir(base_dir: str, run_date: str, source: str) -> str:
    """Return path: data/raw/<run_date>/<source>/. Create if not exists."""
    path = os.path.join(base_dir, "data", "raw", run_date, source)
    os.makedirs(path, exist_ok=True)
    return path


def get_normalized_dir(base_dir: str, run_date: str) -> str:
    """Return path: data/normalized/<run_date>/. Create if not exists."""
    path = os.path.join(base_dir, "data", "normalized", run_date)
    os.makedirs(path, exist_ok=True)
    return path


def get_raw_filepath(base_dir: str, run_date: str, source: str, run_timestamp: str) -> str:
    """Return path: data/raw/<run_date>/<source>/<source>_raw_<run_timestamp>.json"""
    directory = get_raw_dir(base_dir, run_date, source)
    return os.path.join(directory, f"{source}_raw_{run_timestamp}.json")


def get_errors_filepath(base_dir: str, run_date: str, source: str, run_id: str) -> str:
    """Return path: data/raw/<run_date>/<source>/errors_<run_id>.json"""
    directory = get_raw_dir(base_dir, run_date, source)
    return os.path.join(directory, f"errors_{run_id}.json")


def write_quarantine(quarantine_path: str, records: list[dict]) -> None:
    """Append quarantine records to a CSV file, creating the header when needed."""
    if not records:
        return

    directory = os.path.dirname(quarantine_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    file_exists = os.path.exists(quarantine_path) and os.path.getsize(quarantine_path) > 0
    with open(quarantine_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_QUARANTINE_FIELDS)
        if not file_exists:
            writer.writeheader()

        for record in records:
            row = {
                "reason_code": record.get("reason_code", ""),
                "source_platform": record.get("source_platform", ""),
                "run_id": record.get("run_id", ""),
                "source_record_id": record.get("source_record_id", ""),
                "raw_data": json.dumps(record.get("raw_data", {}), ensure_ascii=False),
                "validation_errors": json.dumps(
                    record.get("validation_errors", []), ensure_ascii=False
                ),
            }
            writer.writerow(row)


def quarantine_record(
    reason_code: str,
    source_platform: str,
    raw_data: dict,
    run_id: str | None = None,
    source_record_id: str | None = None,
    validation_errors: list[str] | None = None,
) -> dict:
    """Create a quarantine record dict with the given fields."""
    return {
        "reason_code": reason_code,
        "source_platform": source_platform,
        "run_id": run_id,
        "source_record_id": source_record_id,
        "raw_data": raw_data,
        "validation_errors": validation_errors or [],
    }


def normalize_url(url: str) -> str:
    """Strip tracking params, trailing slashes, and surrounding whitespace."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        return cleaned.rstrip("/")

    filtered_query = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if key.startswith("utm_") or key in {"fbclid", "gclid"}:
            continue
        for value in values:
            filtered_query.append((key, value))

    normalized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            parsed.params,
            urlencode(filtered_query, doseq=True),
            "",
        )
    )
    return normalized.rstrip("/")


def extract_base_url(full_url: str) -> str:
    """Extract scheme + netloc from a URL, including Google redirect unwrapping."""
    if not full_url:
        return ""

    try:
        if full_url.startswith("/url?"):
            full_url = "https://www.google.com" + full_url

        parsed = urlparse(full_url)

        if parsed.netloc in ("www.google.com", "google.com") and parsed.path == "/url":
            query = parse_qs(parsed.query)
            real_url = query.get("q", [""])[0]
            if real_url:
                return extract_base_url(unquote(real_url))
            return ""

        if not parsed.scheme.startswith("http") or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""


def normalize_text(text: str) -> str:
    """Strip whitespace and collapse internal whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_phone(phone: str) -> str:
    """Strip a leading 'Phone:' label and remove whitespace from the number."""
    cleaned = re.sub(r"^phone:\s*", "", (phone or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", "", cleaned)
