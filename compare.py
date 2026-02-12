#!/usr/bin/env python3
"""
Compare menus across all Michigan Dining halls for a given meal.

Usage:
    python compare.py                  # Auto-detect current meal, all halls
    python compare.py dinner           # Compare dinner across all halls
    python compare.py lunch -v         # With calorie counts
    python compare.py dinner --vegan   # Only show vegan items
    python compare.py dinner --cn      # With Chinese translations
"""

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from scraper import DINING_HALLS, fetch_menu

# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
MAGENTA = "\033[35m"

TRAIT_SYMBOLS = {
    "Vegan": "V",
    "Vegetarian": "VG",
    "Gluten Free": "GF",
    "Halal": "H",
    "Kosher": "K",
}

DIET_FILTERS = {
    "vegan": "Vegan",
    "vegetarian": "Vegetarian",
    "gf": "Gluten Free",
    "halal": "Halal",
}


def guess_meal() -> str:
    hour = datetime.now().hour
    if hour < 10:
        return "breakfast"
    elif hour < 16:
        return "lunch"
    else:
        return "dinner"


def format_hall(slug: str) -> str:
    return slug.replace("-", " ").title()


def trait_tags(traits: list[str]) -> str:
    return " ".join(TRAIT_SYMBOLS[t] for t in traits if t in TRAIT_SYMBOLS)


