"""
Michigan Dining - Bursley Menu Scraper

Scrapes menu data from dining.umich.edu and returns structured JSON.
Supports any dining hall and any date with the ?menuDate= parameter.

Usage:
    python scraper.py                          # Bursley, today
    python scraper.py --date 2026-02-15        # Bursley, specific date
    python scraper.py --hall south-quad         # South Quad, today
    python scraper.py --hall bursley --date 2026-02-15 --meal dinner
"""

import argparse
import json
import re
import sys
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://dining.umich.edu/menus-locations/dining-halls/{hall}/"

DINING_HALLS = [
    "bursley",
    "east-quad",
    "mosher-jordan",
    "south-quad",
    "twigs-at-oxford",
]

HALL_ALIASES = {
    "bursley": "bursley", "b": "bursley",
    "east-quad": "east-quad", "eq": "east-quad",
    "mosher-jordan": "mosher-jordan", "mj": "mosher-jordan",
    "south-quad": "south-quad", "sq": "south-quad",
    "twigs-at-oxford": "twigs-at-oxford", "twigs": "twigs-at-oxford",
}


def resolve_hall(name: str) -> str:
    """Resolve a hall name or alias to the canonical slug."""
    key = name.lower().strip()
    if key in HALL_ALIASES:
        return HALL_ALIASES[key]
    raise ValueError(
        f"Unknown hall '{name}'. Options: {', '.join(DINING_HALLS)} "
        f"(aliases: b, eq, mj, sq, twigs)"
    )


def fetch_menu(hall: str = "bursley", menu_date: str | None = None) -> dict:
    """Fetch and parse the menu for a given dining hall and date.

    Args:
        hall: Dining hall slug (e.g. "bursley", "south-quad").
        menu_date: Date string in YYYY-MM-DD format. Defaults to today.

    Returns:
        Dict with hall, date, and meals (each containing stations and items).
    """
    if menu_date is None:
        menu_date = date.today().isoformat()

    url = BASE_URL.format(hall=hall)
    params = {"menuDate": menu_date}

    # Retry with exponential backoff for transient failures
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s
    else:
        raise last_exc  # type: ignore[misc]

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find(id="mdining-items")

    if not container:
        return {"hall": hall, "date": menu_date, "meals": {}}

    meals = {}
    # Each meal is: <h3> header followed by <div class="courses">
    meal_headers = container.find_all("h3")

    for h3 in meal_headers:
        meal_name = h3.get_text(strip=True).lower()
        courses_div = h3.find_next_sibling("div", class_="courses")
        if not courses_div:
            continue

        stations = {}
        # The courses div contains a mix of direct children:
        #   - <ul class="courses_wrapper"> with the first station
        #   - Flat <li> elements for subsequent stations/items
        #   - <div class="nutrition"> elements (ignored)
        # An <li> with <h4> starts a new station; <li> without <h4>
        # belongs to the current station. Items may be nested in
        # <ul class="items"> or directly in the <li>.
        current_station = None
        for child in courses_div.children:
            if not child.name:
                continue
            # Process <li> elements (both inside wrapper and flat)
            lis_to_process = []
            if child.name == "ul" and "courses_wrapper" in (child.get("class") or []):
                lis_to_process = child.find_all("li", recursive=False)
            elif child.name == "li":
                lis_to_process = [child]
            for li in lis_to_process:
                h4 = li.find("h4")
                if h4:
                    current_station = h4.get_text(strip=True)
                    if current_station not in stations:
                        stations[current_station] = []
                    # Parse items nested in <ul class="items">
                    for item_li in li.select("ul.items > li"):
                        item = parse_item(item_li)
                        if item:
                            stations[current_station].append(item)
                elif current_station:
                    # Flat item <li> belonging to the current station
                    item = parse_item(li)
                    if item:
                        stations[current_station].append(item)

        if stations:
            meals[meal_name] = stations

    return {"hall": hall, "date": menu_date, "meals": meals}


ALLERGEN_CLASS_MAP = {
    "eggs": "Eggs",
    "milk": "Milk",
    "oats": "Oats",
    "peanuts": "Peanuts",
    "sesame-seed": "Sesame",
    "soy": "Soy",
    "tree-nuts": "Tree Nuts",
    "wheat_barley_rye": "Wheat",
    "fish": "Fish",
    "shellfish": "Shellfish",
    "corn": "Corn",
}

