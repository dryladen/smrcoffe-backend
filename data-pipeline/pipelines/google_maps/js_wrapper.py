from typing import Any

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
