import argparse
import asyncio
import csv
import logging
import os
import random
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

CSV_FIELDNAMES = [
    "name",
    "rating",
    "reviews",
    "category",
    "address",
    "website",
    "phone",
    "maps_link",
]

# ---------------------------------------------------------------------------
# Category normalisation map
# Maps keywords found in Google Maps category labels → a clean category name.
# Add more entries freely — matching is case-insensitive substring search.
# ---------------------------------------------------------------------------
keyword = ("cafe", "café", "coffee", "kopi", "espresso", "kafe", "kopitiam")


def normalise_category(raw_category: str) -> str:
    """
    Map Google Maps's raw category string to a standardised label.
    Falls back to 'Other' if no match is found.
    """
    if not raw_category:
        return ""
    lower = raw_category.strip().lower()
    for keywords, label in CATEGORY_MAP:
        for kw in keywords:
            if kw in lower:
                return label
    # Fallback: return 'Other' for any uncategorized business
    return "Other"


def extract_base_url(full_url: str) -> str:
    """
    Extract only the base URL (scheme + netloc), e.g.:
    https://www.henshinjakarta.com/menu/dinner → https://www.henshinjakarta.com

    Also unwraps Google redirect URLs like:
      /url?q=https%3A%2F%2Fplataran.com%2F&sa=D&...
      https://www.google.com/url?q=https%3A%2F%2F...&...
    """
    if not full_url:
        return ""
    try:
        # Normalise relative Google redirect → absolute so urlparse can handle it
        if full_url.startswith("/url?"):
            full_url = "https://www.google.com" + full_url

        parsed = urlparse(full_url)

        # Unwrap Google redirect (/url?q=REAL_URL)
        if parsed.netloc in ("www.google.com", "google.com") and parsed.path == "/url":
            qs = parse_qs(parsed.query)
            real = qs.get("q", [""])[0]
            if real:
                return extract_base_url(unquote(real))
            return ""

        if not parsed.scheme.startswith("http") or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def random_delay(min_ms=600, max_ms=1800):
    """Slightly shorter delays by default for speed."""
    delay = random.uniform(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)


def init_csv(filepath: str):
    """Create the CSV file with headers if it doesn't exist or is empty."""
    file_exists = os.path.isfile(filepath) and os.path.getsize(filepath) > 0
    if not file_exists:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
        logger.info(f"CSV initialized at: {filepath}")
    return filepath


def append_row_to_csv(filepath: str, row: dict):
    """Append a single data row to the CSV immediately after extraction."""
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writerow(row)


async def close_detail_pane(page):
    """
    Try multiple strategies to return to the search results list.
    Returns True if we successfully got back to the feed, False otherwise.
    """
    # Strategy 1: click Back button
    try:
        back_btn = page.locator('button[aria-label="Back"]')
        if await back_btn.count() > 0:
            await back_btn.first.click()
            await random_delay(800, 1500)
            if await page.locator('div[role="feed"]').count() > 0:
                return True
    except Exception:
        pass

    # Strategy 2: browser go_back
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=8000)
        await random_delay(800, 1500)
        if await page.locator('div[role="feed"]').count() > 0:
            return True
    except Exception:
        pass

    # Strategy 3: press Escape
    try:
        await page.keyboard.press("Escape")
        await random_delay(600, 1200)
        if await page.locator('div[role="feed"]').count() > 0:
            return True
    except Exception:
        pass

    return False


async def ensure_feed_visible(page, search_url: str):
    """Ensure the results feed is visible; navigate to search URL if needed."""
    if await page.locator('div[role="feed"]').count() > 0:
        return True
    logger.warning("Feed not visible, navigating back to search URL...")
    try:
        await page.goto(search_url, wait_until="domcontentloaded")
        await page.wait_for_selector('div[role="feed"]', timeout=15000)
        return True
    except Exception as e:
        logger.error(f"Could not recover feed: {e}")
        return False


