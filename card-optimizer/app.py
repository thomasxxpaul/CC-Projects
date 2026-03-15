"""
card-optimizer/app.py

Tells you which credit card to use for each spending category
to maximize points / cashback.

Cards:
  - Chase Sapphire Preferred (CSP)
  - Chase Freedom Unlimited (CFU)
  - Discover it Cash Back
  - Bilt Mastercard

No external APIs — all reward data is hardcoded below.
To update rates when Chase or Discover changes them, edit the CARDS dict.
"""

from flask import Flask, jsonify, render_template, request, Response
from datetime import date
import base64
import socket
import io
from deals_scraper import get_deals
from transfer_news import get_transfer_news
try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

app = Flask(__name__)


# ── UPDATE RATES HERE ─────────────────────────────────────────────────────────
# When Discover announces new quarterly categories, update CARDS["discover"]
# When Chase changes a bonus category, update the "rules" list for that card.
# ─────────────────────────────────────────────────────────────────────────────

CARDS = {
    "csp": {
        "name": "Chase Sapphire Preferred",
        "short": "CSP",
        "color": "#1a73e8",
        # Points are worth ~2¢ each when transferred to travel partners
        # (e.g. United, Hyatt, Southwest). That's what makes CSP so powerful.
        "points_value_cents": 2.0,
        "currency": "points",
        "rules": [
            # Checked in order — first match wins
            {"categories": ["chase_travel"],  "rate": 5.0, "label": "5x via Chase Travel"},
            {"categories": ["dining"],        "rate": 3.0, "label": "3x Dining"},
            {"categories": ["streaming"],     "rate": 3.0, "label": "3x Streaming"},
            {"categories": ["online_grocery"],"rate": 3.0, "label": "3x Online Grocery"},
            {"categories": ["travel_other"],  "rate": 2.0, "label": "2x Travel"},
        ],
        "fallback": {"rate": 1.0, "label": "1x Everything Else"},
    },

    "cfu": {
        "name": "Chase Freedom Unlimited",
        "short": "CFU",
        "color": "#34a853",
        # Base: 1¢ per point as cashback.
        # Trifecta bonus: when paired with CSP, you can transfer CFU points
        # to CSP and redeem at 2¢ — making the 1.5x flat rate effectively 3%.
        "points_value_cents": 1.5,   # blended value assuming trifecta pairing
        "currency": "points",
        "rules": [
            {"categories": ["chase_travel"],  "rate": 5.0, "label": "5x via Chase Travel"},
            {"categories": ["dining"],        "rate": 3.0, "label": "3x Dining"},
            {"categories": ["drugstore"],     "rate": 3.0, "label": "3x Drugstores"},
        ],
        "fallback": {"rate": 1.5, "label": "1.5x Everything Else"},
    },

    "discover": {
        "name": "Discover it Cash Back",
        "short": "Discover",
        "color": "#ff6600",
        # Pure cashback — 1¢ per cent
        "points_value_cents": 1.0,
        "currency": "cashback",
        # Rotating 5% quarterly categories (up to $1,500/quarter, then 1%)
        # Update this list each January when Discover announces the new year's schedule.
        "quarterly_categories": [
            {
                "quarter": "Q1 2025",
                "label": "Grocery Stores, Fitness Clubs & Gyms",
                "months": [1, 2, 3],
                "categories": ["grocery", "fitness"],
                "rate": 5.0,
                "cap_dollars": 1500,
            },
            {
                "quarter": "Q2 2025",
                "label": "Gas Stations, EV Charging, Home Improvement, Streaming",
                "months": [4, 5, 6],
                "categories": ["gas", "home_improvement", "streaming"],
                "rate": 5.0,
                "cap_dollars": 1500,
            },
            {
                "quarter": "Q3 2025",
                "label": "Restaurants, Hotels, Wholesale Clubs",
                "months": [7, 8, 9],
                "categories": ["dining", "hotels", "wholesale"],
                "rate": 5.0,
                "cap_dollars": 1500,
            },
            {
                "quarter": "Q4 2025",
                "label": "Amazon.com, Walmart.com, Target.com",
                "months": [10, 11, 12],
                "categories": ["amazon", "walmart", "target"],
                "rate": 5.0,
                "cap_dollars": 1500,
            },
        ],
        "fallback": {"rate": 1.0, "label": "1% Everything Else"},
    },

    "bilt": {
        "name": "Bilt Mastercard",
        "short": "Bilt",
        "color": "#6c3fc4",
        # Bilt points transfer to the same airline/hotel partners as Chase (United,
        # Hyatt, American, etc.) and are generally valued at ~1.5-2¢ each.
        # Superpower: only card that earns points on RENT with zero transaction fee.
        # Note: Bilt removed the 5-transaction-per-month requirement in 2024.
        "points_value_cents": 1.7,   # conservative blended estimate
        "currency": "points",
        "rules": [
            {"categories": ["rent"],         "rate": 1.0, "label": "1x Rent (no fee!)"},
            {"categories": ["dining"],       "rate": 3.0, "label": "3x Dining"},
            {"categories": ["travel_other", "chase_travel"], "rate": 2.0, "label": "2x Travel"},
        ],
        "fallback": {"rate": 1.0, "label": "1x Everything Else"},
    },
}


