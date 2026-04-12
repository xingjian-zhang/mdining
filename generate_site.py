#!/usr/bin/env python3
"""
Generate a static multilingual menu website for Michigan Dining.

Scrapes all dining halls, translates item names to multiple languages, and outputs
a single self-contained index.html with embedded CSS and minimal JS.

Usage:
    python generate_site.py                # Generate site/index.html
    python generate_site.py --output out   # Custom output directory
    python generate_site.py --no-translate # Skip translation API calls
"""

import argparse
import glob
import html as html_mod
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from scraper import DINING_HALLS, fetch_menu

# Firebase Realtime Database config for dish ratings (optional).
# Set the FIREBASE_CONFIG env var as a JSON string, e.g.:
#   export FIREBASE_CONFIG='{"apiKey":"...","authDomain":"...","databaseURL":"...","projectId":"..."}'
# Or edit this dict directly. To set up:
#   1. Create a Firebase project at https://console.firebase.google.com
#   2. Add a Web App, copy the config object
#   3. Enable Realtime Database, set rules (see below)
# Recommended security rules for "ratings" node:
#   { "rules": { "ratings": { "$item": {
#       ".read": true,
#       "votes": { "$uid": {
#           ".write": "$uid === auth.uid",
#           ".validate": "newData.isString() && (newData.val() === 'up' || newData.val() === 'down')"
#       }}
#   }}}}
# Also enable Anonymous Auth in Firebase Console → Authentication → Sign-in method.
_fb_env = os.environ.get("FIREBASE_CONFIG", "{}")
try:
    FIREBASE_CONFIG = json.loads(_fb_env) if _fb_env else {}
except json.JSONDecodeError:
    FIREBASE_CONFIG = {}

SUPPORTED_LANGUAGES = {
    "zh-CN": {"name": "中文(简体)", "google_code": "zh-CN"},
    "zh-TW": {"name": "中文(繁體)", "google_code": "zh-TW"},
    "ko": {"name": "한국어", "google_code": "ko"},
    "ja": {"name": "日本語", "google_code": "ja"},
    "es": {"name": "Español", "google_code": "es"},
    "pt": {"name": "Português", "google_code": "pt"},
}

MEAL_NAMES = {
    "zh-CN": {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "brunch": "早午餐"},
    "zh-TW": {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "brunch": "早午餐"},
    "ko": {"breakfast": "아침식사", "lunch": "점심식사", "dinner": "저녁식사", "brunch": "브런치"},
    "ja": {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "brunch": "ブランチ"},
    "es": {"breakfast": "Desayuno", "lunch": "Almuerzo", "dinner": "Cena", "brunch": "Brunch"},
    "pt": {"breakfast": "Café da manhã", "lunch": "Almoço", "dinner": "Jantar", "brunch": "Brunch"},
}

# Google Maps embed queries for each dining hall
HALL_MAP_QUERIES = {
    "bursley": "Bursley+Dining+Hall,+1931+Duffield+St,+Ann+Arbor,+MI+48109",
    "east-quad": "East+Quad+Dining+Hall,+701+E+University,+Ann+Arbor,+MI+48109",
    "mosher-jordan": "Mosher-Jordan+Dining+Hall,+200+Observatory,+Ann+Arbor,+MI+48109",
    "south-quad": "South+Quad+Dining+Hall,+600+E+Madison,+Ann+Arbor,+MI+48109",
    "twigs-at-oxford": "Twigs+at+Oxford,+619+Oxford+Rd,+Ann+Arbor,+MI+48109",
}

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

TRAIT_DISPLAY = {
    # trait: (emoji, cn_label, en_label, css_class)
    # trait: (label, css_class)
    "Vegan": ("V", "vegan"),
    "Vegetarian": ("VG", "vegetarian"),
    "Gluten Free": ("GF", "gluten-free"),
    "Halal": ("H", "halal"),
    "Kosher": ("K", "kosher"),
    "Spicy": ("Spicy", "spicy"),
    "Carbon Footprint Low": ("Low CO₂", "carbon-low"),
    "Carbon Footprint Medium": ("", "carbon-med"),
    "Carbon Footprint High": ("High CO₂", "carbon-high"),
    "Nutrient Dense High": ("Nutritious", "nutri-high"),
    "Nutrient Dense Medium High": ("Nutritious", "nutri-medhigh"),
    "Nutrient Dense Medium": ("", "nutri-med"),
    "Nutrient Dense Low Medium": ("", "nutri-lowmed"),
    "Nutrient Dense Low": ("", "nutri-low"),
}



# Keyword rules for meat type detection (checked against lowercased item name)
MEAT_RULES = [
    ("Beef", ["beef", "steak", "burger"]),
    ("Pork", ["pork", "bacon", "ham", "kielbasa", "sausage"]),
    ("Chicken", ["chicken"]),
    ("Turkey", ["turkey"]),
    ("Lamb", ["lamb"]),
    ("Fish", ["fish", "salmon", "tuna", "cod", "tilapia", "mahi"]),
    ("Seafood", ["shrimp", "lobster", "crab", "clam", "mussel", "oyster", "scallop", "calamari"]),
]

# Items where keyword match is a false positive (not actually that meat)
MEAT_FALSE_POSITIVES = {
    "hamburger buns", "steak fries", "steak sauce",
}

def _meat_keyword_match(name_lower: str, keyword: str) -> bool:
    """Check if keyword matches meaningfully in the item name.

    Matches whole words and compound words (e.g. 'fish' in 'catfish',
    'burger' in 'cheeseburger', 'steak' in 'cheesesteak') but not
    'ham' in 'hamburger' where the keyword is a prefix of a larger
    unrelated word.
    """
    # Keywords that are too short and prone to prefix false-positives
    # are checked with a suffix/whole-word pattern instead.
    PREFIX_TRAPS = {"ham", "cod"}
    if keyword in PREFIX_TRAPS:
        # Must be a whole word: "ham" but not "hamburger"
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', name_lower))
    # For other keywords, allow suffix compounds (catfish, cheeseburger)
    # but require word boundary at the end
    return bool(re.search(re.escape(keyword) + r's?\b', name_lower))

SITE_DIR = "site"


def detect_meat_type(name: str, traits: list[str] | None = None) -> str | None:
    """Detect meat type from item name using keyword rules.

    Returns None if the item's API traits indicate Vegan/Vegetarian,
    or if the name is a known false positive.
    """
    # Trust the API: vegan/vegetarian items are not meat
    if traits:
        for t in traits:
            if t in ("Vegan", "Vegetarian"):
                return None
    low = name.lower()
    # Explicit false-positive list
    if low in MEAT_FALSE_POSITIVES:
        return None
    # Exclude compound-word false positives
    if "oyster cracker" in low or "oyster sauce" in low:
        return None
    for label, keywords in MEAT_RULES:
        if any(_meat_keyword_match(low, kw) for kw in keywords):
            return label
    return None


def fetch_all_halls(menu_date: str | None = None) -> list[dict]:
    """Fetch menus from all halls concurrently. Returns list of menu dicts."""
    results = []
    with ThreadPoolExecutor(max_workers=len(DINING_HALLS)) as pool:
        futures = {
            pool.submit(fetch_menu, hall, menu_date): hall
            for hall in DINING_HALLS
        }
        for future in as_completed(futures):
            hall = futures[future]
            try:
                data = future.result()
                results.append(data)
            except Exception as e:
                print(f"  Warning: Failed to fetch {hall}: {e}", file=sys.stderr)
                # Add empty entry so the hall still shows up
                results.append({
                    "hall": hall,
                    "date": menu_date or datetime.now().strftime("%Y-%m-%d"),
                    "meals": {},
                })
    # Sort by hall order to keep consistent
    hall_order = {h: i for i, h in enumerate(DINING_HALLS)}
    results.sort(key=lambda d: hall_order.get(d["hall"], 99))
    return results


def collect_unique_names(all_menus: list[dict]) -> list[str]:
    """Collect all unique item names across all halls/meals."""
    seen = set()
    names = []
    for menu in all_menus:
        for stations in menu.get("meals", {}).values():
            for items in stations.values():
                for item in items:
                    name = item["name"]
                    if name not in seen:
                        seen.add(name)
                        names.append(name)
    return names


def load_all_data_files(data_dir: str = "data") -> list[list[dict]]:
    """Load all saved JSON data files, returning list of per-date menu lists."""
    all_days = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        with open(path) as f:
            all_days.append(json.load(f))
    return all_days


def compute_item_stats(all_days: list[list[dict]]) -> dict[str, dict]:
    """Compute per-item stats (count, last_seen, halls) from saved daily data."""
    stats: dict[str, dict] = {}
    for day_menus in all_days:
        if not day_menus:
            continue
        day_date = day_menus[0].get("date", "")
        for hall_menu in day_menus:
            hall = hall_menu.get("hall", "")
            for meal_stations in hall_menu.get("meals", {}).values():
                for items in meal_stations.values():
                    for item in items:
                        name = item["name"]
                        if name not in stats:
                            stats[name] = {"count": 0, "last_seen": "", "halls": set()}
                        stats[name]["count"] += 1
                        if day_date > stats[name]["last_seen"]:
                            stats[name]["last_seen"] = day_date
                        stats[name]["halls"].add(hall)
    # Convert sets to sorted lists for JSON-friendliness
    for v in stats.values():
        v["halls"] = sorted(v["halls"])
    return stats