async def scroll_feed_to_load_more(page, target_count: int, max_scrolls: int = 15):
    """
    Scroll the feed panel until we have at least `target_count` items,
    or we've scrolled `max_scrolls` times without growth.
    Returns the final element count.
    """
    stale = 0
    for _ in range(max_scrolls):
        current = await page.locator('a[href*="/maps/place/"]').count()
        if current >= target_count:
            break
        feed = page.locator('div[role="feed"]')
        try:
            await feed.hover()
            await page.mouse.wheel(0, 5000)
        except Exception:
            pass
        await random_delay(1500, 2500)
        new_count = await page.locator('a[href*="/maps/place/"]').count()
        if new_count == current:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
    return await page.locator('a[href*="/maps/place/"]').count()


async def extract_detail(page) -> dict:
    """Extract all detail fields from an already-open detail pane."""
    data = {
        "name": "",
        "rating": "",
        "reviews": "",
        "category": "",
        "address": "",
        "website": "",
        "phone": "",
        "maps_link": "",
    }

    # -----------------------------------------------------------------------
    # Name
    # Use the Google Maps detail-pane heading class (DUwDvf) to avoid picking
    # up the generic "Results" h1 that lives in the search sidebar.
    # -----------------------------------------------------------------------
    try:
        name_loc = page.locator("h1.DUwDvf")
        if await name_loc.count() > 0:
            data["name"] = (await name_loc.first.inner_text()).strip()
        else:
            # Fallback: iterate h1 elements, skip anything that looks like a
            # results-count header (pure digits / "Results" / very short text)
            all_h1 = page.locator("h1")
            for idx in range(await all_h1.count()):
                text = (await all_h1.nth(idx).inner_text()).strip()
                if text and text.lower() not in ("results", "") and not text.isdigit():
                    data["name"] = text
                    break
    except Exception:
        pass

    # Rating & reviews
    try:
        rating_elem = page.locator('div[aria-label*="stars"]')
        if await rating_elem.count() > 0:
            aria = await rating_elem.first.get_attribute("aria-label")
            if aria:
                data["rating"] = aria.split(" ")[0]
    except Exception:
        pass

    try:
        reviews_elem = page.locator('span[aria-label*="reviews"]')
        if await reviews_elem.count() > 0:
            aria = await reviews_elem.first.get_attribute("aria-label")
            if aria:
                data["reviews"] = aria.split(" ")[0].replace(",", "")
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # Category
    # Google Maps changed its DOM — try multiple selectors in priority order.
    # -----------------------------------------------------------------------
    try:
        raw_cat = ""

        # Priority 1: current class used for the category chip (2024-2026)
        cat_loc = page.locator("button.DkEaL")
        if await cat_loc.count() > 0:
            raw_cat = (await cat_loc.first.inner_text()).strip()

        # Priority 2: old jsaction attribute (kept as fallback)
        if not raw_cat:
            cat_loc2 = page.locator('button[jsaction*="pane.rating.category"]')
            if await cat_loc2.count() > 0:
                raw_cat = (await cat_loc2.first.inner_text()).strip()

        # Priority 3: aria-label on the category section
        if not raw_cat:
            cat_loc3 = page.locator('[aria-label*="Category"]')
            if await cat_loc3.count() > 0:
                aria = await cat_loc3.first.get_attribute("aria-label")
                raw_cat = aria.replace("Category:", "").strip() if aria else ""

        if raw_cat:
            data["category"] = normalise_category(raw_cat)
    except Exception:
        pass

    # Address
    try:
        addr_btn = page.locator('button[data-item-id="address"]')
        if await addr_btn.count() > 0:
            aria = await addr_btn.first.get_attribute("aria-label")
            if aria:
                data["address"] = aria.replace("Address:", "").strip()
            else:
                data["address"] = (await addr_btn.first.inner_text()).strip()
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # Website — store only the base URL (https://www.example.com)
    # Google Maps serves the href as a redirect: /url?q=https%3A%2F%2F...
    # extract_base_url() unwraps that automatically.
    # -----------------------------------------------------------------------
    try:
        website_href = ""

        # Priority 1: anchor with data-item-id=authority (most common)
        # Accept ANY non-empty href — extract_base_url handles redirect unwrap.
        web_loc = page.locator('a[data-item-id="authority"]')
        if await web_loc.count() > 0:
            # Try to get the real URL from aria-label first (fastest, no redirect)
            aria = await web_loc.first.get_attribute("aria-label")
            if aria:
                # aria-label is usually "Website: www.example.com" or just the domain
                domain_part = aria.split(":", 1)[-1].strip()
                if domain_part and "." in domain_part:
                    # Ensure scheme
                    if not domain_part.startswith("http"):
                        domain_part = "https://" + domain_part
                    website_href = domain_part

            # Fallback: use the href and let extract_base_url unwrap the redirect
            if not website_href:
                h = await web_loc.first.get_attribute("href")
                if h:
                    website_href = h  # /url?q=... or https://... both handled below

        # Priority 2: aria-label fallback on any website-labelled anchor
        if not website_href:
            web_loc2 = page.locator('a[aria-label*="website" i]')
            if await web_loc2.count() > 0:
                h = await web_loc2.first.get_attribute("href")
                if h:
                    website_href = h

        # Priority 3: div container with data-item-id=authority, grab child anchor
        if not website_href:
            web_loc3 = page.locator('div[data-item-id="authority"] a')
            if await web_loc3.count() > 0:
                h = await web_loc3.first.get_attribute("href")
                if h:
                    website_href = h

        data["website"] = extract_base_url(website_href)
    except Exception:
        pass

    # Phone
    try:
        phone_btn = page.locator('button[data-item-id*="phone:tel:"]')
        if await phone_btn.count() > 0:
            aria = await phone_btn.first.get_attribute("aria-label")
            if aria:
                data["phone"] = aria.replace("Phone:", "").strip()
            else:
                data["phone"] = (await phone_btn.first.inner_text()).strip()
    except Exception:
        pass

    # Maps link (current page URL)
    data["maps_link"] = page.url

    return data


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------


