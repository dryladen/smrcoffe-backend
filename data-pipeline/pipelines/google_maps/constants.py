import re

SOURCE_PLATFORM = "google_maps"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PLACE_HREF_RE = re.compile(r"(?:https://www\.google\.com)?(/maps/place/[^\"'<>\s\\]+)")
PATH_COORD_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
DAY_ORDER = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
DAY_NAME_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.IGNORECASE
)
HOURS_NOISE_RE = re.compile(
    r"^(hours|open website|open menu link|see more hours|copy open hours|show open hours for the week|suggest an edit to open hours)$",
    re.IGNORECASE,
)
TIME_TOKEN_RE = re.compile(
    r"(?:\b\d{1,2}(?::\d{2})?\s*[ap]m\b|open\s+24\s+hours|\bclosed\b)",
    re.IGNORECASE,
)
GOOGLE_HOST_RE = re.compile(r"(?:^|\.)google\.[A-Za-z.]+$")
EMBEDDED_ABSOLUTE_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
EMBEDDED_PLACE_PATH_RE = re.compile(r"/maps/place/[^\s\"'<>]+", re.IGNORECASE)
EMBEDDED_URL_PARAM_NAMES = ("q", "url", "u", "dest", "destination", "href")
PLACE_ID_PATTERNS = (
    re.compile(r"[?&](?:ftid|cid|place_id)=([^&#]+)"),
    re.compile(r"!1s([^!/?&#]+)"),
    re.compile(r"\b(ChI[A-Za-z0-9_-]+)\b"),
)
VALIDATED_PLACE_ID_PATTERNS = (
    re.compile(r"^ChI[A-Za-z0-9_-]+$"),
    re.compile(r"^[0-9]+$"),
    re.compile(r"^(?:0x)?[0-9A-Fa-f]+:(?:0x)?[0-9A-Fa-f]+$"),
)
BINDING_STATUS_VALUES = {"matched", "mismatch", "unverified"}
BINDING_REASON_CODES = {
    "matched": "binding_matched",
    "mismatch": "binding_mismatch",
    "unverified": "binding_unverified",
}
DUPLICATE_EXACT_PAGE_REASON_CODE = "duplicate_exact_page"
CROSS_RUN_SAME_DATE_SKIP_REASON_CODE = "duplicate_previous_run_same_date"
SESSION_FAILURE_WINDOW = 5
SESSION_FAILURE_THRESHOLD = 3
SESSION_MIN_SCROLLS = 6
SESSION_MAX_SCROLLS = 30
ABOUT_ATTRIBUTE_KEYS = (
    "accessibility",
    "service_options",
    "highlights",
    "popular_for",
    "offerings",
    "dining_options",
    "amenities",
    "atmosphere",
    "crowd",
    "planning",
    "recycling",
    "children",
    "parking",
    "health_and_safety",
    "payments",
)
PERSISTED_RAW_EXCLUDED_FIELDS = {
    "google_place_id",
    "binding_status",
    "binding_diagnostics",
    "discovery_url",
    "final_page_url",
    "canonical_place_url",
    "canonical_place_key",
    "validated_google_place_id",
    "share_url",
}
RAW_RECORD_METADATA_FIELDS = {
    "source_platform",
    "run_id",
    "scraped_at",
    "query",
    "source_record_id",
}

__all__ = [
    "SOURCE_PLATFORM",
    "CHROME_USER_AGENT",
    "PLACE_HREF_RE",
    "PATH_COORD_RE",
    "DAY_ORDER",
    "DAY_NAME_RE",
    "HOURS_NOISE_RE",
    "TIME_TOKEN_RE",
    "GOOGLE_HOST_RE",
    "EMBEDDED_ABSOLUTE_URL_RE",
    "EMBEDDED_PLACE_PATH_RE",
    "EMBEDDED_URL_PARAM_NAMES",
    "PLACE_ID_PATTERNS",
    "VALIDATED_PLACE_ID_PATTERNS",
    "BINDING_STATUS_VALUES",
    "BINDING_REASON_CODES",
    "DUPLICATE_EXACT_PAGE_REASON_CODE",
    "CROSS_RUN_SAME_DATE_SKIP_REASON_CODE",
    "SESSION_FAILURE_WINDOW",
    "SESSION_FAILURE_THRESHOLD",
    "SESSION_MIN_SCROLLS",
    "SESSION_MAX_SCROLLS",
    "ABOUT_ATTRIBUTE_KEYS",
    "PERSISTED_RAW_EXCLUDED_FIELDS",
    "RAW_RECORD_METADATA_FIELDS",
]