def compute_chart_data(all_menus: list[dict]) -> dict:
    """Compute data for insights charts (scatter per hall per meal)."""
    # Structure: {hall_slug: {meal_key: [points]}}
    hall_scatter: dict[str, dict[str, list[dict]]] = {}

    for menu in all_menus:
        hall = menu["hall"]
        if hall not in hall_scatter:
            hall_scatter[hall] = {}
        for meal_key, stations in menu.get("meals", {}).items():
            # Normalize brunch to lunch (matches the meal toggle)
            tab_key = "lunch" if meal_key == "brunch" else meal_key
            if tab_key not in hall_scatter[hall]:
                hall_scatter[hall][tab_key] = []
            for items in stations.values():
                for item in items:
                    traits = item.get("traits", [])
                    nutrition = item.get("nutrition", {})
                    cal = nutrition.get("calories")
                    protein_raw = nutrition.get("protein", "")
                    if cal is not None and protein_raw:
                        try:
                            cal_val = int(str(cal).replace(",", ""))
                            protein_val = float(str(protein_raw).replace("g", "").strip())
                        except (ValueError, TypeError):
                            continue
                        if "Vegan" in traits:
                            cat = "vegan"
                        elif "Vegetarian" in traits:
                            cat = "vegetarian"
                        else:
                            cat = "other"
                        point = {
                            "x": cal_val, "y": protein_val,
                            "n": item["name"], "c": cat,
                        }
                        fat = nutrition.get("total_fat", "")
                        carbs = nutrition.get("total_carbohydrate", "")
                        serving = nutrition.get("serving_size", "")
                        if fat:
                            point["fat"] = str(fat)
                        if carbs:
                            point["carbs"] = str(carbs)
                        if serving:
                            point["srv"] = str(serving)
                        display_traits = []
                        for t in traits:
                            info = TRAIT_DISPLAY.get(t)
                            if info and info[0]:
                                display_traits.append({"l": info[0], "cls": info[1]})
                        if display_traits:
                            point["t"] = display_traits
                        hall_scatter[hall][tab_key].append(point)

    # Flag ~10 landmark items per hall+meal for scatter labels
    for hall in hall_scatter.values():
        for points in hall.values():
            if not points:
                continue
            labeled: set[int] = set()
            labeled_names: set[str] = set()

            def _add(idx: int) -> None:
                if points[idx]["n"] not in labeled_names:
                    labeled.add(idx)
                    labeled_names.add(points[idx]["n"])

            # Top 3 by protein
            for i in sorted(range(len(points)), key=lambda i: points[i]["y"], reverse=True):
                if len(labeled) >= 3:
                    break
                _add(i)
            # Top 3 by calories
            for i in sorted(range(len(points)), key=lambda i: points[i]["x"], reverse=True):
                if len(labeled) >= 6:
                    break
                _add(i)
            # Top 2 by protein/calorie ratio (min 5g protein)
            eligible = [i for i in range(len(points)) if points[i]["y"] >= 5 and points[i]["x"] > 0]
            for i in sorted(eligible, key=lambda i: points[i]["y"] / points[i]["x"], reverse=True):
                if len(labeled) >= 8:
                    break
                _add(i)
            # Top 2 by lowest calories (min 2g protein to skip condiments)
            eligible = [i for i in range(len(points)) if points[i]["y"] >= 2]
            for i in sorted(eligible, key=lambda i: points[i]["x"]):
                if len(labeled) >= 10:
                    break
                _add(i)
            for i in labeled:
                points[i]["lbl"] = 1

    return {"scatter": hall_scatter}


def translate_names(names: list[str], target_lang: str) -> dict[str, str]:
    """Translate item names to target language using Google Translate (free)."""
    if not names:
        return {}

    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("  Warning: deep-translator not installed, skipping translation", file=sys.stderr)
        return {}

    translator = GoogleTranslator(source="en", target=target_lang)
    translations = {}
    # Google Translate supports batch via translate_batch (max ~5000 chars)
    BATCH_SIZE = 50
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        if len(names) > BATCH_SIZE:
            print(f"    Batch {i // BATCH_SIZE + 1}/{(len(names) + BATCH_SIZE - 1) // BATCH_SIZE}...")
        try:
            results = translator.translate_batch(batch)
            for name, translated in zip(batch, results):
                if translated:
                    translations[name] = translated
        except Exception as e:
            print(f"  Warning: Translation batch failed ({e})", file=sys.stderr)
            # Fallback: translate one by one
            for name in batch:
                try:
                    translated = translator.translate(name)
                    if translated:
                        translations[name] = translated
                except Exception:
                    pass

    return translations


def load_translation_cache(cache_path: str) -> dict[str, dict[str, str]]:
    """Load cached translations from JSON file.

    Returns nested dict: {english_name: {lang_code: translation}}.
    Migrates old flat format {english_name: chinese_translation} automatically.
    """
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        if not data:
            return {}
        # Migrate old flat format: if any value is a string, wrap in {"zh-CN": value}
        first_val = next(iter(data.values()))
        if isinstance(first_val, str):
            migrated = {k: {"zh-CN": v} for k, v in data.items()}
            save_translation_cache(cache_path, migrated)
            return migrated
        return data
    return {}


def save_translation_cache(cache_path: str, cache: dict[str, dict[str, str]]):
    """Save translations cache to JSON file."""
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def translate_with_cache(names: list[str], cache_path: str,
                         languages: dict | None = None) -> dict[str, dict[str, str]]:
    """Translate names into all supported languages using cache.

    Returns nested dict: {english_name: {lang_code: translation}}.
    """
    if languages is None:
        languages = SUPPORTED_LANGUAGES
    cache = load_translation_cache(cache_path)

    total_new = 0
    for lang_code, lang_info in languages.items():
        # Find names missing translation for this language
        new_names = [n for n in names if lang_code not in cache.get(n, {})]
        if not new_names:
            continue
        total_new += len(new_names)
        print(f"  Translating {len(new_names)} items to {lang_info['name']}...")
        new_translations = translate_names(new_names, lang_info["google_code"])
        for name, translated in new_translations.items():
            if name not in cache:
                cache[name] = {}
            cache[name][lang_code] = translated

    if total_new:
        save_translation_cache(cache_path, cache)
        print(f"  Translated {total_new} new item-language pairs.")
    else:
        print(f"  All {len(names)} items cached for {len(languages)} languages.")

    return cache