async def scrape_google_maps(keyword, city, show_browser, max_results, csv_path):
    search_query = f"{keyword} in {city}"
    safe_query = quote_plus(search_query)
    url = f"https://www.google.com/maps/search/{safe_query}?hl=en"

    results_count = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not show_browser,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        # Navigate to Google Maps search
        logger.info(f"Navigating to: {url}")
        await page.goto(url, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector('div[role="feed"]', timeout=20000)
        except PlaywrightTimeoutError:
            logger.error("Results feed not found. Maybe no results or blocked.")
            await browser.close()
            return 0

        # Accept cookies if dialog exists
        try:
            accept_button = page.locator("button:has-text('Accept all')")
            if await accept_button.count() > 0:
                await accept_button.first.click()
                await random_delay(600, 1200)
        except Exception:
            pass

        processed_keys: set[str] = set()
        stale_scroll_count = 0
        MAX_STALE_SCROLLS = 6
        processed_index = 0  # track which index we've already iterated past

        while results_count < max_results:
            # Make sure the feed is visible
            if not await ensure_feed_visible(page, url):
                logger.error("Cannot recover feed. Stopping.")
                break

            # ------------------------------------------------------------------
            # Step 1: Scroll to load enough items, starting from where we left off
            # ------------------------------------------------------------------
            target_load = processed_index + 10  # load at least 10 more than processed
            current_visible = await scroll_feed_to_load_more(page, target_load)
            logger.info(
                f"Visible: {current_visible} | Processed index: {processed_index} | Scraped: {results_count}"
            )

            elements = page.locator('a[href*="/maps/place/"]')
            total_elements = await elements.count()

            if total_elements <= processed_index:
                stale_scroll_count += 1
                logger.info(
                    f"No new elements loaded (stale #{stale_scroll_count}/{MAX_STALE_SCROLLS})"
                )
                if stale_scroll_count >= MAX_STALE_SCROLLS:
                    logger.info("Confirmed end of search results.")
                    break
                # Try a deeper scroll
                try:
                    feed = page.locator('div[role="feed"]')
                    await feed.hover()
                    await page.mouse.wheel(0, 8000)
                    await random_delay(3000, 4500)
                except Exception:
                    pass
                continue

            stale_scroll_count = 0
            new_this_round = 0

            # ------------------------------------------------------------------
            # Step 2: Process elements from processed_index onwards
            # ------------------------------------------------------------------
            for i in range(processed_index, total_elements):
                if results_count >= max_results:
                    break

                # Re-query to avoid stale handles
                elements = page.locator('a[href*="/maps/place/"]')
                if i >= await elements.count():
                    break

                elem = elements.nth(i)

                # Dedup by aria-label before clicking
                elem_label = await elem.get_attribute("aria-label") or ""
                dedup_key = elem_label.strip().lower()
                if dedup_key and dedup_key in processed_keys:
                    processed_index = i + 1
                    continue

                try:
                    await elem.scroll_into_view_if_needed()
                    await random_delay(400, 900)

                    await elem.click(force=True)

                    try:
                        await page.wait_for_selector("h1", timeout=10000)
                    except PlaywrightTimeoutError:
                        logger.warning(
                            f"Detail pane didn't open for item #{i}, skipping."
                        )
                        await close_detail_pane(page)
                        await ensure_feed_visible(page, url)
                        processed_index = i + 1
                        continue

                    await random_delay(1000, 2000)

                    # Extract all detail fields
                    data = await extract_detail(page)

                    # Secondary dedup by name + address
                    canonical_key = f"{data['name'].lower()}|{data['address'].lower()}"
                    if canonical_key in processed_keys:
                        logger.debug(f"Duplicate skipped: {data['name']}")
                        await close_detail_pane(page)
                        await ensure_feed_visible(page, url)
                        processed_index = i + 1
                        continue

                    if dedup_key:
                        processed_keys.add(dedup_key)
                    if canonical_key:
                        processed_keys.add(canonical_key)

                    logger.info(
                        f"[{results_count + 1}] {data['name']} | "
                        f"Cat: {data['category']} | "
                        f"Phone: {data['phone']} | "
                        f"Web: {data['website']}"
                    )

                    append_row_to_csv(csv_path, data)
                    results_count += 1
                    new_this_round += 1
                    processed_index = i + 1

                    # Return to list
                    returned = await close_detail_pane(page)
                    if not returned:
                        logger.warning(
                            "Could not return to feed naturally; re-navigating..."
                        )
                        await ensure_feed_visible(page, url)

                    await random_delay(700, 1400)

                except Exception as e:
                    logger.error(f"Error processing item #{i}: {e}")
                    await close_detail_pane(page)
                    await ensure_feed_visible(page, url)
                    await random_delay(800, 1500)
                    processed_index = i + 1
                    continue

            if new_this_round == 0 and total_elements <= processed_index:
                stale_scroll_count += 1
                logger.info(
                    f"Nothing new in this round (stale #{stale_scroll_count}/{MAX_STALE_SCROLLS})"
                )
                if stale_scroll_count >= MAX_STALE_SCROLLS:
                    logger.info("Confirmed end of search results (no new items).")
                    break

        await browser.close()

    return results_count


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Scrape business leads from Google Maps."
    )
    parser.add_argument(
        "--keyword",
        type=str,
        required=True,
        help='Business keyword to search for (e.g. "restaurants", "plumbers")',
    )
    parser.add_argument(
        "--city",
        type=str,
        required=True,
        help='Target city/location (e.g. "New York", "Jakarta")',
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show the browser interface (headed mode). Default: headless.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Maximum number of results to scrape (default: 50)",
    )
    args = parser.parse_args()

    logger.info(f"Starting scraping: '{args.keyword}' in '{args.city}'")

    safe_city = args.city.replace(" ", "_").lower()
    safe_key = args.keyword.replace(" ", "_").lower()
    filename = f"leads_{safe_key}_{safe_city}.csv"
    save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    init_csv(save_path)
    logger.info(f"Results will be written live to: {save_path}")

    try:
        total = asyncio.run(
            scrape_google_maps(
                args.keyword,
                args.city,
                args.show_browser,
                args.max_results,
                save_path,
            )
        )
        logger.info(f"Scraping complete. Total leads extracted: {total}")
        logger.info(f"Results saved to: {save_path}")

    except KeyboardInterrupt:
        logger.info(
            "\nScraping interrupted by user. Partial results already saved to CSV."
        )
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    main()
