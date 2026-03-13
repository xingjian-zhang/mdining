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
import subprocess
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

TRAIT_NAMES_CN = {
    "Vegan": "纯素",
    "Vegetarian": "素食",
    "Gluten Free": "无麸质",
    "Halal": "清真",
    "Kosher": "犹太洁食",
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


def translate_batch(names: list[str]) -> dict[str, str]:
    """Translate a single batch of item names via Claude CLI."""
    prompt = (
        "Translate each food/dish name to Chinese (simplified). "
        "Return ONLY a numbered list with the Chinese translation, one per line, "
        "same order as input. No pinyin, no explanations.\n\n"
        + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  Warning: Translation batch failed ({e})", file=sys.stderr)
        return {}

    if result.returncode != 0:
        print(f"  Warning: Translation batch failed (exit {result.returncode})", file=sys.stderr)
        return {}

    translations = {}
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(".", 1) if "." in line[:5] else line.split("、", 1)
        if len(parts) == 2:
            try:
                idx = int(parts[0].strip()) - 1
                if 0 <= idx < len(names):
                    translations[names[idx]] = parts[1].strip()
            except ValueError:
                continue
    return translations


def translate_names(names: list[str]) -> dict[str, str]:
    """Translate item names to Chinese using Claude CLI. Batches large lists."""
    if not names:
        return {}

    # Batch into groups of 50 to avoid CLI timeouts on large menus
    BATCH_SIZE = 50
    all_translations = {}
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        if len(names) > BATCH_SIZE:
            print(f"    Batch {i // BATCH_SIZE + 1}/{(len(names) + BATCH_SIZE - 1) // BATCH_SIZE}...",
                  file=sys.stderr)
        result = translate_batch(batch)
        all_translations.update(result)
    return all_translations


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
            f'<span class="cn">{hall_cn}</span>'
            f'<span class="en">{hall_en}</span>'
            f'</button>\n'
        )

        meals_html = ""
        if not menu["meals"]:
            meals_html = '<div class="no-menu"><span class="cn">暂无菜单</span><span class="en">No menu available</span></div>'
        else:
            for meal_key, stations in menu["meals"].items():
                meal_cn = MEAL_NAMES_CN.get(meal_key, meal_key.title())
                meal_en = meal_key.title()

                stations_html = ""
                for station_name, items in stations.items():
                    items_html = ""
                    for item in items:
                        name_en = item["name"]
                        name_cn = translations.get(name_en, name_en)

                        # Trait badges
                        traits_html = ""
                        for trait in item.get("traits", []):
                            trait_cn = TRAIT_NAMES_CN.get(trait, trait)
                            css_class = trait.lower().replace(" ", "-")
                            traits_html += (
                                f'<span class="trait-badge {css_class}">'
                                f'<span class="cn">{trait_cn}</span>'
                                f'<span class="en">{trait}</span>'
                                f'</span>'
                            )

                        # Allergens
                        allergens_html = ""
                        if item.get("allergens"):
                            allergen_items = []
                            for a in item["allergens"]:
                                a_cn = ALLERGEN_NAMES_CN.get(a, a)
                                allergen_items.append(
                                    f'<span class="allergen">'
                                    f'<span class="cn">{a_cn}</span>'
                                    f'<span class="en">{a}</span>'
                                    f'</span>'
                                )
                            allergens_html = (
                                f'<div class="allergens">'
                                f'<span class="allergen-label">'
                                f'<span class="cn">过敏原:</span>'
                                f'<span class="en">Allergens:</span>'
                                f'</span> '
                                f'{", ".join(allergen_items)}'
                                f'</div>'
                            )

                        # Nutrition (collapsible)
                        nutrition_html = ""
                        if item.get("nutrition"):
                            nut = item["nutrition"]
                            rows = ""
                            for key, val in nut.items():
                                label = key.replace("_", " ").title()
                                rows += f"<tr><td>{label}</td><td>{val}</td></tr>"
                            nutrition_html = (
                                f'<details class="nutrition-details">'
                                f'<summary>'
                                f'<span class="cn">营养信息</span>'
                                f'<span class="en">Nutrition</span>'
                                f'</summary>'
                                f'<table class="nutrition-table">{rows}</table>'
                                f'</details>'
                            )

                        trait_data = " ".join(
                            t.lower().replace(" ", "-") for t in item.get("traits", [])
                        )
                        items_html += (
                            f'<div class="menu-item" data-traits="{trait_data}">'
                            f'<div class="item-header">'
                            f'<span class="item-name">'
                            f'<span class="cn">{name_cn}</span>'
                            f'<span class="en">{name_en}</span>'
                            f'</span>'
                            f'<span class="item-traits">{traits_html}</span>'
                            f'</div>'
                            f'{allergens_html}'
                            f'{nutrition_html}'
                            f'</div>'
                        )

                    stations_html += (
                        f'<div class="station">'
                        f'<h4 class="station-name">{station_name}</h4>'
                        f'{items_html}'
                        f'</div>'
                    )

                meals_html += (
                    f'<div class="meal-section">'
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
    --bg: #ffffff;
    --bg-card: #f8f9fa;
    --bg-hover: #e9ecef;
    --text: #212529;
    --text-secondary: #6c757d;
    --border: #dee2e6;
    --accent: #0d6efd;
    --accent-light: #e7f1ff;
    --shadow: 0 1px 3px rgba(0,0,0,0.08);
    --radius: 8px;
}}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) {{
        --bg: #1a1a2e;
        --bg-card: #16213e;
        --bg-hover: #1a2744;
        --text: #e4e6eb;
        --text-secondary: #8b949e;
        --border: #30363d;
        --accent: #58a6ff;
        --accent-light: #1c2d41;
        --shadow: 0 1px 3px rgba(0,0,0,0.3);
    }}
}}
.dark-theme {{
    --bg: #1a1a2e;
    --bg-card: #16213e;
    --bg-hover: #1a2744;
    --text: #e4e6eb;
    --text-secondary: #8b949e;
    --border: #30363d;
    --accent: #58a6ff;
    --accent-light: #1c2d41;
    --shadow: 0 1px 3px rgba(0,0,0,0.3);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Noto Sans SC', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    max-width: 900px;
    margin: 0 auto;
    padding: 16px;
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
.toggle-btn {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 0.85rem;
    transition: all 0.2s;
}}
.toggle-btn:hover {{
    background: var(--bg-hover);
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
    margin-bottom: 24px;
}}
.meal-name {{
    font-size: 1.2rem;
    font-weight: 600;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 12px;
}}
.station {{
    margin-bottom: 16px;
}}
.station-name {{
    font-size: 0.95rem;
    font-weight: 500;
    color: var(--accent);
    padding: 4px 0;
    margin-bottom: 8px;
}}
.menu-item {{
    background: var(--bg-card);
    border-radius: var(--radius);
    padding: 10px 14px;
    margin-bottom: 6px;
    box-shadow: var(--shadow);
}}
.item-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
}}
.item-name {{
    font-weight: 500;
    flex: 1;
}}
.item-name .en {{
    color: var(--text-secondary);
    font-size: 0.85rem;
    font-weight: 400;
}}
.item-traits {{
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    flex-shrink: 0;
}}
.trait-badge {{
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 12px;
    font-weight: 500;
    white-space: nowrap;
}}
.trait-badge.vegan {{ background: #d4edda; color: #155724; }}
.trait-badge.vegetarian {{ background: #d1ecf1; color: #0c5460; }}
.trait-badge.gluten-free {{ background: #fff3cd; color: #856404; }}
.trait-badge.halal {{ background: #f8d7da; color: #721c24; }}
.trait-badge.kosher {{ background: #e2d5f1; color: #4a235a; }}
@media (prefers-color-scheme: dark) {{
    :root:not(.light-theme) .trait-badge.vegan {{ background: #1e3a2a; color: #75d69c; }}
    :root:not(.light-theme) .trait-badge.vegetarian {{ background: #1a3a4a; color: #6ec8db; }}
    :root:not(.light-theme) .trait-badge.gluten-free {{ background: #3a3520; color: #e0c36a; }}
    :root:not(.light-theme) .trait-badge.halal {{ background: #3a1a1a; color: #e87878; }}
    :root:not(.light-theme) .trait-badge.kosher {{ background: #2a1a3a; color: #b088d0; }}
}}
.dark-theme .trait-badge.vegan {{ background: #1e3a2a; color: #75d69c; }}
.dark-theme .trait-badge.vegetarian {{ background: #1a3a4a; color: #6ec8db; }}
.dark-theme .trait-badge.gluten-free {{ background: #3a3520; color: #e0c36a; }}
.dark-theme .trait-badge.halal {{ background: #3a1a1a; color: #e87878; }}
.dark-theme .trait-badge.kosher {{ background: #2a1a3a; color: #b088d0; }}
.allergens {{
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin-top: 4px;
}}
.allergen-label {{
    font-weight: 500;
}}
.nutrition-details {{
    margin-top: 6px;
    font-size: 0.8rem;
}}
.nutrition-details summary {{
    cursor: pointer;
    color: var(--text-secondary);
    font-weight: 500;
}}
.nutrition-table {{
    width: 100%;
    margin-top: 6px;
    border-collapse: collapse;
}}
.nutrition-table td {{
    padding: 3px 8px;
    border-bottom: 1px solid var(--border);
    font-size: 0.78rem;
}}
.nutrition-table td:last-child {{
    text-align: right;
    font-weight: 500;
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
/* Dietary filter bar */
.filter-bar {{
    display: flex;
    justify-content: center;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 12px;
}}
.filter-btn {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    padding: 4px 12px;
    border-radius: 16px;
    cursor: pointer;
    font-size: 0.8rem;
    font-family: inherit;
    transition: all 0.2s;
}}
.filter-btn:hover {{ background: var(--bg-hover); }}
.filter-btn.active {{ border-color: var(--accent); color: var(--accent); background: var(--accent-light); }}
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
/* Language toggle: default shows Chinese primary */
body.lang-en .cn {{ display: none; }}
body.lang-cn .en {{ display: none; }}
body:not(.lang-en):not(.lang-cn) .item-name .cn {{ display: block; }}
body:not(.lang-en):not(.lang-cn) .item-name .en {{ display: block; }}
body:not(.lang-en):not(.lang-cn) .meal-name .en,
body:not(.lang-en):not(.lang-cn) .hall-tab .en {{
    font-size: 0.8em;
    color: var(--text-secondary);
}}
/* Default: both languages shown, Chinese primary */
.cn {{ display: inline; }}
.en {{ display: inline; margin-left: 4px; }}
.item-name .cn {{ display: block; margin-left: 0; }}
.item-name .en {{ display: block; margin-left: 0; }}
.meal-name .en {{ margin-left: 6px; font-size: 0.85em; color: var(--text-secondary); }}
.hall-tab .en {{ display: block; font-size: 0.75em; color: var(--text-secondary); }}
.hall-tab .cn {{ display: block; }}
body.lang-en .cn {{ display: none !important; }}
body.lang-cn .en {{ display: none !important; }}
@media (max-width: 600px) {{
    body {{ padding: 8px; }}
    header h1 {{ font-size: 1.2rem; }}
    .hall-tab {{ padding: 6px 10px; font-size: 0.8rem; }}
    .menu-item {{ padding: 8px 10px; }}
    .item-header {{ flex-direction: column; }}
    .item-traits {{ margin-top: 4px; }}
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
        <button class="toggle-btn" id="lang-toggle" onclick="toggleLang()">EN / 中文</button>
        <button class="toggle-btn" id="theme-toggle" onclick="toggleTheme()">🌙 / ☀️</button>
    </div>
</header>

<div class="filter-bar">
    <button class="filter-btn" data-filter="vegan" onclick="toggleFilter(this)">
        <span class="cn">纯素</span><span class="en">Vegan</span>
    </button>
    <button class="filter-btn" data-filter="vegetarian" onclick="toggleFilter(this)">
        <span class="cn">素食</span><span class="en">Vegetarian</span>
    </button>
    <button class="filter-btn" data-filter="gluten-free" onclick="toggleFilter(this)">
        <span class="cn">无麸质</span><span class="en">GF</span>
    </button>
    <button class="filter-btn" data-filter="halal" onclick="toggleFilter(this)">
        <span class="cn">清真</span><span class="en">Halal</span>
    </button>
    <button class="filter-btn" data-filter="kosher" onclick="toggleFilter(this)">
        <span class="cn">犹太洁食</span><span class="en">Kosher</span>
    </button>
</div>

<nav class="hall-tabs">
{hall_tabs_html}
</nav>

<main>
{hall_contents_html}
</main>

<footer>
    <span class="cn">最后更新: {now}</span>
    <span class="en">Last updated: {now}</span>
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

// Dietary filter toggles
let activeFilters = new Set();
function toggleFilter(btn) {{
    const filter = btn.dataset.filter;
    if (activeFilters.has(filter)) {{
        activeFilters.delete(filter);
        btn.classList.remove('active');
    }} else {{
        activeFilters.add(filter);
        btn.classList.add('active');
    }}
    applyFilters();
}}
function applyFilters() {{
    if (activeFilters.size === 0) {{
        document.querySelectorAll('.menu-item').forEach(el => el.classList.remove('filtered-out'));
        return;
    }}
    document.querySelectorAll('.menu-item').forEach(el => {{
        const traits = el.dataset.traits || '';
        const match = [...activeFilters].some(f => traits.includes(f));
        el.classList.toggle('filtered-out', !match);
    }});
}}

// Language toggle
let langState = 0; // 0=both, 1=cn-only, 2=en-only
function toggleLang() {{
    langState = (langState + 1) % 3;
    document.body.classList.remove('lang-cn', 'lang-en');
    if (langState === 1) document.body.classList.add('lang-cn');
    else if (langState === 2) document.body.classList.add('lang-en');
    const labels = ['中英', '中文', 'EN'];
    document.getElementById('lang-toggle').textContent = labels[langState];
}}

// Theme toggle
function toggleTheme() {{
    const html = document.documentElement;
    if (html.classList.contains('dark-theme')) {{
        html.classList.remove('dark-theme');
        html.classList.add('light-theme');
    }} else if (html.classList.contains('light-theme')) {{
        html.classList.remove('light-theme');
        html.classList.add('dark-theme');
    }} else {{
        // Auto mode — detect and flip
        const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        html.classList.add(isDark ? 'light-theme' : 'dark-theme');
    }}
}}
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
