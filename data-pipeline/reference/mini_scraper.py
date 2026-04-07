"""
mini_scraper.py — Robust & Dynamic Content Scraper
====================================================
Usage:
    python mini_scraper.py <url1> [url2 ...] [options]

Options:
    --output <file>         Output markdown file (default: output.md)
    --output-dir <dir>      Save one .md file per URL in this directory
    --timeout <ms>          Page timeout in ms (default: 90000)
    --retries <n>           Max retries per URL (default: 3)
    --no-paywall            Disable paywall bypass (archive.ph fallback)
    --no-popup              Disable popup / overlay dismissal JS
    --no-scroll             Disable auto-scroll hydration
    --headless              Force headless mode (default: True)
    --visible               Run browser in visible mode (useful for debug)

Paywall / anti-bot strategies implemented:
  1. Realistic Chrome user-agent & headers
  2. Generic JS popup / cookie-banner / overlay dismissal
  3. Notion-specific toggle expansion + scroll hydration
  4. Medium "Friend Link" URL normalization
  5. archive.ph fallback for paywalled articles (opt-in by default)
  6. Smart retry with back-off on failure
  7. Site-type auto-detection → selects best wait_for / js_code strategy
"""

import argparse
import asyncio
import os
import re
import sys
import random
import aiohttp
from pathlib import Path
from urllib.parse import urlparse, urlencode, quote_plus, urljoin

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

# ---------------------------------------------------------------------------
# Common disguise headers / UA
# ---------------------------------------------------------------------------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# ---------------------------------------------------------------------------
# Generic JS: dismiss overlays, cookie banners, paywalls, popups
# ---------------------------------------------------------------------------
DISMISS_POPUPS_JS = """
(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));

    // Common selectors for overlays, cookie banners, paywall gates
    const overlaySelectors = [
        // Cookie banners
        '[id*="cookie"] button[class*="accept"]',
        '[class*="cookie"] button[class*="accept"]',
        '[id*="consent"] button[class*="agree"]',
        '#onetrust-accept-btn-handler',
        '.cc-btn.cc-allow',
        'button[aria-label*="Accept"]',
        'button[aria-label*="accept"]',
        // Generic modals / gates
        '[class*="modal"] [class*="close"]',
        '[class*="popup"] [class*="close"]',
        '[class*="overlay"] [class*="close"]',
        '[class*="paywall"] [class*="close"]',
        '[aria-label="Close"]',
        '[aria-label="close"]',
        'button[class*="dismiss"]',
        'button[class*="Dismiss"]',
        '[role="dialog"] button[class*="close"]',
        // Medium / Substack "become a member" nag
        '[data-testid="close-button"]',
        // Generic X / × close buttons
        'button.close', '.close-button', '.btn-close',
    ];

    for (const sel of overlaySelectors) {
        try {
            const el = document.querySelector(sel);
            if (el) {
                el.click();
                await sleep(300);
            }
        } catch (_) {}
    }

    // Force-remove fixed/sticky overlays that block content
    const blockingStyles = ['fixed', 'sticky'];
    document.querySelectorAll('*').forEach(el => {
        try {
            const style = window.getComputedStyle(el);
            if (blockingStyles.includes(style.position) && style.zIndex > 10) {
                const tag = el.tagName.toLowerCase();
                const cls = (el.className || '').toLowerCase();
                const id  = (el.id || '').toLowerCase();
                const isPaywall = ['paywall','gate','modal','overlay','popup','banner',
                                   'newsletter','subscribe','member','cookie','consent',
                                   'interstitial','nag'].some(k => cls.includes(k) || id.includes(k));
                if (isPaywall) {
                    el.remove();
                }
            }
        } catch (_) {}
    });

    // Un-blur paywalled content
    document.querySelectorAll('*').forEach(el => {
        try {
            const s = el.style;
            if (s.filter && s.filter.includes('blur')) s.filter = '';
            if (s.overflow === 'hidden' && el.scrollHeight > el.clientHeight * 1.5) {
                s.overflow = 'visible';
                s.maxHeight = 'none';
            }
        } catch (_) {}
    });

    // Remove CSS rules that hide/blur content (paywall tricks)
    try {
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules) {
                    if (rule.style) {
                        if (rule.style.filter && rule.style.filter.includes('blur')) {
                            rule.style.filter = '';
                        }
                        if (rule.style.overflow === 'hidden') {
                            rule.style.overflow = 'visible';
                            rule.style.maxHeight = 'none';
                        }
                    }
                }
            } catch (_) {}
        }
    } catch (_) {}

    await sleep(500);
})();
"""

