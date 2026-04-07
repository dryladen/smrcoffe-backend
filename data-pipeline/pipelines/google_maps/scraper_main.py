from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from crawl4ai import AsyncWebCrawler, BrowserConfig
from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth
except Exception:  # pragma: no cover - optional dependency shape differs by version
    Stealth = None

from .browser import (
    close_detail_pane_on_page,
    discover_place_urls,
    extract_about_attributes,
    extract_expanded_opening_hours,
    extract_hd_photos,
    extract_place_details,
    extract_short_share_url,
    match_sidebar_item_on_page,
    navigate_to_feed,
    route_nonessential_requests,
    scroll_detail_pane_for_lazy_content,
    wait_for_optional_detail_sections,
)
from .constants import (
    BINDING_REASON_CODES,
    CHROME_USER_AGENT,
    CROSS_RUN_SAME_DATE_SKIP_REASON_CODE,
    DUPLICATE_EXACT_PAGE_REASON_CODE,
    SESSION_FAILURE_THRESHOLD,
    SESSION_FAILURE_WINDOW,
    SOURCE_PLATFORM,
)
from .history import (
    _resolve_normalized_history_dir,
    append_google_maps_diagnostic_example,
    apply_same_date_cross_run_google_dedup,
    build_google_maps_duplicate_skip_diagnostic,
    build_persisted_google_raw_payload,
)
from .identity import (
    _resolve_google_identity_match,
    build_google_maps_record_identity_snapshot,
    normalize_google_binding_text,
    normalize_place_url,
    stabilize_google_accepted_record_identity,
    validate_google_maps_identity_contract,
)
from .js_wrapper import build_cookie_consent_js, build_detail_extraction_js
from .extractors import (
    expand_opening_hours_to_full_week,
    has_structured_opening_hours,
    needs_about_attributes,
    needs_short_share_url,
    normalize_opening_hours,
    sanitize_about_attributes,
    should_extract_hd_photos,
    upgrade_photo_url_to_hd,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _collect_invalid_detail_reasons(details: dict[str, Any]) -> tuple[list[str], str, str]:
    name_value = str(details.get("name") or "").strip()
    address_value = str(details.get("address") or "").strip()
    invalid_detail_reasons: list[str] = []
    if not name_value:
        invalid_detail_reasons.append("missing_required_field:name")
    elif name_value.casefold() == "results":
        invalid_detail_reasons.append("invalid_place_name:results")
    if not address_value:
        invalid_detail_reasons.append("missing_required_field:address")
    return invalid_detail_reasons, name_value, address_value


async def _extract_address_fallback_from_page(page: Any) -> str:
    extracted = await page.evaluate(
        r"""() => {
            const clean = (value) => (value || '')
                .replace(/\u00a0/g, ' ')
                .replace(/\s+/g, ' ')
                .trim();

            const stripPrefix = (value) => clean(value).replace(/^Address\s*:\s*/i, '').trim();
            const invalidRe = /^(address|directions|save|share|nearby|send to your phone)$/i;
            const candidates = [];
            const seen = new Set();

            const push = (value) => {
                const cleaned = stripPrefix(value);
                if (!cleaned || invalidRe.test(cleaned) || cleaned.length < 8) return;
                const key = cleaned.toLowerCase();
                if (seen.has(key)) return;
                seen.add(key);
                candidates.push(cleaned);
            };

            const selectors = [
                'button[data-item-id="address"]',
                '[data-item-id="address"]',
                'button[data-item-id*="address"]',
                '[data-item-id*="address"]',
                'button[data-item-id="oloc"]',
                '[data-item-id="oloc"]',
                'button[data-item-id*="oloc"]',
                '[data-item-id*="oloc"]',
                '[aria-label^="Address:" i]',
                '[aria-label*="Address" i]',
                '[aria-label^="Located in" i]',
                'button[aria-label*="address" i]',
                '[role="button"][aria-label*="address" i]',
            ];

            for (const selector of selectors) {
                for (const node of document.querySelectorAll(selector)) {
                    push(node.getAttribute ? (node.getAttribute('aria-label') || '') : '');
                    push(node.textContent || '');
                    const parent = node.closest('button, [role="button"], div, li');
                    if (parent) {
                        push(parent.getAttribute ? (parent.getAttribute('aria-label') || '') : '');
                        push(parent.textContent || '');
                    }
                }
            }

            const lineCandidates = (document.body?.innerText || '')
                .split(/\n+/)
                .map(clean)
                .filter(Boolean);
            const addressLikeRe = /(Jl\.|Jalan\b|Street\b|Road\b|Ave\b|Kec\.|Kota\b|Indonesia\b)/i;
            for (const line of lineCandidates) {
                if (line.length < 12 || line.length > 180) continue;
                if (!addressLikeRe.test(line)) continue;
                if (/^(open|closed|hours|reviews?)\b/i.test(line)) continue;
                push(line);
            }

            for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const payload = JSON.parse(script.textContent || '{}');
                    const list = Array.isArray(payload) ? payload : [payload];
                    for (const item of list) {
                        const address = item && item.address;
                        if (!address) continue;
                        if (typeof address === 'string') {
                            push(address);
                            continue;
                        }
                        if (typeof address === 'object') {
                            const parts = [
                                address.streetAddress,
                                address.addressLocality,
                                address.addressRegion,
                                address.postalCode,
                                address.addressCountry,
                            ]
                                .map(v => clean(v || ''))
                                .filter(Boolean);
                            if (parts.length) push(parts.join(', '));
                        }
                    }
                } catch (error) {}
            }

            const decodeEscaped = (value) => {
                const text = String(value || '');
                return text
                    .replace(/\\u([0-9A-Fa-f]{4})/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)))
                    .replace(/\\"/g, '"')
                    .replace(/\\\\/g, '\\');
            };
            const scriptBlob = Array.from(document.querySelectorAll('script'))
                .map(node => node.textContent || '')
                .join('\n');
            const scriptPatterns = [
                /"formattedAddress"\s*:\s*"([^"\\]*(?:\\.[^"\\]*){8,})"/g,
                /"address"\s*:\s*"([^"\\]*(?:\\.[^"\\]*){8,})"/g,
                /"addressLines"\s*:\s*\[([^\]]+)\]/g,
            ];
            for (const pattern of scriptPatterns) {
                let match;
                while ((match = pattern.exec(scriptBlob)) !== null) {
                    if (!match || !match[1]) continue;
                    const raw = decodeEscaped(match[1]);
                    if (pattern.source.includes('addressLines')) {
                        const parts = raw
                            .split(',')
                            .map(part => clean(decodeEscaped(part.replace(/^\s*"|"\s*$/g, ''))))
                            .filter(Boolean);
                        if (parts.length) push(parts.join(', '));
                    } else {
                        push(raw);
                    }
                }
            }

            if (!candidates.length) return '';
            const scored = candidates
                .map(value => ({
                    value,
                    score: (value.includes(',') ? 3 : 0) + (/[0-9]/.test(value) ? 2 : 0) + Math.min(value.length, 120) / 120,
                }))
                .sort((a, b) => b.score - a.score);
            return scored[0].value || '';
        }"""
    )
    return str(extracted or "").strip()

def build_search_url(keyword: str, city: str, locale: str = "en") -> str:
    query = f"{keyword} in {city}"
    return f"https://www.google.com/maps/search/{quote_plus(query)}?hl={locale}"

async def scrape_google_maps(
    keyword: str,
    city: str,
    max_results: int = 50,
    headless: bool = True,
    output_dir: str = "data/raw",
    run_id: str | None = None,
    run_date: str | None = None,
) -> dict:
    """Main entry point: scrape Google Maps and save raw JSON."""
    started_at = datetime.now(timezone.utc)
    run_timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"run_{started_at.strftime('%Y%m%d_%H%M%S_%f')}"
    run_date = run_date or started_at.strftime("%Y-%m-%d")
    scraped_at = started_at.isoformat(timespec="seconds")
    query = f"{keyword} in {city}"
    search_url = build_search_url(keyword, city, locale="en")
    normalized_history_dir = _resolve_normalized_history_dir(output_dir)

    target_dir = os.path.join(output_dir, run_date, SOURCE_PLATFORM)
    os.makedirs(target_dir, exist_ok=True)
    output_path = os.path.join(
        target_dir, f"{SOURCE_PLATFORM}_raw_{run_timestamp}.json"
    )
    errors_path = os.path.join(target_dir, f"errors_{run_id}.json")

    logger.info("Starting Google Maps scrape for query '%s' (run_id=%s)", query, run_id)

    browser_config = BrowserConfig(
        headless=headless,
        viewport_width=1280,
        viewport_height=900,
        user_agent=CHROME_USER_AGENT,
        text_mode=True,
        light_mode=True,
        extra_args=["--disable-blink-features=AutomationControlled"],
    )

    records: list[dict] = []
    errors: list[dict] = []
    place_urls: list[dict[str, str]] = []
    discovery_diagnostics: dict[str, Any] = {}
    accepted_exact_page_keys: set[str] = set()
    accepted_record_context_by_key: dict[str, dict[str, str]] = {}
    accepted_gmaps_urls: set[str] = set()
    accepted_record_context_by_gmaps_url: dict[str, dict[str, str]] = {}
    accepted_names: set[str] = set()
    accepted_record_context_by_name: dict[str, dict[str, str]] = {}
    record_diagnostics: dict[str, Any] = {
        "cross_run_same_date_skips": {
            "reason_code": CROSS_RUN_SAME_DATE_SKIP_REASON_CODE,
            "count": 0,
            "examples": [],
            "load_diagnostics": {},
        },
        "duplicate_exact_page_skips": {
            "reason_code": DUPLICATE_EXACT_PAGE_REASON_CODE,
            "count": 0,
            "examples": [],
        },
        "binding_mismatches": {
            "reason_code": BINDING_REASON_CODES["mismatch"],
            "count": 0,
            "examples": [],
        },
    }

    try:
        discovery_started = time.perf_counter()
        async with AsyncWebCrawler(config=browser_config) as crawler:
            place_urls, discovery_diagnostics = await discover_place_urls(
                crawler=crawler,
                keyword=keyword,
                city=city,
                max_results=max_results,
            )
        discovery_seconds = time.perf_counter() - discovery_started
        logger.info(
            "Discovery timing: %.2fs (%s2.00s target) | discovered=%s",
            discovery_seconds,
            "<" if discovery_seconds < 2 else ">=",
            len(place_urls),
        )

        if not place_urls:
            logger.warning("No Google Maps places discovered for query '%s'", query)
        else:
            ordered_places = []
            for place in place_urls:
                if not isinstance(place, dict):
                    continue
                place_url = normalize_place_url(str(place.get("place_url") or ""))
                if not place_url:
                    continue
                ordered_places.append(
                    {
                        "place_url": place_url,
                        "place_id": str(place.get("place_id") or "").strip(),
                    }
                )

            ordered_places, cross_run_skip_diagnostics = (
                apply_same_date_cross_run_google_dedup(
                    ordered_places,
                    target_dir=target_dir,
                    current_output_path=output_path,
                    normalized_history_dir=normalized_history_dir,
                )
            )
            record_diagnostics["cross_run_same_date_skips"] = cross_run_skip_diagnostics
            if cross_run_skip_diagnostics["count"]:
                logger.info(
                    "Skipping %s previously scraped same-date Google Maps places before visits: %s",
                    cross_run_skip_diagnostics["count"],
                    json.dumps(
                        cross_run_skip_diagnostics,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            else:
                logger.info(
                    "Same-date Google Maps cross-run dedup loaded %s prior identities and skipped 0 places",
                    cross_run_skip_diagnostics["load_diagnostics"].get(
                        "identity_records_loaded", 0
                    ),
                )

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    user_agent=CHROME_USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                await context.route("**/*", route_nonessential_requests)
                page = await context.new_page()
                if Stealth is not None:
                    with contextlib.suppress(Exception):
                        stealth = Stealth()
                        await stealth.apply_stealth_async(context)
                        await stealth.apply_stealth_async(page)

                with contextlib.suppress(Exception):
                    await navigate_to_feed(page, search_url)

                with contextlib.suppress(Exception):
                    await page.evaluate(build_cookie_consent_js())

                recent_outcomes: deque[bool] = deque(maxlen=SESSION_FAILURE_WINDOW)
                for index, place in enumerate(ordered_places, start=1):
                    if max_results > 0 and len(records) >= max_results:
                        logger.info(
                            "Reached accepted Google Maps result cap (%s); stopping further visits",
                            max_results,
                        )
                        break
                    place_url = place["place_url"]
                    place_id = place["place_id"]
                    record_started = time.perf_counter()
                    try:
                        if await page.locator('div[role="feed"]').count() == 0:
                            await navigate_to_feed(page, search_url)

                        match_started = time.perf_counter()
                        target, matched_index = await match_sidebar_item_on_page(
                            page, place_url, index - 1
                        )
                        if target is None or matched_index is None:
                            raise RuntimeError(
                                "Discovered place could not be matched to an active sidebar item"
                            )

                        await target.scroll_into_view_if_needed()
                        previous_url = page.url
                        previous_heading = ""
                        with contextlib.suppress(Exception):
                            heading_locator = page.locator("h1.DUwDvf, h1").first
                            if await heading_locator.count() > 0:
                                previous_heading = (
                                    await heading_locator.text_content() or ""
                                ).strip()
                        await target.click(force=True)

                        await page.wait_for_selector("h1.DUwDvf, h1", timeout=20000)
                        with contextlib.suppress(Exception):
                            await page.wait_for_function(
                                """(state) => {
                                    const prevUrl = state.prevUrl || '';
                                    const prevHeading = state.prevHeading || '';
                                    if (window.location.href !== prevUrl) return true;
                                    const heading = (document.querySelector('h1.DUwDvf, h1')?.textContent || '').trim();
                                    return !!heading && heading !== prevHeading;
                                }""",
                                arg={
                                    "prevUrl": previous_url,
                                    "prevHeading": previous_heading,
                                },
                                timeout=2500,
                            )
                        match_seconds = time.perf_counter() - match_started

                        base_started = time.perf_counter()
                        await scroll_detail_pane_for_lazy_content(page)
                        await wait_for_optional_detail_sections(page)

                        payload = await page.evaluate(
                            f"() => {{ {build_detail_extraction_js()} }}"
                        )
                        payload = payload if isinstance(payload, dict) else {}
                        if not str(payload.get("name") or "").strip():
                            for _ in range(2):
                                await page.wait_for_timeout(450)
                                await scroll_detail_pane_for_lazy_content(page)
                                await wait_for_optional_detail_sections(page)
                                retry_payload = await page.evaluate(
                                    f"() => {{ {build_detail_extraction_js()} }}"
                                )
                                if isinstance(retry_payload, dict):
                                    payload = retry_payload
                                if str(payload.get("name") or "").strip():
                                    break
                        details = await extract_place_details(
                            place_url=place_url,
                            place_id=place_id,
                            payload=payload,
                        )
                        base_seconds = time.perf_counter() - base_started

                        invalid_detail_reasons, name_value, address_value = (
                            _collect_invalid_detail_reasons(details)
                        )
                        if invalid_detail_reasons:
                            logger.warning(
                                "Invalid Google Maps detail payload for %s (%s); retrying direct navigation",
                                place_url,
                                ", ".join(invalid_detail_reasons),
                            )
                            retry_target_url = normalize_place_url(place_url) or place_url
                            with contextlib.suppress(Exception):
                                await page.goto(
                                    retry_target_url,
                                    wait_until="domcontentloaded",
                                    timeout=60000,
                                )
                                await page.wait_for_selector("h1.DUwDvf, h1", timeout=20000)

                            for _ in range(2):
                                with contextlib.suppress(Exception):
                                    await scroll_detail_pane_for_lazy_content(page)
                                    await wait_for_optional_detail_sections(page)

                                retry_payload = await page.evaluate(
                                    f"() => {{ {build_detail_extraction_js()} }}"
                                )
                                retry_payload = (
                                    retry_payload if isinstance(retry_payload, dict) else {}
                                )
                                if not retry_payload:
                                    continue
                                with contextlib.suppress(Exception):
                                    details = await extract_place_details(
                                        place_url=place_url,
                                        place_id=place_id,
                                        payload=retry_payload,
                                    )
                                invalid_detail_reasons, name_value, address_value = (
                                    _collect_invalid_detail_reasons(details)
                                )
                                if "missing_required_field:address" in invalid_detail_reasons:
                                    fallback_address = (
                                        await _extract_address_fallback_from_page(page)
                                    )
                                    if fallback_address:
                                        details["address"] = fallback_address
                                        invalid_detail_reasons, name_value, address_value = (
                                            _collect_invalid_detail_reasons(details)
                                        )
                                if not invalid_detail_reasons:
                                    logger.info(
                                        "Recovered invalid payload for %s after direct navigation retry",
                                        place_url,
                                    )
                                    break

                        photos_seconds = 0.0
                        share_seconds = 0.0
                        about_seconds = 0.0
                        hours_seconds = 0.0

                        website = str(details.get("website") or "")
                        if not details.get("ig_url") and "instagram.com" in website:
                            details["ig_url"] = website
                        if not details.get("tiktok_url") and "tiktok.com" in website:
                            details["tiktok_url"] = website

                        # Upgrade existing photo URLs to HD
                        details["photos"] = [
                            upgrade_photo_url_to_hd(str(p).strip())
                            for p in (details.get("photos") or [])
                            if str(p).strip()
                        ][:12]

                        if should_extract_hd_photos(details.get("photos") or []):
                            photos_started = time.perf_counter()
                            hd_photos = await extract_hd_photos(page, max_photos=12)
                            photos_seconds = time.perf_counter() - photos_started
                            if hd_photos:
                                details["photos"] = hd_photos

                        if needs_short_share_url(str(details.get("gmaps_url") or "")):
                            share_started = time.perf_counter()
                            short_url = await extract_short_share_url(page)
                            share_seconds = time.perf_counter() - share_started
                            if short_url:
                                details["share_url"] = short_url
                                details["gmaps_url"] = short_url

                        if needs_about_attributes(details):
                            about_started = time.perf_counter()
                            current_detail_url = page.url
                            about_attrs = await extract_about_attributes(
                                page, place_url=current_detail_url
                            )
                            about_seconds = time.perf_counter() - about_started
                            if about_attrs:
                                details.update(about_attrs)
                        sanitize_about_attributes(details)

                        opening_hours = normalize_opening_hours(
                            [str(item) for item in (details.get("opening_hours") or [])]
                        )

                        if not has_structured_opening_hours(opening_hours):
                            hours_started = time.perf_counter()
                            expanded_hours = await extract_expanded_opening_hours(page)
                            hours_seconds = time.perf_counter() - hours_started
                            if expanded_hours:
                                opening_hours = expanded_hours

                        if not opening_hours:
                            label_candidates = await page.evaluate(
                                """() => {
                                    const candidates = [];
                                    for (const node of document.querySelectorAll('[aria-label*="open" i], [aria-label*="hours" i], button[data-item-id="oh"]')) {
                                        const label = (node.getAttribute('aria-label') || '').trim();
                                        if (label) candidates.push(label);
                                    }
                                    return candidates;
                                }"""
                            )
                            if isinstance(label_candidates, list):
                                opening_hours = normalize_opening_hours(
                                    [
                                        str(item).strip()
                                        for item in label_candidates
                                        if str(item).strip()
                                    ]
                                )

                        final_hours, final_days = expand_opening_hours_to_full_week(
                            opening_hours
                        )
                        details["opening_hours"] = final_hours
                        details["opening_days"] = final_days

                        if not str(details.get("address") or "").strip():
                            fallback_address = await _extract_address_fallback_from_page(page)
                            if fallback_address:
                                details["address"] = fallback_address

                        invalid_detail_reasons, name_value, address_value = (
                            _collect_invalid_detail_reasons(details)
                        )

                        blocking_invalid_reasons = [
                            reason
                            for reason in invalid_detail_reasons
                            if reason != "missing_required_field:address"
                        ]

                        if blocking_invalid_reasons:
                            errors.append(
                                {
                                    "source_platform": SOURCE_PLATFORM,
                                    "run_id": run_id,
                                    "scraped_at": datetime.now(timezone.utc).isoformat(
                                        timespec="seconds"
                                    ),
                                    "query": query,
                                    "place_url": place_url,
                                    "stage": "validation",
                                    "reason_code": "invalid_detail_payload",
                                    "validation_errors": blocking_invalid_reasons,
                                    "raw_preview": {
                                        "name": name_value,
                                        "address": address_value,
                                        "gmaps_url": str(details.get("gmaps_url") or ""),
                                        "maps_link": str(details.get("maps_link") or ""),
                                    },
                                }
                            )
                            logger.warning(
                                "Skipping invalid Google Maps detail payload for %s: %s",
                                place_url,
                                ", ".join(blocking_invalid_reasons),
                            )
                            recent_outcomes.append(True)
                            continue

                        if "missing_required_field:address" in invalid_detail_reasons:
                            logger.warning(
                                "Proceeding without address for %s after all fallback attempts",
                                place_url,
                            )

                        details, accepted_exact_page_key, source_record_id = (
                            stabilize_google_accepted_record_identity(details)
                        )
                        record_identity = build_google_maps_record_identity_snapshot(
                            details,
                            place_url=place_url,
                            source_record_id=source_record_id or place_url,
                        )
                        accepted_gmaps_url = normalize_place_url(
                            details.get("gmaps_url") or details.get("maps_link") or ""
                        )
                        accepted_name = normalize_google_binding_text(
                            details.get("name")
                        )
                        if str(details.get("binding_status") or "") == "mismatch":
                            binding_mismatch_diagnostic = {
                                "reason_code": BINDING_REASON_CODES["mismatch"],
                                "source_record_id": source_record_id or place_url,
                                "identity_context": record_identity,
                                "binding_diagnostics": dict(
                                    details.get("binding_diagnostics") or {}
                                ),
                            }
                            record_diagnostics["binding_mismatches"]["count"] += 1
                            append_google_maps_diagnostic_example(
                                record_diagnostics["binding_mismatches"]["examples"],
                                binding_mismatch_diagnostic,
                            )
                            logger.warning(
                                "Flagging Google Maps binding mismatch for %s: %s",
                                place_url,
                                json.dumps(
                                    binding_mismatch_diagnostic,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            )
                        (
                            duplicate_kept_record,
                            duplicate_matched_on,
                            duplicate_matched_value,
                        ) = _resolve_google_identity_match(
                            validated_google_place_id="",
                            canonical_place_key=accepted_exact_page_key,
                            gmaps_url=accepted_gmaps_url,
                            normalized_name=accepted_name,
                            by_place_id={},
                            by_exact_page_key=accepted_record_context_by_key,
                            by_gmaps_url=accepted_record_context_by_gmaps_url,
                            by_name=accepted_record_context_by_name,
                            exact_page_match_name="exact_page_key",
                        )

                        if duplicate_kept_record:
                            duplicate_skip_diagnostic = (
                                build_google_maps_duplicate_skip_diagnostic(
                                    accepted_exact_page_key=accepted_exact_page_key,
                                    skipped_record=record_identity,
                                    kept_record=duplicate_kept_record,
                                    matched_on=duplicate_matched_on,
                                    matched_value=duplicate_matched_value,
                                )
                            )
                            record_diagnostics["duplicate_exact_page_skips"][
                                "count"
                            ] += 1
                            append_google_maps_diagnostic_example(
                                record_diagnostics["duplicate_exact_page_skips"][
                                    "examples"
                                ],
                                duplicate_skip_diagnostic,
                            )
                            logger.info(
                                "Skipping duplicate accepted Google Maps exact page %s: %s",
                                accepted_exact_page_key,
                                json.dumps(
                                    duplicate_skip_diagnostic,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            )
                            recent_outcomes.append(True)
                            continue

                        if accepted_exact_page_key:
                            accepted_exact_page_keys.add(accepted_exact_page_key)
                            accepted_record_context_by_key[accepted_exact_page_key] = (
                                dict(record_identity)
                            )
                        if accepted_gmaps_url:
                            accepted_gmaps_urls.add(accepted_gmaps_url)
                            accepted_record_context_by_gmaps_url[accepted_gmaps_url] = (
                                dict(record_identity)
                            )
                        if accepted_name:
                            accepted_names.add(accepted_name)
                            accepted_record_context_by_name[accepted_name] = dict(
                                record_identity
                            )

                        persisted_payload = build_persisted_google_raw_payload(details)
                        records.append(
                            {
                                "source_platform": SOURCE_PLATFORM,
                                "run_id": run_id,
                                "scraped_at": scraped_at,
                                "query": query,
                                "source_record_id": source_record_id or place_url,
                                "raw": persisted_payload,
                            }
                        )
                        recent_outcomes.append(True)
                        record_seconds = time.perf_counter() - record_started
                        logger.info(
                            "Extracted %s/%s: %s | total=%.2fs (%s4.00s) match=%.2fs base=%.2fs photos=%.2fs share=%.2fs about=%.2fs hours=%.2fs",
                            len(records),
                            max_results if max_results > 0 else len(ordered_places),
                            details.get("name")
                            or details.get("gmaps_url")
                            or place_url,
                            record_seconds,
                            "<" if record_seconds < 4 else ">=",
                            match_seconds,
                            base_seconds,
                            photos_seconds,
                            share_seconds,
                            about_seconds,
                            hours_seconds,
                        )
                    except Exception as exc:
                        recent_outcomes.append(False)
                        errors.append(
                            {
                                "source_platform": SOURCE_PLATFORM,
                                "run_id": run_id,
                                "scraped_at": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                                "query": query,
                                "place_url": place_url,
                                "error": str(exc),
                            }
                        )
                        logger.warning("Failed to extract %s: %s", place_url, exc)
                    finally:
                        with contextlib.suppress(Exception):
                            await close_detail_pane_on_page(page)

                    failure_count = sum(1 for outcome in recent_outcomes if not outcome)
                    if (
                        len(recent_outcomes) == SESSION_FAILURE_WINDOW
                        and failure_count >= SESSION_FAILURE_THRESHOLD
                    ):
                        with contextlib.suppress(Exception):
                            await navigate_to_feed(page, search_url)
                        recent_outcomes.clear()

                await context.close()
                await browser.close()

    except Exception as exc:
        logger.exception("Google Maps crawl aborted: %s", exc)
        errors.append(
            {
                "source_platform": SOURCE_PLATFORM,
                "run_id": run_id,
                "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "query": query,
                "stage": "crawl",
                "error": str(exc),
            }
        )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    with open(errors_path, "w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)

    summary = {
        "run_id": run_id,
        "count": len(records),
        "total_discovered": len(place_urls),
        "total_extracted": len(records),
        "total_errors": len(errors),
        "discovery_diagnostics": discovery_diagnostics,
        "record_diagnostics": record_diagnostics,
        "output_path": output_path,
        "errors_path": errors_path,
    }
    logger.info(
        "Google Maps scrape summary: %s", json.dumps(summary, ensure_ascii=False)
    )
    return summary

def main() -> int:
    """CLI entry point for the Google Maps Crawl4AI scraper."""
    parser = argparse.ArgumentParser(description="Google Maps Cafe Scraper (Crawl4AI)")
    parser.add_argument("--keyword", "-k", required=True, help="Business keyword")
    parser.add_argument("--city", "-c", required=True, help="Target city")
    parser.add_argument("--max-results", "-m", type=int, default=50)
    parser.add_argument("--output-dir", default="data/raw")
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()

    try:
        summary = asyncio.run(
            scrape_google_maps(
                keyword=args.keyword,
                city=args.city,
                max_results=args.max_results,
                headless=not args.show_browser,
                output_dir=args.output_dir,
            )
        )
        logger.info(
            "Completed Google Maps scrape: discovered=%s extracted=%s errors=%s output=%s",
            summary["total_discovered"],
            summary["total_extracted"],
            summary["total_errors"],
            summary["output_path"],
        )
        return 0
    except KeyboardInterrupt:
        logger.warning("Google Maps scrape interrupted by user")
        return 130
    except Exception as exc:
        logger.exception("Google Maps scraper failed: %s", exc)
        return 1
