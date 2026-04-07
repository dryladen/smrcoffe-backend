import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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


def build_search_url(keyword: str, city: str, locale: str = "en") -> str:
    query = f"{keyword} in {city}"
    return f"https://www.google.com/maps/search/{quote_plus(query)}?hl={locale}"


def build_cookie_consent_js() -> str:
    return """
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const selectors = [
    'button[aria-label*="Accept all"]',
    'button[jsname="b3VHJd"]',
    '#L2AGLb',
    'form button + button',
    'button[aria-label*="accept"]'
];
const texts = [/^accept all$/i, /^i agree$/i, /^accept$/i, /^allow all$/i];

const clickElement = async (el) => {
    if (!el) return false;
    try {
        el.click();
        await wait(500);
        return true;
    } catch (error) {
        return false;
    }
};

for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (await clickElement(el)) {
        return true;
    }
}

const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
for (const button of buttons) {
    const text = (button.textContent || '').trim();
    if (texts.some((pattern) => pattern.test(text))) {
        if (await clickElement(button)) {
            return true;
        }
    }
}

return false;
"""


def build_discovery_js(num_scrolls: int, target_count: int = 0) -> str:
    return f"""
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const feed = document.querySelector('div[role="feed"]');
const ordered = [];
const seen = new Set();
const normalize = (value) => {{
    if (!value) return '';
    try {{
        const url = new URL(value, window.location.origin);
        const path = decodeURIComponent(url.pathname || '');
        if (!path.includes('/maps/place/')) return '';
        return `https://www.google.com${{path}}`;
    }} catch (error) {{
        return value;
    }}
}};
const collect = () => {{
    const scope = feed || document;
    scope.querySelectorAll('a[href*="/maps/place/"]').forEach(anchor => {{
        const normalized = normalize(anchor.href || '');
        if (normalized && !seen.has(normalized)) {{
            seen.add(normalized);
            ordered.push(normalized);
        }}
    }});
}};

collect();
if (feed) {{
    let stale = 0;
    let previous = ordered.length;
    for (let step = 0; step < {num_scrolls}; step++) {{
        if ({target_count} && ordered.length >= {target_count}) break;
        feed.scrollTop = feed.scrollHeight;
        feed.dispatchEvent(new WheelEvent('wheel', {{ deltaY: 5000, bubbles: true }}));
        feed.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
        await wait(350);
        collect();
        if (ordered.length <= previous) {{
            stale += 1;
            if (stale >= 2) break;
        }} else {{
            stale = 0;
        }}
        previous = ordered.length;
    }}
}}

return ordered;
"""