# ---------------------------------------------------------------------------
# Auto-scroll JS: works for generic pages and Notion
# ---------------------------------------------------------------------------
SCROLL_HYDRATE_JS = """
(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const scroller = document.querySelector('.notion-scroller.vertical') || window;
    let lastH = 0;

    for (let i = 0; i < 30; i++) {
        const h = (scroller !== window ? scroller.scrollHeight : document.body.scrollHeight);
        if (scroller.scrollTo) scroller.scrollTo(0, h);
        else window.scrollTo(0, h);
        await sleep(600);

        // Expand Notion toggles
        document.querySelectorAll('.notion-toggle-block').forEach(el => {
            const b = el.querySelector('[role="button"]');
            if (b && b.getAttribute('aria-expanded') === 'false') b.click();
        });

        // Expand "Show more" / "Load more" generically
        const moreButtons = Array.from(document.querySelectorAll('button, [role="button"]'))
            .filter(b => /show more|load more|expand|see more/i.test(b.textContent));
        for (const btn of moreButtons.slice(0, 3)) {
            try { btn.click(); await sleep(300); } catch (_) {}
        }

        if (h === lastH && i > 10) break;
        lastH = h;
    }

    // Force-render code blocks (Notion specific)
    document.querySelectorAll('.notion-code-block, pre, code').forEach(b => b.scrollIntoView());
    await sleep(1500);
})();
"""

# ---------------------------------------------------------------------------
# Site detection helpers
# ---------------------------------------------------------------------------
def detect_site_type(url: str) -> str:
    """Return a site-type tag used to pick the best scraping strategy."""
    domain = urlparse(url).netloc.lower()
    if "notion.so" in domain or "notion.site" in domain:
        return "notion"
    if "medium.com" in domain or "towardsdatascience.com" in domain or "pub.towardsai.net" in domain:
        return "medium"
    if "substack.com" in domain:
        return "substack"
    if "arxiv.org" in domain:
        return "arxiv"
    if "huggingface.co" in domain:
        return "huggingface"
    return "generic"


def normalize_url(url: str) -> str:
    """Normalize / fix well-known URL patterns before scraping."""
    # Medium Friend Links: convert to standard article URLs
    # https://medium.com/m/callback/... -> keep as-is (they redirect)
    # Notion share links: often already canonical
    return url.strip()


def maybe_archive_url(url: str) -> str | None:
    """Return an archive.ph mirror URL for paywall bypass, or None if inapplicable."""
    domain = urlparse(url).netloc.lower()
    # Likely paywalled domains
    paywalled = [
        "medium.com", "towardsdatascience.com", "bloomberg.com",
        "wsj.com", "ft.com", "nytimes.com", "thenation.com",
        "wired.com", "theatlantic.com", "hbr.org",
    ]
    if any(p in domain for p in paywalled):
        return f"https://archive.ph/{url}"
    return None


# ---------------------------------------------------------------------------
# Build CrawlerRunConfig based on site type
# ---------------------------------------------------------------------------
def build_crawler_config(
    site_type: str,
    timeout_ms: int,
    use_popup_js: bool,
    use_scroll: bool,
) -> CrawlerRunConfig:
    """Return a CrawlerRunConfig tailored to the detected site type."""

    # Compose JS pieces
    js_parts = []
    if use_popup_js:
        js_parts.append(DISMISS_POPUPS_JS)
    if use_scroll:
        js_parts.append(SCROLL_HYDRATE_JS)
    combined_js = "\n".join(js_parts) if js_parts else None

    # Shared base kwargs
    base_kwargs = dict(
        cache_mode=CacheMode.BYPASS,
        excluded_tags=["nav", "footer", "aside", "header", "form", "script", "style"],
        remove_forms=True,
        remove_overlay_elements=True,
        exclude_social_media_links=True,
        page_timeout=timeout_ms,
        js_code=combined_js,
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=0.35, threshold_type="fixed")
        ),
    )

    if site_type == "notion":
        return CrawlerRunConfig(
            **base_kwargs,
            wait_until="domcontentloaded",
            wait_for="""js:() => {
                const blocks = document.querySelectorAll('.notion-code-block');
                if (blocks.length === 0) return true;  // No code blocks = already loaded
                return Array.from(blocks).every(
                    el => !el.innerText.includes('Loading') && el.innerText.length > 10
                );
            }""",
            css_selector=".notion-page-content",
        )

    if site_type == "medium":
        return CrawlerRunConfig(
            **base_kwargs,
            wait_until="networkidle",
            wait_for="css:article",
            css_selector="article",
        )

    if site_type == "substack":
        return CrawlerRunConfig(
            **base_kwargs,
            wait_until="networkidle",
            wait_for="css:.post-content, .body",
            css_selector=".post-content, .body",
        )

    if site_type == "arxiv":
        return CrawlerRunConfig(
            **base_kwargs,
            wait_until="domcontentloaded",
            css_selector="#abs-container, .abs-page",
        )

    if site_type == "huggingface":
        return CrawlerRunConfig(
            **base_kwargs,
            wait_until="domcontentloaded",
        )

    # Generic fallback
    return CrawlerRunConfig(
        **base_kwargs,
        wait_until="domcontentloaded",
    )


