import argparse
import asyncio
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse

import httpx
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from ddgs import DDGS

from utils import (
    load_urls_json,
    parse_topics,
    resolve_materials_dir,
    save_urls_json,
    slugify_query,
)


# ---------------------------------------------------------------------------
# Source sites configuration
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "name": "machinelearningmastery",
        "strategy": "search",
        "search": "https://machinelearningmastery.com/?s={q}",
        "prefix": "https://machinelearningmastery.com/",
        "exclude": {"start-here", "products", "faq", "about", "contact",
                    "newsletter", "author", "tag", "category", "page",
                    "blog", "sitemap", "rss-feed", "feed", "disclaimer"},
    },
    {
        "name": "learnopencv",
        "strategy": "search",
        "search": "https://learnopencv.com/?s={q}",
        "prefix": "https://learnopencv.com/",
        "exclude": {"author", "tag", "category", "page", "courses",
                    "about", "contact", "privacy-policy", "terms-and-conditions",
                    "getting-started-with-opencv"},
    },
    {
        "name": "pyimagesearch",
        "strategy": "search",
        "search": "https://pyimagesearch.com/?s={q}",
        "prefix": "https://pyimagesearch.com/",
        "exclude": {"start", "about", "contact", "author", "tag",
                    "category", "page"},
    },
    {
        "name": "analyticsvidhya",
        "strategy": "sitemap",
        "sitemap_urls": [
            "https://www.analyticsvidhya.com/post-sitemap11.xml",
            "https://www.analyticsvidhya.com/post-sitemap10.xml",
            "https://www.analyticsvidhya.com/post-sitemap9.xml",
            "https://www.analyticsvidhya.com/post-sitemap8.xml",
            "https://www.analyticsvidhya.com/post-sitemap7.xml",
            "https://www.analyticsvidhya.com/post-sitemap6.xml",
        ],
        "prefix": "https://www.analyticsvidhya.com/blog/",
    },
    {
        "name": "huggingface_blog",
        "strategy": "sitemap",
        "sitemap_urls": [
            "https://huggingface.co/sitemap-blog.xml",
        ],
        "prefix": "https://huggingface.co/blog/",
    },
    {
        "name": "medium",
        "strategy": "search",
        "search": "https://medium.com/search?q={q}",
        "prefix": "https://medium.com/",
        "exclude": {"tag", "topic", "topics", "jobs", "about", "membership",
                    "new-story", "search", "m", "p", "sitemap"},
    },
    {
        "name": "towardsdatascience",   
        "strategy": "search",
        "search": "https://towardsdatascience.com/?s={q}",
        "prefix": "https://towardsdatascience.com/",
        "exclude": {"tag", "topic", "topics", "jobs", "about", "membership",
                    "new-story", "search", "m", "p", "sitemap", "page"},
    },
    {
        "name": "duckduckgo",
        "strategy": "search_engine",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def query_to_keywords(query: str) -> list[str]:
    """Extract meaningful lowercase keywords from a query string."""
    STOPWORDS = {"a", "an", "the", "and", "or", "in", "of", "to",
                 "for", "with", "on", "at", "by", "from", "is", "are"}
    words = re.findall(r"[a-zA-Z0-9]+", query.lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


def is_article(href: str, prefix: str, exclude: set) -> bool:
    """Return True if href looks like a genuine article for this source."""
    if not href.startswith(prefix):
        return False

    pure_url = href.split("?")[0].split("#")[0]
    parsed = urlparse(pure_url)
    path = parsed.path.strip("/")
    if not path:
        return False

    parts = path.split("/")
    first = parts[0].lower()

    if "medium.com" in prefix or "towardsdatascience.com" in prefix:
        if first in exclude:
            return False
        # Profiles like @username. 
        # If it's just /@username, it's a profile page (exclude).
        # If it's /@username/article-slug, it's an article (allow).
        if first.startswith("@"):
            return len(parts) > 1
        
        if ".xml" in pure_url:
            return False
        return True

    if first.isdigit() and len(first) == 4:
        return True

    return first not in exclude


def extract_links(result, prefix: str, exclude: set) -> list[str]:
    """Collect article-like links from a prefetch result."""
    found = []
    # Ensure base_url is a string for urljoin
    raw_url = getattr(result, "url", None)
    base_url = str(raw_url) if raw_url else prefix
    
    if hasattr(result, "links") and result.links:
        for group in ("internal", "external"):
            for link in result.links.get(group, []):
                href = link.get("href", "")
                if href:
                    # Resolve relative URLs
                    from urllib.parse import urljoin
                    full_url = urljoin(base_url, href)
                    
                    if is_article(full_url, prefix, exclude):
                        # Normalize: strip query params
                        found.append(full_url.split("?")[0].split("#")[0])
    return list(set(found))

# ---------------------------------------------------------------------------
# Sitemap-based URL extraction (strategy: "sitemap")
# ---------------------------------------------------------------------------

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _parse_sitemap_xml(xml_text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries

    for url_el in root.iter(f"{_SITEMAP_NS}url"):
        loc_el = url_el.find(f"{_SITEMAP_NS}loc")
        last_mod_el = url_el.find(f"{_SITEMAP_NS}lastmod")
        if loc_el is not None and loc_el.text:
            loc = loc_el.text.strip()
            lastmod = last_mod_el.text.strip() if last_mod_el is not None and last_mod_el.text else ""
            entries.append((loc, lastmod))
    return entries


async def fetch_sitemap_urls_async(
    client: httpx.AsyncClient,
    src: dict,
    query: str,
    max_per_source: int,
) -> list[str]:
    keywords = query_to_keywords(query)
    prefix: str = src["prefix"]
    all_entries: list[tuple[str, str]] = []

    for sitemap_url in src["sitemap_urls"]:
        try:
            resp = await client.get(sitemap_url, timeout=30)
            if resp.status_code == 200:
                entries = _parse_sitemap_xml(resp.text)
                all_entries.extend(entries)
        except Exception as e:
            print(f"  ⚠️  [{src['name']}] sitemap error: {e}")

    matched: list[tuple[str, str]] = []
    for loc, lastmod in all_entries:
        if not loc.startswith(prefix):
            continue
        slug = loc[len(prefix):].lower().replace("/", "-").replace("_", "-")
        if keywords and not any(kw in slug for kw in keywords):
            continue
        matched.append((loc, lastmod))

    matched.sort(key=lambda t: t[1], reverse=True)
    return [loc.split("?")[0].split("#")[0] for loc, _ in matched[:max_per_source]]


# ---------------------------------------------------------------------------
# Search engine based (DuckDuckGo)
# ---------------------------------------------------------------------------

def fetch_duckduckgo_urls(query: str, max_results: int) -> list[str]:
    """Blocking function to fetch DDG results, excluding social media."""
    EXCLUDED_DOMAINS = {
        "facebook.com", "x.com", "twitter.com", "linkedin.com", "instagram.com",
        "youtube.com", "reddit.com", "pinterest.com", "tiktok.com",
        "tumblr.com", "twitch.tv", "vimeo.com", "dict.leo.org", "wiktionary.org",
        "merriam-webster.com", "dictionary.com", "thesaurus.com", "collinsdictionary.com",
        "oxfordlearnersdictionaries.com", "cambridge.org", "britannica.com",
        "wikipedia.org", "en.wikipedia.org", "zhidao.baidu.com", "zhihu.com", "baidu.com"
    }
    try:
        # Increase results to provide more buffer for filtered domains
        # Using region 'wt-wt' (no region) for better global/technical relevance
        with DDGS() as ddgs:
            results = ddgs.text(query, region='wt-wt', max_results=max_results + 40)
            urls = []
            results_list = list(results)
            print(f"  🔍 [duckduckgo] Raw results found: {len(results_list)}")
            for r in results_list:
                href = r.get("href", "")
                if not href:
                    continue
                domain = urlparse(href).netloc.lower()
                # Skip social media
                if any(domain == d or domain.endswith("." + d) for d in EXCLUDED_DOMAINS):
                    continue
                urls.append(href)
            return urls[:max_results]
    except Exception as e:
        print(f"  ⚠️  [duckduckgo] error for query '{query}': {e}")
        return []


async def ensure_warp_connected():
    """Ensure Cloudflare WARP is connected to bypass blocking."""
    print("  🌐 Connecting to WARP...")
    try:
        # Check if already connected
        check = await asyncio.create_subprocess_exec(
            "warp-cli", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await check.communicate()
        if b"Status: Connected" in stdout:
            print("  🌐 WARP is already connected.")
            return

        proc = await asyncio.create_subprocess_exec(
            "warp-cli", "connect",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        await asyncio.sleep(5) # Wait for connection to stabilize
        
        # Verify connection
        check = await asyncio.create_subprocess_exec(
            "warp-cli", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await check.communicate()
        if b"Status: Connected" not in stdout:
            print("  ⚠️  WARP failed to connect. Results may be limited.")
        else:
            print("  ✅ WARP connected successfully.")
    except Exception as e:
        print(f"  ⚠️  WARP connect error: {e}")

async def fetch_duckduckgo_urls_async(query: str, max_results: int) -> list[str]:
    """Async wrapper for DDG fetch, with a larger discovery floor."""
    # Ensure we discovery at least 20 results for logging and variety, even if request is smaller
    discovery_limit = max(20, max_results)
    return await asyncio.to_thread(fetch_duckduckgo_urls, query, discovery_limit)


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------

async def run(topics: list[str], queries_per_topic: list[list[str]], max_urls: int) -> tuple[list[dict], list[dict]]:
    # Optimized browser config: disable images and media
    browser_cfg = BrowserConfig(
        headless=True, 
        viewport_width=1280, 
        viewport_height=900,
        extra_args=["--disable-gpu", "--disable-setuid-sandbox", "--no-sandbox", "--disable-dev-shm-usage"],
    )
    
    # SIMPLIFIED crawler config
    search_cfg = CrawlerRunConfig(
        remove_overlay_elements=True,
        page_timeout=30000,
        cache_mode=CacheMode.ENABLED
    )

    # Custom config for Medium with dynamic 'Show more' clicking
    medium_js = """
    async () => {
        for (let i = 0; i < 5; i++) {
            const buttons = Array.from(document.querySelectorAll('button'));
            const moreBtn = buttons.find(b => b.textContent.includes('Show more'));
            if (moreBtn) {
                console.log('Clicking "Show more"...');
                moreBtn.scrollIntoView();
                moreBtn.click();
                await new Promise(r => setTimeout(r, 2000));
            } else {
                break;
            }
        }
    }
    """
    medium_cfg = CrawlerRunConfig(
        remove_overlay_elements=True,
        page_timeout=60000,
        cache_mode=CacheMode.ENABLED,
        js_code=medium_js,
        wait_for="css:body"
    )

    # Collect all tasks to be performed
    search_tasks_info = [] # List of (topic_idx, query_idx, src_dict, search_url, max_per_source)
    sitemap_tasks_info = [] # List of (topic_idx, query_idx, src_dict, query_str, max_per_source)
    ddg_tasks_info = [] # List of (topic_idx, query_idx, src_dict, query_str, max_per_source)
    
    for t_idx, queries in enumerate(queries_per_topic):
        max_per_source = max(2, max_urls // max(len(queries), 1))
        for q_idx, q in enumerate(queries):
            q_slug = slugify_query(q)
            for src in SOURCES:
                if src["strategy"] == "search":
                    search_url = src["search"].format(q=q_slug)
                    search_tasks_info.append((t_idx, q_idx, src, search_url, max_per_source))
                elif src["strategy"] == "sitemap":
                    sitemap_tasks_info.append((t_idx, q_idx, src, q, max_per_source))
                elif src["strategy"] == "search_engine":
                    ddg_tasks_info.append((t_idx, q_idx, src, q, max_per_source))

    # Part 1: Concurrent Sitemap fetches
    print(f"🚀  Fetching sitemaps for {len(sitemap_tasks_info)} query/source pairs...")
    sitemap_results_map = {} # (t_idx, q_idx, src_name) -> [urls]
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True) as client:
        sitemap_tasks = [fetch_sitemap_urls_async(client, info[2], info[3], info[4]) for info in sitemap_tasks_info]
        sitemap_results = await asyncio.gather(*sitemap_tasks)
        for info, urls in zip(sitemap_tasks_info, sitemap_results):
            sitemap_results_map[(info[0], info[1], info[2]["name"])] = urls

    # Part 2: Batched Search crawls
    print(f"🚀  Performing batched search discovery for {len(search_tasks_info)} query/source pairs...")
    search_results_map = {} # (t_idx, q_idx, src_name) -> [urls]
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        # We need to run Medium separately if they have custom config
        medium_tasks = [info for info in search_tasks_info if info[2]["name"] == "medium"]
        other_tasks = [info for info in search_tasks_info if info[2]["name"] != "medium"]

        # Run Medium tasks with dynamic loading
        if medium_tasks:
            print(f"🚀  Crawling Medium with dynamic 'Show more' at max_per_source...")
            results_medium = await crawler.arun_many(
                urls=[info[3] for info in medium_tasks],
                config=medium_cfg, # Use medium_cfg with js_code
                max_concurrent=2   # Slower for JS interaction
            )
            for info, result in zip(medium_tasks, results_medium):
                if result.success:
                    links = extract_links(result, info[2]["prefix"], info[2]["exclude"])
                    search_results_map[(info[0], info[1], info[2]["name"])] = links
                else:
                    search_results_map[(info[0], info[1], info[2]["name"])] = []

        # Run others
        if other_tasks:
            results_others = await crawler.arun_many(
                urls=[info[3] for info in other_tasks],
                config=search_cfg,
                max_concurrent=10 # Increased from 5
            )
            for info, result in zip(other_tasks, results_others):
                if result.success:
                    links = extract_links(result, info[2]["prefix"], info[2]["exclude"])
                    search_results_map[(info[0], info[1], info[2]["name"])] = links
                else:
                    search_results_map[(info[0], info[1], info[2]["name"])] = []

    # Part 3: DuckDuckGo search engine fetches
    print(f"🚀  Performing DuckDuckGo searches for {len(ddg_tasks_info)} query/source pairs...")
    ddg_results_map = {} # (t_idx, q_idx, src_name) -> [urls]
    
    if ddg_tasks_info:
        await ensure_warp_connected()
        print(f"  🔍 DuckDuckGo: Processing {len(ddg_tasks_info)} searches in parallel (semaphore=3)...")
        
        sem = asyncio.Semaphore(3) # Limit parallel DDG searches
        
        async def bounded_fetch(info):
            async with sem:
                topic_idx, q_idx, src_dict, query_str, max_res = info
                try:
                    urls = await fetch_duckduckgo_urls_async(query_str, max_res)
                    ddg_results_map[(topic_idx, q_idx, src_dict["name"])] = urls
                    # Small delay still helpful to avoid IP bans
                    await asyncio.sleep(0.5) 
                except Exception as e:
                    print(f"  ⚠️  DDG search error for '{query_str}': {e}")
                    ddg_results_map[(topic_idx, q_idx, src_dict["name"])] = []

        await asyncio.gather(*(bounded_fetch(info) for info in ddg_tasks_info))
            

    # Part 3: Re-assemble and Interleave
    output = []
    all_raw_log = []
    
    for t_idx, topic in enumerate(topics):
        topic_urls = []
        seen = set()
        queries = queries_per_topic[t_idx]
        
        # 1. Collect all results for all queries/sources for this topic
        all_query_sources = [] # list of lists
        topic_raw_log = []
        
        for q_idx, q in enumerate(queries):
            query_source_map = {}
            for src in SOURCES:
                key = (t_idx, q_idx, src["name"])
                urls = (
                    sitemap_results_map.get(key) or 
                    search_results_map.get(key) or 
                    ddg_results_map.get(key) or 
                    []
                )
                query_source_map[src["name"]] = urls
                if urls:
                    all_query_sources.append(urls)
            
            topic_raw_log.append({"topic": topic, "query": q, "sources": query_source_map})
        
        all_raw_log.extend(topic_raw_log)

        # 2. Advanced Round-Robin Interleaving across ALL queries and sources
        if all_query_sources:
            max_len = max(len(lst) for lst in all_query_sources)
            for i in range(max_len):
                for lst in all_query_sources:
                    if i < len(lst):
                        u = lst[i]
                        if u not in seen and len(topic_urls) < max_urls:
                            seen.add(u)
                            topic_urls.append(u)
                if len(topic_urls) >= max_urls:
                    break

        output.append({
            "topic": topic,
            "query": queries,
            "urls": topic_urls  # Put ALL raw URLs here for the agent to filter
        })
        print(f"   ✅  {topic[:50]}...: {len(topic_urls)} raw URLs found")

    return output, all_raw_log


def main():
    parser = argparse.ArgumentParser(description="Crawl4AI URL Extractor")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--max-urls", type=int, default=50)
    parser.add_argument("--plan-file", help="Explicit path to plan.md")
    parser.add_argument("--urls-file", help="Explicit path to urls.json")
    parser.add_argument("--topic-id", help="Override TOPIC_ID (useful for single topic run)")
    args = parser.parse_args()

    # Resolve materials directory
    mat_dir = resolve_materials_dir(args.date)
    
    # Resolve topic ID (from arg or environment)
    topic_id = args.topic_id or os.environ.get("TOPIC_ID", "01")
    # Ensure topic_id is padded string
    if isinstance(topic_id, str) and topic_id.isdigit():
        topic_id = f"{int(topic_id):02d}"
    elif isinstance(topic_id, int):
        topic_id = f"{topic_id:02d}"
        
    topic_dir = os.environ.get("TOPIC_DIR") or os.path.join(mat_dir, f"topics{topic_id}")
    
    # Resolve plan file path
    if args.plan_file:
        plan_path = os.path.abspath(args.plan_file)
    elif os.environ.get("PLAN_FILE"):
        plan_path = os.path.abspath(os.environ["PLAN_FILE"])
    else:
        # Check topic-specific plan first, then root fallback
        topic_specific_plan = os.path.join(topic_dir, f"plan{topic_id}.md")
        plan_path = topic_specific_plan if os.path.exists(topic_specific_plan) else os.path.join(mat_dir, "plan.md")
        
    if not os.path.exists(plan_path):
        sys.exit(f"❌ plan file not found at {plan_path}")

    # Resolve output urls file path
    if args.urls_file:
        output_path = os.path.abspath(args.urls_file)
    elif os.environ.get("URLS_FILE"):
        output_path = os.path.abspath(os.environ["URLS_FILE"])
    else:
        # Check topic-specific urls first, then root fallback
        topic_specific_urls = os.path.join(topic_dir, f"urls{topic_id}.json")
        output_path = topic_specific_urls if os.path.exists(topic_specific_urls) else os.path.join(mat_dir, "urls.json")
        
    topics = parse_topics(plan_path)
    if not topics:
        sys.exit("❌ No topics found in plan.md")

    existing: list[dict] = load_urls_json(output_path)
    existing_map = {e["topic"]: e.get("query", []) for e in existing}
    queries_per_topic = []
    for t in topics:
        q = existing_map.get(t, [t])
        # If query is a single string, convert to list
        if isinstance(q, str):
            q = [q]
        queries_per_topic.append(q)

    print(f"\n🚀  Starting Optimized URL Extraction...")
    print(f"   Plan: {plan_path}")
    print(f"   URLs: {output_path}")
    
    results, all_crawled = asyncio.run(run(topics, queries_per_topic, args.max_urls))

    debug_path = os.path.join(os.path.dirname(output_path), "temp_all_crawled_urls.json")
    save_urls_json(debug_path, all_crawled)
    save_urls_json(output_path, results)
    print(f"\n✅  Saved → {output_path}")

if __name__ == "__main__":
    main()
