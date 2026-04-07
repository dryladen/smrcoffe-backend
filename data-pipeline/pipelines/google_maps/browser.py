from __future__ import annotations

import contextlib
import json
import logging
import re
from collections import deque
from typing import Any
from urllib.parse import quote_plus

from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

from .constants import SESSION_MAX_SCROLLS, SESSION_MIN_SCROLLS
from .extractors import (
    expand_opening_hours_to_full_week,
    extract_base_url,
    extract_coordinates_from_url,
    extract_opening_days,
    extract_place_urls_from_html,
    extract_place_urls_from_links,
    has_structured_opening_hours,
    needs_about_attributes,
    needs_short_share_url,
    normalize_opening_hours,
    sanitize_about_attributes,
    should_extract_hd_photos,
    upgrade_photo_url_to_hd,
)
from .identity import (
    build_google_maps_binding_diagnostics,
    build_google_maps_identity_contract,
    extract_google_maps_place_label,
    extract_google_place_id,
    merge_discovered_place_targets,
    normalize_place_url,
    validate_google_maps_identity_contract,
)
from .js_wrapper import build_detail_extraction_js, build_discovery_js, extract_js_payload

logger = logging.getLogger(__name__)


def build_search_url(keyword: str, city: str, locale: str = "en") -> str:
    query = f"{keyword} in {city}"
    return f"https://www.google.com/maps/search/{quote_plus(query)}?hl={locale}"

async def close_detail_pane_on_page(page: Any) -> bool:
    back_button = page.locator('button[aria-label="Back"]')
    if await back_button.count() > 0:
        with contextlib.suppress(Exception):
            await back_button.first.click()
            await page.wait_for_selector('div[role="feed"]', timeout=2500)
    if await page.locator('div[role="feed"]').count() > 0:
        return True
    with contextlib.suppress(Exception):
        await page.go_back(wait_until="domcontentloaded", timeout=8000)
        await page.wait_for_selector('div[role="feed"]', timeout=2500)
    if await page.locator('div[role="feed"]').count() > 0:
        return True
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_selector('div[role="feed"]', timeout=2000)
    return await page.locator('div[role="feed"]').count() > 0

async def wait_for_optional_detail_sections(page: Any) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_function(
            """() => {
                const selectors = [
                    '[aria-label*="Hours" i]',
                    'button[data-item-id="oh"]',
                    'a[href*="instagram.com"], a[data-item-id="instagram"]',
                    'a[href*="tiktok.com"], a[data-item-id="tiktok"]',
                    'img[data-photo-preview], a[href*="/photos/"] img[src]'
                ];
                return selectors.some((selector) => document.querySelector(selector));
            }""",
            timeout=2200,
        )

async def scroll_detail_pane_for_lazy_content(page: Any) -> None:
    detail_candidates = [
        'div[role="main"]',
        'div[role="main"] [jsaction*="scroll"]',
        'div.m6QErb[aria-label*="Results" i]',
        'div.m6QErb[tabindex="0"]',
    ]
    detail = None
    for selector in detail_candidates:
        locator = page.locator(selector)
        if await locator.count() > 0:
            detail = locator.first
            break

    if detail is None:
        return

    with contextlib.suppress(Exception):
        await detail.scroll_into_view_if_needed()

    stale_cycles = 0
    last_snapshot: tuple[int, int] | None = None

    for _ in range(5):
        with contextlib.suppress(Exception):
            await detail.evaluate(
                "el => el.scrollBy(0, Math.max(900, Math.floor(el.clientHeight * 0.9)))"
            )

        await page.wait_for_timeout(220)

        snapshot = await page.evaluate(
            """() => {
                const root = document.querySelector('div[role="main"]') || document.body;
                const hours = root.querySelectorAll('[aria-label*="Hours" i], button[data-item-id="oh"]').length;
                const social = root.querySelectorAll('a[href*="instagram.com"], a[data-item-id="instagram"], a[href*="tiktok.com"], a[data-item-id="tiktok"]').length;
                const photos = root.querySelectorAll('img[data-photo-preview], a[href*="/photos/"] img[src], img[src*="googleusercontent.com"]').length;
                const meaningful = hours + social + photos;
                const scrollHeight = (root && root.scrollHeight) ? root.scrollHeight : 0;
                return [scrollHeight, meaningful];
            }"""
        )
        if not isinstance(snapshot, list) or len(snapshot) != 2:
            continue

        current = (int(snapshot[0] or 0), int(snapshot[1] or 0))
        if current[1] >= 6:
            break

        if last_snapshot == current:
            stale_cycles += 1
            if stale_cycles >= 2:
                break
        else:
            stale_cycles = 0
        last_snapshot = current

