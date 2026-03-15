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
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

STATION_NAMES_CN = {
    "Soup": "汤类",
    "Signature Maize": "招牌 Maize",
    "Signature Blue": "招牌 Blue",
    "24 Carrots": "24 Carrots 健康",
    "Halal": "清真",
    "Pizziti": "披萨",
    "Wild Fire Maize": "烧烤",
    "Deli": "熟食",
    "MBakery": "烘焙",
    "World Palate Maize": "世界风味",
    "World Palate Blue": "世界风味",
    "Kosher Deli": "犹太熟食",
    "Fresh Coast": "新鲜海岸",
    "Blue Garden": "花园沙拉",
    "Maize Garden": "花园沙拉",
    "Cereal": "麦片",
    "Grill": "烧烤",
    "Breakfast": "早餐",
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

SITE_DIR = "site"


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
                menu_date: str) -> str:
    """Render all menu data into a self-contained HTML page."""
    date_display = datetime.strptime(menu_date, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

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

                        trait_data = " ".join(
                            TRAIT_DISPLAY[t][1] if t in TRAIT_DISPLAY else t.lower().replace(" ", "-")
                            for t in item.get("traits", [])
                        )
                        items_wrap = f'<span class="item-traits">{traits_html}</span>' if traits_html else ''
                        items_html += (
                            f'<div class="menu-item" data-traits="{trait_data}">'
                            f'<span class="item-name">'
                            f'<span class="cn">{name_cn}</span>'
                            f'<span class="en">{name_en}</span>'
                            f'</span>'
                            f'{items_wrap}'
                            f'</div>'
                        )

                    station_cn = STATION_NAMES_CN.get(station_name, "")
                    station_label = (
                        f'<span class="cn">{station_cn}</span> <span class="en">{station_name}</span>'
                        if station_cn else station_name
                    )
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
<meta property="og:title" content="密歇根大学餐厅菜单 / Michigan Dining Menus">
<meta property="og:description" content="每日更新的密歇根大学餐厅菜单，中英双语 - Daily updated bilingual menus">
<meta property="og:type" content="website">
<meta property="og:locale" content="zh_CN">
<meta property="og:locale:alternate" content="en_US">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="密歇根大学餐厅菜单 / Michigan Dining Menus">
<meta name="twitter:description" content="每日更新的密歇根大学餐厅菜单，中英双语">
<meta name="theme-color" content="#0d6efd" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1a1a2e" media="(prefers-color-scheme: dark)">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🍽️</text></svg>">
<title>密歇根大学餐厅菜单 / Michigan Dining Menus</title>
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
    gap: 4px;
    flex-wrap: wrap;
    margin-bottom: 4px;
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
    .controls, .toggle-btn, .filter-bar, .hall-tabs {{ display: none; }}
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
</style>
</head>
<body>
<header>
    <h1>
        <span class="cn">密歇根大学餐厅菜单</span>
        <span class="en">Michigan Dining Menus</span>
    </h1>
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
    <div class="filter-bar">
        <button class="filter-btn active" data-filter="all" onclick="clearFilters()">All</button>
        <button class="filter-btn" data-filter="vegan" onclick="toggleFilter(this)">Vegan</button>
        <button class="filter-btn" data-filter="vegetarian" onclick="toggleFilter(this)">Vegetarian</button>
        <button class="filter-btn" data-filter="gluten-free" onclick="toggleFilter(this)">Gluten Free</button>
        <button class="filter-btn" data-filter="halal" onclick="toggleFilter(this)">Halal</button>
        <button class="filter-btn" data-filter="kosher" onclick="toggleFilter(this)">Kosher</button>
        <button class="filter-btn" data-filter="carbon-low" onclick="toggleFilter(this)">Low CO₂</button>
        <button class="filter-btn" data-filter="carbon-high" onclick="toggleFilter(this)">High CO₂</button>
        <button class="filter-btn" data-filter="nutri-high nutri-medhigh" onclick="toggleFilter(this)">Nutritious</button>
    </div>
</header>

<nav class="hall-tabs">
{hall_tabs_html}
</nav>

<main>
{hall_contents_html}
</main>

<footer>
    Last updated: {now}
</footer>

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
    if (activeFilters.size === 0) {{
        document.querySelectorAll('.menu-item').forEach(el => el.classList.remove('filtered-out'));
        return;
    }}
    document.querySelectorAll('.menu-item').forEach(el => {{
        const traits = el.dataset.traits || '';
        const match = [...activeFilters].some(f => f.split(' ').some(sub => traits.includes(sub)));
        el.classList.toggle('filtered-out', !match);
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
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate Michigan Dining menu website")
    parser.add_argument("--output", default=SITE_DIR, help="Output directory (default: site)")
    parser.add_argument("--date", default=None, help="Menu date YYYY-MM-DD (default: today)")
    parser.add_argument("--no-translate", action="store_true", help="Skip Chinese translation")
    args = parser.parse_args()

    menu_date = args.date or datetime.now().strftime("%Y-%m-%d")

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
    translations = {}
    if not args.no_translate:
        names = collect_unique_names(all_menus)
        if names:
            cache_path = os.path.join(args.output, "translations_cache.json")
            translations = translate_with_cache(names, cache_path)
            print(f"  Translations: {len(translations)} / {len(names)} items")

    # Render HTML
    print("Generating HTML...")
    html = render_html(all_menus, translations, menu_date)

    os.makedirs(args.output, exist_ok=True)
    output_path = os.path.join(args.output, "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Done! Output: {output_path} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