# ---------------------------------------------------------------------------
# Core scrape function with retry + archive fallback
# ---------------------------------------------------------------------------
async def scrape_url(
    crawler: AsyncWebCrawler,
    url: str,
    timeout_ms: int,
    max_retries: int,
    use_popup_js: bool,
    use_scroll: bool,
    use_paywall_bypass: bool,
) -> tuple[str | None, str, dict, list[str]]:
    """
    Attempt to scrape `url`. Returns (markdown_content, final_url, metadata).
    Returns (None, url, {}) on total failure.
    """
    urls_to_try = [normalize_url(url)]

    if use_paywall_bypass:
        archive = maybe_archive_url(url)
        if archive:
            urls_to_try.append(archive)

    for attempt_url in urls_to_try:
        site_type = detect_site_type(attempt_url)
        config = build_crawler_config(site_type, timeout_ms, use_popup_js, use_scroll)

        label = f"[{site_type.upper()}] {attempt_url[:80]}"

        for attempt in range(1, max_retries + 1):
            try:
                print(f"  🌐 Attempt {attempt}/{max_retries}: {label}")
                result = await crawler.arun(url=attempt_url, config=config)

                if result.success:
                    # Pick best markdown representation
                    md = None
                    if hasattr(result, "markdown_v2") and result.markdown_v2:
                        md = result.markdown_v2.fit_markdown or result.markdown_v2.raw_markdown
                    elif hasattr(result, "markdown") and hasattr(result.markdown, "fit_markdown"):
                        md = result.markdown.fit_markdown or result.markdown.raw_markdown
                    elif hasattr(result, "markdown") and isinstance(result.markdown, str):
                        md = result.markdown

                    if md and len(md.strip()) > 200:
                        print(f"  ✅ Success ({len(md)} chars) via {attempt_url[:60]}")
                        
                        images = []
                        if hasattr(result, "media") and isinstance(result.media, dict):
                            img_data = result.media.get("images", [])
                            for img in img_data:
                                if isinstance(img, dict) and "src" in img:
                                    images.append(img["src"])
                                elif isinstance(img, dict) and "url" in img:
                                    images.append(img["url"])
                                elif isinstance(img, str):
                                    images.append(img)
                        return md, attempt_url, result.metadata or {}, images
                    else:
                        print(f"  ⚠️  Success but content too short ({len(md or '')} chars). Retrying...")
                else:
                    print(f"  ❌ Failed: {result.error_message}")

            except Exception as exc:
                print(f"  💥 Exception: {exc}")

            if attempt < max_retries:
                backoff = 2 ** attempt + random.uniform(0, 1)
                print(f"  ⏳ Back-off {backoff:.1f}s before retry...")
                await asyncio.sleep(backoff)

    return None, url, {}, []


# ---------------------------------------------------------------------------
# Filename utilities
# ---------------------------------------------------------------------------
def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "page"


def make_output_dir_and_path(base_dir: str, title: str) -> tuple[str, str]:
    title_slug = slugify(title, 60) if title and title != "Unknown Title" else "scraped_page"
    page_dir = os.path.join(base_dir, title_slug)
    os.makedirs(page_dir, exist_ok=True)
    
    md_filename = f"{title_slug}.md"
    md_path = os.path.join(page_dir, md_filename)
    return page_dir, md_path

async def download_image(session: aiohttp.ClientSession, img_url: str, output_dir: str, idx: int) -> tuple[str | None, str | None]:
    try:
        parsed = urlparse(img_url)
        ext = os.path.splitext(parsed.path)[1]
        if not ext or len(ext) > 5:
            ext = ".jpg"
        
        filename = f"image_{idx:03d}{ext}"
        filepath = os.path.join(output_dir, filename)
        
        async with session.get(img_url, timeout=15) as resp:
            if resp.status == 200:
                with open(filepath, "wb") as f:
                    f.write(await resp.read())
                return filename, img_url
    except Exception as e:
        print(f"    ⚠️ Failed to download {img_url}: {e}")
    return None, None