# All spending categories shown in the dashboard
CATEGORIES = [
    {"id": "rent",             "label": "Rent",                            "icon": "🏠"},
    {"id": "dining",           "label": "Dining / Restaurants",           "icon": "🍽️"},
    {"id": "grocery",          "label": "Groceries (In-Store)",            "icon": "🛒"},
    {"id": "online_grocery",   "label": "Online Groceries",                "icon": "🛍️"},
    {"id": "gas",              "label": "Gas / EV Charging",               "icon": "⛽"},
    {"id": "chase_travel",     "label": "Travel (Chase Portal)",           "icon": "✈️"},
    {"id": "travel_other",     "label": "Travel (Hotels, Flights, Taxis)", "icon": "🏨"},
    {"id": "streaming",        "label": "Streaming Services",              "icon": "📺"},
    {"id": "drugstore",        "label": "Drugstores / Pharmacy",           "icon": "💊"},
    {"id": "amazon",           "label": "Amazon / Online Shopping",        "icon": "📦"},
    {"id": "walmart",          "label": "Walmart / Target",                "icon": "🏪"},
    {"id": "home_improvement", "label": "Home Improvement",                "icon": "🔨"},
    {"id": "fitness",          "label": "Fitness / Gym",                   "icon": "💪"},
    {"id": "everything_else",  "label": "Everything Else",                 "icon": "💳"},
]


