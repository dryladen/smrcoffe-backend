import argparse
import asyncio
from html import unescape
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from playwright_stealth import Stealth


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
VIDEO_URL_PATTERN = re.compile(r"https://www\.tiktok\.com/@[^/\s\"'?#]+/video/\d+")
HASHTAG_PATTERN = re.compile(r"#(\w+)")
TIKTOK_JSON_SCRIPT_PATTERN = re.compile(
    r'<script\b(?=[^>]*(?:type=["\']application/json["\']|id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\']))[^>]*>(.*?)</script>',
    flags=re.IGNORECASE | re.DOTALL,
)
SEARCH_VIDEO_LINK_SELECTORS = (
    'a[href*="/video/"]',
    'div[data-e2e="search_video-item"] a[href]',
    '[data-e2e="search_video-item"] a[href]',
    '[data-e2e="search_top-item"] a[href]',
    'a[href*="/@"][href*="/video/"]',
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_run_id() -> str:
    return _utc_now().strftime("run_%Y%m%d_%H%M%S_%f")


def _get_run_date() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _get_run_timestamp() -> str:
    return _utc_now().strftime("%Y%m%d_%H%M%S_%f")


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_output_root(output_dir: str) -> str:
    if os.path.isabs(output_dir):
        return output_dir
    return os.path.join(_project_root(), output_dir)


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _save_json(path: str, data: list[dict] | dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file_obj:
        json.dump(data, file_obj, indent=2, ensure_ascii=False)


def _normalize_video_url(url: str) -> str:
    if not url:
        return ""
    match = VIDEO_URL_PATTERN.search(url)
    if match:
        return match.group(0)
    cleaned = url.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if cleaned.startswith("//"):
        cleaned = f"https:{cleaned}"
    return cleaned if VIDEO_URL_PATTERN.fullmatch(cleaned) else ""


def _dedupe_urls(urls: list[str], limit: int | None = None) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        canonical = _normalize_video_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        deduped.append(canonical)
        if limit is not None and len(deduped) >= limit:
            break
    return deduped


def _parse_js_return_value(value):
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            return current
        stripped = current.strip()
        if not stripped:
            return None
        try:
            current = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    return current


def _extract_result_links(result) -> list[str]:
    links = getattr(result, "links", None) or {}
    extracted: list[str] = []
    groups = links.values() if isinstance(links, dict) else [links]
    for group in groups:
        if not group:
            continue
        for entry in group:
            if isinstance(entry, dict):
                href = entry.get("href") or entry.get("url") or entry.get("link") or ""
            else:
                href = str(entry)
            if href:
                extracted.append(href)
    return extracted


def _build_tiktok_search_session_id(query: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    return f"tiktok-search-{slug or 'query'}"


def _extract_video_urls_from_search_result(result) -> list[str]:
    collected_urls: list[str] = []

    js_value = _parse_js_return_value(getattr(result, "js_return_value", None))
    if isinstance(js_value, list):
        collected_urls.extend(str(item) for item in js_value if item)
    elif isinstance(js_value, dict):
        hrefs = js_value.get("hrefs") or js_value.get("urls") or []
        if isinstance(hrefs, list):
            collected_urls.extend(str(item) for item in hrefs if item)

    html_variants = [
        getattr(result, "html", "") or "",
        getattr(result, "cleaned_html", "") or "",
    ]
    for html in html_variants:
        if not html:
            continue
        collected_urls.extend(VIDEO_URL_PATTERN.findall(html))
        if "\\/" in html:
            collected_urls.extend(VIDEO_URL_PATTERN.findall(html.replace("\\/", "/")))

    collected_urls.extend(_extract_result_links(result))
    return collected_urls


def _build_tiktok_scroll_js(max_results: int, scroll_round: int) -> str:
    selector_list = json.dumps(list(SEARCH_VIDEO_LINK_SELECTORS))
    scroll_target = max(5, min(10, max_results + 2))
    round_pause_ms = 2400 + (scroll_round * 250)
    return rf"""
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const selectors = {selector_list};
const hrefs = new Set();
const addHref = (href) => {{
    if (!href) return;
    hrefs.add(String(href).replace(/\\u002F/g, '/').replace(/\\\\\//g, '/'));
}};
const collectFromDom = () => {{
    for (const selector of selectors) {{
        document.querySelectorAll(selector).forEach((node) => addHref(node.href || node.getAttribute('href')));
    }}
    document.querySelectorAll('a[href]').forEach((node) => {{
        const href = node.href || node.getAttribute('href') || '';
        if (href.includes('/video/')) addHref(href);
    }});
}};
const collectFromScripts = () => {{
    const regex = /https:\/\/www\.tiktok\.com\/@[^\s"'?#]+\/video\/\d+/g;
    for (const script of document.scripts) {{
        const text = script.textContent || '';
        const normalized = text.replace(/\\u002F/g, '/').replace(/\\\//g, '/');
        const matches = normalized.match(regex) || [];
        matches.forEach(addHref);
    }}
}};
const nudgePointer = () => {{
    const x = Math.floor(window.innerWidth * (0.25 + Math.random() * 0.5));
    const y = Math.floor(window.innerHeight * (0.2 + Math.random() * 0.6));
    document.dispatchEvent(new MouseEvent('mousemove', {{ clientX: x, clientY: y, bubbles: true }}));
}};
const tryDismissOverlays = () => {{
    const buttons = [...document.querySelectorAll('button, [role="button"]')];
    for (const button of buttons) {{
        const label = (button.innerText || button.textContent || '').trim().toLowerCase();
        if (/(accept|agree|continue|not now|close|dismiss)/.test(label)) {{
            button.click();
        }}
    }}
}};

window.scrollTo({{ top: 0, behavior: 'smooth' }});
await wait(1800 + Math.floor(Math.random() * 700));
tryDismissOverlays();
collectFromDom();
collectFromScripts();

for (let i = 0; i < {scroll_target}; i++) {{
    nudgePointer();
    const step = Math.max(500, Math.floor(window.innerHeight * (0.85 + Math.random() * 0.35)));
    window.scrollBy({{ top: step, behavior: 'smooth' }});
    await wait({round_pause_ms} + Math.floor(Math.random() * 900));
    collectFromDom();
    collectFromScripts();
    window.scrollBy({{ top: -Math.floor(step * 0.12), behavior: 'smooth' }});
    await wait(500 + Math.floor(Math.random() * 350));
    collectFromDom();
}}

return [...hrefs];
"""


async def on_page_context_created(page, context=None, **kwargs):
    await Stealth().apply_stealth_async(page)
    return page


def _default_video_metadata(video_url: str) -> dict:
    canonical_url = _normalize_video_url(video_url) or video_url
    author_match = re.search(r"/@([^/]+)/video/", canonical_url)
    video_match = re.search(r"/video/(\d+)", canonical_url)
    author_handle = f"@{author_match.group(1)}" if author_match else ""
    video_id = video_match.group(1) if video_match else ""
    return {
        "video_url": canonical_url,
        "video_id": video_id,
        "author_handle": author_handle,
        "author_display_name": "",
        "caption": "",
        "description": "",
        "post_date": "",
        "view_count": None,
        "like_count": None,
        "comment_count": None,
        "share_count": None,
        "embed_url": f"https://www.tiktok.com/embed/v2/{video_id}" if video_id else "",
        "embed_html": "",
        "music_title": "",
        "hashtags": [],
    }


def _coerce_int(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d]", "", value)
        return int(cleaned) if cleaned else None
    return None


def _extract_meta_content(html: str, meta_name: str) -> str:
    if not html:
        return ""
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(meta_name)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(meta_name)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _extract_tiktok_item_struct(parsed: object) -> dict:
    if not isinstance(parsed, dict):
        return {}

    item_struct = parsed.get("props", {}).get("pageProps", {}).get("itemInfo", {}).get("itemStruct")
    if isinstance(item_struct, dict) and item_struct:
        return item_struct

    item_struct = (
        parsed.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct")
    )
    if isinstance(item_struct, dict) and item_struct:
        return item_struct

    item_module = parsed.get("ItemModule")
    if isinstance(item_module, dict) and item_module:
        if any(key in item_module for key in ("author", "desc", "stats", "createTime")):
            return item_module
        for value in item_module.values():
            if isinstance(value, dict) and any(
                key in value for key in ("author", "desc", "stats", "createTime")
            ):
                return value

    return {}


def _format_tiktok_post_date(value) -> str:
    timestamp = _coerce_int(value)
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _extract_tiktok_data_from_page(video_url: str, html: str) -> dict:
    if not html:
        return _default_video_metadata(video_url)

    raw_data: dict = {}

    try:
        for script_match in TIKTOK_JSON_SCRIPT_PATTERN.finditer(html):
            script_tag = script_match.group(0)
            script_text = unescape((script_match.group(1) or "").strip())
            if not script_text or not any(
                marker in script_text for marker in ("videoData", "ItemModule", "itemInfo")
            ):
                continue

            try:
                parsed = json.loads(script_text)
                raw = _extract_tiktok_item_struct(parsed)
                if not raw:
                    continue

                canonical_url = _normalize_video_url(video_url) or video_url
                video_id = raw.get("id") or raw.get("video", {}).get("id") or ""
                if not video_id:
                    match = re.search(r"/video/(\d+)", canonical_url)
                    video_id = match.group(1) if match else ""

                author_handle = raw.get("author", {}).get("uniqueId") or raw.get("author_handle") or ""
                if author_handle and not str(author_handle).startswith("@"):
                    author_handle = f"@{str(author_handle).lstrip('@')}"

                desc = raw.get("desc") or raw.get("description") or raw.get("caption") or ""
                hashtags = [match.group(1) for match in HASHTAG_PATTERN.finditer(desc)]

                raw_data = {
                    "video_url": canonical_url,
                    "video_id": str(video_id),
                    "author_handle": author_handle,
                    "author_display_name": raw.get("author", {}).get("nickname")
                    or raw.get("author_display_name", ""),
                    "caption": desc,
                    "description": desc or raw.get("desc", ""),
                    "post_date": raw.get("post_date")
                    or _format_tiktok_post_date(raw.get("createTime")),
                    "view_count": raw.get("view_count") or raw.get("stats", {}).get("playCount"),
                    "like_count": raw.get("like_count") or raw.get("stats", {}).get("diggCount"),
                    "comment_count": raw.get("comment_count")
                    or raw.get("stats", {}).get("commentCount"),
                    "share_count": raw.get("share_count") or raw.get("stats", {}).get("shareCount"),
                    "embed_url": raw.get("embed_url")
                    or (f"https://www.tiktok.com/embed/v2/{video_id}" if video_id else ""),
                    "embed_html": raw.get("embed_html", ""),
                    "music_title": raw.get("music", {}).get("title") or raw.get("music_title", ""),
                    "hashtags": hashtags,
                }
                break
            except Exception:
                LOGGER.debug("Failed to parse script tag %s, skipping", script_tag, exc_info=True)
                continue

        return _finalize_video_metadata(_default_video_metadata(video_url), raw_data, html)
    except Exception as exc:
        LOGGER.warning("Failed to merge default metadata for %s: %s", video_url, exc)
        return _default_video_metadata(video_url)


def _finalize_video_metadata(base: dict, extracted: dict | None = None, html: str = "") -> dict:
    payload = dict(base)
    extracted = extracted or {}

    for key in payload:
        if key not in extracted:
            continue
        value = extracted[key]
        if value in (None, ""):
            continue
        if key == "hashtags":
            if isinstance(value, list):
                payload[key] = [str(item).strip().lstrip("#") for item in value if str(item).strip()]
            continue
        payload[key] = value

    payload["video_url"] = _normalize_video_url(payload.get("video_url", "")) or base["video_url"]

    if not payload["description"]:
        payload["description"] = _extract_meta_content(html, "description")
    if not payload["caption"]:
        payload["caption"] = _extract_meta_content(html, "og:title") or payload["description"]
    if not payload["author_display_name"]:
        payload["author_display_name"] = _extract_meta_content(html, "og:site_name")

    if payload["author_handle"] and not payload["author_handle"].startswith("@"):
        payload["author_handle"] = f"@{payload['author_handle'].lstrip('@')}"

    if not payload["video_id"]:
        match = re.search(r"/video/(\d+)", payload["video_url"])
        payload["video_id"] = match.group(1) if match else ""

    if payload["video_id"] and not payload["embed_url"]:
        payload["embed_url"] = f"https://www.tiktok.com/embed/v2/{payload['video_id']}"

    payload["view_count"] = _coerce_int(payload.get("view_count"))
    payload["like_count"] = _coerce_int(payload.get("like_count"))
    payload["comment_count"] = _coerce_int(payload.get("comment_count"))
    payload["share_count"] = _coerce_int(payload.get("share_count"))

    if not payload["hashtags"] and payload["caption"]:
        payload["hashtags"] = [match.group(1) for match in HASHTAG_PATTERN.finditer(payload["caption"])]
    elif payload["hashtags"]:
        deduped_tags: list[str] = []
        seen_tags: set[str] = set()
        for hashtag in payload["hashtags"]:
            normalized = str(hashtag).strip().lstrip("#")
            if not normalized or normalized in seen_tags:
                continue
            seen_tags.add(normalized)
            deduped_tags.append(normalized)
        payload["hashtags"] = deduped_tags

    return payload


async def _run_with_retries(
    crawler: AsyncWebCrawler,
    url: str,
    config: CrawlerRunConfig,
    attempts: int = 3,
    base_delay: float = 2.0,
):
    last_result = None
    for attempt in range(1, attempts + 1):
        try:
            last_result = await crawler.arun(url=url, config=config)
        except Exception as exc:
            last_result = None
            error_message = str(exc)
        else:
            if getattr(last_result, "success", False):
                return last_result
            error_message = getattr(last_result, "error_message", "unknown error")

        if getattr(last_result, "success", False):
            return last_result

        if attempt >= attempts:
            return last_result

        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.25, 1.0)
        LOGGER.warning(
            "Crawl attempt %s/%s failed for %s: %s. Retrying in %.2fs",
            attempt,
            attempts,
            url,
            error_message,
            delay,
        )
        await asyncio.sleep(delay)

    return last_result


async def discover_video_urls(
    crawler: AsyncWebCrawler,
    query: str,
    max_results: int = 30,
) -> list[str]:
    """Phase 1: Search TikTok and collect video URLs."""
    encoded_query = quote_plus(query)
    search_url = f"https://www.tiktok.com/search?q={encoded_query}"
    session_id = _build_tiktok_search_session_id(query)
    initial_search_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        wait_for="css:body",
        page_timeout=90000,
        delay_before_return_html=2,
        remove_overlay_elements=True,
        simulate_user=True,
        override_navigator=True,
        magic=True,
    )

    LOGGER.info("Searching TikTok for query '%s'", query)
    result = await _run_with_retries(crawler, search_url, initial_search_cfg)
    if not result or not getattr(result, "success", False):
        error_message = getattr(result, "error_message", "Unknown TikTok search failure")
        LOGGER.warning("TikTok search crawl did not succeed for '%s': %s", query, error_message)

    collected_urls = _extract_video_urls_from_search_result(result)

    urls = _dedupe_urls(collected_urls, limit=max_results)
    if len(urls) >= max_results:
        LOGGER.info("Discovered %s unique TikTok video URLs for '%s'", len(urls), query)
        return urls

    session_bootstrap_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        session_id=session_id,
        wait_until="domcontentloaded",
        wait_for="css:body",
        wait_for_timeout=20000,
        page_timeout=90000,
        delay_before_return_html=2.5,
        remove_overlay_elements=True,
        remove_consent_popups=True,
        simulate_user=True,
        override_navigator=True,
        magic=True,
    )
    session_result = await _run_with_retries(crawler, search_url, session_bootstrap_cfg, attempts=2)
    if session_result and getattr(session_result, "success", False):
        collected_urls.extend(_extract_video_urls_from_search_result(session_result))
        urls = _dedupe_urls(collected_urls, limit=max_results)

    scroll_rounds = max(3, min(6, (max_results + 1) // 2 + 1))
    for scroll_round in range(1, scroll_rounds + 1):
        if len(urls) >= max_results:
            break

        LOGGER.info(
            "TikTok search scroll round %s/%s for '%s' (currently %s URLs)",
            scroll_round,
            scroll_rounds,
            query,
            len(urls),
        )
        scroll_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            session_id=session_id,
            js_only=True,
            wait_for="css:body",
            wait_for_timeout=15000,
            page_timeout=60000,
            delay_before_return_html=2.5,
            js_code=_build_tiktok_scroll_js(max_results=max_results, scroll_round=scroll_round),
            remove_overlay_elements=True,
            remove_consent_popups=True,
            simulate_user=True,
            override_navigator=True,
            magic=True,
        )
        scroll_result = await _run_with_retries(
            crawler,
            search_url,
            scroll_cfg,
            attempts=2,
            base_delay=1.5,
        )
        if not scroll_result or not getattr(scroll_result, "success", False):
            error_message = getattr(scroll_result, "error_message", "Unknown TikTok scroll failure")
            LOGGER.warning(
                "TikTok search scroll round %s failed for '%s': %s",
                scroll_round,
                query,
                error_message,
            )
            continue
        collected_urls.extend(_extract_video_urls_from_search_result(scroll_result))
        urls = _dedupe_urls(collected_urls, limit=max_results)

    LOGGER.info("Discovered %s unique TikTok video URLs for '%s'", len(urls), query)
    return urls


async def extract_video_metadata(
    crawler: AsyncWebCrawler,
    video_url: str,
) -> dict:
    """Phase 2: Visit a TikTok video page and extract metadata."""
    metadata_js = r"""
const data = {
    video_url: window.location.href,
    author_handle: '',
    author_display_name: '',
    caption: '',
    description: '',
    post_date: '',
    view_count: null,
    like_count: null,
    comment_count: null,
    share_count: null,
    embed_html: '',
    music_title: '',
    hashtags: []
};

const metaContent = (prop) => {
    const el = document.querySelector(`meta[property="${prop}"]`) ||
               document.querySelector(`meta[name="${prop}"]`);
    return el ? el.getAttribute('content') || '' : '';
};

data.description = metaContent('description');
data.caption = metaContent('og:title') || data.description;

const authorEl = document.querySelector('span[data-e2e="browse-username"]') ||
                 document.querySelector('a[href*="/@"] span');
if (authorEl) data.author_handle = authorEl.textContent.trim();

try {
    const scripts = document.querySelectorAll('script[type="application/json"], script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
    for (const script of scripts) {
        try {
            const text = script.textContent || '';
            if (!text || (!text.includes('videoData') && !text.includes('ItemModule') && !text.includes('itemInfo'))) {
                continue;
            }

            const parsed = JSON.parse(text);
            const itemModule = parsed?.props?.pageProps?.itemInfo?.itemStruct ||
                               parsed?.__DEFAULT_SCOPE__?.['webapp.video-detail']?.itemInfo?.itemStruct ||
                               parsed?.ItemModule || {};

            if (itemModule && Object.keys(itemModule).length > 0) {
                data.author_handle = itemModule.author?.uniqueId || data.author_handle;
                data.author_display_name = itemModule.author?.nickname || '';
                data.caption = itemModule.desc || data.caption;
                if (itemModule.createTime) {
                    data.post_date = new Date(itemModule.createTime * 1000).toISOString().split('T')[0];
                }
                data.view_count = itemModule.stats?.playCount || null;
                data.like_count = itemModule.stats?.diggCount || null;
                data.comment_count = itemModule.stats?.commentCount || null;
                data.share_count = itemModule.stats?.shareCount || null;
                data.music_title = itemModule.music?.title || '';
                break;
            }
        } catch (e) {
            continue;
        }
    }
} catch(e) {}

const hashtagRegex = /#(\w+)/g;
let match;
while ((match = hashtagRegex.exec(data.caption)) !== null) {
    data.hashtags.push(match[1]);
}

if (!data.author_handle) {
    const urlMatch = window.location.pathname.match(/@([^/]+)/);
    if (urlMatch) data.author_handle = '@' + urlMatch[1];
}

const videoIdMatch = window.location.pathname.match(/\/video\/(\d+)/);
data.video_id = videoIdMatch ? videoIdMatch[1] : '';

if (data.video_id) {
    data.embed_url = `https://www.tiktok.com/embed/v2/${data.video_id}`;
}

return data;
"""
    metadata_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=30000,
        js_code=metadata_js,
        wait_for="css:body",
        remove_overlay_elements=True,
    )

    result = await _run_with_retries(crawler, video_url, metadata_cfg)
    if not result or not getattr(result, "success", False):
        error_message = getattr(result, "error_message", "Unknown TikTok video crawl failure")
        raise RuntimeError(f"Failed to crawl TikTok video page: {error_message}")

    html = getattr(result, "html", "") or ""
    base_metadata = _extract_tiktok_data_from_page(video_url, html)
    parsed = _parse_js_return_value(getattr(result, "js_return_value", None))
    extracted = parsed if isinstance(parsed, dict) else {}
    return _finalize_video_metadata(base_metadata, extracted, html)