def fetch_hall_reviews(api_key: str) -> dict[str, dict]:
    """Fetch Google Maps reviews for all dining halls using the Places API (New).

    Uses the v1 Text Search endpoint (POST) with field masks.
    Returns a dict mapping hall slug to review data:
    {hall: {"rating": float, "total_ratings": int, "reviews": [...]}}
    """
    if not api_key:
        return {}

    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.rating,places.userRatingCount,places.reviews.authorAttribution.displayName,places.reviews.rating,places.reviews.text,places.reviews.relativePublishTimeDescription,places.reviews.publishTime",
    }

    results = {}
    for i, (hall, query) in enumerate(HALL_MAP_QUERIES.items()):
        if i > 0:
            time.sleep(0.5)  # Rate limit between halls
        try:
            resp = requests.post(url, headers=headers, json={
                "textQuery": query.replace("+", " "),
                "pageSize": 1,
            }, timeout=10)
            resp.raise_for_status()
            places = resp.json().get("places", [])
            if not places:
                print(f"  Warning: No place found for {hall}", file=sys.stderr)
                continue
            place = places[0]

            # Sort by publishTime (newest first), keep only reviews with text
            raw_reviews = place.get("reviews", [])
            raw_reviews.sort(key=lambda r: r.get("publishTime", ""), reverse=True)
            reviews = []
            for r in raw_reviews:
                text = r.get("text", {}).get("text", "")
                if not text:
                    continue
                author_attr = r.get("authorAttribution", {})
                reviews.append({
                    "author": author_attr.get("displayName", "Anonymous"),
                    "rating": r.get("rating", 0),
                    "text": text,
                    "time": r.get("relativePublishTimeDescription", ""),
                })
                if len(reviews) >= 3:
                    break

            results[hall] = {
                "rating": place.get("rating", 0),
                "total_ratings": place.get("userRatingCount", 0),
                "reviews": reviews,
                "fetched": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception as e:
            print(f"  Warning: Failed to fetch reviews for {hall}: {e}", file=sys.stderr)

    return results


def load_reviews_cache(cache_path: str) -> dict[str, dict]:
    """Load cached reviews from JSON file."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_reviews_cache(cache_path: str, cache: dict[str, dict]):
    """Save reviews cache to JSON file."""
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def fetch_reviews_with_cache(api_key: str, cache_path: str) -> dict[str, dict]:
    """Fetch reviews using cache. Re-fetches if cache is older than 7 days."""
    cache = load_reviews_cache(cache_path)
    today_dt = datetime.strptime(datetime.now().strftime("%Y-%m-%d"), "%Y-%m-%d")

    # Find halls that are missing or stale (>= 7 days old)
    stale_halls = []
    for hall in HALL_MAP_QUERIES:
        hall_data = cache.get(hall)
        if not hall_data:
            stale_halls.append(hall)
        else:
            fetched = hall_data.get("fetched", "")
            if fetched:
                age = (today_dt - datetime.strptime(fetched, "%Y-%m-%d")).days
                if age >= 7:
                    stale_halls.append(hall)

    if stale_halls and api_key:
        print(f"  Fetching Google Maps reviews for {len(stale_halls)} halls...")
        new_reviews = fetch_hall_reviews(api_key)
        if new_reviews:
            cache.update(new_reviews)
            save_reviews_cache(cache_path, cache)
            print(f"  Reviews fetched for {len(new_reviews)} halls")
        else:
            print("  No new reviews fetched (API may be unavailable)")
    elif cache:
        print(f"  Reviews: {len(cache)} halls cached (all fresh)")
    else:
        print("  Reviews: skipped (no GOOGLE_MAPS_API_KEY)")

    return cache


def format_hall_name(slug: str) -> str:
    """Format hall slug to display name."""
    return slug.replace("-", " ").title()


def render_html(all_menus: list[dict], translations: dict[str, dict[str, str]],
                menu_date: str, item_stats: dict | None = None,
                num_days: int = 0, firebase_config: dict | None = None,
                hall_reviews: dict | None = None) -> str:
    """Render all menu data into a self-contained HTML page."""
    date_display = datetime.strptime(menu_date, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    now = datetime.now(ZoneInfo("America/Detroit")).strftime("%Y-%m-%d %H:%M")

    # Insights chart data
    chart_data = compute_chart_data(all_menus)
    chart_data_json = json.dumps(chart_data, ensure_ascii=False)

    # Build hall tabs and content
    hall_tabs_html = ""
    hall_contents_html = ""
    for i, menu in enumerate(all_menus):
        hall = menu["hall"]
        hall_en = format_hall_name(hall)
        active = " active" if i == 0 else ""

        hall_tabs_html += (
            f'<button class="hall-tab{active}" data-hall="{hall}">'
            f'{hall_en}'
            f'</button>\n'
        )

        meals_html = ""
        if not menu["meals"]:
            meals_html = '<div class="no-menu">No menu available</div>'
        else:
            for meal_key, stations in menu["meals"].items():
                meal_en = meal_key.title()
                # Normalize brunch to lunch tab so it's visible on weekends
                tab_key = "lunch" if meal_key == "brunch" else meal_key

                stations_html = ""
                for station_name, items in stations.items():
                    items_html = ""
                    for item in items:
                        name_en = item["name"]
                        item_translations = translations.get(name_en, {})

                        traits_html = ""
                        for trait in item.get("traits", []):
                            info = TRAIT_DISPLAY.get(trait)
                            if not info:
                                continue
                            label, css_class = info
                            if not label:
                                continue
                            traits_html += f'<span class="trait-badge {css_class}">{label}</span>'

                        # Meat type badge
                        meat_type = detect_meat_type(name_en, item.get("traits", []))
                        if meat_type:
                            css = meat_type.lower()
                            traits_html += f'<span class="trait-badge meat-{css}">{meat_type}</span>'

                        # Look up item stats for popover and rare/common badges
                        st = (item_stats or {}).get(name_en, {})
                        has_stats = item_stats is not None and len(item_stats) > 0
                        is_rare = has_stats and st.get("count", 0) < 2
                        is_common = (has_stats and num_days > 0
                                     and st.get("count", 0) / num_days >= 0.6)
                        if is_rare:
                            traits_html += '<span class="trait-badge rare">Rare</span>'

                        trait_data = " ".join(
                            TRAIT_DISPLAY[t][1] if t in TRAIT_DISPLAY else t.lower().replace(" ", "-")
                            for t in item.get("traits", [])
                        )
                        if meat_type:
                            trait_data += f" meat meat-{meat_type.lower()}"
                        if is_rare:
                            trait_data += " rare"
                        if is_common:
                            trait_data += " common"
                        items_wrap = f'<span class="item-traits">{traits_html}</span>' if traits_html else ''

                        # Build full-name tags for popover (css_class:Full Name)
                        full_tags = []
                        for trait in item.get("traits", []):
                            info = TRAIT_DISPLAY.get(trait)
                            if not info or not info[0]:
                                continue
                            full_name = trait if trait in ("Vegan", "Vegetarian", "Halal", "Kosher", "Spicy", "Gluten Free") else info[0]
                            full_tags.append(f"{info[1]}:{full_name}")
                        if meat_type:
                            full_tags.append(f"meat-{meat_type.lower()}:{meat_type}")
                        if is_rare:
                            full_tags.append("rare:Rare")

                        # Build data attributes for popover
                        stat_attrs = ""
                        if full_tags:
                            stat_attrs += f' data-tags="{"|".join(full_tags)}"'
                        if st:
                            stat_attrs += f' data-freq="{st.get("count", 0)}"'
                            stat_attrs += f' data-last="{st.get("last_seen", "")}"'
                            stat_attrs += f' data-halls="{",".join(st.get("halls", []))}"'
                            stat_attrs += f' data-days="{num_days}"'

                        # Allergen data for filtering and popover
                        allergens = item.get("allergens", [])
                        if allergens:
                            stat_attrs += f' data-allergens="{",".join(allergens)}"'

                        # Nutrition data for popover
                        nutrition = item.get("nutrition", {})
                        if nutrition:
                            nut_parts = []
                            cal = nutrition.get("calories", "")
                            if cal:
                                nut_parts.append(f"cal:{cal}")
                            for key, label in [("protein", "protein"), ("total_fat", "fat"), ("total_carbohydrate", "carbs")]:
                                val = nutrition.get(key, "")
                                if val:
                                    nut_parts.append(f"{label}:{val}")
                            serving = nutrition.get("serving_size", "")
                            if serving:
                                nut_parts.append(f"serving:{serving}")
                            if nut_parts:
                                stat_attrs += f' data-nutrition="{"|".join(nut_parts)}"'

                        rate_html = ('<span class="rate-group">'
                                     '<span class="rate-btn rate-up">'
                                     '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M5 2L9 7H1Z" fill="currentColor"/></svg>'
                                     '<span class="rating-count"></span></span>'
                                     '<span class="rate-btn rate-down">'
                                     '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M5 8L1 3H9Z" fill="currentColor"/></svg>'
                                     '<span class="rating-count"></span></span>'
                                     '</span>') if firebase_config else ''
                        lang_spans = ""
                        for lang_code in SUPPORTED_LANGUAGES:
                            translated = item_translations.get(lang_code, "")
                            if translated:
                                lang_spans += f'<span class="lang" data-lang="{lang_code}">{html_mod.escape(translated)}</span>'
                        items_html += (
                            f'<div class="menu-item" data-traits="{trait_data}"{stat_attrs}>'
                            f'<span class="item-name">'
                            f'{lang_spans}'
                            f'<span class="en">{html_mod.escape(name_en)}</span>'
                            f'</span>'
                            f'{items_wrap}'
                            f'{rate_html}'
                            f'</div>'
                        )

                    station_label = station_name
                    stations_html += (
                        f'<div class="station">'
                        f'<h4 class="station-name">{station_label}</h4>'
                        f'{items_html}'
                        f'</div>'
                    )

                meal_lang_spans = ""
                for lang_code, lang_meals in MEAL_NAMES.items():
                    meal_translated = lang_meals.get(meal_key, meal_en)
                    meal_lang_spans += f'<span class="lang" data-lang="{lang_code}">{meal_translated}</span>'
                meals_html += (
                    f'<div class="meal-section" data-meal="{tab_key}">'
                    f'<h3 class="meal-name">'
                    f'{meal_lang_spans}'
                    f'<span class="en">{meal_en}</span>'
                    f'</h3>'
                    f'{stations_html}'
                    f'</div>'
                )

        display = "block" if i == 0 else "none"
        map_query = HALL_MAP_QUERIES.get(hall, hall_en + ",+Ann+Arbor,+MI")

        # Build reviews HTML if available
        reviews_html = ""
        review_data = (hall_reviews or {}).get(hall, {})
        if review_data and review_data.get("rating"):
            rating = review_data["rating"]
            total = review_data.get("total_ratings", 0)
            # Star display (round to nearest integer)
            filled = round(rating)
            stars_html = '<span class="review-stars">'
            stars_html += "&#9733;" * filled
            stars_html += "&#9734;" * (5 - filled)
            stars_html += "</span>"

            google_maps_url = f'https://www.google.com/maps/search/?api=1&amp;query={map_query}'
            reviews_html += (
                f'<div class="hall-reviews">'
                f'<div class="review-summary">'
                f'{stars_html}'
                f'<span class="review-rating">{rating}</span>'
                f'<a class="review-count" href="{google_maps_url}" target="_blank" rel="noopener">{total} reviews on Google</a>'
                f'</div>'
            )

            # Top reviews
            reviews_list = review_data.get("reviews", [])
            if reviews_list:
                reviews_html += '<div class="review-list-header">Top reviews</div>'
                reviews_html += '<div class="review-list">'
                for rev in reviews_list[:3]:
                    rev_stars = "&#9733;" * int(rev.get("rating", 0)) + "&#9734;" * (5 - int(rev.get("rating", 0)))
                    author = html_mod.escape(rev.get("author", "Anonymous"))
                    text = html_mod.escape(rev.get("text", ""))
                    # Truncate long reviews
                    if len(text) > 200:
                        text = text[:200] + "..."
                    time_ago = html_mod.escape(rev.get("time", ""))
                    reviews_html += (
                        f'<div class="review-item">'
                        f'<div class="review-author">'
                        f'<span class="review-author-name">{author}</span>'
                        f'<span class="review-stars-sm">{rev_stars}</span>'
                        f'<span class="review-time">{time_ago}</span>'
                        f'</div>'
                        f'<div class="review-text">{text}</div>'
                        f'</div>'
                    )
                reviews_html += '</div>'

            reviews_html += '</div>'

        hall_contents_html += (
            f'<div class="hall-content" data-hall="{hall}" data-hall-name="{hall_en}" style="display:{display}">'
            f'{meals_html}'
            f'{reviews_html}'
            f'</div>\n'
        )

    # Build Firebase rating CSS and JS (empty strings if not configured)
    firebase_css = ""
    firebase_js = ""
    if firebase_config and firebase_config.get("databaseURL"):
        firebase_css = (
            ".rate-group { display: inline-flex; gap: 3px; margin-left: auto; flex-shrink: 0; align-items: center; }\n"
            ".rate-btn { display: inline-flex; align-items: center; gap: 2px; "
            "font-size: 10px; font-weight: 500; cursor: pointer; padding: 2px 5px; border-radius: 4px; "
            "background: transparent; color: var(--text-secondary); "
            "user-select: none; transition: all 0.15s; line-height: 1; }\n"
            ".rate-btn svg { display: block; }\n"
            ".rate-btn:hover { background: var(--bg-hover); color: var(--text); }\n"
            ".rate-up.voted { background: hsl(142 72% 94%); color: hsl(142 72% 29%); }\n"
            ".rate-down.voted { background: hsl(0 72% 93%); color: hsl(0 72% 35%); }\n"
            ".dark-theme .rate-up.voted { background: hsl(142 30% 16%); color: hsl(142 50% 65%); }\n"
            ".dark-theme .rate-down.voted { background: hsl(0 30% 16%); color: hsl(0 50% 65%); }\n"
            "@media (prefers-color-scheme: dark) {\n"
            "  :root:not(.light-theme) .rate-up.voted { background: hsl(142 30% 16%); color: hsl(142 50% 65%); }\n"
            "  :root:not(.light-theme) .rate-down.voted { background: hsl(0 30% 16%); color: hsl(0 50% 65%); }\n"
            "}\n"
            ".rating-count { font-size: 10px; min-width: 6px; text-align: center; }\n"
        )
        # Escape </script> and <!-- in config JSON to prevent XSS
        firebase_config_json = json.dumps(firebase_config).replace("<", "\\u003c")
        firebase_js = (
            "// Dish rating system (Firebase)\n"
            "(function() {\n"
            "  var config = " + firebase_config_json + ";\n"
            "  if (!config || !config.databaseURL) return;\n"
            "  function loadScript(src) {\n"
            "    return new Promise(function(resolve, reject) {\n"
            "      var s = document.createElement('script');\n"
            "      s.src = src; s.onload = resolve; s.onerror = reject;\n"
            "      document.head.appendChild(s);\n"
            "    });\n"
            "  }\n"
            "  function itemKey(name) {\n"
            "    return name.replace(/[.#$\\[\\]\\/\\x00-\\x1f]/g, '_').substring(0, 128);\n"
            "  }\n"
            "  function countVotes(votes) {\n"
            "    var up = 0, down = 0;\n"
            "    if (votes) Object.values(votes).forEach(function(v) { if (v === 'up') up++; else if (v === 'down') down++; });\n"
            "    return {up: up, down: down};\n"
            "  }\n"
            "  loadScript('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js')\n"
            "  .then(function() { return loadScript('https://www.gstatic.com/firebasejs/9.23.0/firebase-auth-compat.js'); })\n"
            "  .then(function() { return loadScript('https://www.gstatic.com/firebasejs/9.23.0/firebase-database-compat.js'); })\n"
            "  .then(function() {\n"
            "    firebase.initializeApp(config);\n"
            "    var db = firebase.database();\n"
            "    return firebase.auth().signInAnonymously().then(function(cred) {\n"
            "      var uid = cred.user.uid;\n"
            "      // Load ratings per item (can't read /ratings directly due to rules)\n"
            "      document.querySelectorAll('.rate-group').forEach(function(group) {\n"
            "        var en = group.parentElement.querySelector('.item-name .en');\n"
            "        if (!en) return;\n"
            "        var key = itemKey(en.textContent.trim());\n"
            "        db.ref('ratings/' + key + '/votes').once('value').then(function(snap) {\n"
            "          var votes = snap.val() || {};\n"
            "          var counts = countVotes(votes);\n"
            "          var upBtn = group.querySelector('.rate-up');\n"
            "          var downBtn = group.querySelector('.rate-down');\n"
            "          upBtn.querySelector('.rating-count').textContent = counts.up || '';\n"
            "          downBtn.querySelector('.rating-count').textContent = counts.down || '';\n"
            "          if (votes[uid] === 'up') upBtn.classList.add('voted');\n"
            "          if (votes[uid] === 'down') downBtn.classList.add('voted');\n"
            "        });\n"
            "      });\n"
            "      // Handle rating clicks\n"
            "      document.addEventListener('click', function(e) {\n"
            "        var btn = e.target.closest('.rate-btn');\n"
            "        if (!btn) return;\n"
            "        e.stopPropagation();\n"
            "        e.preventDefault();\n"
            "        var group = btn.closest('.rate-group');\n"
            "        var en = group.parentElement.querySelector('.item-name .en');\n"
            "        if (!en) return;\n"
            "        var key = itemKey(en.textContent.trim());\n"
            "        var isUp = btn.classList.contains('rate-up');\n"
            "        var type = isUp ? 'up' : 'down';\n"
            "        var other = isUp ? 'down' : 'up';\n"
            "        var otherBtn = group.querySelector('.rate-' + other);\n"
            "        var countEl = btn.querySelector('.rating-count');\n"
            "        var otherCountEl = otherBtn.querySelector('.rating-count');\n"
            "        var current = parseInt(countEl.textContent) || 0;\n"
            "        var otherCurrent = parseInt(otherCountEl.textContent) || 0;\n"
            "        var ref = db.ref('ratings/' + key + '/votes/' + uid);\n"
            "        // If already voted this type, undo\n"
            "        if (btn.classList.contains('voted')) {\n"
            "          btn.classList.remove('voted');\n"
            "          countEl.textContent = Math.max(0, current - 1) || '';\n"
            "          ref.remove();\n"
            "        } else {\n"
            "          // If voted the other type, undo that first\n"
            "          if (otherBtn.classList.contains('voted')) {\n"
            "            otherBtn.classList.remove('voted');\n"
            "            otherCountEl.textContent = Math.max(0, otherCurrent - 1) || '';\n"
            "          }\n"
            "          btn.classList.add('voted');\n"
            "          countEl.textContent = current + 1;\n"
            "          ref.set(type);\n"
            "        }\n"
            "      });\n"
            "    });\n"
            "  }).catch(function() { /* Firebase unavailable — buttons stay inert */ });\n"
            "})();\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="Michigan Dining daily menus with multilingual translations">
<meta property="og:title" content="Michigan Dining Menus">
<meta property="og:description" content="Daily updated Michigan Dining menus with multilingual translations">
<meta property="og:type" content="website">
<meta property="og:locale" content="en_US">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Michigan Dining Menus">
<meta name="twitter:description" content="Daily updated Michigan Dining menus with multilingual translations">
<meta name="theme-color" content="#0d6efd" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1a1a2e" media="(prefers-color-scheme: dark)">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍽️</text></svg>">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍽️</text></svg>">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="MDining">
<title>Michigan Dining Menus</title>
<script data-goatcounter="https://mdining.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Sans+TC:wght@400;500;700&family=Noto+Sans+JP:wght@400;500;700&family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
<script defer src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
:root {{
    --bg: hsl(0 0% 100%);
    --bg-card: hsl(0 0% 97%);
    --bg-hover: hsl(0 0% 93%);
    --text: hsl(0 0% 9%);
    --text-secondary: hsl(0 0% 45%);
    --border: hsl(0 0% 90%);
    --accent: hsl(222 47% 31%);
    --accent-light: hsl(222 47% 95%);
    --shadow: 0 1px 2px rgba(0,0,0,0.05);
    --radius: 6px;
}}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) {{
        --bg: hsl(222 47% 7%);
        --bg-card: hsl(222 47% 11%);
        --bg-hover: hsl(222 30% 16%);
        --text: hsl(0 0% 93%);
        --text-secondary: hsl(0 0% 55%);
        --border: hsl(222 15% 20%);
        --accent: hsl(217 92% 76%);
        --accent-light: hsl(222 47% 15%);
        --shadow: 0 1px 2px rgba(0,0,0,0.2);
    }}
}}
.dark-theme {{
    --bg: hsl(222 47% 7%);
    --bg-card: hsl(222 47% 11%);
    --bg-hover: hsl(222 30% 16%);
    --text: hsl(0 0% 93%);
    --text-secondary: hsl(0 0% 55%);
    --border: hsl(222 15% 20%);
    --accent: hsl(217 92% 76%);
    --accent-light: hsl(222 47% 15%);
    --shadow: 0 1px 2px rgba(0,0,0,0.2);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
*, *::before, *::after {{
    transition: background-color 0.4s ease, color 0.4s ease, border-color 0.4s ease, box-shadow 0.4s ease;
}}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, 'Noto Sans SC', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    max-width: 860px;
    margin: 0 auto;
    padding: 16px;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
}}
header {{
    text-align: center;
    padding: 24px 0 16px;
}}
header h1 {{
    font-size: 1.5rem;
    font-weight: 700;
    margin-bottom: 4px;
}}
.date-display {{
    color: var(--text-secondary);
    font-size: 0.9rem;
}}
.controls {{
    display: flex;
    justify-content: center;
    gap: 8px;
    margin: 12px 0;
}}
.toggle-switch {{
    position: relative;
    display: flex;
    align-items: center;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 9999px;
    padding: 2px;
    cursor: pointer;
    font-size: 12px;
    user-select: none;
    transition: background 0.2s;
}}
.toggle-switch .toggle-option {{
    padding: 4px 10px;
    border-radius: 16px;
    transition: all 0.25s;
    color: var(--text-secondary);
    z-index: 1;
    white-space: nowrap;
}}
.toggle-switch .toggle-option.active {{
    color: var(--text);
    font-weight: 600;
}}
.toggle-switch .toggle-slider {{
    position: absolute;
    top: 2px;
    left: 2px;
    height: calc(100% - 4px);
    border-radius: 16px;
    background: var(--bg-hover);
    transition: all 0.25s ease;
}}
.hall-tabs {{
    display: flex;
    gap: 4px;
    overflow-x: auto;
    padding: 8px 0;
    border-bottom: 2px solid var(--border);
    margin-bottom: 16px;
    -webkit-overflow-scrolling: touch;
}}
.hall-tab {{
    background: none;
    border: none;
    color: var(--text-secondary);
    padding: 8px 16px;
    cursor: pointer;
    font-size: 0.9rem;
    white-space: nowrap;
    border-radius: var(--radius) var(--radius) 0 0;
    transition: all 0.2s;
    font-family: inherit;
}}
.hall-tab:hover {{
    color: var(--text);
    background: var(--bg-card);
}}
.hall-tab.active {{
    color: var(--accent);
    font-weight: 500;
    border-bottom: 2px solid var(--accent);
    margin-bottom: -2px;
}}
.meal-section {{
    margin-bottom: 16px;
}}
.meal-name {{
    font-size: 1.1rem;
    font-weight: 600;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 8px;
}}
.station {{
    margin-bottom: 12px;
}}
.station:last-child {{
    margin-bottom: 0;
}}
.station-name {{
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--accent);
    padding: 4px 0 2px;
    margin-bottom: 0;
    letter-spacing: 0.02em;
}}
.menu-item {{
    display: flex;
    align-items: baseline;
    flex-wrap: nowrap;
    gap: 4px;
    padding: 2px 10px;
    border-radius: var(--radius);
    line-height: 1.6;
}}
.menu-item:hover {{
    background: var(--bg-card);
}}
.item-name {{
    font-size: 0.9rem;
}}
body[data-lang] .item-name .en {{
    color: var(--text-secondary);
    font-size: 0.8rem;
}}
.item-traits {{
    display: inline-flex;
    gap: 3px;
    flex-shrink: 0;
    white-space: nowrap;
    align-items: center;
}}
.trait-badge {{
    font-size: 10px;
    font-weight: 500;
    line-height: 1;
    padding: 2px 5px;
    border-radius: 4px;
    letter-spacing: 0.01em;
}}
.trait-badge.vegan {{ background: hsl(142 72% 94%); color: hsl(142 72% 29%); }}
.trait-badge.vegetarian {{ background: hsl(173 58% 92%); color: hsl(173 58% 28%); }}
.trait-badge.gluten-free {{ background: hsl(43 96% 92%); color: hsl(43 96% 30%); }}
.trait-badge.halal {{ background: hsl(262 60% 93%); color: hsl(262 60% 35%); }}
.trait-badge.kosher {{ background: hsl(220 60% 93%); color: hsl(220 60% 35%); }}
.trait-badge.spicy {{ background: hsl(0 72% 93%); color: hsl(0 72% 35%); }}
.trait-badge.carbon-low {{ background: hsl(152 60% 92%); color: hsl(152 60% 28%); }}
.trait-badge.carbon-high {{ background: hsl(25 95% 92%); color: hsl(25 95% 30%); }}
.trait-badge.nutri-high,
.trait-badge.nutri-medhigh {{ background: hsl(48 96% 91%); color: hsl(48 80% 30%); }}
.dark-theme .trait-badge.vegan {{ background: hsl(142 30% 16%); color: hsl(142 50% 65%); }}
.dark-theme .trait-badge.vegetarian {{ background: hsl(173 25% 16%); color: hsl(173 40% 60%); }}
.dark-theme .trait-badge.gluten-free {{ background: hsl(43 30% 15%); color: hsl(43 60% 60%); }}
.dark-theme .trait-badge.halal {{ background: hsl(262 25% 17%); color: hsl(262 40% 70%); }}
.dark-theme .trait-badge.kosher {{ background: hsl(220 25% 17%); color: hsl(220 40% 70%); }}
.dark-theme .trait-badge.spicy {{ background: hsl(0 30% 16%); color: hsl(0 50% 65%); }}
.dark-theme .trait-badge.carbon-low {{ background: hsl(152 25% 15%); color: hsl(152 40% 60%); }}
.dark-theme .trait-badge.carbon-high {{ background: hsl(25 30% 15%); color: hsl(25 60% 60%); }}
.dark-theme .trait-badge.nutri-high,
.dark-theme .trait-badge.nutri-medhigh {{ background: hsl(48 30% 15%); color: hsl(48 50% 60%); }}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) .trait-badge.vegan {{ background: hsl(142 30% 16%); color: hsl(142 50% 65%); }}
    :root:not(.light-theme) .trait-badge.vegetarian {{ background: hsl(173 25% 16%); color: hsl(173 40% 60%); }}
    :root:not(.light-theme) .trait-badge.gluten-free {{ background: hsl(43 30% 15%); color: hsl(43 60% 60%); }}
    :root:not(.light-theme) .trait-badge.halal {{ background: hsl(262 25% 17%); color: hsl(262 40% 70%); }}
    :root:not(.light-theme) .trait-badge.kosher {{ background: hsl(220 25% 17%); color: hsl(220 40% 70%); }}
    :root:not(.light-theme) .trait-badge.spicy {{ background: hsl(0 30% 16%); color: hsl(0 50% 65%); }}
    :root:not(.light-theme) .trait-badge.carbon-low {{ background: hsl(152 25% 15%); color: hsl(152 40% 60%); }}
    :root:not(.light-theme) .trait-badge.carbon-high {{ background: hsl(25 30% 15%); color: hsl(25 60% 60%); }}
    :root:not(.light-theme) .trait-badge.nutri-high,
    :root:not(.light-theme) .trait-badge.nutri-medhigh {{ background: hsl(48 30% 15%); color: hsl(48 50% 60%); }}
}}
.no-menu {{
    text-align: center;
    padding: 40px 20px;
    color: var(--text-secondary);
    font-size: 1.1rem;
}}
footer {{
    text-align: center;
    padding: 24px 0;
    color: var(--text-secondary);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    margin-top: 24px;
}}
/* Meal tabs */
.meal-section.meal-hidden {{ display: none; }}
/* Filter pills */
.filter-bar {{
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
    margin-bottom: 4px;
}}
.filter-sep {{
    width: 1px;
    height: 16px;
    background: var(--border);
    margin: 0 2px;
    flex-shrink: 0;
}}
.filter-btn {{
    background: transparent;
    border: 1.5px dashed var(--border);
    padding: 3px 12px;
    border-radius: 9999px;
    cursor: pointer;
    font-size: 11px;
    font-weight: 500;
    line-height: 1.4;
    transition: all 0.15s;
    white-space: nowrap;
    font-family: inherit;
    color: var(--text-secondary);
}}
.filter-btn:hover {{ border-style: solid; background: var(--bg-hover); color: var(--text); }}
.filter-btn.active {{ border-style: solid; border-color: var(--accent); color: var(--accent); background: var(--accent-light); font-weight: 600; }}
.filter-btn[title] {{ cursor: help; }}
.more-filters {{
    display: none;
    justify-content: center;
    gap: 4px;
    flex-wrap: wrap;
    margin-bottom: 4px;
}}
.more-filters.visible {{ display: flex; }}
/* Smooth hall content transitions */
.hall-content {{
    animation: fadeIn 0.2s ease-in;
}}
@keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(4px); }}
    to {{ opacity: 1; transform: translateY(0); }}
}}
.menu-item.filtered-out {{
    display: none;
}}
/* Multilingual: default English only, secondary language shown when selected */
.lang {{ display: none; }}
.en {{ display: inline; }}
body[data-lang="zh-CN"] .lang[data-lang="zh-CN"],
body[data-lang="zh-TW"] .lang[data-lang="zh-TW"],
body[data-lang="ko"] .lang[data-lang="ko"],
body[data-lang="ja"] .lang[data-lang="ja"],
body[data-lang="es"] .lang[data-lang="es"],
body[data-lang="pt"] .lang[data-lang="pt"] {{ display: inline; }}
body[data-lang] .item-name .en {{ margin-left: 4px; }}
body[data-lang] .meal-name .en {{ margin-left: 6px; font-size: 0.85em; color: var(--text-secondary); }}
body[data-lang] .toggle-option .en {{ margin-left: 3px; font-size: 0.85em; }}
@media (max-width: 600px) {{
    body {{ padding: 8px; }}
    header h1 {{ font-size: 1.1rem; }}
    .controls {{ flex-wrap: wrap; justify-content: center; }}
    .hall-tab {{ padding: 4px 8px; font-size: 0.75rem; }}
    .menu-item {{ padding: 2px 6px; gap: 3px; }}
    .item-name {{ font-size: 0.82rem; }}
    body[data-lang] .item-name .en {{ font-size: 0.75rem; }}
    .trait-badge.carbon-low,
    .trait-badge.carbon-high,
    .trait-badge.nutri-high,
    .trait-badge.nutri-medhigh,
    .trait-badge.rare {{ display: none; }}
    .station-name {{ font-size: 0.8rem; }}
    .meal-name {{ font-size: 1rem; }}
}}
@media print {{
    .controls, .toggle-btn, .filter-bar, .more-filters, .hall-tabs {{ display: none; }}
    .hall-content {{
        display: block !important;
        break-inside: avoid-page;
        margin-bottom: 24px;
        animation: none;
    }}
    .hall-content::before {{
        content: attr(data-hall-name);
        display: block;
        font-size: 1.3rem;
        font-weight: bold;
        margin: 16px 0 8px;
        border-bottom: 2px solid #333;
        padding-bottom: 4px;
    }}
    .menu-item {{ box-shadow: none; border: 1px solid #ddd; }}
    body {{ max-width: 100%; }}
}}
.trait-badge.rare {{ background: hsl(280 60% 93%); color: hsl(280 50% 35%); }}
.trait-badge[class*="meat-"] {{ background: hsl(15 60% 93%); color: hsl(15 50% 32%); }}
.dark-theme .trait-badge.rare {{ background: hsl(280 25% 17%); color: hsl(280 40% 70%); }}
.dark-theme .trait-badge[class*="meat-"] {{ background: hsl(15 25% 16%); color: hsl(15 45% 65%); }}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) .trait-badge.rare {{ background: hsl(280 25% 17%); color: hsl(280 40% 70%); }}
    :root:not(.light-theme) .trait-badge[class*="meat-"] {{ background: hsl(15 25% 16%); color: hsl(15 45% 65%); }}
}}
/* Item popover */
.item-popover {{
    display: none;
    position: absolute;
    z-index: 100;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    padding: 10px 14px;
    font-size: 0.82rem;
    line-height: 1.6;
    max-width: 280px;
    pointer-events: none;
    color: var(--text);
}}
.item-popover.visible {{
    display: block;
}}
.popover-tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 6px;
}}
.popover-tags .trait-badge {{
    font-size: 11px;
    padding: 2px 8px;
}}
.popover-content .popover-row {{
    white-space: nowrap;
}}
@media (max-width: 600px) {{
    .item-popover {{
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        top: auto;
        max-width: 100%;
        border-radius: 12px 12px 0 0;
        pointer-events: auto;
        padding: 16px 20px;
        box-shadow: 0 -4px 24px rgba(0,0,0,0.15);
    }}
}}
.help-btn {{
    position: fixed;
    top: 12px;
    right: 12px;
    z-index: 50;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 50%;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
    padding: 0;
    transition: all 0.15s;
    box-shadow: var(--shadow);
}}
.help-btn:hover {{ color: var(--text); border-color: var(--accent); }}
.help-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.4);
    z-index: 200;
    justify-content: center;
    align-items: center;
}}
.help-overlay.visible {{ display: flex; }}
.help-card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    max-width: 420px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0,0,0,0.15);
}}
.help-card h2 {{
    font-size: 1.1rem;
    margin-bottom: 12px;
}}
.help-card p {{
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin-bottom: 12px;
    line-height: 1.6;
}}
.help-card .badge-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
}}
.help-card .badge-desc {{
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin-bottom: 12px;
}}
.help-close {{
    float: right;
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: var(--text-secondary);
    font-family: inherit;
}}
.help-close:hover {{ color: var(--text); }}
/* Allergen exclude filter buttons */
.allergen-filters {{
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 4px;
}}
.allergen-filters .filter-label {{
    font-size: 10px;
    color: var(--text-secondary);
    font-weight: 500;
    margin-right: 2px;
    white-space: nowrap;
}}
.filter-btn.allergen-exclude.active {{
    border-color: hsl(0 60% 50%);
    color: hsl(0 60% 45%);
    background: hsl(0 60% 96%);
}}
.dark-theme .filter-btn.allergen-exclude.active {{
    border-color: hsl(0 40% 55%);
    color: hsl(0 45% 70%);
    background: hsl(0 30% 16%);
}}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) .filter-btn.allergen-exclude.active {{
        border-color: hsl(0 40% 55%);
        color: hsl(0 45% 70%);
        background: hsl(0 30% 16%);
    }}
}}
/* Popover allergen pills */
.popover-allergens {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 6px;
}}
.popover-allergens .allergen-pill {{
    font-size: 10px;
    font-weight: 500;
    padding: 1px 6px;
    border-radius: 4px;
    background: hsl(25 80% 93%);
    color: hsl(25 60% 35%);
}}
.dark-theme .popover-allergens .allergen-pill {{
    background: hsl(25 25% 16%);
    color: hsl(25 50% 65%);
}}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) .popover-allergens .allergen-pill {{
        background: hsl(25 25% 16%);
        color: hsl(25 50% 65%);
    }}
}}
/* Popover nutrition row */
.popover-nutrition {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin-bottom: 4px;
    padding: 4px 0;
    border-top: 1px solid var(--border);
}}
.popover-nutrition .nut-item {{
    white-space: nowrap;
}}
.popover-nutrition .nut-val {{
    font-weight: 600;
    color: var(--text);
}}
.popover-nutrition .nut-serving {{
    font-size: 0.72rem;
    color: var(--text-secondary);
    font-style: italic;
    width: 100%;
}}
/* Google Maps reviews */
.hall-reviews {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 10px 14px;
    margin-bottom: 12px;
}}
.review-summary {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;
}}
.review-stars {{
    color: hsl(43 96% 56%);
    font-size: 1rem;
    letter-spacing: -1px;
}}
.review-rating {{
    font-weight: 700;
    font-size: 0.95rem;
}}
.review-count {{
    color: var(--accent);
    font-size: 0.8rem;
    text-decoration: none;
}}
.review-count:hover {{
    text-decoration: underline;
}}
.review-list {{
    display: flex;
    flex-direction: column;
    gap: 8px;
}}
.review-item {{
    padding: 8px 0;
    border-top: 1px solid var(--border);
}}
.review-item:first-child {{
    padding-top: 0;
    border-top: none;
}}
.review-author {{
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 2px;
}}
.review-author-name {{
    font-weight: 600;
    font-size: 0.82rem;
}}
.review-stars-sm {{
    color: hsl(43 96% 56%);
    font-size: 0.75rem;
    letter-spacing: -1px;
}}
.review-time {{
    color: var(--text-secondary);
    font-size: 0.72rem;
}}
.review-text {{
    font-size: 0.82rem;
    color: var(--text-secondary);
    line-height: 1.5;
}}
.review-list-header {{
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 4px;
}}
#lang-select {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 9999px;
    padding: 4px 10px;
    font-size: 12px;
    font-family: inherit;
    color: var(--text);
    cursor: pointer;
    appearance: none;
    -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5' viewBox='0 0 8 5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%23999'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 22px;
}}
#lang-select:hover {{ border-color: var(--accent); }}
/* Insights section */
.insights-section {{
    margin-top: 16px;
}}
.insights-content {{
    display: flex;
    gap: 16px;
    flex-direction: column;
}}
.chart-container {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
}}
.chart-title {{
    font-size: 0.95rem;
    font-weight: 600;
    margin-bottom: 2px;
}}
.chart-subtitle {{
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin-bottom: 12px;
}}
.chart-wrapper {{
    position: relative;
    height: 350px;
}}
@media (max-width: 600px) {{
    .chart-wrapper {{ height: 280px; }}
}}
@media print {{
    .insights-section {{ display: none; }}
}}
{firebase_css}</style>
</head>
<body>
<header>
    <h1>Michigan Dining Menus <span style="font-size:0.45em;font-weight:400;color:var(--text-secondary);vertical-align:middle">(unofficial)</span></h1>
    <div class="date-display">{date_display}</div>
    <div class="controls">
        <div class="toggle-switch" id="meal-toggle">
            <div class="toggle-slider" id="meal-slider"></div>
            <span class="toggle-option" data-meal="breakfast" onclick="switchMeal('breakfast')">{''.join(f'<span class="lang" data-lang="{lc}">{MEAL_NAMES[lc]["breakfast"]}</span>' for lc in SUPPORTED_LANGUAGES)}<span class="en">Breakfast</span></span>
            <span class="toggle-option" data-meal="lunch" onclick="switchMeal('lunch')">{''.join(f'<span class="lang" data-lang="{lc}">{MEAL_NAMES[lc]["lunch"]}</span>' for lc in SUPPORTED_LANGUAGES)}<span class="en">Lunch</span></span>
            <span class="toggle-option" data-meal="dinner" onclick="switchMeal('dinner')">{''.join(f'<span class="lang" data-lang="{lc}">{MEAL_NAMES[lc]["dinner"]}</span>' for lc in SUPPORTED_LANGUAGES)}<span class="en">Dinner</span></span>
        </div>
        <div class="toggle-switch" id="theme-toggle" onclick="toggleTheme()">
            <div class="toggle-slider" id="theme-slider"></div>
            <span class="toggle-option active" data-theme="light">☀️</span>
            <span class="toggle-option" data-theme="dark">🌙</span>
        </div>
        <select id="lang-select" onchange="switchLang(this.value)" aria-label="Language">
            <option value="">English</option>
            {''.join(f'<option value="{lc}">{info["name"]}</option>' for lc, info in SUPPORTED_LANGUAGES.items())}
        </select>
    </div>
    <button class="help-btn" onclick="document.getElementById('help').classList.add('visible')" aria-label="Help">?</button>
    <div class="filter-bar">
        <button class="filter-btn active" data-filter="all" onclick="clearFilters()">All</button>
        <span class="filter-sep"></span>
        <button class="filter-btn" data-filter="meat" onclick="toggleFilter(this)">Has Meat</button>
        <button class="filter-btn" data-filter="nutri-high nutri-medhigh" onclick="toggleFilter(this)">Nutritious</button>
        <button class="filter-btn" data-filter="rare" onclick="toggleFilter(this)" title="Items that rarely appear on the menu">Rare</button>
        <span class="filter-sep"></span>
        <button class="filter-btn" onclick="toggleMoreFilters(this)">More ▾</button>
    </div>
    <div class="more-filters" id="more-filters">
        <button class="filter-btn" data-filter="vegan" onclick="toggleFilter(this)">Vegan</button>
        <button class="filter-btn" data-filter="vegetarian" onclick="toggleFilter(this)">Vegetarian</button>
        <button class="filter-btn" data-filter="gluten-free" onclick="toggleFilter(this)">Gluten Free</button>
        <button class="filter-btn" data-filter="halal" onclick="toggleFilter(this)">Halal</button>
        <button class="filter-btn" data-filter="kosher" onclick="toggleFilter(this)">Kosher</button>
        <button class="filter-btn" data-filter="carbon-low" onclick="toggleFilter(this)">Low CO₂</button>
        <button class="filter-btn" data-filter="carbon-high" onclick="toggleFilter(this)">High CO₂</button>
        <div class="allergen-filters" style="width:100%">
            <span class="filter-label">Exclude:</span>
            <button class="filter-btn allergen-exclude" data-allergen="Wheat" onclick="toggleAllergen(this)">Wheat</button>
            <button class="filter-btn allergen-exclude" data-allergen="Milk" onclick="toggleAllergen(this)">Milk</button>
            <button class="filter-btn allergen-exclude" data-allergen="Eggs" onclick="toggleAllergen(this)">Eggs</button>
            <button class="filter-btn allergen-exclude" data-allergen="Soy" onclick="toggleAllergen(this)">Soy</button>
            <button class="filter-btn allergen-exclude" data-allergen="Peanuts" onclick="toggleAllergen(this)">Peanuts</button>
            <button class="filter-btn allergen-exclude" data-allergen="Tree Nuts" onclick="toggleAllergen(this)">Tree Nuts</button>
            <button class="filter-btn allergen-exclude" data-allergen="Sesame" onclick="toggleAllergen(this)">Sesame</button>
            <button class="filter-btn allergen-exclude" data-allergen="Oats" onclick="toggleAllergen(this)">Oats</button>
            <button class="filter-btn allergen-exclude" data-allergen="Fish" onclick="toggleAllergen(this)">Fish</button>
            <button class="filter-btn allergen-exclude" data-allergen="Shellfish" onclick="toggleAllergen(this)">Shellfish</button>
        </div>
    </div>
</header>

<nav class="hall-tabs">
{hall_tabs_html}
</nav>

<main>
{hall_contents_html}
</main>

<script>var CHART_DATA = {chart_data_json};</script>
<section class="insights-section">
    <div class="insights-content" id="insights-content">
        <div class="chart-container">
            <div class="chart-title">Protein vs Calories</div>
            <div class="chart-subtitle">Each dot is a menu item. Find high-protein, lower-calorie options in the upper left.</div>
            <div class="chart-wrapper"><canvas id="scatter-chart"></canvas></div>
        </div>
    </div>
</section>

<div id="item-popover" class="item-popover">
    <div class="popover-content"></div>
</div>

<footer>
    Last updated: {now}<br>
    <span style="font-size:0.85em">Unofficial &middot; Not affiliated with U&#8209;M &middot; <a href="https://github.com/xingjian-zhang/mdining" target="_blank" style="color:var(--text-secondary)">GitHub</a></span>
</footer>

<div id="help" class="help-overlay" onclick="if(event.target===this)this.classList.remove('visible')">
<div class="help-card">
    <button class="help-close" onclick="document.getElementById('help').classList.remove('visible')">&times;</button>
    <h2>How to use</h2>
    <p>Browse menus for each dining hall using the tabs. Switch between meals with the toggle at the top.</p>
    <h2>Filter by diet</h2>
    <p>Tap filter buttons to show only matching items. Tap "More" for dietary restriction filters (Vegan, Vegetarian, Gluten Free, Halal, Kosher). You can combine multiple filters.</p>
    <h2>Labels</h2>
    <div class="badge-row">
        <span class="trait-badge vegan">V</span>
        <span class="trait-badge vegetarian">VG</span>
        <span class="trait-badge gluten-free">GF</span>
        <span class="trait-badge halal">H</span>
        <span class="trait-badge kosher">K</span>
    </div>
    <div class="badge-desc">V = Vegan, VG = Vegetarian, GF = Gluten Free, H = Halal, K = Kosher</div>
    <div class="badge-row">
        <span class="trait-badge carbon-low">Low CO&#8322;</span>
        <span class="trait-badge carbon-high">High CO&#8322;</span>
        <span class="trait-badge nutri-high">Nutritious</span>
        <span class="trait-badge spicy">Spicy</span>
    </div>
    <div class="badge-desc">Environmental and nutritional indicators from Michigan Dining.</div>
    <div class="badge-row">
        <span class="trait-badge meat-beef">Beef</span>
        <span class="trait-badge meat-pork">Pork</span>
        <span class="trait-badge meat-chicken">Chicken</span>
        <span class="trait-badge meat-fish">Fish</span>
    </div>
    <div class="badge-desc">Meat type detected from the item name.</div>
    <div class="badge-row">
        <span class="trait-badge rare">Rare</span>
    </div>
    <div class="badge-desc">Items that rarely appear on the menu (fewer than 2 times in the past 2 weeks). Don't miss these!</div>
    <h2>Allergens</h2>
    <p>Use the "Exclude" filters in the More panel to hide items containing specific allergens. Allergen data comes from Michigan Dining and may not cover all ingredients.</p>
    <h2>Nutrition</h2>
    <p>Hover (or tap) an item to see calories, protein, fat, and carbs in the popover.</p>
    <h2>About</h2>
    <p>Not affiliated with Michigan Dining. Menu data is scraped from the UMich dining website and may not always be accurate.</p>
    <p>If you represent the University of Michigan and have concerns, please <a href="https://github.com/xingjian-zhang/mdining/issues" target="_blank" style="color: var(--accent);">open an issue on GitHub</a>.</p>
    <p><a href="https://github.com/xingjian-zhang/mdining" target="_blank" style="color: var(--accent);">GitHub</a></p>
</div>
</div>

<script>
// Language switching
function switchLang(code) {{
    if (code) {{
        document.body.setAttribute('data-lang', code);
    }} else {{
        document.body.removeAttribute('data-lang');
    }}
    localStorage.setItem('mdining-lang', code || '');
    // Recalculate meal toggle slider width after text change
    updateMealSlider();
}}
(function() {{
    var saved = localStorage.getItem('mdining-lang');
    if (saved) {{
        document.body.setAttribute('data-lang', saved);
        var sel = document.getElementById('lang-select');
        if (sel) sel.value = saved;
    }}
}})();

// Hall tab switching with fade
document.querySelectorAll('.hall-tab').forEach(tab => {{
    tab.addEventListener('click', () => {{
        document.querySelectorAll('.hall-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const hall = tab.dataset.hall;
        document.querySelectorAll('.hall-content').forEach(c => {{
            if (c.dataset.hall === hall) {{
                c.style.display = 'block';
                c.style.animation = 'none';
                c.offsetHeight; // trigger reflow
                c.style.animation = '';
            }} else {{
                c.style.display = 'none';
            }}
        }});
        applyFilters();
        updateScatterChart();
    }});
}});

// Meal toggle switching
let activeMeal = 'lunch';
function updateMealSlider() {{
    const toggle = document.getElementById('meal-toggle');
    const options = toggle.querySelectorAll('.toggle-option');
    options.forEach(o => o.classList.toggle('active', o.dataset.meal === activeMeal));
    const active = toggle.querySelector('.toggle-option.active');
    if (active) positionSlider(toggle, active, 'meal-slider');
}}
function switchMeal(meal) {{
    activeMeal = meal;
    document.querySelectorAll('.meal-section').forEach(el => {{
        if (el.dataset.meal === activeMeal) {{
            el.classList.remove('meal-hidden');
        }} else {{
            el.classList.add('meal-hidden');
        }}
    }});
    updateMealSlider();
    updateScatterChart();
}}
// Default meal based on current time (ET)
(function() {{
    var now = new Date();
    // Convert to ET
    var et = new Date(now.toLocaleString('en-US', {{timeZone: 'America/New_York'}}));
    var h = et.getHours();
    var meal = h < 11 ? 'breakfast' : h < 16 ? 'lunch' : 'dinner';
    // Check the meal section actually exists (some meals may not be served)
    var exists = document.querySelector('.meal-section[data-meal="' + meal + '"]');
    if (!exists) meal = 'lunch';
    exists = document.querySelector('.meal-section[data-meal="' + meal + '"]');
    if (!exists) meal = document.querySelector('.meal-section') ? document.querySelector('.meal-section').dataset.meal : 'lunch';
    switchMeal(meal);
}})();

// Dietary filter toggles
let activeFilters = new Set();
let excludedAllergens = new Set();
const allBtn = () => document.querySelector('.filter-btn[data-filter="all"]');
function updateAllBtn() {{
    const btn = allBtn();
    if (btn) btn.classList.toggle('active', activeFilters.size === 0 && excludedAllergens.size === 0);
}}
function clearFilters() {{
    activeFilters.clear();
    excludedAllergens.clear();
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    updateAllBtn();
    applyFilters();
}}
function toggleAllergen(btn) {{
    const allergen = btn.dataset.allergen;
    if (excludedAllergens.has(allergen)) {{
        excludedAllergens.delete(allergen);
        btn.classList.remove('active');
    }} else {{
        excludedAllergens.add(allergen);
        btn.classList.add('active');
    }}
    updateAllBtn();
    applyFilters();
}}
function toggleMoreFilters(btn) {{
    const panel = document.getElementById('more-filters');
    const open = panel.classList.toggle('visible');
    btn.textContent = open ? 'More ▴' : 'More ▾';
    btn.classList.toggle('active', open);
}}
function toggleFilter(btn) {{
    const filter = btn.dataset.filter;
    if (activeFilters.has(filter)) {{
        activeFilters.delete(filter);
        btn.classList.remove('active');
    }} else {{
        activeFilters.add(filter);
        btn.classList.add('active');
    }}
    updateAllBtn();
    applyFilters();
}}
function applyFilters() {{
    document.querySelectorAll('.menu-item').forEach(el => {{
        const traits = el.dataset.traits || '';
        var hidden = false;
        // Inclusive trait filters (OR logic)
        if (activeFilters.size > 0) {{
            hidden = ![...activeFilters].some(f => f.split(' ').some(sub => traits.includes(sub)));
        }}
        // Allergen exclusion filters (AND logic - hide if item has ANY excluded allergen)
        if (!hidden && excludedAllergens.size > 0) {{
            const itemAllergens = el.dataset.allergens || '';
            if (itemAllergens) {{
                hidden = [...excludedAllergens].some(a => itemAllergens.split(',').includes(a));
            }}
        }}
        el.classList.toggle('filtered-out', hidden);
    }});
}}

// Slider positioning helper
function positionSlider(toggleEl, activeOption, sliderId) {{
    const slider = document.getElementById(sliderId);
    slider.style.width = activeOption.offsetWidth + 'px';
    slider.style.left = (activeOption.offsetLeft) + 'px';
}}


// Theme toggle
let themeState = 0; // 0=light, 1=dark
function updateThemeSlider() {{
    const toggle = document.getElementById('theme-toggle');
    const options = toggle.querySelectorAll('.toggle-option');
    options.forEach((o, i) => o.classList.toggle('active', i === themeState));
    positionSlider(toggle, options[themeState], 'theme-slider');
}}
function toggleTheme() {{
    const html = document.documentElement;
    if (html.classList.contains('dark-theme')) {{
        html.classList.remove('dark-theme');
        html.classList.add('light-theme');
        themeState = 0;
    }} else if (html.classList.contains('light-theme')) {{
        html.classList.remove('light-theme');
        html.classList.add('dark-theme');
        themeState = 1;
    }} else {{
        const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        html.classList.add(isDark ? 'light-theme' : 'dark-theme');
        themeState = isDark ? 0 : 1;
    }}
    updateThemeSlider();
    updateInsightChartTheme();
}}
// Init theme slider based on current state
if (document.documentElement.classList.contains('dark-theme') ||
    (!document.documentElement.classList.contains('light-theme') &&
     window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
    themeState = 1;
}}
updateThemeSlider();

// Item popover
(function() {{
    var popover = document.getElementById('item-popover');
    var content = popover.querySelector('.popover-content');
    var hoverTimer = null;
    var isMobile = window.matchMedia('(max-width: 600px)').matches;

    function formatHall(slug) {{
        return slug.replace(/-/g, ' ').replace(/\\b\\w/g, function(c) {{ return c.toUpperCase(); }});
    }}

    function buildContent(el) {{
        var tags = el.dataset.tags || '';
        var freq = el.dataset.freq;
        var rows = [];

        // Tag pills row
        if (tags) {{
            var pills = tags.split('|').map(function(t) {{
                var parts = t.split(':');
                return '<span class="trait-badge ' + parts[0] + '">' + parts[1] + '</span>';
            }}).join(' ');
            rows.push('<div class="popover-tags">' + pills + '</div>');
        }}

        // Allergen pills
        var allergens = el.dataset.allergens || '';
        if (allergens) {{
            var pills = allergens.split(',').map(function(a) {{
                return '<span class="allergen-pill">' + a + '</span>';
            }}).join(' ');
            rows.push('<div class="popover-allergens">' + pills + '</div>');
        }}

        // Nutrition info
        var nutData = el.dataset.nutrition || '';
        if (nutData) {{
            var nutParts = {{}};
            nutData.split('|').forEach(function(p) {{
                var kv = p.split(':');
                if (kv.length >= 2) nutParts[kv[0]] = kv.slice(1).join(':');
            }});
            var nutHtml = '<div class="popover-nutrition">';
            if (nutParts.cal) nutHtml += '<span class="nut-item"><span class="nut-val">' + nutParts.cal + '</span> cal</span>';
            if (nutParts.protein) nutHtml += '<span class="nut-item"><span class="nut-val">' + nutParts.protein + '</span> protein</span>';
            if (nutParts.fat) nutHtml += '<span class="nut-item"><span class="nut-val">' + nutParts.fat + '</span> fat</span>';
            if (nutParts.carbs) nutHtml += '<span class="nut-item"><span class="nut-val">' + nutParts.carbs + '</span> carbs</span>';
            if (nutParts.serving) nutHtml += '<span class="nut-serving">' + nutParts.serving + '</span>';
            nutHtml += '</div>';
            rows.push(nutHtml);
        }}

        if (!freq) return rows.join('');
        var days = el.dataset.days || '14';
        var last = el.dataset.last || '';
        var halls = el.dataset.halls ? el.dataset.halls.split(',') : [];

        // Frequency row
        rows.push('<div class="popover-row">' + freq + ' times in ' + days + ' days</div>');

        // Last seen row (skip if today)
        if (last) {{
            var today = new Date();
            today.setHours(0,0,0,0);
            var parts = last.split('-');
            var lastDate = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
            var diff = Math.round((today - lastDate) / 86400000);
            if (diff > 0) {{
                rows.push('<div class="popover-row">Last seen ' + diff + ' day' + (diff > 1 ? 's' : '') + ' ago</div>');
            }}
        }}

        // Hall exclusivity row
        if (halls.length === 1) {{
            var hallEn = formatHall(halls[0]);
            rows.push('<div class="popover-row">Only at ' + hallEn + '</div>');
        }}

        return rows.join('');
    }}

    function showPopover(el) {{
        var html = buildContent(el);
        if (!html) return;
        content.innerHTML = html;
        popover.classList.add('visible');
        if (!isMobile) {{
            var rect = el.getBoundingClientRect();
            var top = rect.bottom + window.scrollY + 4;
            var left = rect.left + window.scrollX;
            // Flip above if near bottom
            if (rect.bottom + 120 > window.innerHeight) {{
                top = rect.top + window.scrollY - popover.offsetHeight - 4;
            }}
            popover.style.top = top + 'px';
            popover.style.left = Math.max(4, Math.min(left, window.innerWidth - 290)) + 'px';
        }}
    }}

    function hidePopover() {{
        popover.classList.remove('visible');
    }}

    var main = document.querySelector('main');

    if (isMobile) {{
        // Tap to show, tap elsewhere to dismiss
        main.addEventListener('click', function(e) {{
            if (e.target.closest('.rate-btn')) return;
            var item = e.target.closest('.menu-item');
            if (item) {{
                e.preventDefault();
                hidePopover();
                showPopover(item);
            }}
        }});
        document.addEventListener('click', function(e) {{
            if (!e.target.closest('.menu-item') && !e.target.closest('.item-popover')) {{
                hidePopover();
            }}
        }});
    }} else {{
        // Desktop: mouseenter/mouseleave with debounce
        main.addEventListener('mouseenter', function(e) {{
            var item = e.target.closest('.menu-item');
            if (!item) return;
            clearTimeout(hoverTimer);
            hoverTimer = setTimeout(function() {{
                showPopover(item);
            }}, 200);
        }}, true);
        main.addEventListener('mouseleave', function(e) {{
            var item = e.target.closest('.menu-item');
            if (!item) return;
            clearTimeout(hoverTimer);
            hidePopover();
        }}, true);
    }}

    // Dismiss on scroll
    window.addEventListener('scroll', hidePopover, {{passive: true}});
}})();

// Insights charts
var _scatterChart = null;
function getActiveHall() {{
    var tab = document.querySelector('.hall-tab.active');
    return tab ? tab.dataset.hall : '';
}}
function getChartThemeColors() {{
    var s = getComputedStyle(document.documentElement);
    return {{
        text: s.getPropertyValue('--text').trim() || '#171717',
        textSec: s.getPropertyValue('--text-secondary').trim() || '#737373',
        border: s.getPropertyValue('--border').trim() || '#e5e5e5',
        bgCard: s.getPropertyValue('--bg-card').trim() || '#f7f7f7',
    }};
}}
function getScatterDatasets(hallSlug, meal) {{
    var hallData = (CHART_DATA.scatter || {{}})[hallSlug] || {{}};
    var points = hallData[meal || activeMeal] || [];
    var vegan = [], vegetarian = [], other = [];
    points.forEach(function(p) {{
        if (p.c === 'vegan') vegan.push(p);
        else if (p.c === 'vegetarian') vegetarian.push(p);
        else other.push(p);
    }});
    return [
        {{ label: 'Vegan', data: vegan, backgroundColor: 'hsla(142,60%,45%,0.6)', pointRadius: 5, pointHoverRadius: 7 }},
        {{ label: 'Vegetarian', data: vegetarian, backgroundColor: 'hsla(173,50%,45%,0.6)', pointRadius: 5, pointHoverRadius: 7 }},
        {{ label: 'Other', data: other, backgroundColor: 'hsla(25,70%,55%,0.65)', pointRadius: 5, pointHoverRadius: 7 }},
    ];
}}
function updateScatterChart() {{
    if (!_scatterChart) return;
    var ds = getScatterDatasets(getActiveHall());
    _scatterChart.data.datasets.forEach(function(dataset, i) {{
        dataset.data = ds[i].data;
    }});
    _scatterChart.update();
}}
function initInsightCharts() {{
    if (typeof Chart === 'undefined') return;
    if (typeof ChartDataLabels !== 'undefined') Chart.register(ChartDataLabels);
    var d = CHART_DATA;
    var tc = getChartThemeColors();

    // Scatter: per-hall data
    var scatterCtx = document.getElementById('scatter-chart');
    _scatterChart = new Chart(scatterCtx, {{
        type: 'scatter',
        data: {{ datasets: getScatterDatasets(getActiveHall()) }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            plugins: {{
                datalabels: {{
                    display: function(ctx) {{ return ctx.dataset.data[ctx.dataIndex].lbl === 1; }},
                    formatter: function(v) {{
                        var n = v.n || '';
                        return n.length > 20 ? n.substring(0, 18) + '...' : n;
                    }},
                    color: tc.textSec,
                    font: {{ size: 10, weight: 500 }},
                    anchor: 'end',
                    align: 'top',
                    offset: 2,
                    clamp: true,
                }},
                tooltip: {{
                    enabled: false,
                    external: function(context) {{
                        var el = document.getElementById('chart-tooltip');
                        if (!el) {{
                            el = document.createElement('div');
                            el.id = 'chart-tooltip';
                            el.className = 'item-popover';
                            el.style.pointerEvents = 'none';
                            el.style.position = 'absolute';
                            el.style.zIndex = '100';
                            document.body.appendChild(el);
                        }}
                        var tooltip = context.tooltip;
                        if (tooltip.opacity === 0) {{ el.style.display = 'none'; return; }}
                        var p = tooltip.dataPoints[0].raw;
                        var html = '<div class="popover-content">';
                        html += '<div style="font-weight:600;margin-bottom:4px">' + p.n + '</div>';
                        if (p.t) {{
                            html += '<div class="popover-tags">';
                            p.t.forEach(function(tag) {{ html += '<span class="trait-badge ' + tag.cls + '">' + tag.l + '</span>'; }});
                            html += '</div>';
                        }}
                        html += '<div class="popover-nutrition">';
                        html += '<span class="nut-item"><span class="nut-val">' + p.x + '</span> cal</span>';
                        html += '<span class="nut-item"><span class="nut-val">' + p.y + 'g</span> protein</span>';
                        if (p.fat) html += '<span class="nut-item"><span class="nut-val">' + p.fat + '</span> fat</span>';
                        if (p.carbs) html += '<span class="nut-item"><span class="nut-val">' + p.carbs + '</span> carbs</span>';
                        if (p.srv) html += '<span class="nut-serving">' + p.srv + '</span>';
                        html += '</div></div>';
                        el.innerHTML = html;
                        el.style.display = 'block';
                        var canvas = context.chart.canvas;
                        var rect = canvas.getBoundingClientRect();
                        el.style.left = (rect.left + window.scrollX + tooltip.caretX) + 'px';
                        el.style.top = (rect.top + window.scrollY + tooltip.caretY - el.offsetHeight - 8) + 'px';
                    }},
                }},
                legend: {{ labels: {{ color: tc.textSec, usePointStyle: true, pointStyle: 'circle' }} }},
            }},
            scales: {{
                x: {{ title: {{ display: true, text: 'Calories', color: tc.textSec }}, ticks: {{ color: tc.textSec }}, grid: {{ color: tc.border }} }},
                y: {{ title: {{ display: true, text: 'Protein (g)', color: tc.textSec }}, ticks: {{ color: tc.textSec }}, grid: {{ color: tc.border }} }},
            }},
        }},
    }});

}}
function updateInsightChartTheme() {{
    if (!_scatterChart) return;
    var tc = getChartThemeColors();
    [_scatterChart].forEach(function(chart) {{
        if (!chart) return;
        chart.options.plugins.tooltip.backgroundColor = tc.bgCard;
        chart.options.plugins.tooltip.titleColor = tc.text;
        chart.options.plugins.tooltip.bodyColor = tc.text;
        chart.options.plugins.tooltip.borderColor = tc.border;
        chart.options.plugins.legend.labels.color = tc.textSec;
        chart.options.scales.x.ticks.color = tc.textSec;
        chart.options.scales.x.grid.color = tc.border;
        chart.options.scales.y.ticks.color = tc.textSec;
        chart.options.scales.y.grid.color = tc.border;
        if (chart.options.scales.x.title) chart.options.scales.x.title.color = tc.textSec;
        if (chart.options.scales.y.title) chart.options.scales.y.title.color = tc.textSec;
        if (chart.options.plugins.datalabels && chart.options.plugins.datalabels.color) chart.options.plugins.datalabels.color = tc.textSec;
        chart.update('none');
    }});
}}

// Init insight charts once Chart.js is loaded (defer scripts)
if (typeof Chart !== 'undefined') {{
    initInsightCharts();
}} else {{
    window.addEventListener('load', function() {{ initInsightCharts(); }});
}}

// PWA: register service worker
if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('sw.js').catch(function() {{}});
}}


{firebase_js}
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate Michigan Dining menu website")
    parser.add_argument("--output", default=SITE_DIR, help="Output directory (default: site)")
    parser.add_argument("--date", default=None, help="Menu date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-translate", action="store_true", help="Skip translation API calls (use cache only)")
    args = parser.parse_args()

    menu_date = args.date or datetime.now(ZoneInfo("America/Detroit")).strftime("%Y-%m-%d")

    print(f"Fetching menus for {menu_date} from {len(DINING_HALLS)} halls...")
    all_menus = fetch_all_halls(menu_date)
    total_items = sum(
        len(items)
        for menu in all_menus
        for stations in menu.get("meals", {}).values()
        for items in stations.values()
    )
    print(f"  Found {total_items} items across {len(all_menus)} halls.")

    # Save dataset
    data_dir = "data"
    os.makedirs(data_dir, exist_ok=True)
    data_path = os.path.join(data_dir, f"{menu_date}.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(all_menus, f, ensure_ascii=False, indent=2)
    print(f"  Dataset saved: {data_path}")

    # Translate
    cache_path = os.path.join(args.output, "translations_cache.json")
    if args.no_translate:
        translations = load_translation_cache(cache_path)
        print(f"  Translations: {len(translations)} cached (skipping API)")
    else:
        names = collect_unique_names(all_menus)
        if names:
            translations = translate_with_cache(names, cache_path)
            print(f"  Translations: {len(translations)} / {len(names)} items")
        else:
            translations = {}

    # Compute item stats from historical data (last k days)
    all_days = load_all_data_files("data")
    item_stats = None
    num_days = 0
    if len(all_days) >= 3:
        recent_days = all_days
        num_days = len(recent_days)
        item_stats = compute_item_stats(recent_days) if recent_days else None
        if item_stats:
            rare_count = sum(1 for v in item_stats.values() if v["count"] < 2)
            print(f"  Item stats: {len(item_stats)} items tracked ({rare_count} rare) over {num_days} days")

    # Fetch Google Maps reviews (optional, requires GOOGLE_MAPS_API_KEY)
    reviews_cache_path = os.path.join(args.output, "reviews_cache.json")
    hall_reviews = fetch_reviews_with_cache(GOOGLE_MAPS_API_KEY, reviews_cache_path)

    # Render HTML
    print("Generating HTML...")
    html = render_html(all_menus, translations, menu_date,
                       item_stats=item_stats, num_days=num_days,
                       firebase_config=FIREBASE_CONFIG,
                       hall_reviews=hall_reviews)

    os.makedirs(args.output, exist_ok=True)
    output_path = os.path.join(args.output, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # PWA: manifest.json
    manifest = {
        "name": "Michigan Dining Menus",
        "short_name": "MDining",
        "description": "Daily Michigan Dining menus with multilingual translations",
        "start_url": ".",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#1a365d",
        "icons": [
            {
                "src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍽️</text></svg>",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any"
            }
        ]
    }
    manifest_path = os.path.join(args.output, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # PWA: service worker
    sw_js = (
        "const CACHE_NAME = 'mdining-" + menu_date + "';\n"
        "const URLS_TO_CACHE = ['./', 'index.html'];\n"
        "\n"
        "self.addEventListener('install', function(e) {\n"
        "  e.waitUntil(\n"
        "    caches.open(CACHE_NAME).then(function(cache) {\n"
        "      return cache.addAll(URLS_TO_CACHE);\n"
        "    })\n"
        "  );\n"
        "  self.skipWaiting();\n"
        "});\n"
        "\n"
        "self.addEventListener('activate', function(e) {\n"
        "  e.waitUntil(\n"
        "    caches.keys().then(function(names) {\n"
        "      return Promise.all(\n"
        "        names.filter(function(n) { return n !== CACHE_NAME; })\n"
        "             .map(function(n) { return caches.delete(n); })\n"
        "      );\n"
        "    })\n"
        "  );\n"
        "  self.clients.claim();\n"
        "});\n"
        "\n"
        "self.addEventListener('fetch', function(e) {\n"
        "  e.respondWith(\n"
        "    fetch(e.request).then(function(response) {\n"
        "      var clone = response.clone();\n"
        "      caches.open(CACHE_NAME).then(function(cache) {\n"
        "        cache.put(e.request, clone);\n"
        "      });\n"
        "      return response;\n"
        "    }).catch(function() {\n"
        "      return caches.match(e.request);\n"
        "    })\n"
        "  );\n"
        "});\n"
    )
    sw_path = os.path.join(args.output, "sw.js")
    with open(sw_path, "w", encoding="utf-8") as f:
        f.write(sw_js)

    # LLM-friendly: today.json (copy of today's menu data)
    import shutil
    today_json_path = os.path.join(args.output, "today.json")
    shutil.copy2(data_path, today_json_path)

    # LLM-friendly: llms.txt
    site_base = "https://xingjianz.com/mdining"
    llms_txt = f"""# Michigan Dining Menus — LLM Access

> Unofficial daily menus for University of Michigan dining halls.
> Updated daily at ~5 AM ET via GitHub Actions.

## Quick Access

- Today's menu (JSON): {site_base}/today.json
- Website: {site_base}/

## today.json Schema

The file is a JSON array of hall objects:

```
[
  {{
    "hall": "bursley",          // one of: bursley, east-quad, mosher-jordan, south-quad, twigs-at-oxford
    "date": "YYYY-MM-DD",
    "meals": {{
      "breakfast|lunch|dinner": {{
        "Station Name": [
          {{
            "name": "Item Name",
            "traits": ["Vegan", "Gluten Free", ...],   // dietary tags from UMich
            "allergens": ["Wheat", "Milk", ...],        // food allergens
            "nutrition": {{                              // per-serving nutrition
              "serving_size": "8 oz Cup (227g)",
              "calories": 164,
              "total_fat": "3g",
              "protein": "6g",
              "total_carbohydrate": "29g",
              ...
            }}
          }}
        ]
      }}
    }}
  }}
]
```

## Trait Values

Dietary: Vegan, Vegetarian, Gluten Free, Halal, Kosher, Spicy
Carbon: Carbon Footprint Low, Carbon Footprint Medium, Carbon Footprint High
Nutrition: Nutrient Dense High, Nutrient Dense Medium High, Nutrient Dense Medium, Nutrient Dense Low Medium, Nutrient Dense Low

## Allergen Values

Wheat, Milk, Eggs, Soy, Peanuts, Tree Nuts, Sesame, Oats, Fish, Shellfish, Corn

## Dining Halls

- bursley (Bursley Dining Hall)
- east-quad (East Quad Dining Hall)
- mosher-jordan (Mosher-Jordan Dining Hall)
- south-quad (South Quad Dining Hall)
- twigs-at-oxford (Twigs at Oxford)
"""
    llms_path = os.path.join(args.output, "llms.txt")
    with open(llms_path, "w", encoding="utf-8") as f:
        f.write(llms_txt)

    print(f"Done! Output: {output_path} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
