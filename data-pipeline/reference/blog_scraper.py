import os
import sys
import re
import asyncio
import aiohttp
import random
from urllib.parse import urlparse, urljoin
from datetime import datetime

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from utils import load_urls_json, resolve_materials_dir, slugify_filename


async def download_image(session, img_url, download_dir, base_url, index, topic_idx):
    try:
        if img_url.startswith('/'):
            img_url = urljoin(base_url, img_url)
            
        async with session.get(img_url, timeout=10) as response:
            if response.status == 200:
                content = await response.read()
                ext = os.path.splitext(urlparse(img_url).path)[1]
                if not ext or ext.lower() not in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
                    ext = '.jpg'
                    
                filename = f"t{topic_idx:02d}_m{index:02d}_img{random.randint(1000,9999)}{ext}"
                filepath = os.path.join(download_dir, filename)
                
                with open(filepath, 'wb') as f:
                    f.write(content)
                return filepath, img_url
    except Exception as e:
        pass
    return None, None

def clean_markdown(content):
    """Remove unwanted boilerplate content from the markdown."""
    patterns_to_remove = [
        r"## (\[\s*)?Table of contents.*?(?=##|$)",
        r"## (\[\s*)?Popular Categories.*?(?=##|$)",
        r"## (\[\s*)?Free Courses.*?(?=##|$)",
        r"## (\[\s*)?Flagship Programs.*?(?=##|$)",
        r"## (\[\s*)?Become an Author.*?(?=##|$)",
        r"## (\[\s*)?Generative AI Tools and Techniques.*?(?=##|$)",
        r"## (\[\s*)?Popular GenAI Models.*?(?=##|$)",
        r"## (\[\s*)?AI Development Frameworks.*?(?=##|$)",
        r"## (\[\s*)?Data Science Tools and Techniques.*?(?=##|$)",
        r"#### Login to continue reading and enjoy expert-curated content\.",
        r"Keep Reading for Free",
        r"Download Projects.*?\)\n",
        r"\[ Master Generative AI with 10\+ Real-world Projects in 2025!.*?\]\(.*?\)",
        r"\[ Master Generative AI.*?\]\(.*?\)",
        r"\[ Free Certification Courses.*?\]\(.*?\)",
        r"\[Interview Prep\].*?\[AIML Projects\]\(.*?\)\n",
        r"(Data Science Trainee|Data Scientist|Author) at Analytics Vidhya.*?(?=##|$)",
        r"📩 You can also reach out to me at.*?\n",
        r"\[ !\[.*?\]\(.*?\) \]\(https://www\.analyticsvidhya\.com/blog/author/.*?\)\n\[ .*? \]\(https://www\.analyticsvidhya\.com/blog/author/.*?\)",
        r"\[ ![A-Za-z ]+ \]\(https://www\.analyticsvidhya\.com/blog/author/.*?\)\s+Last Updated :.*?\n\s+\d+ min read\s+0",
        r"Source:\s*\n",
        r"!\[\]\(https://huggingface\.co/avatars/.*?\).*$",
        r"EditPreview.*?to comment",
        r"\[Sign up\].*?to comment",
        r"\[Team Article\]\(.*?\)\s*(Published.*?\n)?(.*?\[.*?Follow \]\(.*?\)[\s\n]*)*",
        r"\[Team Article\]\(.*?\)",
        r"\[\s*\]\(https://huggingface\.co/blog\)",
    ]
    
    cleaned_content = content
    cleaned_content = re.sub(r'\[Interview Prep\].*?\[AIML Projects\]\(.*?\)', '', cleaned_content, flags=re.DOTALL)
    
    for pattern in patterns_to_remove:
        cleaned_content = re.sub(pattern, "", cleaned_content, flags=(re.DOTALL | re.IGNORECASE | re.MULTILINE) if "^" in pattern else (re.DOTALL | re.IGNORECASE))
    
    cleaned_content = re.sub(r':\n\s+\* [a-z]\n:', '', cleaned_content)
    cleaned_content = re.sub(r'\* [a-z]\n:\n', '', cleaned_content)
    cleaned_content = re.sub(r'\n{3,}', '\n\n', cleaned_content)
    
    return cleaned_content.strip()