async def scrape_tiktok(
    query: str,
    max_results: int = 30,
    headless: bool = True,
    output_dir: str = "data/raw",
    run_id: str | None = None,
    run_date: str | None = None,
) -> dict:
    """Main entry point: scrape TikTok and save raw JSON."""
    run_id = run_id or _generate_run_id()
    run_date = run_date or _get_run_date()
    run_timestamp = _get_run_timestamp()

    output_root = _resolve_output_root(output_dir)
    target_dir = _ensure_dir(os.path.join(output_root, run_date, "tiktok"))
    raw_output_path = os.path.join(target_dir, f"tiktok_raw_{run_timestamp}.json")
    errors_output_path = os.path.join(target_dir, f"errors_{run_id}.json")

    browser_cfg = BrowserConfig(
        headless=headless,
        viewport_width=1280,
        viewport_height=900,
        user_agent=CHROME_USER_AGENT,
        extra_args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1280,900",
        ],
    )

    raw_records: list[dict] = []
    errors: list[dict] = []
    video_urls: list[str] = []

    LOGGER.info("Starting TikTok scrape for '%s' with max_results=%s", query, max_results)
    crawler = AsyncWebCrawler(config=browser_cfg)
    set_hook = getattr(crawler.crawler_strategy, "set_hook", None)
    if not callable(set_hook):
        raise RuntimeError("Crawl4AI crawler strategy does not expose set_hook for stealth injection")
    set_hook("on_page_context_created", on_page_context_created)
    await crawler.start()
    try:
        try:
            video_urls = await discover_video_urls(crawler, query, max_results=max_results)
        except Exception as exc:
            LOGGER.warning("TikTok discovery failed for '%s': %s", query, exc)
            errors.append(
                {
                    "source_platform": "tiktok",
                    "run_id": run_id,
                    "query": query,
                    "source_record_id": query,
                    "error": str(exc),
                    "failed_at": _utc_now().isoformat(),
                }
            )

        for index, video_url in enumerate(video_urls, start=1):
            LOGGER.info("Extracting TikTok metadata %s/%s: %s", index, len(video_urls), video_url)
            try:
                extracted_metadata = await extract_video_metadata(crawler, video_url)
                raw_records.append(
                    {
                        "source_platform": "tiktok",
                        "run_id": run_id,
                        "scraped_at": _utc_now().isoformat(),
                        "query": query,
                        "source_record_id": extracted_metadata.get("video_url") or video_url,
                        "raw": extracted_metadata,
                    }
                )
            except Exception as exc:
                LOGGER.warning("Failed to extract TikTok metadata from %s: %s", video_url, exc)
                errors.append(
                    {
                        "source_platform": "tiktok",
                        "run_id": run_id,
                        "query": query,
                        "source_record_id": video_url,
                        "error": str(exc),
                        "failed_at": _utc_now().isoformat(),
                    }
                )

            if index < len(video_urls):
                delay_seconds = random.uniform(2.0, 5.0)
                LOGGER.info("Sleeping %.2fs before the next TikTok video request", delay_seconds)
                await asyncio.sleep(delay_seconds)
    finally:
        await crawler.close()

    _save_json(raw_output_path, raw_records)
    _save_json(errors_output_path, errors)

    summary = {
        "source_platform": "tiktok",
        "run_id": run_id,
        "query": query,
        "run_date": run_date,
        "scraped_at": _utc_now().isoformat(),
        "discovered_video_count": len(video_urls),
        "scraped_record_count": len(raw_records),
        "error_count": len(errors),
        "raw_output_path": raw_output_path,
        "errors_output_path": errors_output_path,
    }
    LOGGER.info(
        "TikTok scrape finished: %s discovered, %s saved, %s errors",
        summary["discovered_video_count"],
        summary["scraped_record_count"],
        summary["error_count"],
    )
    return summary


def main():
    """Parse CLI arguments and run the TikTok scraper."""
    parser = argparse.ArgumentParser(description="TikTok Video Scraper (Crawl4AI)")
    parser.add_argument("--query", "-q", required=True, help="Search query")
    parser.add_argument("--max-results", "-m", type=int, default=30)
    parser.add_argument("--output-dir", default="data/raw")
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(
        scrape_tiktok(
            query=args.query,
            max_results=args.max_results,
            headless=not args.show_browser,
            output_dir=args.output_dir,
        )
    )
    LOGGER.info("Run summary: %s", json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