async def match_sidebar_item_on_page(
    page: Any,
    target_place_url: str,
    fallback_index: int,
    max_attempts: int = 8,
) -> tuple[Any | None, int | None]:
    normalized_target = normalize_place_url(target_place_url)
    target_place_id = extract_google_place_id(target_place_url)

    feed = page.locator('div[role="feed"]').first
    anchors = page.locator('div[role="feed"] a[href*="/maps/place/"]')
    last_count = 0

    for attempt in range(max_attempts):
        entries = await anchors.evaluate_all(
            """nodes => nodes.map((node, idx) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                const visible = !!(
                    rect.width > 0
                    && rect.height > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && node.offsetParent !== null
                );
                return {
                    idx,
                    href: node.href || node.getAttribute('href') || '',
                    visible,
                };
            })"""
        )
        if not isinstance(entries, list):
            entries = []
        visible_entries = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("visible")
        ]
        candidate_entries = visible_entries or [
            entry for entry in entries if isinstance(entry, dict)
        ]

        count = len(candidate_entries)
        last_count = max(last_count, count)

        for entry in candidate_entries:
            idx = int(entry.get("idx", 0))
            href = str(entry.get("href") or "")
            normalized_href = normalize_place_url(href)
            if normalized_target and normalized_href == normalized_target:
                return anchors.nth(idx), idx

            if target_place_id:
                candidate_place_id = extract_google_place_id(href)
                if candidate_place_id and candidate_place_id == target_place_id:
                    return anchors.nth(idx), idx

        if attempt < max_attempts - 1:
            with contextlib.suppress(Exception):
                await feed.hover()
            with contextlib.suppress(Exception):
                await page.mouse.wheel(0, 1400)
            await page.wait_for_timeout(240)

    if last_count > 0:
        fallback_entries = await anchors.evaluate_all(
            """nodes => nodes.map((node, idx) => ({
                idx,
                visible: !!(node.offsetParent !== null),
            }))"""
        )
        if not isinstance(fallback_entries, list):
            fallback_entries = []
        visible_fallback = [
            entry
            for entry in fallback_entries
            if isinstance(entry, dict) and entry.get("visible")
        ]
        candidates = visible_fallback or [
            entry for entry in fallback_entries if isinstance(entry, dict)
        ]
        safe_pos = fallback_index if 0 <= fallback_index < len(candidates) else 0
        safe_index = int(candidates[safe_pos].get("idx", 0))
        return anchors.nth(safe_index), safe_index

    return None, None

async def extract_hd_photos(page: Any, max_photos: int = 12) -> list[str]:
    """Open the photo gallery, extract high-definition photo URLs, close gallery."""
    photos: list[str] = []

    try:
        # Try to open the photo gallery
        clicked = False
        for selector in (
            'button[aria-label*="See photos" i]',
            'button[aria-label*="photos" i]',
            'button[jsaction*="pane.photos"]',
            'div[role="tab"][aria-label*="Photo" i]',
        ):
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    await locator.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # Try clicking the hero photo area
            try:
                hero = page.locator(
                    'button[data-photo-preview] img, img[src*="googleusercontent.com"]'
                ).first
                if await hero.count() > 0:
                    await hero.click()
                    clicked = True
            except Exception:
                pass

        if not clicked:
            return photos

        with contextlib.suppress(Exception):
            await page.wait_for_selector(
                'img[src*="googleusercontent.com"], img[src*="lh3.google"]',
                timeout=3000,
            )

        # Scroll inside gallery to load more photos
        for _ in range(3):
            with contextlib.suppress(Exception):
                await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(220)

        # Extract high-resolution photo URLs
        payload = await page.evaluate("""() => {
            const photos = [];
            const seen = new Set();
            const skipRe = /avatar|profile|glyph|logo|gstatic/i;
            const addPhoto = (src) => {
                if (!src || seen.has(src) || skipRe.test(src)) return;
                seen.add(src);
                photos.push(src);
            };
            const selectors = [
                'img[src*="googleusercontent.com"]',
                'img[src*="lh3.google"]',
                'img[src*="lh4.google"]',
                'img[src*="lh5.google"]',
            ];
            for (const sel of selectors) {
                for (const img of document.querySelectorAll(sel)) {
                    addPhoto(img.getAttribute('src') || '');
                }
            }
            for (const node of document.querySelectorAll('[style*="background-image"]')) {
                const style = node.getAttribute('style') || '';
                const match = style.match(/url\\(["']?([^"')]+)["']?\\)/i);
                if (match) addPhoto(match[1]);
            }
            return photos;
        }""")

        if isinstance(payload, list):
            for url in payload:
                url = str(url).strip()
                if url:
                    photos.append(upgrade_photo_url_to_hd(url))

        photos = photos[:max_photos]
    except Exception as exc:
        logger.warning("Failed to extract HD photos from gallery: %s", exc)

    # Close gallery / lightbox
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)

    return photos

