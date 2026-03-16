#!/usr/bin/env python3
"""
Generate a static bilingual (Chinese/English) menu website for Michigan Dining.

Scrapes all dining halls, translates item names to Chinese, and outputs
a single self-contained index.html with embedded CSS and minimal JS.

Usage:
    python generate_site.py                # Generate site/index.html
    python generate_site.py --output out   # Custom output directory
    python generate_site.py --no-translate # Skip Chinese translation
"""

import argparse
import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scraper import DINING_HALLS, fetch_menu

# Chinese names for dining halls
HALL_NAMES_CN = {
    "bursley": "伯斯利",
    "east-quad": "东方庭院",
    "mosher-jordan": "莫舍-乔丹",
    "south-quad": "南方庭院",
    "twigs-at-oxford": "牛津小枝",
}

MEAL_NAMES_CN = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "brunch": "早午餐",
}

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


ALLERGEN_NAMES_CN = {
    "Wheat": "小麦",
    "Soy": "大豆",
    "Milk": "牛奶",
    "Eggs": "鸡蛋",
    "Fish": "鱼",
    "Shellfish": "贝类",
    "Tree Nuts": "坚果",
    "Peanuts": "花生",
    "Sesame": "芝麻",
    "Gluten": "麸质",
    "Corn": "玉米",
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


def translate_names(names: list[str]) -> dict[str, str]:
    """Translate item names to Chinese using Google Translate (free)."""
    if not names:
        return {}

    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("  Warning: deep-translator not installed, skipping translation", file=sys.stderr)
        return {}

    translator = GoogleTranslator(source="en", target="zh-CN")
    translations = {}
    # Google Translate supports batch via translate_batch (max ~5000 chars)
    BATCH_SIZE = 50
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        if len(names) > BATCH_SIZE:
            print(f"    Batch {i // BATCH_SIZE + 1}/{(len(names) + BATCH_SIZE - 1) // BATCH_SIZE}...")
        try:
            results = translator.translate_batch(batch)
            for name, cn in zip(batch, results):
                if cn:
                    translations[name] = cn
        except Exception as e:
            print(f"  Warning: Translation batch failed ({e})", file=sys.stderr)
            # Fallback: translate one by one
            for name in batch:
                try:
                    cn = translator.translate(name)
                    if cn:
                        translations[name] = cn
                except Exception:
                    pass

    return translations


def load_translation_cache(cache_path: str) -> dict[str, str]:
    """Load cached translations from JSON file."""
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_translation_cache(cache_path: str, cache: dict[str, str]):
    """Save translations cache to JSON file."""
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def translate_with_cache(names: list[str], cache_path: str) -> dict[str, str]:
    """Translate names using cache, only calling API for new items."""
    cache = load_translation_cache(cache_path)

    # Find names not in cache
    new_names = [n for n in names if n not in cache]

    if new_names:
        print(f"  Translating {len(new_names)} new items ({len(names) - len(new_names)} cached)...")
        new_translations = translate_names(new_names)
        cache.update(new_translations)
        save_translation_cache(cache_path, cache)
    else:
        print(f"  All {len(names)} items found in cache.")

    return cache


def format_hall_name(slug: str) -> str:
    """Format hall slug to display name."""
    return slug.replace("-", " ").title()


def render_html(all_menus: list[dict], translations: dict[str, str],
                menu_date: str, item_stats: dict | None = None,
                num_days: int = 0) -> str:
    """Render all menu data into a self-contained HTML page."""
    date_display = datetime.strptime(menu_date, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    now = datetime.now(ZoneInfo("America/Detroit")).strftime("%Y-%m-%d %H:%M")

    # Build hall tabs and content
    hall_tabs_html = ""
    hall_contents_html = ""
    for i, menu in enumerate(all_menus):
        hall = menu["hall"]
        hall_en = format_hall_name(hall)
        hall_cn = HALL_NAMES_CN.get(hall, hall_en)
        active = " active" if i == 0 else ""

        hall_tabs_html += (
            f'<button class="hall-tab{active}" data-hall="{hall}">'
            f'{hall_en}'
            f'</button>\n'
        )

        meals_html = ""
        if not menu["meals"]:
            meals_html = '<div class="no-menu"><span class="cn">暂无菜单</span><span class="en">No menu available</span></div>'
        else:
            for meal_key, stations in menu["meals"].items():
                meal_cn = MEAL_NAMES_CN.get(meal_key, meal_key.title())
                meal_en = meal_key.title()
                # Normalize brunch to lunch tab so it's visible on weekends
                tab_key = "lunch" if meal_key == "brunch" else meal_key

                stations_html = ""
                for station_name, items in stations.items():
                    items_html = ""
                    for item in items:
                        name_en = item["name"]
                        name_cn = translations.get(name_en, name_en)

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

                        items_html += (
                            f'<div class="menu-item" data-traits="{trait_data}"{stat_attrs}>'
                            f'<span class="item-name">'
                            f'<span class="cn">{name_cn}</span>'
                            f'<span class="en">{name_en}</span>'
                            f'</span>'
                            f'{items_wrap}'
                            f'</div>'
                        )

                    station_label = station_name
                    stations_html += (
                        f'<div class="station">'
                        f'<h4 class="station-name">{station_label}</h4>'
                        f'{items_html}'
                        f'</div>'
                    )

                meals_html += (
                    f'<div class="meal-section" data-meal="{tab_key}">'
                    f'<h3 class="meal-name">'
                    f'<span class="cn">{meal_cn}</span>'
                    f'<span class="en">{meal_en}</span>'
                    f'</h3>'
                    f'{stations_html}'
                    f'</div>'
                )

        display = "block" if i == 0 else "none"
        hall_contents_html += (
            f'<div class="hall-content" data-hall="{hall}" data-hall-name="{hall_cn} {hall_en}" style="display:{display}">'
            f'{meals_html}'
            f'</div>\n'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="密歇根大学餐厅每日菜单 - Michigan Dining daily menus in Chinese and English">
<meta property="og:title" content="Michigan Dining Menus">
<meta property="og:description" content="Daily updated Michigan Dining menus with Chinese translations">
<meta property="og:type" content="website">
<meta property="og:locale" content="zh_CN">
<meta property="og:locale:alternate" content="en_US">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Michigan Dining Menus">
<meta name="twitter:description" content="Daily updated Michigan Dining menus with Chinese translations">
<meta name="theme-color" content="#0d6efd" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1a1a2e" media="(prefers-color-scheme: dark)">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍽️</text></svg>">
<title>Michigan Dining Menus</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&display=swap" rel="stylesheet">
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
.item-name .en {{
    color: var(--text-secondary);
    font-size: 0.8rem;
    margin-left: 4px;
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
/* Bilingual: Chinese primary, English secondary */
.cn {{ display: inline; }}
.en {{ display: inline; margin-left: 4px; }}
.item-name .cn {{ display: inline; }}
.item-name .en {{ display: inline; }}
.meal-name .en {{ margin-left: 6px; font-size: 0.85em; color: var(--text-secondary); }}
@media (max-width: 600px) {{
    body {{ padding: 8px; }}
    header h1 {{ font-size: 1.1rem; }}
    .controls {{ flex-wrap: wrap; justify-content: center; }}
    .hall-tab {{ padding: 4px 8px; font-size: 0.75rem; }}
    .hall-tab .en {{ font-size: 0.65em; }}
    .menu-item {{ padding: 2px 6px; gap: 3px; }}
    .item-name {{ font-size: 0.82rem; }}
    .item-name .en {{ font-size: 0.75rem; }}
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
</style>
</head>
<body>
<header>
    <h1>Michigan Dining Menus <span style="font-size:0.45em;font-weight:400;color:var(--text-secondary);vertical-align:middle">(unofficial)</span></h1>
    <div class="date-display">{date_display}</div>
    <div class="controls">
        <div class="toggle-switch" id="meal-toggle">
            <div class="toggle-slider" id="meal-slider"></div>
            <span class="toggle-option" data-meal="breakfast" onclick="switchMeal('breakfast')"><span class="cn">早餐</span><span class="en">Breakfast</span></span>
            <span class="toggle-option active" data-meal="lunch" onclick="switchMeal('lunch')"><span class="cn">午餐</span><span class="en">Lunch</span></span>
            <span class="toggle-option" data-meal="dinner" onclick="switchMeal('dinner')"><span class="cn">晚餐</span><span class="en">Dinner</span></span>
        </div>
        <div class="toggle-switch" id="theme-toggle" onclick="toggleTheme()">
            <div class="toggle-slider" id="theme-slider"></div>
            <span class="toggle-option active" data-theme="light">☀️</span>
            <span class="toggle-option" data-theme="dark">🌙</span>
        </div>
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
    </div>
</header>

<nav class="hall-tabs">
{hall_tabs_html}
</nav>

<main>
{hall_contents_html}
</main>

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
    <h2>About</h2>
    <p>Not affiliated with Michigan Dining. Menu data is scraped from the UMich dining website and may not always be accurate.</p>
    <p>If you represent the University of Michigan and have concerns, please <a href="https://github.com/xingjian-zhang/mdining/issues" target="_blank" style="color: var(--accent);">open an issue on GitHub</a>.</p>
    <p><a href="https://github.com/xingjian-zhang/mdining" target="_blank" style="color: var(--accent);">GitHub</a></p>
</div>
</div>

<script>
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
}}
// Default to lunch on load
document.querySelectorAll('.meal-section').forEach(el => {{
    if (el.dataset.meal !== 'lunch') el.classList.add('meal-hidden');
}});
updateMealSlider();

// Dietary filter toggles
let activeFilters = new Set();
const allBtn = () => document.querySelector('.filter-btn[data-filter="all"]');
function updateAllBtn() {{
    const btn = allBtn();
    if (btn) btn.classList.toggle('active', activeFilters.size === 0);
}}
function clearFilters() {{
    activeFilters.clear();
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
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
        if (activeFilters.size > 0) {{
            hidden = ![...activeFilters].some(f => f.split(' ').some(sub => traits.includes(sub)));
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

</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate Michigan Dining menu website")
    parser.add_argument("--output", default=SITE_DIR, help="Output directory (default: site)")
    parser.add_argument("--date", default=None, help="Menu date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-translate", action="store_true", help="Skip Chinese translation")
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
        k = min(10, len(all_days))
        recent_days = all_days[-k:]
        num_days = len(recent_days)
        item_stats = compute_item_stats(recent_days) if recent_days else None
        if item_stats:
            rare_count = sum(1 for v in item_stats.values() if v["count"] < 2)
            print(f"  Item stats: {len(item_stats)} items tracked ({rare_count} rare) over {num_days} days")

    # Render HTML
    print("Generating HTML...")
    html = render_html(all_menus, translations, menu_date,
                       item_stats=item_stats, num_days=num_days)

    os.makedirs(args.output, exist_ok=True)
    output_path = os.path.join(args.output, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Done! Output: {output_path} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
