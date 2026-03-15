"""
transfer_news.py

Fetches RSS feeds from travel points blogs and filters for articles
about transfer partner bonuses (Chase UR, Bilt, etc.).
Cached for 2 hours so it's fast on repeat loads.

Each article is enriched with bonus_pct + bonus_partner when the title
contains a detectable bonus offer (e.g. "30% Transfer Bonus to Hyatt").
"""

import re
import time
import requests
from xml.etree import ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

CACHE_TTL = 2 * 3600   # 2 hours
_cache = {"data": None, "ts": 0}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# RSS feeds to monitor — all public, no login needed
FEEDS = [
    {"url": "https://frequentmiler.com/feed/",          "source": "Frequent Miler"},
    {"url": "https://thepointsguy.com/feed/",           "source": "The Points Guy"},
    {"url": "https://millionmilesecrets.com/feed/",     "source": "Million Mile Secrets"},
]

# Keywords that indicate a transfer bonus article
BONUS_KEYWORDS = [
    "transfer bonus", "transfer partner bonus", "bonus miles transfer",
    "transfer your points", "chase transfer", "bilt transfer",
    "ultimate rewards transfer", "transfer to hyatt", "transfer to united",
    "transfer to southwest", "transfer to marriott", "transfer to ihg",
    "transfer to british airways", "transfer to air canada",
    "transfer to singapore", "transfer to flying blue",
    "transfer to world of hyatt", "transfer bonus offer",
    "limited time transfer", "points transfer bonus",
]

# Partners we care about (Chase UR + Bilt)
PARTNER_KEYWORDS = [
    "hyatt", "united", "southwest", "marriott", "ihg", "british airways",
    "air canada", "singapore airlines", "flying blue", "air france",
    "emirates", "turkish", "virgin atlantic", "jetblue", "thai airways",
    "chase ultimate rewards", "bilt", "transfer partner",
]

# Ordered list: (regex to match in title, display name) — first match wins
PARTNER_EXTRACT = [
    (r'\bworld of hyatt\b',       "World of Hyatt"),
    (r'\bhyatt\b',                "World of Hyatt"),
    (r'\bunited\b',               "United MileagePlus"),
    (r'\bsouthwest\b',            "Southwest"),
    (r'\bmarriott\b',             "Marriott Bonvoy"),
    (r'\bihg\b',                  "IHG"),
    (r'\bbritish airways\b',      "British Airways"),
    (r'\baeroplan\b',             "Air Canada Aeroplan"),
    (r'\bair canada\b',           "Air Canada Aeroplan"),
    (r'\bsingapore\b',            "Singapore KrisFlyer"),
    (r'\bflying blue\b',          "Flying Blue"),
    (r'\bair france\b',           "Flying Blue"),
    (r'\bemirates\b',             "Emirates Skywards"),
    (r'\bturkish\b',              "Turkish Miles&Smiles"),
    (r'\bvirgin atlantic\b',      "Virgin Atlantic"),
    (r'\bjetblue\b',              "JetBlue"),
    (r'\balaska\b',               "Alaska Airlines"),
    (r'\blifemiles\b',            "Avianca LifeMiles"),
    (r'\bamerican airlines\b',    "American AAdvantage"),
    (r'\baadvantage\b',           "American AAdvantage"),
]


def _extract_bonus_info(title):
    """
    Try to extract a bonus percentage and partner name from an article title.
    E.g. "30% Transfer Bonus to World of Hyatt" → ("30%", "World of Hyatt")
    Returns (bonus_pct_str, partner_name) — either may be None.
    """
    text = title.lower()

    # Match: "30%", "25 percent", "30% bonus"
    pct_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:%|percent)', text)
    bonus_pct = (pct_match.group(1) + "%") if pct_match else None

    bonus_partner = None
    for pattern, name in PARTNER_EXTRACT:
        if re.search(pattern, text):
            bonus_partner = name
            break

    return bonus_pct, bonus_partner


def _is_relevant(title, description):
    """Returns True if an article is about a transfer bonus for our cards."""
    text = (title + " " + description).lower()
    has_bonus   = any(kw in text for kw in BONUS_KEYWORDS)
    has_partner = any(kw in text for kw in PARTNER_KEYWORDS)
    return has_bonus or has_partner


def _parse_date(date_str):
    """Parse RSS pubDate string to a unix timestamp. Returns 0 on failure."""
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp())
    except Exception:
        return 0


def _fetch_feed(feed_info):
    """Fetch one RSS feed and return list of relevant articles."""
    articles = []
    try:
        resp = requests.get(feed_info["url"], headers=HEADERS, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception:
        return articles

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") or root.findall(".//atom:entry", ns)

    for item in items[:30]:
        title    = (item.findtext("title") or "").strip()
        link     = (item.findtext("link")  or "").strip()
        desc     = (item.findtext("description") or
                    item.findtext("content") or "").strip()
        date_str = (item.findtext("pubDate") or
                    item.findtext("published") or "")
        pub_ts   = _parse_date(date_str)

        if not link:
            link_el = item.find("link")
            if link_el is not None:
                link = link_el.get("href", "")

        snippet = re.sub(r"<[^>]+>", "", desc)[:200].strip()

        if title and _is_relevant(title, desc):
            # Search title + snippet so we catch bonuses mentioned in the body
            bonus_pct, bonus_partner = _extract_bonus_info(title + " " + snippet)
            articles.append({
                "title":         title,
                "url":           link,
                "snippet":       snippet,
                "source":        feed_info["source"],
                "pub_ts":        pub_ts,
                "pub_date":      datetime.fromtimestamp(pub_ts).strftime("%b %d")
                                 if pub_ts else "Recent",
                "bonus_pct":     bonus_pct,      # e.g. "30%" or None
                "bonus_partner": bonus_partner,  # e.g. "World of Hyatt" or None
            })

    return articles


def get_transfer_news(force_refresh=False):
    """
    Returns a list of recent articles about transfer partner bonuses.
    Cached for 2 hours.
    Each article: {title, url, snippet, source, pub_ts, pub_date, bonus_pct, bonus_partner}
    """
    now = time.time()
    if not force_refresh and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    all_articles = []
    for feed in FEEDS:
        all_articles.extend(_fetch_feed(feed))

    # Sort by date newest first; deduplicate by title
    seen   = set()
    unique = []
    for a in sorted(all_articles, key=lambda x: x["pub_ts"], reverse=True):
        key = a["title"].lower()[:60]
        if key not in seen:
            seen.add(key)
            unique.append(a)

    # Drop articles older than 21 days — transfer bonuses don't run that long
    cutoff = time.time() - 21 * 86400
    recent = [a for a in unique if a["pub_ts"] == 0 or a["pub_ts"] >= cutoff]

    result = recent[:20]
    _cache["data"] = result
    _cache["ts"]   = now
    return result