def build_detail_extraction_js() -> str:
    return r"""
const data = {
    name: '', rating: '', reviews: '', category: '', address: '',
    website: '', phone: '', gmaps_url: window.location.href, maps_link: window.location.href,
    google_place_id: '', opening_hours: [], opening_days: [], price_range: '',
    ig_url: '', tiktok_url: '', photos: [], latitude: null, longitude: null
};
const unwrapGoogleUrl = (value) => {
    if (!value) return '';
    try {
        const url = new URL(value, window.location.origin);
        if (url.pathname === '/url') {
            return decodeURIComponent(url.searchParams.get('q') || url.searchParams.get('url') || '');
        }
        return url.href;
    } catch (error) {
        return value;
    }
};
const safe = (fn) => { try { fn(); } catch(e) {} };
safe(() => {
    const selectors = [
        'h1.DUwDvf',
        'h1.fontHeadlineLarge',
        'div[role="main"] h1',
        'h1',
    ];
    for (const selector of selectors) {
        const el = document.querySelector(selector);
        const text = (el && el.textContent) ? el.textContent.trim() : '';
        if (text) {
            data.name = text;
            break;
        }
    }
});
safe(() => { const el = document.querySelector('div[aria-label*="stars"]'); if (el) data.rating = (el.getAttribute('aria-label') || '').split(' ')[0]; });
safe(() => { const el = document.querySelector('span[aria-label*="reviews"]'); if (el) data.reviews = (el.getAttribute('aria-label') || '').split(' ')[0].replace(/,/g, ''); });
safe(() => {
    for (const sel of ['button.DkEaL', 'button[jsaction*="pane.rating.category"]', '[aria-label*="Category"]']) {
        const catEl = document.querySelector(sel);
        if (catEl) { data.category = (catEl.textContent || '').trim() || (catEl.getAttribute('aria-label') || '').replace('Category:', '').trim(); break; }
    }
});
safe(() => { const el = document.querySelector('button[data-item-id="address"]'); if (el) { const aria = el.getAttribute('aria-label') || ''; data.address = aria ? aria.replace('Address:', '').trim() : (el.textContent || '').trim(); } });
try {
    let webHref = '';
    const webEl = document.querySelector('a[data-item-id="authority"]');
    if (webEl) {
        const aria = webEl.getAttribute('aria-label') || '';
        let domain = aria.split(':').slice(1).join(':').trim();
        if (domain && domain.includes('.')) {
            if (!domain.startsWith('http')) domain = 'https://' + domain;
            webHref = domain;
        } else {
            const href = webEl.getAttribute('href') || '';
            if (href.startsWith('/url?')) {
                const params = new URLSearchParams(href.split('?')[1]);
                webHref = decodeURIComponent(params.get('q') || '');
            } else if (href.startsWith('http')) {
                webHref = href;
            }
        }
    }
    if (webHref) {
        try {
            const url = new URL(webHref);
            data.website = url.origin;
        } catch (error) {}
    }
} catch (error) {}
safe(() => { const el = document.querySelector('button[data-item-id*="phone:tel:"]'); if (el) { const aria = el.getAttribute('aria-label') || ''; data.phone = aria ? aria.replace('Phone:', '').trim() : (el.textContent || '').trim(); } });
try {
    const socialLinks = [
        ['ig_url', [
            'a[data-item-id="instagram"]',
            'a[href*="instagram.com"]',
            'a[aria-label*="Instagram"]',
        ]],
        ['tiktok_url', [
            'a[data-item-id="tiktok"]',
            'a[href*="tiktok.com"]',
            'a[aria-label*="TikTok"]',
        ]],
    ];
    for (const [key, selectors] of socialLinks) {
        for (const selector of selectors) {
            const anchor = document.querySelector(selector);
            if (!anchor) continue;
            const href = unwrapGoogleUrl(anchor.getAttribute('href') || '');
            if (href) {
                data[key] = href;
                break;
            }
        }
    }
} catch (error) {}

try {
    const hours = [];
    const seenHours = new Set();
    const dayRe = /\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b/i;
    const timeRe = /(?:\b\d{1,2}(?::\d{2})?\s*[ap]m\b|open\s+24\s+hours|\bclosed\b)/i;
    const pushHour = (value) => {
        const cleaned = (value || '').replace(/\s+/g, ' ').trim();
        if (cleaned && !seenHours.has(cleaned)) {
            seenHours.add(cleaned);
            hours.push(cleaned);
        }
    };
    const hoursTable = document.querySelector('[aria-label="Hours"], [aria-label*="hours" i], table[aria-label*="Hours" i]');
    if (hoursTable) {
        const rows = Array.from(hoursTable.querySelectorAll('tr'));
        for (const row of rows) {
            const cells = Array.from(row.querySelectorAll('td, th')).map(cell => (cell.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
            if (cells.length >= 2) {
                pushHour(`${cells[0]}: ${cells.slice(1).join(' ')}`);
            } else if (cells.length === 1 && cells[0].includes(':')) {
                pushHour(cells[0]);
            }
        }
    }
    if (!hours.length) {
        const candidates = Array.from(document.querySelectorAll('[aria-label*="hours" i], [aria-label*="open" i], button[data-item-id="oh"], button[aria-label*="hours" i]'));
        for (const candidate of candidates) {
            const label = candidate.getAttribute('aria-label') || '';
            if (!label) continue;
            label
                .split(/[·⋅]/)
                .map(part => part.trim())
                .filter(part => part.includes(':') || (dayRe.test(part) && timeRe.test(part)))
                .forEach(pushHour);
        }
    }
    data.opening_hours = hours;
    data.opening_days = hours.map(item => item.split(':', 1)[0].trim()).filter(Boolean);
} catch (error) {}

try {
    const normalizePriceText = (value) => (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    const seenPrices = new Set();
    const priceCandidates = [];
    const addCandidate = (value) => {
        const text = normalizePriceText(value);
        if (!text || seenPrices.has(text)) return;
        seenPrices.add(text);
        priceCandidates.push(text);
    };
    const summarySelectors = [
        'div[role="main"] .DfOCNb.fontBodyMedium',
        'div[role="main"] .MNVeJb.eXOdV.eF9eN.PnPrlf',
        'div[role="main"] .TIHn2 .LBgpqf .fontBodyMedium.dmRWX',
        'div[role="main"] .LBgpqf .fontBodyMedium.dmRWX',
        'div[role="main"] [jsname="tJHJj"]',
        '[jsname="tJHJj"][role="button"]',
    ];
    for (const selector of summarySelectors) {
        for (const node of document.querySelectorAll(selector)) {
            addCandidate(node.textContent || '');
            addCandidate(node.getAttribute('aria-label') || '');
        }
    }
    for (const table of document.querySelectorAll('div[role="main"] table[aria-label*="Price range" i]')) {
        addCandidate(table.getAttribute('aria-label') || '');
        for (const row of table.querySelectorAll('tr')) {
            addCandidate(row.textContent || '');
        }
    }
    const broadTextScope = [
        ...document.querySelectorAll('div[role="main"] [role="button"], div[role="main"] button, div[role="main"] div, div[role="main"] span'),
        ...document.querySelectorAll('[role="button"][jsname], [aria-label*="price" i], [aria-label*="per person" i]'),
    ];
    for (const node of broadTextScope) {
        const text = normalizePriceText(node.textContent || '');
        const aria = normalizePriceText(node.getAttribute('aria-label') || '');
        for (const value of [text, aria]) {
            if (/\bper\s+person\b/i.test(value) || /(Rp|IDR|\$|€|£|¥|₹)/i.test(value)) {
                addCandidate(value);
            }
        }
    }
    const currencyPattern = /(Rp|IDR|\$|€|£|¥|₹)\s*[\d.,Kk]+(?:\s*[–-]\s*[\d.,Kk]+)?\+?/i;
    const findPriceFromPerPersonSummary = (text) => {
        if (!/per\s+person/i.test(text)) return '';
        const lines = text.split(/\n|\u2022|\|/).map(normalizePriceText).filter(Boolean);
        for (const line of lines) {
            const match = line.match(currencyPattern);
            if (match) {
                const start = match.index || 0;
                const tail = line.slice(start);
                const perPersonMatch = tail.match(/^(.*?\bper\s+person\b)/i);
                if (perPersonMatch) return normalizePriceText(perPersonMatch[1]);
                return normalizePriceText(match[0]);
            }
        }
        const fullMatch = text.match(currencyPattern);
        if (fullMatch) return normalizePriceText(fullMatch[0]);
        return '';
    };
    const findPriceFromCompactRow = (text) => {
        const tokens = text.split(/·|\u2022/).map(normalizePriceText).filter(Boolean);
        for (const token of tokens) {
            const match = token.match(currencyPattern);
            if (!match) continue;
            const start = match.index || 0;
            return normalizePriceText(token.slice(start));
        }
        const directMatch = text.match(currencyPattern);
        return directMatch ? normalizePriceText(directMatch[0]) : '';
    };
    let extractedPrice = '';
    for (const candidate of priceCandidates) {
        extractedPrice = findPriceFromPerPersonSummary(candidate);
        if (extractedPrice) break;
    }
    if (!extractedPrice) {
        for (const candidate of priceCandidates) {
            extractedPrice = findPriceFromCompactRow(candidate);
            if (extractedPrice) break;
        }
    }
    if (!extractedPrice) {
        const bodyText = normalizePriceText(document.body ? (document.body.innerText || '') : '');
        const bodyMatch = bodyText.match(new RegExp(currencyPattern.source + '[^\\n]*?\\bper\\s+person\\b', 'i'))
            || bodyText.match(currencyPattern);
        if (bodyMatch) extractedPrice = normalizePriceText(bodyMatch[0]);
    }
    data.price_range = extractedPrice;
} catch (error) {}

try {
    const photoUrls = [];
    const seenPhotos = new Set();
    const skipRe = /avatar|profile|glyph|logo|maps\.gstatic\.com\//i;
    const addPhoto = (src) => {
        if (!src || seenPhotos.has(src) || skipRe.test(src)) return;
        seenPhotos.add(src);
        photoUrls.push(src);
    };
    const photoSelectors = [
        'img[src][data-photo-preview]',
        'img[src*="googleusercontent.com"]',
        'button[aria-label*="photo" i] img[src]',
        'a[href*="/photos/"] img[src]',
        'div[role="main"] img[src]',
    ];
    for (const selector of photoSelectors) {
        for (const img of document.querySelectorAll(selector)) {
            addPhoto(img.getAttribute('src') || '');
        }
    }

    const bgCandidates = Array.from(document.querySelectorAll('[style*="background-image"]'));
    for (const node of bgCandidates) {
        const style = node.getAttribute('style') || '';
        const match = style.match(/url\(["']?([^"')]+)["']?\)/i);
        addPhoto(match ? match[1] : '');
        if (photoUrls.length >= 12) break;
    }

    data.photos = photoUrls.slice(0, 12);
} catch (error) {}

safe(() => {
    const decode = (value) => {
        try {
            return decodeURIComponent(value || '');
        } catch (error) {
            return value || '';
        }
    };
    const isValid = (value) => /^(ChI[A-Za-z0-9_-]+|[0-9]+|(?:0x)?[0-9A-Fa-f]+:(?:0x)?[0-9A-Fa-f]+)$/.test(value || '');
    const matches = [
        window.location.href.match(/[?&](?:ftid|cid|place_id)=([^&#]+)/),
        window.location.href.match(/!1s([^!/?&#]+)/),
        window.location.href.match(/\b(ChI[A-Za-z0-9_-]+)\b/),
    ];
    for (const match of matches) {
        const candidate = decode(match && match[1] ? match[1] : '').trim();
        if (isValid(candidate)) {
            data.google_place_id = candidate;
            break;
        }
    }
});
safe(() => { const p = new URLSearchParams(window.location.search); const c = p.get('center'); if (c) { const [lat, lng] = c.split(',').map(Number); if (!isNaN(lat) && !isNaN(lng)) { data.latitude = lat; data.longitude = lng; } } });
safe(() => { if (data.latitude === null) { const m = window.location.href.match(/@(-?\d+\.\d+),(-?\d+\.\d+)/); if (m) { data.latitude = parseFloat(m[1]); data.longitude = parseFloat(m[2]); } } });
return data;
"""