async def extract_about_attributes(
    page: Any, place_url: str = ""
) -> dict[str, list[str]]:
    """Click About tab, extract structured attributes from the About view.

    Strategy: click the About tab, then parse the visible text line-by-line
    to find category headers (Accessibility, Service options, …) and their
    items.  This text-based approach is more robust than DOM traversal
    because Google Maps renders About content outside the usual detail pane.
    """
    about: dict[str, list[str]] = {}

    try:
        # Find and click the About tab
        about_btn = None
        for selector in (
            'button[role="tab"][aria-label^="About"]',
            'button[aria-label="About"]',
            'button[role="tab"]:has-text("About")',
        ):
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0:
                    about_btn = locator
                    break
            except Exception:
                continue

        if about_btn is None:
            return about

        await about_btn.click()
        with contextlib.suppress(Exception):
            await page.wait_for_selector(
                '[role="tabpanel"], [aria-label*="Accessibility" i], [aria-label*="Service options" i]',
                timeout=1400,
            )

        # Scroll inside every scrollable panel to force lazy sections to render
        for _ in range(3):
            with contextlib.suppress(Exception):
                await page.mouse.wheel(0, 1500)
            await page.wait_for_timeout(90)

        # ---- text-based extraction -----------------------------------------
        payload = await page.evaluate(r"""
        (() => {
            const result = {};
            const KNOWN = [
                'Accessibility', 'Service options', 'Highlights', 'Popular for',
                'Offerings', 'Dining options', 'Amenities', 'Atmosphere', 'Crowd',
                'Planning', 'Recycling', 'Children', 'Parking', 'Health and safety', 'Payments',
                'Health & safety',
            ];
            const NOISE_RE = /collapse side panel|sign in|show your location|learn more|zoom|show slider|hide slider|map details|map tools|map type|globe view|labels|map data|loading|google apps|layers|transit|traffic|biking|terrain|street view|wildfires|air quality|terms|privacy|send product feedback|entry=ttu|g_ep|search|directions|call|save|share|nearby|your maps|coordinates|coords|copy/i;

            const toKey = (t) => {
                const low = (t || '').trim().toLowerCase();
                for (const c of KNOWN) {
                    if (low === c.toLowerCase()) return c.toLowerCase().replace(/\s+/g, '_').replace(/&/g, 'and');
                }
                return '';
            };

            /*  Collect visible text from the largest scrollable panel.
                Google Maps may render About content in div[role="main"],
                a tabpanel, or a generic scrollable div.  We pick the one
                whose innerText contains the most KNOWN keywords.           */
            const candidates = [
                ...document.querySelectorAll(
                    '[role="tabpanel"], [role="main"], .m6QErb, .DkEaL, div[jsname]'
                ),
            ];
            // also consider the right-hand sidebar panel
            const sidePanel = document.querySelector('[jsname="rfVbPc"]')
                || document.querySelector('[jsname="QA0Szd"]')
                || document.querySelector('[role="complementary"]');
            if (sidePanel) candidates.push(sidePanel);

            let bestText = '';
            let bestHits = 0;
            for (const el of candidates) {
                const txt = (el.innerText || '');
                const hits = KNOWN.filter(k => txt.toLowerCase().includes(k.toLowerCase())).length;
                if (hits > bestHits) {
                    bestHits = hits;
                    bestText = txt;
                }
            }
            // Fallback: if no panel had known keywords, scan body
            if (bestHits === 0) {
                bestText = document.body.innerText || '';
            }

            const lines = bestText.split(/\n/).map(l => l.trim()).filter(Boolean);
            let cur = '';

            for (const line of lines) {
                const key = toKey(line);
                if (key) {
                    cur = key;
                    if (!result[cur]) result[cur] = [];
                    continue;
                }
                if (!cur) continue;
                if (NOISE_RE.test(line)) continue;
                if (line.length < 2 || line.length > 80) continue;
                // skip if it looks like another known header that didn't match exactly
                if (KNOWN.some(k => line.toLowerCase().startsWith(k.toLowerCase()))) continue;
                result[cur].push(line);
            }

            // DOM-based pass: remove items that have a close/X icon (OazX1c)
            // and keep only items with a verify/checkmark icon (SwaGS).
            const CLOSE_RE = /\bOazX1c\b/;
            for (const el of document.querySelectorAll('[class*="OazX1c"], [class*="SwaGS"]')) {
                const row = el.closest('li, tr, [role="listitem"], div');
                if (!row) continue;
                const txt = (row.textContent || '').trim();
                if (!txt) continue;
                // If this element has the close icon, remove items whose text is in this row
                if (CLOSE_RE.test(el.className || '')) {
                    for (const k of Object.keys(result)) {
                        result[k] = result[k].filter(item => !txt.includes(item));
                        if (result[k].length === 0) delete result[k];
                    }
                }
            }

            // Deduplicate and prune empty
            for (const k of Object.keys(result)) {
                result[k] = [...new Set(result[k])];
                if (result[k].length === 0) delete result[k];
            }
            return result;
        })()
        """)

        if isinstance(payload, dict):
            about = payload

        restored = False
        back_button = page.locator('button[aria-label="Back"]').first
        if await back_button.count() > 0:
            with contextlib.suppress(Exception):
                await back_button.click()
                await page.wait_for_selector("h1.DUwDvf", timeout=7000)
                restored = True

        if not restored:
            with contextlib.suppress(Exception):
                await page.go_back(wait_until="domcontentloaded", timeout=9000)
                await page.wait_for_selector("h1.DUwDvf", timeout=7000)
                restored = True

        if not restored and place_url:
            with contextlib.suppress(Exception):
                await page.goto(place_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector("h1.DUwDvf", timeout=15000)

    except Exception as exc:
        logger.warning("Failed to extract about attributes: %s", exc)
        if place_url:
            with contextlib.suppress(Exception):
                await page.goto(place_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector("h1.DUwDvf", timeout=15000)

    return about

async def extract_short_share_url(page: Any) -> str:
    """Click Share button, extract short maps.app.goo.gl URL, close dialog."""
    short_url = ""

    try:
        # Scroll detail pane to top so action buttons (Share, Save) are visible
        with contextlib.suppress(Exception):
            detail = page.locator('div[role="main"]').first
            if await detail.count() > 0:
                await detail.evaluate("el => el.scrollTo(0, 0)")
                await page.wait_for_timeout(300)

        # Click Share button - try multiple selectors, prefer detail pane
        clicked = False
        for selector in (
            'div[role="main"] button[aria-label="Share"]',
            'div[role="main"] button[data-item-id="share"]',
            'button[data-item-id="share"]',
            'button[aria-label="Share"]',
            'button[aria-label*="Share" i]:not([aria-label*="share your"])',
            'button[jsaction*="share"]',
        ):
            try:
                locators = page.locator(selector)
                count = await locators.count()
                if count > 0:
                    # Use last match (detail pane button), not header button
                    target = locators.nth(count - 1) if count > 1 else locators.first
                    await target.scroll_into_view_if_needed()
                    await page.wait_for_timeout(250)
                    await target.click(force=True)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.debug("Share button not found on page")
            return short_url

        # Wait for the share dialog to appear
        try:
            await page.wait_for_selector('div[role="dialog"]', timeout=8000)
            await page.wait_for_timeout(650)
        except Exception:
            logger.debug("Share dialog did not appear after click")
            await page.wait_for_timeout(1800)

        # Extract the short URL from the share dialog
        short_url = await page.evaluate(r"""() => {
            // Look for input fields inside dialogs that contain the short URL
            const dialogs = document.querySelectorAll('div[role="dialog"]');
            for (const dialog of dialogs) {
                // Try input fields first (most reliable)
                const inputs = dialog.querySelectorAll('input');
                for (const input of inputs) {
                    const val = (input.value || input.getAttribute('value') || '').trim();
                    if (val.includes('maps.app.goo.gl') || val.includes('goo.gl')) return val;
                }
                // Try links
                const links = dialog.querySelectorAll('a');
                for (const link of links) {
                    const href = (link.getAttribute('href') || '').trim();
                    if (href.includes('maps.app.goo.gl') || href.includes('goo.gl/maps')) return href;
                }
                // Try text content
                const text = dialog.textContent || '';
                const match = text.match(new RegExp('https://maps\\.app\\.goo\\.gl/[A-Za-z0-9]+', 'i'));
                if (match) return match[0];
            }
            // Fallback: scan entire page for the short URL
            const allInputs = document.querySelectorAll('input');
            for (const input of allInputs) {
                const val = (input.value || '').trim();
                if (val.includes('maps.app.goo.gl')) return val;
            }
            return '';
        }""")

        if not short_url:
            with contextlib.suppress(Exception):
                await page.wait_for_timeout(350)
            short_url = await page.evaluate(r"""() => {
                const dialogs = document.querySelectorAll('div[role="dialog"]');
                for (const dialog of dialogs) {
                    const text = (dialog.textContent || '').trim();
                    const match = text.match(new RegExp('https://maps\\.app\\.goo\\.gl/[A-Za-z0-9]+', 'i'));
                    if (match) return match[0];
                }
                return '';
            }""")

        short_url = str(short_url or "").strip()
    except Exception as exc:
        logger.warning("Failed to extract share URL: %s", exc)

    # Close share dialog
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)

    return short_url

async def extract_expanded_opening_hours(page: Any) -> list[str]:
    """Try to open the hours panel and capture day-by-day rows."""
    clicked = await page.evaluate(
        r"""() => {
            const selectors = [
                'div[role="main"] [aria-label*="show open hours for the week" i]',
                'div[role="main"] button[data-item-id="oh"]',
                'div[role="main"] [role="button"][aria-label*="hours" i]',
                'div[role="main"] button[aria-label*="hours" i]',
                'div[role="main"] [aria-label*="See more hours" i]',
                'div[role="main"] [aria-label*="open hours" i]',
                '[aria-label*="show open hours for the week" i]',
                '[role="button"][aria-label*="hours" i]',
            ];

            for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                for (const node of nodes) {
                    const label = ((node.getAttribute('aria-label') || '') + ' ' + (node.textContent || ''))
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    if (
                        !label
                        || label.includes('share')
                        || label.includes('save')
                        || label.includes('directions')
                        || label.includes('suggest an edit')
                        || label.includes('copy open hours')
                    ) {
                        continue;
                    }
                    const clickable = node.closest('button, [role="button"]') || node;
                    try {
                        clickable.scrollIntoView({ block: 'center', inline: 'nearest' });
                        clickable.click();
                        return true;
                    } catch (error) {}
                }
            }
            return false;
        }"""
    )

    if not clicked:
        return []

    with contextlib.suppress(Exception):
        await page.wait_for_selector(
            'div[role="dialog"], table[aria-label*="Hours" i], [aria-label*="Hours" i]',
            timeout=2500,
        )

    extracted = await page.evaluate(
        r"""() => {
            const dayRe = /\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b/i;
            const timeRe = /(?:\b\d{1,2}(?::\d{2})?\s*[ap]m\b|open\s+24\s+hours|\bclosed\b)/i;
            const noiseRe = /^(hours|open website|open menu link|see more hours|copy open hours)$/i;
            const rows = [];
            const seen = new Set();

            const push = (value) => {
                const cleaned = (value || '')
                    .replace(/\s+/g, ' ')
                    .replace(/,?\s*Copy open hours\b/ig, '')
                    .trim()
                    .replace(/[\s,]+$/, '');
                if (!cleaned || noiseRe.test(cleaned)) return;
                if (!dayRe.test(cleaned) || !timeRe.test(cleaned)) return;
                const key = cleaned.toLowerCase();
                if (seen.has(key)) return;
                seen.add(key);
                rows.push(cleaned);
            };

            const containers = [
                ...document.querySelectorAll('div[role="dialog"], [aria-label*="Hours" i], table[aria-label*="Hours" i]'),
            ];
            if (!containers.length) containers.push(document.body);

            for (const container of containers) {
                const nodes = container.querySelectorAll('[aria-label], tr, [role="row"], li, div, span, button');
                for (const node of nodes) {
                    push(node.getAttribute ? (node.getAttribute('aria-label') || '') : '');
                    push(node.textContent || '');
                }
            }

            return rows;
        }"""
    )

    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)

    if isinstance(extracted, list):
        return normalize_opening_hours([str(item) for item in extracted])
    return []