def fetch_all(meal: str, menu_date: str | None = None) -> dict[str, dict]:
    """Fetch menus from all halls concurrently. Returns {hall_slug: data}."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(DINING_HALLS)) as pool:
        futures = {
            pool.submit(fetch_menu, hall, menu_date): hall
            for hall in DINING_HALLS
        }
        for future in as_completed(futures):
            hall = futures[future]
            try:
                data = future.result()
                if meal in data.get("meals", {}):
                    results[hall] = data["meals"][meal]
            except Exception as e:
                print(f"  {DIM}Failed to fetch {hall}: {e}{RESET}", file=sys.stderr)
    return results


def filter_by_diet(stations: dict, diet_trait: str) -> dict:
    """Filter stations to only items matching a dietary trait."""
    filtered = {}
    for station, items in stations.items():
        matching = [i for i in items if diet_trait in i.get("traits", [])]
        if matching:
            filtered[station] = matching
    return filtered


def collect_names_for_translation(hall_menus: dict[str, dict]) -> list[str]:
    names = []
    for stations in hall_menus.values():
        for items in stations.values():
            for item in items:
                if item["name"] not in names:
                    names.append(item["name"])
    return names


def translate_names(names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    prompt = (
        "Translate each food/dish name to Chinese (simplified). "
        "Return ONLY a numbered list with the Chinese translation, one per line, "
        "same order as input. No pinyin, no explanations.\n\n"
        + "\n".join(f"{i+1}. {n}" for i, n in enumerate(names))
    )
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", "haiku"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
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


def apply_translations(hall_menus: dict[str, dict], translations: dict[str, str]):
    for stations in hall_menus.values():
        for items in stations.values():
            for item in items:
                cn = translations.get(item["name"])
                if cn:
                    item["name"] = f"{item['name']}  {cn}"


def print_comparison(meal: str, menu_date: str, hall_menus: dict[str, dict],
                     verbose: bool = False):
    date_str = datetime.strptime(menu_date, "%Y-%m-%d").strftime("%A, %B %-d")
    print(f"\n{BOLD}{meal.upper()}{RESET}  {DIM}{date_str}{RESET}")
    print(f"{DIM}{'─' * 60}{RESET}")

    if not hall_menus:
        print(f"\n  {DIM}No halls serving {meal} on this date.{RESET}\n")
        return

    # Sort halls alphabetically
    for hall in sorted(hall_menus.keys()):
        stations = hall_menus[hall]
        item_count = sum(len(items) for items in stations.values())
        all_items = [i for items in stations.values() for i in items]
        vegan_count = sum(1 for i in all_items if "Vegan" in i.get("traits", []))

        stats = f"{item_count} items"
        if vegan_count:
            stats += f", {vegan_count} vegan"

        print(f"\n  {BOLD}{GREEN}{format_hall(hall)}{RESET}  {DIM}{stats}{RESET}")

        for station, items in stations.items():
            print(f"  {CYAN}{station}{RESET}")
            for item in items:
                tags = trait_tags(item.get("traits", []))
                tag_str = f" {DIM}{tags}{RESET}" if tags else ""
                cal_str = ""
                if verbose and item.get("nutrition", {}).get("calories"):
                    cal_str = f" {DIM}{item['nutrition']['calories']} cal{RESET}"
                print(f"    {item['name']}{tag_str}{cal_str}")

    print(f"\n{DIM}{'─' * 60}{RESET}")

    # Summary comparison
    print(f"\n{BOLD}Summary{RESET}")
    for hall in sorted(hall_menus.keys()):
        stations = hall_menus[hall]
        all_items = [i for items in stations.values() for i in items]
        total = len(all_items)
        vegan = sum(1 for i in all_items if "Vegan" in i.get("traits", []))
        veg = sum(1 for i in all_items if "Vegetarian" in i.get("traits", []))
        gf = sum(1 for i in all_items if "Gluten Free" in i.get("traits", []))
        halal = sum(1 for i in all_items if "Halal" in i.get("traits", []))
        station_names = list(stations.keys())

        print(f"  {BOLD}{format_hall(hall):20s}{RESET} "
              f"{total:2d} items  "
              f"{DIM}V:{vegan} VG:{veg} GF:{gf} H:{halal}{RESET}")
        print(f"  {' ':20s} {DIM}Stations: {', '.join(station_names)}{RESET}")

    print(f"\n{DIM}V=Vegan VG=Vegetarian GF=Gluten Free H=Halal K=Kosher{RESET}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="compare",
        description="Compare menus across all Michigan Dining halls.",
    )
    parser.add_argument(
        "meal", nargs="?", default=None,
        help="Meal to compare (breakfast/lunch/dinner). Auto-detects if omitted.",
    )
    parser.add_argument("-d", "--date", default=None, help="Date (YYYY-MM-DD)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show calories")
    parser.add_argument("--vegan", action="store_true", help="Only vegan items")
    parser.add_argument("--vegetarian", action="store_true", help="Only vegetarian items")
    parser.add_argument("--gf", action="store_true", help="Only gluten-free items")
    parser.add_argument("--halal", action="store_true", help="Only halal items")
    parser.add_argument("--cn", action="store_true", help="Translate to Chinese")
    args = parser.parse_args()

    meal = args.meal or guess_meal()
    meal = meal.lower()
    menu_date = args.date or datetime.now().strftime("%Y-%m-%d")

    print(f"{DIM}Fetching {meal} from {len(DINING_HALLS)} halls...{RESET}", end="", flush=True)
    hall_menus = fetch_all(meal, menu_date)
    print(f"\r{' ' * 40}\r", end="")

    # Apply diet filter
    diet_trait = None
    for flag, trait in DIET_FILTERS.items():
        if getattr(args, flag.replace("-", "_"), False):
            diet_trait = trait
            break
    if diet_trait:
        hall_menus = {
            hall: filtered
            for hall, stations in hall_menus.items()
            if (filtered := filter_by_diet(stations, diet_trait))
        }

    if args.cn:
        names = collect_names_for_translation(hall_menus)
        print(f"{DIM}Translating {len(names)} items...{RESET}", end="", flush=True)
        translations = translate_names(names)
        apply_translations(hall_menus, translations)
        print(f"\r{' ' * 40}\r", end="")

    print_comparison(meal, menu_date, hall_menus, verbose=args.verbose)


if __name__ == "__main__":
    main()
