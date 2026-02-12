#!/usr/bin/env python3
"""
mdining - Quick CLI to check Michigan Dining menus.

Usage:
    python menu.py                     # Bursley today
    python menu.py dinner              # Bursley dinner only
    python menu.py -h south-quad       # South Quad today
    python menu.py -d 2026-02-15       # Specific date
    python menu.py dinner -v           # Show calories & traits
    python menu.py --json              # Raw JSON output
"""

import argparse
import subprocess
import sys
from datetime import datetime

from scraper import HALL_ALIASES, fetch_menu, resolve_hall

# ANSI colors
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"

TRAIT_SYMBOLS = {
    "Vegan": "V",
    "Vegetarian": "VG",
    "Gluten Free": "GF",
    "Halal": "H",
    "Kosher": "K",
}


def format_hall_name(slug: str) -> str:
    return slug.replace("-", " ").title()


def format_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%A, %B %-d")


def trait_tags(traits: list[str]) -> str:
    tags = []
    for t in traits:
        if t in TRAIT_SYMBOLS:
            tags.append(TRAIT_SYMBOLS[t])
    return " ".join(tags)


def translate_menu(data: dict) -> dict:
    """Translate menu item names to Chinese using claude CLI."""
    # Collect all unique item names
    names = []
    for stations in data["meals"].values():
        for items in stations.values():
            for item in items:
                if item["name"] not in names:
                    names.append(item["name"])

    if not names:
        return data

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
        print(f"Translation failed: {result.stderr}", file=sys.stderr)
        return data

    # Parse translations back into a mapping
    translations = {}
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Match "1. 燕麦片" or "1、燕麦片" etc.
        parts = line.split(".", 1) if "." in line[:5] else line.split("、", 1)
        if len(parts) == 2:
            try:
                idx = int(parts[0].strip()) - 1
                if 0 <= idx < len(names):
                    translations[names[idx]] = parts[1].strip()
            except ValueError:
                continue

    # Apply translations
    for stations in data["meals"].values():
        for items in stations.values():
            for item in items:
                cn = translations.get(item["name"])
                if cn:
                    item["name"] = f"{item['name']}  {cn}"

    return data


def print_menu(data: dict, verbose: bool = False):
    hall = format_hall_name(data["hall"])
    date_str = format_date(data["date"])

    print(f"\n{BOLD}{hall}{RESET}  {DIM}{date_str}{RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")

    if not data["meals"]:
        print(f"\n  {DIM}No menu available for this date.{RESET}\n")
        return

    for meal, stations in data["meals"].items():
        print(f"\n  {BOLD}{YELLOW}{meal.upper()}{RESET}")

        for station, items in stations.items():
            print(f"  {CYAN}{station}{RESET}")

            for item in items:
                tags = trait_tags(item.get("traits", []))
                tag_str = f" {DIM}{tags}{RESET}" if tags else ""

                cal_str = ""
                if verbose and item.get("nutrition", {}).get("calories"):
                    cal = item["nutrition"]["calories"]
                    cal_str = f" {DIM}{cal} cal{RESET}"

                print(f"    {item['name']}{tag_str}{cal_str}")

        print()

    print(f"{DIM}{'─' * 50}{RESET}")
    print(f"{DIM}V=Vegan VG=Vegetarian GF=Gluten Free H=Halal K=Kosher{RESET}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="menu",
        description="Check Michigan Dining menus from the terminal.",
    )
    parser.add_argument(
        "meal", nargs="?", default=None,
        help="Filter to a meal: breakfast, lunch, or dinner",
    )
    aliases = ", ".join(f"{a}={v}" for a, v in HALL_ALIASES.items() if a != v)
    parser.add_argument(
        "-l", "--hall", default="bursley",
        metavar="HALL",
        help=f"Dining hall (default: bursley). Aliases: {aliases}",
    )
    parser.add_argument(
        "-d", "--date", default=None,
        help="Date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show calorie counts",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text",
    )
    parser.add_argument(
        "--cn", action="store_true",
        help="Translate menu item names to Chinese via claude CLI",
    )
    args = parser.parse_args()

    try:
        hall = resolve_hall(args.hall)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    data = fetch_menu(hall=hall, menu_date=args.date)

    if args.meal:
        key = args.meal.lower()
        if key in data["meals"]:
            data["meals"] = {key: data["meals"][key]}
        else:
            avail = ", ".join(data["meals"].keys()) or "none"
            print(f"No '{args.meal}' menu found. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    if args.cn:
        data = translate_menu(data)

    if args.json:
        import json
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_menu(data, verbose=args.verbose)


if __name__ == "__main__":
    main()
