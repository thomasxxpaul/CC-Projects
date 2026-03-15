"""
deals_scraper.py

Scrapes cashbackmonitor.com for the top most-viewed stores and their
best available shopping portal rate today. Cached for 4 hours.

NOTE: This shows the *best rate across all portals*, not Chase-specific.
      For full per-portal breakdown, visit cashbackmonitor.com directly.
"""

import re
import time
import requests
from bs4 import BeautifulSoup

CACHE_TTL = 4 * 3600   # 4 hours
_cache = {"data": None, "ts": 0, "last_update": ""}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Stores relevant to everyday spending (filter out niche/sketchy ones)
RELEVANT_STORES = {
    "walmart", "ebay", "home depot", "target", "best buy", "lowe's",
    "amazon", "booking.com", "marriott", "sephora", "chewy", "apple store",
    "kohl's", "expedia", "nordstrom", "nike", "adidas", "cvs.com",
    "walgreens", "petsmart", "sam's club", "bloomingdale's", "gap",
    "old navy", "macy's", "ulta beauty", "hilton", "hyatt", "hotels.com",
    "rei", "wayfair", "under armour", "new balance", "reebok", "viator",
    "stubhub", "costco", "instacart", "doordash", "grubhub",
}


def get_deals(force_refresh=False):
    """
    Returns a list of deals scraped from cashbackmonitor.com homepage.
    Each deal: {store, rate, cbm_url, note, category}
    Results are cached for 4 hours.
    """
    now = time.time()
    if not force_refresh and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"], _cache["last_update"]

    try:
        resp = requests.get("https://www.cashbackmonitor.com/", headers=HEADERS, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        # Return stale cache if available, otherwise empty
        if _cache["data"]:
            return _cache["data"], _cache["last_update"] + " (stale)"
        return [], "unavailable"

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract "Last Full Update" timestamp from CBM
    last_update = ""
    for line in soup.get_text().split("\n"):
        if "Last Full Update" in line:
            last_update = line.strip()
            break

    # Parse the most-viewed stores table from plain-text
    # CBM homepage renders: integer row → store name → rate (for the first table)
    lines = [l.strip() for l in soup.get_text().split("\n") if l.strip()]

    deals = []
    i = 0
    in_top_stores = False
    stop_after = 50   # only parse first table (most-viewed), not the high-% spam table

    while i < len(lines) and len(deals) < stop_after:
        line = lines[i]

        # Detect we've entered the "Most Viewed Stores" section
        if "Most Viewed Stores" in line:
            in_top_stores = True
            i += 1
            continue

        # Stop when we hit the second table (high % stores are often VPNs/spam)
        if in_top_stores and "Stores Sorted by Rewards" in line:
            break

        if in_top_stores and re.match(r"^\d+$", line):
            store = lines[i + 1].strip() if i + 1 < len(lines) else ""
            rate  = lines[i + 2].strip() if i + 2 < len(lines) else ""

            if store and rate and not rate.isdigit():
                store_lower = store.lower()
                # Only include stores relevant to everyday shopping
                if any(rel in store_lower for rel in RELEVANT_STORES):
                    # Build cashbackmonitor URL for this store
                    slug = re.sub(r"[^a-z0-9]+", "-", store_lower).strip("-")
                    cbm_url = f"https://www.cashbackmonitor.com/cashback/{slug}/"

                    # Parse the rate — strip bonus/signup footnotes for display
                    rate_clean = re.sub(r"\s*\(.*?\)", "", rate).strip()
                    has_bonus  = bool(re.search(r"\(.*\*", rate))

                    # Categorise so we can show relevant card tip
                    category = categorise_store(store_lower)

                    deals.append({
                        "store":       store,
                        "rate":        rate_clean,
                        "rate_full":   rate,
                        "has_bonus":   has_bonus,
                        "cbm_url":     cbm_url,
                        "category":    category,
                    })
            i += 3
        else:
            i += 1

    _cache["data"]        = deals
    _cache["ts"]          = now
    _cache["last_update"] = last_update

    return deals, last_update


def categorise_store(store_name):
    """Maps a store name to our internal card-optimizer categories."""
    s = store_name.lower()
    if any(x in s for x in ["amazon"]):                          return "amazon"
    if any(x in s for x in ["walmart", "target", "sam's"]):     return "walmart"
    if any(x in s for x in ["whole foods", "instacart"]):        return "grocery"
    if any(x in s for x in ["home depot", "lowe"]):              return "home_improvement"
    if any(x in s for x in ["walgreen", "cvs", "rite aid"]):     return "drugstore"
    if any(x in s for x in ["booking", "marriott", "hilton",
                              "hyatt", "hotels", "expedia",
                              "viator"]):                         return "travel_other"
    if any(x in s for x in ["nike", "adidas", "under armour",
                              "reebok", "new balance"]):          return "everything_else"
    return "everything_else"
