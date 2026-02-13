# mdining

CLI tools for checking University of Michigan dining hall menus from the terminal.

## Features

- View menus for any dining hall and date
- Compare menus across all halls side-by-side
- Dietary filters: vegan, vegetarian, gluten-free, halal
- Calorie counts and nutrition info
- Chinese translation of menu items (via `claude` CLI)
- JSON output for scripting
- Short aliases for hall names

## Requirements

- Python 3.10+
- [requests](https://pypi.org/project/requests/)
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/)
- (Optional) [Claude CLI](https://github.com/anthropics/claude-code) for `--cn` translation

## Usage

### `menu.py` — Single hall menu

```sh
python menu.py                        # Bursley, today, all meals
python menu.py dinner                 # Bursley, dinner only
python menu.py -l south-quad          # South Quad, today
python menu.py -l eq lunch            # East Quad, lunch
python menu.py -d 2026-02-15          # Specific date
python menu.py dinner -v              # Show calorie counts
python menu.py --cn                   # Translate item names to Chinese
python menu.py --json                 # Raw JSON output
```

Flags:
| Flag | Description |
|------|-------------|
| `-l, --hall HALL` | Dining hall slug or alias (default: `bursley`) |
| `-d, --date DATE` | Date in `YYYY-MM-DD` format (default: today) |
| `-v, --verbose` | Show calorie counts |
| `--cn` | Translate item names to Chinese |
| `--json` | Output raw JSON |

### `compare.py` — Multi-hall comparison

```sh
python compare.py                     # Auto-detect meal, all halls
python compare.py dinner              # Compare dinner across all halls
python compare.py lunch -v            # With calorie counts
python compare.py dinner --vegan      # Only vegan items
python compare.py --cn                # With Chinese translations
```

Flags:
| Flag | Description |
|------|-------------|
| `-d, --date DATE` | Date in `YYYY-MM-DD` format (default: today) |
| `-v, --verbose` | Show calorie counts |
| `--vegan` | Filter to vegan items only |
| `--vegetarian` | Filter to vegetarian items only |
| `--gf` | Filter to gluten-free items only |
| `--halal` | Filter to halal items only |
| `--cn` | Translate item names to Chinese |

### `scraper.py` — Raw JSON scraper

```sh
python scraper.py                             # Bursley, today
python scraper.py --hall south-quad           # South Quad
python scraper.py --date 2026-02-15 --meal dinner
python scraper.py --compact                   # Names only, no nutrition
```

## Dining Halls & Aliases

| Hall | Alias |
|------|-------|
| `bursley` | `b` |
| `east-quad` | `eq` |
| `mosher-jordan` | `mj` |
| `south-quad` | `sq` |
| `twigs-at-oxford` | `twigs` |

## Dietary Tag Legend

| Symbol | Meaning |
|--------|---------|
| V | Vegan |
| VG | Vegetarian |
| GF | Gluten Free |
| H | Halal |
| K | Kosher |
