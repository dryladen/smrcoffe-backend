import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode, AsyncUrlSeeder, SeedingConfig
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

# Import from existing project structure
try:
    from utils import (
        resolve_materials_dir,
        save_urls_json,
    )
except ImportError:
    # Fallback if run from a different context
    def resolve_materials_dir(date): return "."
    def save_urls_json(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

async def seed_domain(domain: str, max_urls: int) -> list[str]:
    """Discover URLs using sitemaps and Common Crawl (Universal Seeding)."""
    print(f"🌱  Seeding URLs for domain: {domain}...")
    async with AsyncUrlSeeder() as seeder:
        # Use both sitemap and common crawl for maximum coverage
        config = SeedingConfig(
            source="sitemap+cc",
            max_urls=max_urls,
            verbose=True
        )
        discovered = await seeder.urls(domain, config)
        # discovered is a list of dicts with 'url' key
        return [item['url'] for item in discovered]

async def deep_crawl_domain(url: str, max_depth: int, max_pages: int) -> list[str]:
    """Perform a deep crawl on a single domain using Crawl4AI's BFS strategy."""
    print(f"🚀  Starting Deep Crawl discovery for: {url}")
    
    browser_cfg = BrowserConfig(headless=True)
    
    # Configure BFS Deep Crawl Strategy
    strategy = BFSDeepCrawlStrategy(
        max_depth=max_depth,
        include_external=True,
        max_pages=max_pages
    )

    config = CrawlerRunConfig(
        deep_crawl_strategy=strategy,
        cache_mode=CacheMode.BYPASS,
        verbose=True,
        wait_until="domcontentloaded", # More robust fallback
        wait_for="css:.notion-page-content", # Critical for Notion rendering
        page_timeout=60000,
        # Handle Notion's virtual scrolling
        virtual_scroll_config={
            "container_selector": "body",
            "scroll_count": 3,
            "wait_after_scroll": 0.5
        }
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        try:
            results = await crawler.arun(url=url, config=config)
        except Exception as e:
            print(f"⚠️  Deep Crawl error: {e}")
            import traceback
            traceback.print_exc()
            return []

        # Results can be a list if deep_crawl_strategy is used, or a single result
        all_found = set()
        if isinstance(results, list):
            for res in results:
                all_found.add(res.url)
                if res.success and res.links:
                    for link_type in ['internal', 'external']:
                        for link in res.links.get(link_type, []):
                            if link.get('href'):
                                all_found.add(link['href'])
        else:
            all_found.add(results.url)
            if results.success and results.links:
                for link_type in ['internal', 'external']:
                    for link in results.links.get(link_type, []):
                        if link.get('href'):
                            all_found.add(link['href'])
        
        return list(all_found)

async def run_extraction(target_url: str, args):
    """Main extraction logic combining seeding and deep crawl."""
    parsed = urlparse(target_url)
    domain = parsed.netloc or parsed.path.split('/')[0]
    
    # Step 1: Seed URLs (Fast, Bulk)
    seeded_urls = await seed_domain(domain, args.pages)
    
    # Step 2: Combine with Deep Crawl
    all_urls = set(seeded_urls)
    print(f"ℹ️  Seeding found {len(all_urls)} URLs. Running Deep Crawl for thoroughness...")
    deep_urls = await deep_crawl_domain(target_url, args.depth, args.pages)
    all_urls.update(deep_urls)
    
    return list(all_urls)[:args.pages]

def main():
    parser = argparse.ArgumentParser(description="Mini Crawl4AI Extractor (Seeding + Deep Crawl)")
    parser.add_argument("url", help="Starting URL or domain to crawl")
    parser.add_argument("--depth", type=int, default=2, help="Max depth for deep crawl (default: 2)")
    parser.add_argument("--pages", type=int, default=100, help="Max total URLs to collect (default: 100)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output", help="Explicit path to output JSON file")
    
    args = parser.parse_args()

    # Resolve output path
    parsed = urlparse(args.url)
    domain = parsed.netloc or parsed.path.split('/')[0]
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        # Defaults to the script's directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filename = f"urls_seed_{domain.replace('.', '_')}.json"
        output_path = os.path.join(script_dir, filename)

    # Run extraction
    urls = asyncio.run(run_extraction(args.url, args))
    
    # Filter out generic notion.so links if we are crawling a subdomain
    if "notion.site" in args.url or ".notion.so" in args.url:
        target_domain = urlparse(args.url).netloc
        cleaned_urls = []
        for u in urls:
            # Keep subdomain urls and obvious social links
            if target_domain in u or "youtube.com" in u or "twitter.com" in u or "buymeacoffee.com" in u:
                cleaned_urls.append(u)
            elif "notion.so" not in u: # Keep other external links
                cleaned_urls.append(u)
        urls = cleaned_urls
    
    # Format output to match project structure
    results = [{
        "topic": f"Discovery: {args.url}",
        "query": [args.url],
        "urls": urls
    }]

    save_urls_json(output_path, results)
    print(f"\n✅  Successfully collected {len(urls)} URLs for {domain}.")
    print(f"📂  Results saved to: {output_path}")

if __name__ == "__main__":
    main()