# CSS classes that look like allergens but are meat markers, not allergens
ALLERGEN_SKIP = {"beef", "pork", "chicken", "turkey", "lamb"}


def parse_item(li) -> dict | None:
    """Parse a single menu item <li> element."""
    name_el = li.select_one(".item-name")
    if not name_el:
        return None

    name = name_el.get_text(strip=True)

    # Traits (dietary tags)
    traits = [t.get_text(strip=True) for t in li.select(".traits li")]

    # Allergens from CSS classes (e.g., allergen-eggs, allergen-milk)
    allergens = []
    for cls in li.get("class", []):
        if cls.startswith("allergen-"):
            key = cls[len("allergen-"):]
            if key in ALLERGEN_SKIP:
                continue
            label = ALLERGEN_CLASS_MAP.get(key, key.replace("-", " ").replace("_", " ").title())
            allergens.append(label)

    # Nutrition facts from next sibling <div class="nutrition">
    nutrition = None
    next_el = li.find_next_sibling()
    if next_el and next_el.name == "div" and "nutrition" in (next_el.get("class") or []):
        nutrition = parse_nutrition(next_el)

    item = {"name": name}
    if traits:
        item["traits"] = traits
    if allergens:
        item["allergens"] = allergens
    if nutrition:
        item["nutrition"] = nutrition

    return item


def parse_nutrition(li) -> dict | None:
    """Parse the nutrition facts table from a menu item."""
    table = li.select_one("table.nutrition-facts")
    if not table:
        return None

    nutrition = {}

    # Serving size
    serving = table.select_one("tr.serving-size td")
    if serving:
        text = serving.get_text(strip=True)
        text = text.replace("Serving Size", "").strip()
        nutrition["serving_size"] = text

    # Calories
    cal = table.select_one("tr.portion-calories td")
    if cal:
        text = cal.get_text(strip=True)
        match = re.search(r"(\d+)", text)
        if match:
            nutrition["calories"] = int(match.group(1))

    # Macro/micro rows
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 1:
            continue

        label_text = cells[0].get_text(strip=True)

        # Skip header-like rows
        if not label_text or "Amount Per" in label_text or "Daily Value" in label_text:
            continue
        if "Serving Size" in label_text or "Calories" in label_text:
            continue

        dv = cells[1].get_text(strip=True) if len(cells) > 1 else ""

        # Normalize the key from the label
        key = re.sub(r"\s+", "_", label_text).lower()
        key = re.sub(r"_?\d+\.?\d*\s*m?g$", "", key)

        # Micronutrient rows just have the name in col0 and "X%" in col1
        is_micro = "micronutrient" in (row.get("class") or [])
        if is_micro:
            nutrition[key] = dv  # e.g. "10%"
        else:
            # Macro rows: extract the amount from the label text
            val_match = re.search(r"(\d+\.?\d*)\s*m?g", label_text)
            value = val_match.group(0) if val_match else label_text
            nutrition[key] = value

    return nutrition if len(nutrition) > 1 else None


def main():
    parser = argparse.ArgumentParser(description="Scrape Michigan Dining menus")
    parser.add_argument("--hall", default="bursley", choices=DINING_HALLS,
                        help="Dining hall to fetch (default: bursley)")
    parser.add_argument("--date", default=None,
                        help="Menu date in YYYY-MM-DD format (default: today)")
    parser.add_argument("--meal", default=None,
                        help="Filter to a specific meal (breakfast, lunch, dinner)")
    parser.add_argument("--compact", action="store_true",
                        help="Compact output (names only, no nutrition)")
    args = parser.parse_args()

    result = fetch_menu(hall=args.hall, menu_date=args.date)

    if args.meal:
        meal_key = args.meal.lower()
        if meal_key in result["meals"]:
            result["meals"] = {meal_key: result["meals"][meal_key]}
        else:
            print(f"No '{args.meal}' meal found. Available: {list(result['meals'].keys())}",
                  file=sys.stderr)
            sys.exit(1)

    if args.compact:
        compact = {"hall": result["hall"], "date": result["date"], "meals": {}}
        for meal, stations in result["meals"].items():
            compact["meals"][meal] = {
                station: [item["name"] for item in items]
                for station, items in stations.items()
            }
        result = compact

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