def coerce_json_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("[", "{")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def extract_js_payload(result: Any) -> Any:
    js_return_value = getattr(result, "js_return_value", None)
    if js_return_value is not None:
        return coerce_json_value(js_return_value)

    js_execution_result = getattr(result, "js_execution_result", None)
    if not isinstance(js_execution_result, dict):
        return None

    results = js_execution_result.get("results")
    if isinstance(results, list):
        for item in reversed(results):
            if isinstance(item, dict):
                if "result" in item:
                    return coerce_json_value(item["result"])
                continue
            if item is not None:
                return coerce_json_value(item)

    if "result" in js_execution_result:
        return coerce_json_value(js_execution_result["result"])

    return None


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


def resolve_discovery_target_count(max_results: int) -> int:
    if max_results <= 0:
        return SESSION_MAX_SCROLLS
    return max(max_results * 4, max_results + 10)


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
        js_code_before_wait=build_cookie_consent_js(),
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

                        name_value = str(details.get("name") or "").strip()
                        address_value = str(details.get("address") or "").strip()
                        invalid_detail_reasons: list[str] = []
                        if not name_value:
                            invalid_detail_reasons.append("missing_required_field:name")
                        elif name_value.casefold() == "results":
                            invalid_detail_reasons.append("invalid_place_name:results")
                        if not address_value:
                            invalid_detail_reasons.append("missing_required_field:address")

                        if invalid_detail_reasons:
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
                                    "validation_errors": invalid_detail_reasons,
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
                                ", ".join(invalid_detail_reasons),
                            )
                            recent_outcomes.append(True)
                            continue

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


if __name__ == "__main__":
    sys.exit(main())
