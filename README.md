# LRSM Publications Tracker

Tracks publications that acknowledge NSF MRSEC grant **DMR-2309043** (University of Pennsylvania Laboratory for Research on the Structure of Matter). Pulls from OpenAlex, Google Scholar, and Web of Science; scrapes PI publication lists; searches acknowledgment text; and downloads full-text PDFs for mining.

The master output is **`crossref_report.csv`** — a deduplicated, human-reviewed list of confirmed or candidate grant publications.

---

## Setup

```bash
# Create venv and install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# One-time EZproxy login (Penn Libraries access, lasts ~8-24 hours)
.venv/bin/python src/ezproxy_login.py
```

---

## Workflow

### Step 1 — Grant search (OpenAlex + Google Scholar + WoS cross-reference)

**Script:** `openalex_grant_miner.ipynb`

- Queries OpenAlex for works with `awards.funder_id:F4320306076` and `awards.funder_award_id:2309043`
- Searches Google Scholar for five grant ID variants: `2309043`, `DMR-2309043`, `DMR2309043`, `23-09043`, `DMR23-09043`
- Merges and deduplicates into **`grant_publications.csv`**
- Cross-references against the Web of Science export (`savedrecs_05_29(2).xlsx`) to produce **`crossref_report.csv`**

```bash
.venv/bin/jupyter nbconvert --to notebook --execute --inplace src/openalex_grant_miner.ipynb \
    --ExecutePreprocessor.timeout=300
```

**WoS export format note:** New exports (post-May 2025) have headers at row 1 with columns `Article Title` and `Times Cited, WoS Core`. Older exports have headers at row 11 with `Title` and `Total Citations`. The notebook auto-detects both formats.

---

### Step 2 — PI publication scraper

**Script:** `lrsm_scholar_scraper.py`

- Scrapes the 25+ MRSEC PIs listed at `https://www.lrsm.upenn.edu/mrsec-participants/`
- Queries OpenAlex for each PI's works since 2023
- Cross-references against `grant_publications.csv` to find gaps
- Resumes from `lrsm_scraper_progress.json` if interrupted

```bash
.venv/bin/python src/lrsm_scholar_scraper.py
```

**Outputs:**

| File | Description |
|---|---|
| `lrsm_pi_pubs.csv` | All works per PI since 2023 from OpenAlex |
| `lrsm_missing_pubs.csv` | PI works not already in `grant_publications.csv` (candidate pool) |
| `lrsm_grant_matches.csv` | PI works with the grant ID present in the OpenAlex grants field |
| `lrsm_pi_pubs_wos.csv` | PI pubs cross-referenced with the WoS export |

To force a full rerun, delete the progress file and outputs first:

```bash
rm -f lrsm_scraper_progress.json lrsm_pi_pubs.csv lrsm_grant_matches.csv lrsm_missing_pubs.csv
```

---

### Step 3 — Acknowledgment text search

**Script:** `search_acknowledgments.py`

For each paper in `lrsm_missing_pubs.csv`, tries in order:

1. **Publisher DOI landing page** — fetches HTML and searches for grant IDs in acknowledgment sections. Works even for many paywalled papers since acknowledgments often appear in visible HTML.
2. **arXiv preprint** — searches arXiv by title for a preprint version, then checks its text for grant IDs.

Also runs a bulk arXiv search for each grant ID variant to catch papers entirely absent from the candidate pool.

```bash
.venv/bin/python src/search_acknowledgments.py
```

**Outputs:**

| File | Description |
|---|---|
| `ack_matches.csv` | Papers where a grant ID was confirmed in acknowledgment text |
| `ack_search_log.csv` | Full log — one row per paper, showing the result of each method |

To force a full rerun:

```bash
rm -f ack_search_progress.json ack_matches.csv ack_search_log.csv
```

---

### Step 4 — Full-text PDF download

**Script:** `fetch_fulltext.py`

Downloads full text for papers in a CSV with a `doi` column using a tiered strategy:

| Tier | Method | Notes |
|---|---|---|
| 1 | Unpaywall | Legal open-access PDF URLs |
| 2 | bioRxiv / medRxiv | Direct preprint PDF via DOI |
| 3 | arXiv | Preprint PDF via title search |
| 4 | EZproxy (requests) | Penn-licensed content via saved session cookies |
| 4b | EZproxy (Playwright) | Headless Chromium for JS-rendered publisher pages |
| 5 | HTML extraction | BeautifulSoup fallback saving article text |

```bash
# Refresh EZproxy session first (if cookies have expired)
.venv/bin/python src/ezproxy_login.py

# Download for all candidate papers
.venv/bin/python src/fetch_fulltext.py

# Limit to first N papers
.venv/bin/python src/fetch_fulltext.py --limit 50

# Skip Playwright tier (faster, fewer dependencies)
.venv/bin/python src/fetch_fulltext.py --no-playwright
```

PDFs and extracted text are saved to `pdfs/` as `<doi_safe>.pdf` or `<doi_safe>.txt`.
Progress is tracked in `fetch_progress.json`; delete it to reprocess already-attempted papers.