async def process_crawl_result(session, result, topic_dir, index, topic_idx):
    """Processes a single CrawlResult: downloads images, cleans markdown, and saves to file."""
    url = result.url
    if not result.success:
        print(f"  [FAILED] {url}: {getattr(result, 'error_message', 'Unknown Error')}")
        return False
        
    markdown_content = ""
    # Process markdown
    if hasattr(result, "markdown_v2") and result.markdown_v2:
        markdown_content = result.markdown_v2.fit_markdown or result.markdown_v2.raw_markdown
    elif hasattr(result, "markdown") and hasattr(result.markdown, "fit_markdown"):
        markdown_content = result.markdown.fit_markdown or result.markdown.raw_markdown
    elif hasattr(result, "markdown") and isinstance(result.markdown, str):
        markdown_content = result.markdown
        
    if not markdown_content:
        markdown_content = "Content missing or unable to parse."
    
    # Optional: Apply custom cleaning
    markdown_content = clean_markdown(markdown_content)
        
    images_downloaded = []
    
    # Access images from media property
    images = []
    if hasattr(result, 'media') and isinstance(result.media, dict):
        images = result.media.get("images", [])
        
    # Download up to 3 images per article in parallel
    image_tasks = []
    for img in images[:3]:
        img_url = img.get('src')
        if img_url:
            image_tasks.append(download_image(session, img_url, topic_dir, url, index, topic_idx))
            
    if image_tasks:
        download_results = await asyncio.gather(*image_tasks)
        for filepath, original_url in download_results:
            if filepath:
                images_downloaded.append((original_url, os.path.basename(filepath)))
            
    # Replace image URLs in markdown with local paths
    for original_url, local_filename in images_downloaded:
        markdown_content = markdown_content.replace(original_url, local_filename)
        
    title = result.metadata.get("title", "Unknown Title") if result.metadata else "Unknown Title"
    
    final_content = f"# {title}\n**Source:** {url}\n\n{markdown_content}\n"
    
    out_path = os.path.join(topic_dir, f"material{index:02d}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(final_content)
        
    print(f"  ✅ Saved {out_path} ({len(images_downloaded)} images attached)")
    return True

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--urls-file", help="Explicit path to urls.json")
    parser.add_argument("--topic-dir", help="Explicit parent directory for topicsXX folders")
    parser.add_argument("--topic-id", type=int, help="Override TOPIC_ID (useful for single topic run)")
    args = parser.parse_args()

    topic_id = args.topic_id or os.environ.get("TOPIC_ID", "01")
    # Ensure topic_id is at least 2 digits
    if isinstance(topic_id, str) and topic_id.isdigit():
        topic_id = int(topic_id)
    elif isinstance(topic_id, str):
        topic_id = 1 # Fallback
    
    materials_dir = resolve_materials_dir(args.date)
    
    if args.urls_file:
        urls_path = os.path.abspath(args.urls_file)
    elif os.environ.get("URLS_FILE"):
        urls_path = os.path.abspath(os.environ["URLS_FILE"])
    else:
        # Check topic-specific urls first, then root fallback
        topic_dir_name = f"topics{topic_id:02d}"
        topic_dir = os.path.join(materials_dir, topic_dir_name)
        topic_specific_urls = os.path.join(topic_dir, f"urls{topic_id:02d}.json")
        urls_path = topic_specific_urls if os.path.exists(topic_specific_urls) else os.path.join(materials_dir, "urls.json")

    topics = load_urls_json(urls_path)
    if not topics:
        print(f"❌ No topics found at {urls_path}")
        return

    print(f"Found {len(topics)} topic(s) in {os.path.basename(urls_path)}. Starting Crawl4AI optimized scraper.")
    
    # Base topic directory
    base_topic_dir = args.topic_dir if args.topic_dir else materials_dir

    # Reuse configurations
    browser_config = BrowserConfig(
        headless=True,
        viewport_width=1920,
        viewport_height=1080,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    md_generator = DefaultMarkdownGenerator()
    
    crawler_config = CrawlerRunConfig(
        page_timeout=60000,
        markdown_generator=md_generator,
        excluded_tags=["nav", "footer", "aside", "form", "script", "style", "header"],
        excluded_selector=", ".join([
            "#comments", ".comments-area", ".comment-list", ".comment-respond",
            "[id*='comment']", "[class*='comment']", "[class*='related']",
            "[class*='more-on']", "[class*='recommended']", "[class*='suggested']",
            "[class*='author-bio']", "[class*='author-box']", ".author-section",
            ".not-prose", "[class*='newsletter']", "[class*='subscribe']",
            "[class*='cta']", ".av-free-courses", ".pinnacle-program",
            ".popular-categories", ".footer-main", ".social-share",
            ".share-box", ".flash-strip", ".top-header-menu", ".mobile-menu",
        ]),
        remove_forms=True,
        remove_overlay_elements=True,
        exclude_external_links=True,
        exclude_social_media_links=True,
        cache_mode=CacheMode.BYPASS
    )
    
    async with AsyncWebCrawler(config=browser_config) as crawler:
        async with aiohttp.ClientSession() as session:
            for idx, topic in enumerate(topics, 1):
                # Use current run's topic_id if only 1 topic, otherwise use loop idx
                current_topic_id = int(topic_id) if len(topics) == 1 else idx
                
                title = topic["topic"]
                print(f"\n--- Processing Topic {current_topic_id:02d}: {title} ---")
                
                # Check for TOPIC_DIR env, otherwise construct it
                topic_dir = os.environ.get("TOPIC_DIR")
                if not topic_dir or len(topics) > 1:
                    topic_dir = os.path.join(base_topic_dir, f"topics{current_topic_id:02d}")
                
                os.makedirs(topic_dir, exist_ok=True)

                urls = topic["urls"]
                if not urls:
                    print("  No URLs for this topic.")
                    continue

                print(f"  Scraping {len(urls)} URLs concurrently...")
                
                # Perform concurrent scraping for this topic
                results = await crawler.arun_many(
                    urls=urls,
                    config=crawler_config,
                    max_concurrent=3  # Balance speed and rate-limiting
                )
                
                # Process each result in parallel (image downloads + saving)
                process_tasks = []
                for j, result in enumerate(results, 1):
                    process_tasks.append(process_crawl_result(session, result, topic_dir, j, current_topic_id))
                
                if process_tasks:
                    await asyncio.gather(*process_tasks)
                
                # Small delay between topics to be a good citizen
                if idx < len(topics):
                    print(f"  Cooling down after topic {idx}...")
                    await asyncio.sleep( random.uniform(2, 5))

if __name__ == "__main__":
    asyncio.run(main())