# ── Cards you could ADD to your wallet ────────────────────────────────────────
# These are analyzed against your real spending to show which card adds the most value.
# "net_fee" = annual_fee minus the value of included credits you'd realistically use.
CANDIDATE_CARDS = {
    "amex_gold": {
        "name": "Amex Gold",
        "color": "#c9a227",
        "annual_fee": 325,
        "credits_value": 270,   # $120 dining credit + $120 Uber Cash + $84 Dunkin credit
        "net_fee": 55,          # $325 - $270 if you use all credits
        "points_value_cents": 2.0,
        "note": "Best card for dining + groceries. Transfer points to airlines/hotels at 2¢+.",
        "upgrade_from": None,
        "rules": [
            {"categories": ["dining"],                       "rate": 4.0, "label": "4x Dining"},
            {"categories": ["grocery", "online_grocery"],    "rate": 4.0, "label": "4x Supermarkets"},
            {"categories": ["chase_travel", "travel_other"], "rate": 3.0, "label": "3x Flights"},
        ],
        "fallback": {"rate": 1.0, "label": "1x Everything Else"},
    },

    "amex_bcp": {
        "name": "Amex Blue Cash Preferred",
        "color": "#007bc0",
        "annual_fee": 95,
        "credits_value": 84,    # $7/month streaming credit
        "net_fee": 11,
        "points_value_cents": 1.0,
        "note": "6% at US supermarkets (up to $6k/yr) and streaming — pure cashback.",
        "upgrade_from": None,
        "rules": [
            {"categories": ["grocery"],                         "rate": 6.0, "label": "6% Supermarkets"},
            {"categories": ["streaming"],                       "rate": 6.0, "label": "6% Streaming"},
            {"categories": ["gas", "travel_other"],             "rate": 3.0, "label": "3% Gas & Transit"},
        ],
        "fallback": {"rate": 1.0, "label": "1% Everything Else"},
    },

    "csr": {
        "name": "Chase Sapphire Reserve",
        "color": "#1a1a2e",
        "annual_fee": 550,
        "credits_value": 300,   # $300 travel credit
        "net_fee": 250,
        "points_value_cents": 2.0,
        "note": "Upgrades your CSP: 3x dining+travel, Priority Pass lounge access. Can't hold both Sapphires.",
        "upgrade_from": "csp",  # Replaces CSP — factors out CSP's contribution
        "rules": [
            {"categories": ["chase_travel"],                    "rate": 10.0, "label": "10x via Chase Travel"},
            {"categories": ["dining"],                          "rate": 3.0,  "label": "3x Dining"},
            {"categories": ["travel_other"],                    "rate": 3.0,  "label": "3x Travel"},
        ],
        "fallback": {"rate": 1.0, "label": "1x Everything Else"},
    },

    "venture_x": {
        "name": "Capital One Venture X",
        "color": "#c41230",
        "annual_fee": 395,
        "credits_value": 470,   # $300 travel credit + 10k anniversary miles (~$170)
        "net_fee": 0,           # Effectively free if you use the travel credit
        "points_value_cents": 1.7,
        "note": "Effectively $0 net fee if you book $300/yr via Capital One Travel. 2x on everything.",
        "upgrade_from": None,
        "rules": [
            {"categories": ["chase_travel", "travel_other"],    "rate": 10.0, "label": "10x Hotels & Cars"},
        ],
        "fallback": {"rate": 2.0, "label": "2x Everything"},
    },

    "wf_autograph": {
        "name": "Wells Fargo Autograph",
        "color": "#cc0000",
        "annual_fee": 0,
        "credits_value": 0,
        "net_fee": 0,
        "points_value_cents": 1.0,
        "note": "No annual fee. 3x on restaurants, travel, gas, transit, streaming, phone bills.",
        "upgrade_from": None,
        "rules": [
            {"categories": ["dining"],                          "rate": 3.0, "label": "3x Restaurants"},
            {"categories": ["chase_travel", "travel_other"],    "rate": 3.0, "label": "3x Travel"},
            {"categories": ["gas"],                             "rate": 3.0, "label": "3x Gas"},
            {"categories": ["streaming"],                       "rate": 3.0, "label": "3x Streaming"},
        ],
        "fallback": {"rate": 1.0, "label": "1x Everything Else"},
    },

    "citi_double": {
        "name": "Citi Double Cash",
        "color": "#003087",
        "annual_fee": 0,
        "credits_value": 0,
        "net_fee": 0,
        "points_value_cents": 1.0,
        "note": "No annual fee. Flat 2% on everything — best catch-all for spending not covered elsewhere.",
        "upgrade_from": None,
        "rules": [],
        "fallback": {"rate": 2.0, "label": "2% Everything"},
    },
}


# ── Core logic ────────────────────────────────────────────────────────────────

def get_rate(card_id, category_id):
    """
    Returns {rate, label, is_bonus} for a card + category combo.
    Discover auto-detects the current quarter from today's date.
    """
    card = CARDS[card_id]

    if card_id == "discover":
        month = date.today().month
        for q in card["quarterly_categories"]:
            if month in q["months"] and category_id in q["categories"]:
                return {"rate": q["rate"], "label": q["label"], "is_bonus": True}
        return {**card["fallback"], "is_bonus": False}

    for rule in card["rules"]:
        if category_id in rule["categories"]:
            return {"rate": rule["rate"], "label": rule["label"], "is_bonus": True}
    return {**card["fallback"], "is_bonus": False}