**ChemRxiv note:** ChemRxiv (DOIs starting `10.26434/`) is blocked by Cloudflare and cannot be fetched programmatically. Download these manually and place in `Manual Downloads/`.

---

### Step 5 — Full-text acknowledgment mining

Mine all files in `pdfs/` and `Manual Downloads/` for the grant number, then merge hits into `crossref_report.csv`. Run this inline script:

```python
import csv, re
from pathlib import Path
from pdfminer.high_level import extract_text

GRANT_PAT = re.compile(r"2309043|DMR-?2309043|DMR\s*23-?09043", re.IGNORECASE)
DOI_PAT   = re.compile(r"\b(10\.\d{4,}/[^\s,;\"\'<>]{3,})", re.IGNORECASE)

def norm_doi(d):
    return (d or "").lower().replace("https://doi.org/","").replace("http://doi.org/","").strip()

def doi_safe(d):
    return re.sub(r"[^\w\-.]", "_", d)[:120]

# Load DOI->metadata from source CSVs, load crossref DOIs, then:
# For each .pdf/.txt in pdfs/ and Manual Downloads/:
#   1. Extract text
#   2. Search for GRANT_PAT
#   3. Map filename stem to DOI (pdfs/) or extract DOI from text (Manual Downloads/)
#   4. If DOI not in crossref_report.csv, append new row with source="ack_fulltext"
```

See AGENTS.md for the full working script.

---

### Step 6 — Deduplication and cleanup

After merging, deduplicate `crossref_report.csv` with fuzzy title matching:

```python
from rapidfuzz import fuzz
# 1. Normalize all DOIs (strip https://doi.org/ prefix)
# 2. Exact-DOI dedup (keeping highest peer_score)
# 3. Version dedup: collapse ChemRxiv -v2/-v3 etc. into base
# 4. Fuzzy title dedup at threshold=88 (token_sort_ratio), preferring journals over preprints
# 5. Clean HTML tags, special Unicode dashes, curly quotes from title/authors/journal
# 6. Fill missing authors from Crossref API (OpenAlex fallback)
```

**Peer-review preference:** DOIs from `10.26434/` (ChemRxiv), `10.48550/` (arXiv), `10.1101/` (bioRxiv/medRxiv), `10.2139/` (SSRN), `10.21203/` (Research Square), `10.5281/` (Zenodo), and similar are treated as preprints. When a fuzzy title match groups a preprint with a journal paper, the journal paper is kept.

---

## File Reference

### Inputs

| File | Description |
|---|---|
| `savedrecs_05_29(2).xlsx` | Latest Web of Science export (125 records, headers at row 1) |
| `new_pis` | Plain-text list of PIs added after initial setup |

### Intermediates

| File | Description |
|---|---|
| `grant_publications.csv` | Confirmed grant publications from OpenAlex + Scholar (123 records) |
| `lrsm_pi_pubs.csv` | All PI works since 2023 from OpenAlex |
| `lrsm_missing_pubs.csv` | PI works not yet confirmed as grant publications |
| `lrsm_all_pubs_not_in_crossref.csv` | Input CSV for `fetch_fulltext.py` |
| `ack_matches.csv` | Acknowledgment search hits |
| `ack_search_log.csv` | Full acknowledgment search log |
| `blocked_not_fetched.csv` | Papers blocked in ack search not yet fetched (with `doi_url` column) |
| `ezproxy_cookies.json` | Penn EZproxy session cookies — refresh with `ezproxy_login.py` |

### Outputs

| File | Description |
|---|---|
| `crossref_report.csv` | **Master report** — 167 deduplicated records |
| `pdfs/` | Downloaded PDFs and extracted text files (~840 files) |
| `Manual Downloads/` | Manually downloaded PDFs (~97 files, mostly ChemRxiv) |

**`crossref_report.csv` columns:** `title, doi, journal, year, authors, pi_name, in_openalex, in_wos, wos_citations, oa_citations, open_access, source`

---

## Grant Details

| Field | Value |
|---|---|
| Award | DMR-2309043 |
| Funder | National Science Foundation |
| OpenAlex funder ID | F4320306076 |
| Institution | University of Pennsylvania — LRSM MRSEC |
| Min year tracked | 2023 |
| Contact | ghollyer@seas.upenn.edu |

---

## Full Rerun

```bash
# Reset Steps 2 & 3 progress
rm -f lrsm_scraper_progress.json lrsm_pi_pubs.csv lrsm_grant_matches.csv lrsm_missing_pubs.csv
rm -f ack_search_progress.json ack_matches.csv ack_search_log.csv

# Step 1: grant search notebook
.venv/bin/jupyter nbconvert --to notebook --execute --inplace src/openalex_grant_miner.ipynb \
    --ExecutePreprocessor.timeout=300

# Step 2: PI scraper
.venv/bin/python src/lrsm_scholar_scraper.py

# Step 3: acknowledgment search (slow — can run overnight)
.venv/bin/python src/search_acknowledgments.py

# Step 4: full-text download
.venv/bin/python src/ezproxy_login.py
.venv/bin/python src/fetch_fulltext.py

# Steps 5-6: mine PDFs, deduplicate, clean — see AGENTS.md
```
