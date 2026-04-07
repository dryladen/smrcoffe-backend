"""Pydantic v2 schemas for the cafe-scraper data pipeline.

These standalone contracts align the scraping and extraction stages with the
normalized table targets documented in ``docs/database_schema_architecture.md``
and the transform rules in ``docs/data-extraction.md``. They intentionally
model JSON payloads only and do not include any database, ORM, or Directus
integration concerns.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlparse

try:
    orjson = importlib.import_module("orjson")
except ModuleNotFoundError:
    orjson = None

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SLUG_INVALID_CHARS_RE = re.compile(r"[^a-z0-9\s-]")
_SLUG_SEPARATOR_RE = re.compile(r"[-\s]+")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_GOOGLE_PLACE_ID_RE = re.compile(r"^(?:ChI[A-Za-z0-9_-]+|[0-9]+|(?:0x)?[0-9A-Fa-f]+:(?:0x)?[0-9A-Fa-f]+)$")

_SOURCE_PLATFORMS = {"google_maps", "tiktok"}
_INFLUENCER_PLATFORMS = {"instagram", "tiktok"}
_RUN_STATUSES = {"running", "completed", "failed"}


def _normalize_slug(value: str) -> str:
    value = value.strip().lower()
    value = _SLUG_INVALID_CHARS_RE.sub("", value)
    value = _SLUG_SEPARATOR_RE.sub("-", value)
    value = value.strip("-")
    if not value:
        raise ValueError("slug must contain at least one URL-safe character")
    return value


def _validate_url(value: str, *, field_name: str, allow_empty: bool = True) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be a valid http(s) URL")
    return value


def _validate_google_place_token(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not _GOOGLE_PLACE_ID_RE.fullmatch(value):
        raise ValueError("google place token must be validated before storage")
    return value


def _validate_google_maps_identity_payload(
    raw_payload: dict[str, Any],
    *,
    source_record_id: str | None,
) -> None:
    for field_name in (
        "gmaps_url",
        "maps_link",
        "discovery_url",
        "final_page_url",
        "canonical_place_url",
        "share_url",
    ):
        value = str(raw_payload.get(field_name) or "").strip()
        if value:
            _validate_url(value, field_name=field_name, allow_empty=True)

    canonical_place_key = str(raw_payload.get("canonical_place_key") or "").strip()
    canonical_place_url = str(raw_payload.get("canonical_place_url") or "").strip()
    if canonical_place_key and not canonical_place_key.startswith("/maps/place/"):
        raise ValueError("canonical_place_key must be a normalized /maps/place/ path")
    if canonical_place_key and canonical_place_url:
        expected_key = urlparse(canonical_place_url).path or ""
        if canonical_place_key != expected_key:
            raise ValueError("canonical_place_key must match canonical_place_url path")

    validated_google_place_id = _validate_google_place_token(
        str(raw_payload.get("validated_google_place_id") or "")
    )
    url_derived_place_token = ""
    for field_name in ("final_page_url", "canonical_place_url", "discovery_url"):
        value = str(raw_payload.get(field_name) or "").strip()
        match = re.search(r"!1s([^!/?&#]+)", value)
        if match and _GOOGLE_PLACE_ID_RE.fullmatch(match.group(1).strip()):
            url_derived_place_token = match.group(1).strip()
            break
    if url_derived_place_token and validated_google_place_id != url_derived_place_token:
        raise ValueError(
            "validated_google_place_id must match the validated token recoverable from Google identity URLs"
        )
    google_place_id = str(raw_payload.get("google_place_id") or "").strip()
    if google_place_id and google_place_id != validated_google_place_id:
        raise ValueError("google_place_id must contain only the validated Google place token")

    source_record_id_value = str(source_record_id or "").strip()
    if google_place_id and source_record_id_value == google_place_id and not validated_google_place_id:
        raise ValueError("source_record_id must not fall back to an unvalidated google_place_id")


def _validate_iso8601(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 datetime") from exc
    return value


def _validate_date_string(value: str, *, field_name: str, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid YYYY-MM-DD date") from exc
    return value


def _validate_time_string(value: str, *, field_name: str, allow_empty: bool = True) -> str:
    value = value.strip()
    if not value and allow_empty:
        return ""
    if not _TIME_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be in HH:MM format")
    try:
        time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid HH:MM time") from exc
    return value


def _json_roundtrip(data: dict[str, Any]) -> dict[str, Any]:
    if orjson is not None:
        return orjson.loads(orjson.dumps(data))
    return json.loads(json.dumps(data))


class PipelineSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    def to_json_dict(self) -> dict[str, Any]:
        return _json_roundtrip(self.model_dump(mode="json"))


class RawRecord(PipelineSchema):
    """Envelope for raw scraped data - immutable source payload."""

    source_platform: str
    run_id: str
    scraped_at: str
    query: str
    source_record_id: str | None = None
    raw: dict[str, Any]

    @field_validator("source_platform")
    @classmethod
    def validate_source_platform(cls, value: str) -> str:
        if value not in _SOURCE_PLATFORMS:
            raise ValueError("source_platform must be one of: google_maps, tiktok")
        return value

    @field_validator("scraped_at")
    @classmethod
    def validate_scraped_at(cls, value: str) -> str:
        return _validate_iso8601(value, field_name="scraped_at")

    @model_validator(mode="after")
    def validate_google_maps_identity_contract(self) -> "RawRecord":
        if self.source_platform == "google_maps":
            _validate_google_maps_identity_payload(
                self.raw,
                source_record_id=self.source_record_id,
            )
        return self


class CafeRecord(PipelineSchema):
    """Normalized cafe record aligned to the ``cafes`` table contract."""

    slug: str
    name: str
    address: str
    district: str = ""
    gmaps_url: str = ""
    latitude: float | None = None
    longitude: float | None = None
    price_range: str = ""
    opening_time: str = ""
    closing_time: str = ""
    opening_days: list[str] = Field(default_factory=list)
    service_options: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    popular_for: list[str] = Field(default_factory=list)
    offerings: list[str] = Field(default_factory=list)
    dining_options: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    atmosphere: list[str] = Field(default_factory=list)
    crowd: list[str] = Field(default_factory=list)
    planning: list[str] = Field(default_factory=list)
    payments: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    parking: list[str] = Field(default_factory=list)
    ig_url: str = ""
    tiktok_url: str = ""
    image: list[str] = Field(default_factory=list)
    description: str = "<p>lorem ipsum</p>"
    is_featured: bool = False
    status: str = "draft"

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        return _normalize_slug(value)

    @field_validator("gmaps_url", "ig_url", "tiktok_url")
    @classmethod
    def validate_optional_urls(cls, value: str, info: Any) -> str:
        return _validate_url(value, field_name=info.field_name, allow_empty=True)

    @field_validator("opening_time", "closing_time")
    @classmethod
    def validate_times(cls, value: str, info: Any) -> str:
        return _validate_time_string(value, field_name=info.field_name, allow_empty=True)


class CafePhotoRecord(PipelineSchema):
    """Normalized photo record aligned to the ``cafe_photos`` table contract."""

    cafe_slug: str
    url: str
    caption: str = ""
    order: int = 0

    @field_validator("cafe_slug")
    @classmethod
    def validate_cafe_slug(cls, value: str) -> str:
        return _normalize_slug(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_url(value, field_name="url", allow_empty=False)


class CafeTagRecord(PipelineSchema):
    """Normalized tag record aligned to the ``cafe_tags`` table contract."""

    cafe_slug: str
    tag: str

    @field_validator("cafe_slug")
    @classmethod
    def validate_cafe_slug(cls, value: str) -> str:
        return _normalize_slug(value)


class InfluencerReviewRecord(PipelineSchema):
    """Normalized influencer review record aligned to ``influencer_reviews``."""

    cafe_slug: str
    influencer_name: str
    platform: str
    post_url: str
    embed_code: str = ""
    review_date: str = ""
    is_approved: bool = False

    @field_validator("cafe_slug")
    @classmethod
    def validate_cafe_slug(cls, value: str) -> str:
        return _normalize_slug(value)

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        if value not in _INFLUENCER_PLATFORMS:
            raise ValueError("platform must be one of: instagram, tiktok")
        return value

    @field_validator("post_url")
    @classmethod
    def validate_post_url(cls, value: str) -> str:
        return _validate_url(value, field_name="post_url", allow_empty=False)

    @field_validator("review_date")
    @classmethod
    def validate_review_date(cls, value: str) -> str:
        return _validate_date_string(value, field_name="review_date", allow_empty=True)


class QuarantineRecord(PipelineSchema):
    """Invalid record quarantined with reason."""

    reason_code: str
    source_platform: str
    run_id: str | None = None
    source_record_id: str | None = None
    raw_data: dict[str, Any]
    validation_errors: list[str] = Field(default_factory=list)

    @field_validator("source_platform")
    @classmethod
    def validate_source_platform(cls, value: str) -> str:
        if value not in _SOURCE_PLATFORMS:
            raise ValueError("source_platform must be one of: google_maps, tiktok")
        return value

    @model_validator(mode="after")
    def validate_google_maps_identity_contract(self) -> "QuarantineRecord":
        if self.source_platform == "google_maps":
            _validate_google_maps_identity_payload(
                self.raw_data,
                source_record_id=self.source_record_id,
            )
        return self


class ScrapeRun(PipelineSchema):
    """Metadata for a single pipeline run."""

    run_id: str
    run_date: str
    sources: list[str]
    queries: list[str]
    started_at: str
    completed_at: str | None = None
    total_raw_records: int = 0
    total_normalized_records: int = 0
    total_quarantined: int = 0
    status: str = "running"

    @field_validator("run_date")
    @classmethod
    def validate_run_date(cls, value: str) -> str:
        return _validate_date_string(value, field_name="run_date")

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value: str) -> str:
        return _validate_iso8601(value, field_name="started_at")

    @field_validator("completed_at")
    @classmethod
    def validate_completed_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_iso8601(value, field_name="completed_at")

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        invalid = [item for item in value if item not in _SOURCE_PLATFORMS]
        if invalid:
            raise ValueError("sources must contain only: google_maps, tiktok")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in _RUN_STATUSES:
            raise ValueError("status must be one of: running, completed, failed")
        return value


__all__ = [
    "CafePhotoRecord",
    "CafeRecord",
    "CafeTagRecord",
    "InfluencerReviewRecord",
    "QuarantineRecord",
    "RawRecord",
    "ScrapeRun",
]
