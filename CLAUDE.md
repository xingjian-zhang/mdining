# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bilingual (Chinese/English) Michigan Dining menu website and CLI tools. The website is a static single-page app generated at build time and deployed to GitHub Pages.

## Key Commands

```sh
# Generate the website (fetches live menu data, translates, outputs site/index.html)
python3 generate_site.py

# Skip translation (faster, for testing layout/JS changes)
python3 generate_site.py --no-translate

# Generate for a specific date
python3 generate_site.py --date 2026-03-15

# CLI tools (not part of the website)
python3 menu.py                   # Single hall menu
python3 compare.py dinner --vegan # Multi-hall comparison
```

Dependencies: `pip install requests beautifulsoup4 deep-translator`

## Architecture

### Website Generation Pipeline

```
scraper.py::fetch_menu()          # Scrapes UMich dining API for each hall
    → generate_site.py::fetch_all_halls()  # Concurrent fetch of all 5 halls
    → translate_with_cache()       # Google Translate via deep-translator, cached in site/translations_cache.json
    → load_all_data_files()        # Reads data/*.json for historical stats
    → compute_stats()              # Aggregates multi-day data for Chart.js visualizations
    → render_html()                # Outputs a single self-contained HTML file
    → site/index.html              # Static file deployed to GitHub Pages
```

### Key Design Decisions

- **Everything is in `generate_site.py`**: The entire HTML template (CSS, JS, structure) is a single Python f-string in `render_html()`. All changes to the website happen in this file.
- **F-string brace escaping**: All JS `{` `}` in the template must be doubled to `{{` `}}` since it's inside an f-string. This is the most common source of bugs.
- **Translations are build-time only**: No runtime API calls. Items are pre-translated and embedded as `<span class="cn">` / `<span class="en">` pairs. Language toggle is CSS-only.
- **Translation cache**: `site/translations_cache.json` persists translations across builds. Only new items hit the Google Translate API.
- **Stats charts**: Chart.js (CDN) renders 3 charts from aggregated `data/*.json` files. Charts are lazily initialized when the Stats tab is first clicked.
- **Theme**: Light/dark mode via CSS custom properties on `:root`. Charts call `updateChartTheme()` on toggle.

### Data Storage

- `data/YYYY-MM-DD.json` — Daily menu snapshots (committed by CI)
- `site/translations_cache.json` — Persistent translation cache (committed by CI)

### Deployment

GitHub Actions (`.github/workflows/daily-menu.yml`):
- Runs daily at 10:00 UTC (5 AM ET)
- Fetches menus, generates HTML, deploys to GitHub Pages
- Commits updated translation cache and data files back to repo

### Trait System

Menu items have traits from the UMich API using long-form strings (e.g., `"Vegan"`, `"Gluten Free"`, `"Carbon Footprint Low"`, `"Nutrient Dense High"`). The `TRAIT_DISPLAY` dict maps these to `(label, css_class)` pairs for rendering.