async def download_all_images(image_urls: list[str], output_dir: str, base_url: str) -> list[tuple[str, str]]:
    images_downloaded = []
    if not image_urls:
        return images_downloaded
    print(f"  🖼️ Downloading {len(image_urls)} images...")
    async with aiohttp.ClientSession() as session:
        tasks = []
        valid_urls = []
        for u in image_urls:
            if not u.startswith(('http://', 'https://', 'data:')):
                u = urljoin(base_url, u)
            if u.startswith(('http://', 'https://')):
                valid_urls.append(u)
                
        for i, url in enumerate(valid_urls, 1):
            tasks.append(download_image(session, url, output_dir, i))
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, tuple) and r[0] is not None:
                    images_downloaded.append((r[1], r[0]))
        
        print(f"  ✅ Downloaded {len(images_downloaded)}/{len(valid_urls)} images.")
        return images_downloaded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(
        description="Robust mini scraper: handles JS, paywalls, overlays, and dynamic content.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="URLs to scrape")
    parser.add_argument("--output-dir", dest="output_dir", default="scrape_page",
                        help="Main storage directory (default: scrape_page)")
    parser.add_argument("--timeout", type=int, default=90000, help="Page timeout in ms (default: 90000)")
    parser.add_argument("--retries", type=int, default=3, help="Max retries per URL (default: 3)")
    parser.add_argument("--no-paywall", dest="paywall_bypass", action="store_false",
                        help="Disable archive.ph paywall fallback")
    parser.add_argument("--no-popup", dest="popup_js", action="store_false",
                        help="Disable popup/overlay dismissal JS")
    parser.add_argument("--no-scroll", dest="scroll_js", action="store_false",
                        help="Disable auto-scroll hydration JS")
    parser.add_argument("--visible", dest="headless", action="store_false",
                        help="Run browser in visible mode (disables headless)")
    parser.set_defaults(paywall_bypass=True, popup_js=True, scroll_js=True, headless=True)
    args = parser.parse_args()

    if not args.urls:
        parser.print_help()
        print("\nUsage example:")
        print("  python mini_scraper.py https://example.com https://another.com")
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)

    browser_config = BrowserConfig(
        headless=args.headless,
        viewport_width=1920,
        viewport_height=1080,
        user_agent=random.choice(USER_AGENTS),
        headers=EXTRA_HEADERS,
        extra_args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )

    total = len(args.urls)
    succeeded = 0
    failed_urls = []

    print(f"\n🚀 mini_scraper — {total} URL(s) to process")
    print(f"   Paywall bypass : {'✅' if args.paywall_bypass else '❌'}")
    print(f"   Popup dismiss  : {'✅' if args.popup_js else '❌'}")
    print(f"   Auto-scroll    : {'✅' if args.scroll_js else '❌'}")
    print(f"   Headless       : {'✅' if args.headless else '❌ (visible mode)'}")
    print(f"   Timeout        : {args.timeout}ms  |  Retries: {args.retries}")
    print()

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for idx, raw_url in enumerate(args.urls, 1):
            url = raw_url.strip()
            print(f"━━━ [{idx}/{total}] {url}")

            content, final_url, meta, images = await scrape_url(
                crawler=crawler,
                url=url,
                timeout_ms=args.timeout,
                max_retries=args.retries,
                use_popup_js=args.popup_js,
                use_scroll=args.scroll_js,
                use_paywall_bypass=args.paywall_bypass,
            )

            title = meta.get("title", "") or "Unknown Title"

            if content:
                page_dir, path = make_output_dir_and_path(args.output_dir, title)
                
                images_downloaded = []
                if images:
                    images_downloaded = await download_all_images(images, page_dir, url)

                for original_url, local_filename in images_downloaded:
                    content = content.replace(original_url, local_filename)

                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"# {title}\n**Source:** {url}\n\n{content}\n")
                print(f"  💾 Saved markdown → {path}")

                succeeded += 1
            else:
                print(f"  ❌ FAILED to extract content from {url}")
                failed_urls.append(url)

            # Polite delay between requests (except last)
            if idx < total:
                delay = random.uniform(1.5, 3.5)
                print(f"  ⏳ Cooling down {delay:.1f}s...\n")
                await asyncio.sleep(delay)

    print(f"\n{'═'*60}")
    print(f"✨ Done! {succeeded}/{total} URLs scraped successfully.")
    if failed_urls:
        print(f"❌ Failed URLs ({len(failed_urls)}):")
        for u in failed_urls:
            print(f"   • {u}")
    print(f"📁 Output directory: {os.path.abspath(args.output_dir)}")


if __name__ == "__main__":
    asyncio.run(main())