def resolve_discovery_target_count(max_results: int) -> int:
    if max_results <= 0:
        return SESSION_MAX_SCROLLS
    return max(max_results * 4, max_results + 10)

async def discover_place_urls(
    crawler: AsyncWebCrawler,
    keyword: str,
    city: str,
    max_results: int = 50,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Phase 1: Search Google Maps, scroll feed, collect place URLs."""
    discovery_target = resolve_discovery_target_count(max_results)
    query = f"{keyword} in {city}"
    search_url = build_search_url(keyword, city, locale="en")
    num_scrolls = max(
        SESSION_MIN_SCROLLS,
        min(
            SESSION_MAX_SCROLLS,
            discovery_target // 3 if discovery_target else SESSION_MIN_SCROLLS,
        ),
    )
    logger.info("Discovering Google Maps listings for query: %s", query)

    search_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="css:div[role='feed']",
        page_timeout=60000,
        js_code=build_discovery_js(num_scrolls, target_count=discovery_target),
    )

    result: Any = await crawler.arun(url=search_url, config=search_config)
    if not result.success:
        logger.warning("Search crawl reported issues: %s", result.error_message)

    discovered_places: list[dict[str, str]] = []
    discovery_diagnostics: dict[str, Any] = {
        "raw_candidates": 0,
        "unique_candidates": 0,
        "duplicates_skipped": 0,
        "invalid_skipped": 0,
        "unique_exact_page_keys": 0,
        "duplicate_examples": [],
    }

    payload = extract_js_payload(result)
    if isinstance(payload, list):
        merge_discovered_place_targets(
            discovered_places,
            discovery_diagnostics,
            [str(raw_url) for raw_url in payload],
            source_name="js_payload",
        )
        logger.info(
            "JS discovery collected %s unique place URLs (duplicates_skipped=%s)",
            len(discovered_places),
            discovery_diagnostics["sources"]["js_payload"]["duplicates_skipped"],
        )

    if len(discovered_places) < discovery_target:
        link_urls = extract_place_urls_from_links(result)
        merge_discovered_place_targets(
            discovered_places,
            discovery_diagnostics,
            link_urls,
            source_name="link_fallback",
        )
        if link_urls:
            logger.info(
                "Link fallback scanned %s URLs (accepted=%s duplicates_skipped=%s)",
                len(link_urls),
                discovery_diagnostics["sources"]["link_fallback"]["accepted"],
                discovery_diagnostics["sources"]["link_fallback"]["duplicates_skipped"],
            )

    if len(discovered_places) < discovery_target:
        html_urls = extract_place_urls_from_html(getattr(result, "html", "") or "")
        newly_added = merge_discovered_place_targets(
            discovered_places,
            discovery_diagnostics,
            html_urls,
            source_name="html_fallback",
        )
        if newly_added:
            logger.info("HTML fallback added %s place URLs", newly_added)

    discovery_diagnostics.pop("seen_exact_page_keys", None)
    discovery_diagnostics.pop("place_index_by_key", None)
    discovery_diagnostics["returned_candidates"] = len(discovered_places)
    discovery_diagnostics["discovery_target"] = discovery_target
    logger.info(
        "Discovery dedup diagnostics: %s",
        json.dumps(discovery_diagnostics, ensure_ascii=False, sort_keys=True),
    )
    return discovered_places, discovery_diagnostics

async def extract_place_details(
    place_url: str,
    place_id: str = "",
    *,
    payload: dict[str, Any] | None = None,
) -> dict:
    normalized_place_url = normalize_place_url(place_url)
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"No structured payload returned for {normalized_place_url or place_url}"
        )

    final_page_url = str(payload.get("gmaps_url") or place_url).strip()
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if latitude is None or longitude is None:
        fallback_lat, fallback_lng = extract_coordinates_from_url(final_page_url)
        latitude = fallback_lat if latitude is None else latitude
        longitude = fallback_lng if longitude is None else longitude

    identity = build_google_maps_identity_contract(
        discovery_url=normalized_place_url,
        final_page_url=final_page_url,
        place_token=str(
            payload.get("google_place_id")
            or place_id
            or extract_google_place_id(final_page_url)
            or extract_google_place_id(normalized_place_url)
            or ""
        ).strip(),
    )

    google_place_id = identity["validated_google_place_id"]

    name = str(payload.get("name") or "").strip()
    if not name:
        name = (
            extract_google_maps_place_label(identity["canonical_place_url"])
            or extract_google_maps_place_label(identity["final_page_url"])
            or extract_google_maps_place_label(identity["discovery_url"])
        )
    if not name:
        raise RuntimeError(
            f"Detail pane did not load a business heading for {normalized_place_url or place_url}"
        )

    opening_hours_raw: list[str] = []
    raw_opening_hours = payload.get("opening_hours")
    if isinstance(raw_opening_hours, list):
        opening_hours_raw = [
            str(item).strip() for item in raw_opening_hours if str(item).strip()
        ]

    opening_days = extract_opening_days(opening_hours_raw)
    binding_diagnostics = build_google_maps_binding_diagnostics(
        discovery_url=identity["discovery_url"],
        final_page_url=identity["final_page_url"],
        canonical_place_url=identity["canonical_place_url"],
        validated_google_place_id=google_place_id,
        discovery_place_id=place_id,
        name=name,
        address=str(payload.get("address") or "").strip(),
    )

    data = {
        "name": name,
        "rating": str(payload.get("rating") or "").strip(),
        "reviews": str(payload.get("reviews") or "").strip(),
        "category": str(payload.get("category") or "").strip(),
        "address": str(payload.get("address") or "").strip(),
        "website": extract_base_url(str(payload.get("website") or "").strip()),
        "phone": str(payload.get("phone") or "").strip(),
        "price_range": str(payload.get("price_range") or "").strip(),
        "opening_hours": opening_hours_raw,
        "opening_days": opening_days,
        "ig_url": str(payload.get("ig_url") or "").strip(),
        "tiktok_url": str(payload.get("tiktok_url") or "").strip(),
        "photos": payload.get("photos")
        if isinstance(payload.get("photos"), list)
        else [],
        "gmaps_url": final_page_url,
        "maps_link": identity["canonical_place_url"] or final_page_url,
        "google_place_id": google_place_id,
        "binding_status": binding_diagnostics["status"],
        "binding_diagnostics": binding_diagnostics,
        "latitude": latitude,
        "longitude": longitude,
        **identity,
    }
    validate_google_maps_identity_contract(data)
    return data

def _should_block_request(url: str, resource_type: str) -> bool:
    lowered = url.lower()
    if any(
        token in lowered
        for token in (
            "google-analytics.com",
            "doubleclick.net",
            "googletagmanager.com",
            "googleadservices.com",
            "adservice.google.com",
            "analytics.google.com",
            "stats.g.doubleclick.net",
            "hotjar.com",
            "newrelic.com",
            "sentry.io",
        )
    ):
        return True
    return False

async def route_nonessential_requests(route: Any, request: Any) -> None:
    if _should_block_request(str(request.url or ""), str(request.resource_type or "")):
        await route.abort()
        return
    await route.continue_()

async def navigate_to_feed(page: Any, url: str) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector('div[role="feed"]', timeout=20000)
