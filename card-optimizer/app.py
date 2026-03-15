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

    quarters = CARDS["discover"]["quarterly_categories"]
    return jsonify({
        "quarters":             quarters,
        "current_quarter_num":  current_q_num,
        "days_until_rotation":  days_left,
    })


if __name__ == "__main__":
    # host="0.0.0.0" makes the app reachable from your phone on the same WiFi
    app.run(debug=True, port=5052, host="0.0.0.0")