def get_winner(category_id):
    """
    Compares all cards by effective cents per dollar spent.
    Example: CSP earns 3x points worth 2¢ each = 6¢/dollar effective.
             Discover earns 5% cashback = 5¢/dollar.
             CSP wins.

    Special case — rent: Bilt always wins because other cards charge a
    2-3% transaction fee to process rent payments, turning any "earn"
    into a net loss. Bilt has zero fee.

    Returns (winner_card_id, full_results_dict).
    """
    # Bilt is the only viable card for rent — force it as winner
    if category_id == "rent":
        results = {}
        for card_id, card in CARDS.items():
            info = get_rate(card_id, category_id)
            effective_cents = info["rate"] * card["points_value_cents"] / 100
            note = ""
            if card_id != "bilt":
                # Other cards charge a ~2.5% processing fee on rent
                note = " (⚠️ ~2.5% fee applies — net loss)"
                effective_cents = 0  # treat as zero after fee
            results[card_id] = {
                "rate":            info["rate"],
                "label":           info["label"] + note,
                "is_bonus":        info.get("is_bonus", False),
                "effective_cents": round(effective_cents, 4),
                "card_name":       card["name"],
                "card_short":      card["short"],
                "card_color":      card["color"],
                "currency":        card["currency"],
            }
        return "bilt", results
    results = {}
    for card_id, card in CARDS.items():
        info = get_rate(card_id, category_id)
        effective_cents = info["rate"] * card["points_value_cents"] / 100
        results[card_id] = {
            "rate":            info["rate"],
            "label":           info["label"],
            "is_bonus":        info.get("is_bonus", False),
            "effective_cents": round(effective_cents, 4),
            "card_name":       card["name"],
            "card_short":      card["short"],
            "card_color":      card["color"],
            "currency":        card["currency"],
        }
    winner = max(results, key=lambda k: results[k]["effective_cents"])
    return winner, results


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    """PWA manifest — tells the phone how to display this as a home screen app."""
    return jsonify({
        "name": "Card Optimizer",
        "short_name": "Cards",
        "description": "Which card to use for every purchase",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f0f2f5",
        "theme_color": "#004182",
        "orientation": "portrait",
        "icons": [
            {"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.route("/icon.png")
def icon():
    """
    Serves a simple blue credit card icon as a PNG.
    Generated as an SVG rendered to a 1x1 pixel PNG placeholder —
    iOS uses the SVG-backed apple-touch-icon for the home screen.
    """
    # A minimal blue square PNG (1x1) — iOS will use the SVG fallback.
    # For a real icon, drop a 192x192 icon.png into card-optimizer/static/
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
      <rect width="192" height="192" rx="40" fill="#004182"/>
      <rect x="16" y="56" width="160" height="100" rx="14" fill="#0077b5"/>
      <rect x="16" y="88" width="160" height="24" fill="#005f8f"/>
      <rect x="28" y="118" width="60" height="10" rx="5" fill="white" opacity="0.7"/>
      <text x="96" y="82" font-family="Arial" font-size="28" font-weight="bold"
            fill="white" text-anchor="middle">💳</text>
    </svg>"""
    # Return SVG as image/svg+xml — works as apple-touch-icon on modern iOS
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/dashboard")
def dashboard():
    """All 13 categories with winner and per-card rates — loaded once on page open."""
    rows = []
    for cat in CATEGORIES:
        winner, results = get_winner(cat["id"])
        rows.append({
            "id":      cat["id"],
            "label":   cat["label"],
            "icon":    cat["icon"],
            "winner":  winner,
            "results": results,
        })
    return jsonify(rows)


@app.route("/api/calculate", methods=["POST"])
def calculate():
    """
    Given a dollar amount + category, returns how much you'd earn with each card.
    Body: {"amount": 100, "category": "dining"}
    """
    data       = request.json or {}
    amount     = float(data.get("amount", 0))
    cat_id     = data.get("category", "everything_else")
    winner, results = get_winner(cat_id)

    breakdown = []
    for card_id, info in results.items():
        card = CARDS[card_id]
        points_earned = round(amount * info["rate"], 1)
        dollar_value  = round(amount * info["effective_cents"], 2)
        breakdown.append({
            "card_id":      card_id,
            "card_name":    card["name"],
            "card_short":   card["short"],
            "card_color":   card["color"],
            "currency":     card["currency"],
            "rate":         info["rate"],
            "label":        info["label"],
            "points_earned": points_earned,
            "dollar_value":  dollar_value,
            "is_winner":     card_id == winner,
        })
    # Best card first
    breakdown.sort(key=lambda x: x["dollar_value"], reverse=True)
    return jsonify(breakdown)


@app.route("/api/discover-tracker")
def discover_tracker():
    """All four quarters + current quarter + days until next rotation."""
    today   = date.today()
    month   = today.month
    year    = today.year

    quarter_end_dates = {
        1: date(year, 3, 31),
        2: date(year, 6, 30),
        3: date(year, 9, 30),
        4: date(year, 12, 31),
    }
    current_q_num = (month - 1) // 3 + 1
    days_left     = (quarter_end_dates[current_q_num] - today).days

    # Replace hardcoded year in labels with the current year
    quarters = [
        {**q, "quarter": q["quarter"].replace("2025", str(year))}
        for q in CARDS["discover"]["quarterly_categories"]
    ]
    return jsonify({
        "quarters":             quarters,
        "current_quarter_num":  current_q_num,
        "days_until_rotation":  days_left,
    })


@app.route("/api/fee-calculator", methods=["POST"])
def fee_calculator():
    """
    Given monthly spend per category, compares:
      - Annual rewards WITH your current setup (CSP + CFU + Discover + Bilt)
      - Annual rewards WITHOUT CSP (only CFU at 1¢/pt baseline, Discover, Bilt)
    Shows whether CSP's $95/year fee is earning its keep.
    """
    data          = request.json or {}
    monthly_spend = data.get("spend", {})   # {category_id: dollars_per_month}
    CSP_FEE       = 95

    total_with    = 0.0
    total_without = 0.0
    rows          = []

    for cat in CATEGORIES:
        cat_id = cat["id"]
        amount = float(monthly_spend.get(cat_id, 0))
        if amount <= 0:
            continue

        # --- Scenario A: With CSP (existing get_winner logic) ---
        winner_with, results_with = get_winner(cat_id)
        cents_with  = results_with[winner_with]["effective_cents"]
        annual_with = round(amount * 12 * cents_with, 2)
        total_with += annual_with

        # --- Scenario B: Without CSP ---
        # CSP is removed, and CFU points drop to 1¢ each (no trifecta partner)
        if cat_id == "rent":
            winner_without = "bilt"
            cents_without  = 1.0 * CARDS["bilt"]["points_value_cents"] / 100
        else:
            best_ec        = 0.0
            winner_without = "cfu"
            for card_id, card in CARDS.items():
                if card_id == "csp":
                    continue
                pts_val = 1.0 if card_id == "cfu" else card["points_value_cents"]
                info    = get_rate(card_id, cat_id)
                ec      = info["rate"] * pts_val / 100
                if ec > best_ec:
                    best_ec        = ec
                    winner_without = card_id
            cents_without = best_ec

        annual_without  = round(amount * 12 * cents_without, 2)
        total_without  += annual_without

        rows.append({
            "cat_id":              cat_id,
            "cat_label":           cat["label"],
            "cat_icon":            cat["icon"],
            "monthly_spend":       amount,
            "winner_with":         winner_with,
            "winner_with_name":    CARDS[winner_with]["short"],
            "winner_without":      winner_without,
            "winner_without_name": CARDS[winner_without]["short"],
            "annual_with":         annual_with,
            "annual_without":      annual_without,
            "extra":               round(annual_with - annual_without, 2),
        })

    extra_from_csp = round(total_with - total_without, 2)
    net_benefit    = round(extra_from_csp - CSP_FEE, 2)

    return jsonify({
        "rows":           rows,
        "total_with":     round(total_with, 2),
        "total_without":  round(total_without, 2),
        "extra_from_csp": extra_from_csp,
        "csp_fee":        CSP_FEE,
        "net_benefit":    net_benefit,
        "worth_it":       net_benefit > 0,
    })


@app.route("/api/recommend", methods=["POST"])
def recommend():
    """
    Given annualised spending by category, compares each candidate card against
    the user's existing setup and returns recommendations ranked by net annual gain.

    Body: {"spend": {"dining": 4800, "grocery": 2400, ...}}
    spend values are already annualised dollar amounts.
    """
    data  = request.json or {}
    spend = data.get("spend", {})   # {category_id: annual_dollars}

    def card_rate(card_def, cat_id):
        """Get effective cents per dollar for a candidate card + category."""
        for rule in card_def["rules"]:
            if cat_id in rule["categories"]:
                return rule["rate"] * card_def["points_value_cents"] / 100
        fb = card_def["fallback"]
        return fb["rate"] * card_def["points_value_cents"] / 100

    # Current best cents per dollar with existing cards (per category)
    def current_best(cat_id):
        winner, results = get_winner(cat_id)
        return results[winner]["effective_cents"]

    results = []
    for cid, cdef in CANDIDATE_CARDS.items():
        extra_value = 0.0
        category_wins = []

        # If this card replaces an existing card, recalculate baseline without it
        baseline_cards = {k: v for k, v in CARDS.items()}
        if cdef["upgrade_from"]:
            baseline_cards = {k: v for k, v in CARDS.items() if k != cdef["upgrade_from"]}

        for cat_id, annual_dollars in spend.items():
            if annual_dollars <= 0:
                continue

            # Best rate with existing setup (possibly minus the replaced card)
            if cdef["upgrade_from"]:
                # Recompute winner without the card being replaced
                best_ec = 0.0
                for existing_id, existing_card in baseline_cards.items():
                    info = get_rate(existing_id, cat_id)
                    ec   = info["rate"] * existing_card["points_value_cents"] / 100
                    if ec > best_ec:
                        best_ec = ec
                baseline_ec = best_ec
            else:
                baseline_ec = current_best(cat_id)

            new_ec   = card_rate(cdef, cat_id)
            gain_ec  = max(0, new_ec - baseline_ec)
            gain_val = annual_dollars * gain_ec

            if gain_val > 0.5:   # only log meaningful wins (>50¢/yr on this category)
                cat_info = next((c for c in CATEGORIES if c["id"] == cat_id), None)
                category_wins.append({
                    "category":    cat_id,
                    "label":       cat_info["label"] if cat_info else cat_id,
                    "icon":        cat_info["icon"]  if cat_info else "💳",
                    "spend":       round(annual_dollars, 0),
                    "extra_cents": round(gain_ec * 100, 1),   # e.g. 2.5 = 2.5¢ more per $1
                    "extra_value": round(gain_val, 2),
                })
            extra_value += gain_val

        net_gain = extra_value - cdef["net_fee"]
        category_wins.sort(key=lambda x: x["extra_value"], reverse=True)

        results.append({
            "card_id":        cid,
            "name":           cdef["name"],
            "color":          cdef["color"],
            "annual_fee":     cdef["annual_fee"],
            "net_fee":        cdef["net_fee"],
            "credits_value":  cdef["credits_value"],
            "note":           cdef["note"],
            "upgrade_from":   cdef["upgrade_from"],
            "gross_extra":    round(extra_value, 2),
            "net_gain":       round(net_gain, 2),
            "top_categories": category_wins[:4],
        })

    results.sort(key=lambda x: x["net_gain"], reverse=True)
    return jsonify(results)


@app.route("/api/transfer-news")
def transfer_news():
    """
    Returns recent articles about transfer partner bonuses, scraped from
    travel blog RSS feeds (Frequent Miler, TPG, Million Mile Secrets). Cached 2 hours.
    Pass ?refresh=1 to force a fresh fetch.
    """
    force    = request.args.get("refresh") == "1"
    articles = get_transfer_news(force_refresh=force)
    return jsonify({"articles": articles, "cached": not force})


@app.route("/api/deals")
def deals():
    """
    Returns today's best shopping portal rates for popular stores,
    scraped from cashbackmonitor.com. Cached 4 hours.
    Pass ?refresh=1 to force a fresh scrape.
    """
    force = request.args.get("refresh") == "1"
    store_deals, last_update = get_deals(force_refresh=force)
    return jsonify({"deals": store_deals, "last_update": last_update})


@app.route("/qr.png")
def qr_code():
    """Returns a QR code PNG pointing to this app's local network URL."""
    if not HAS_QR:
        return Response("Install qrcode library: pip install qrcode[pil]", status=501, mimetype="text/plain")
    # Auto-detect the machine's local IP so the QR always points to the right address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    url = f"http://{local_ip}:5052"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(buf.read(), mimetype="image/png")


if __name__ == "__main__":
    # host="0.0.0.0" makes the app reachable from your phone on the same WiFi
    app.run(debug=True, port=5052, host="0.0.0.0")
