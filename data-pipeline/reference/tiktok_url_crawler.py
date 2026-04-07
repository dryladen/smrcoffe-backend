import argparse
import asyncio
import json
import os
import re
from urllib.parse import quote

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

try:
    from utils import save_urls_json, slugify_filename
except ImportError:

    def save_urls_json(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def slugify_filename(text):
        return text.replace(" ", "_")


def build_extractor_js(num_scrolls: int) -> str:
    return f"""
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const hrefs = new Set();
for (let i = 0; i < {num_scrolls}; i++) {{
    window.scrollTo(0, document.body.scrollHeight);
    await wait(800);
    document.querySelectorAll('a[href*="/video/"]').forEach(a => hrefs.add(a.href));
}}
return [...hrefs];
"""


async def fetch_tiktok_urls(query: str, max_results: int) -> list[str]:
    encoded_query = quote(quote(query, safe=""), safe="")
    search_url = f"https://www.tiktok.com/search?q={encoded_query}"

    print(f"🚀  Crawling TikTok search for: '{query}'")
    print(f"🔗  URL: {search_url}")

    browser_cfg = BrowserConfig(
        headless=True,
        viewport_width=1280,
        viewport_height=900,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        extra_args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    num_scrolls = max(5, max_results // 4)

    search_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_for="css:a[href*='/video/']",
        page_timeout=60000,
        js_code=build_extractor_js(num_scrolls),
    )

    found_urls = set()

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=search_url, config=search_cfg)

        if not result.success:
            print(f"  ⚠️  Crawl failed: {result.error_message}")
        else:
            print(f"  ℹ️  Page HTML size: {len(result.html):,} chars")

        js_val = getattr(result, "js_return_value", None)
        if js_val:
            try:
                urls_from_js = json.loads(js_val) if isinstance(js_val, str) else js_val
                if isinstance(urls_from_js, list):
                    found_urls.update(urls_from_js)
                    print(f"  ✅  JS extractor returned {len(urls_from_js)} URLs")
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                print(f"  ⚠️  Could not parse js_return_value: {e}")

        if result.success and len(found_urls) < max_results and result.links:
            for group in ("internal", "external"):
                for link in result.links.get(group, []):
                    if m := re.search(
                        r"https://www\.tiktok\.com/@[^/]+/video/\d+",
                        link.get("href", ""),
                    ):
                        found_urls.add(m.group(0))

        if result.success and len(found_urls) < max_results and result.html:
            if raw_matches := re.findall(
                r"https://www\.tiktok\.com/@[^/]+/video/\d+", result.html
            ):
                found_urls.update(raw_matches)
                print(f"  ✅  HTML regex scan added {len(raw_matches)} additional URLs")

    print(f"\n  📋  Total unique video URLs extracted: {len(found_urls)}")
    return list(found_urls)[:max_results]


def main():
    parser = argparse.ArgumentParser(description="TikTok URL Crawler using Crawl4AI")
    parser.add_argument("--query", "-q", type=str, required=True)
    parser.add_argument("--maximum", "-m", type=int, default=20)
    parser.add_argument("--output", help="Explicit output JSON path")
    args = parser.parse_args()

    output_path = (
        os.path.abspath(args.output)
        if args.output
        else os.path.join(
            os.path.dirname(__file__),
            f"urls_tiktok_{slugify_filename(args.query)}.json",
        )
    )

    urls = asyncio.run(fetch_tiktok_urls(args.query, args.maximum))
    save_urls_json(
        output_path,
        [{"topic": f"TikTok: {args.query}", "query": [args.query], "urls": urls}],
    )

    if urls:
        print(f"\n✅  Successfully collected {len(urls)} URLs.")
        print(f"📂  Results saved to: {output_path}\n")
        for u in urls:
            print(u)
    else:
        print(f"\n❌  No URLs found. TikTok may have blocked the request.")
        print(f"    Try running with headless=False to debug, or use a proxy.")


if __name__ == "__main__":
    main()
