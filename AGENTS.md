# AGENTS.md — AI Assistant Guide

This file documents the codebase conventions, repeatable scripts, and gotchas for AI agents (Claude Code, etc.) assisting with the LRSM Publications Tracker. Read this before modifying any files.

---

## Project Purpose

Track all publications acknowledging NSF MRSEC grant **DMR-2309043** (UPenn LRSM). The master output is `crossref_report.csv`. The workflow is: search for grant-acknowledged papers → scrape PI publication lists → download full text → mine PDFs for acknowledgments → deduplicate and clean.

---

## Key Conventions

### DOI normalization

All DOIs in this project are stored and compared in **normalized form**: lowercase, no `https://doi.org/` or `http://doi.org/` prefix. Always apply this before any comparison or lookup:

```python
def norm_doi(d):
    return (d or "").lower().replace("https://doi.org/","").replace("http://doi.org/","").strip()
```

When writing new entries to `crossref_report.csv`, normalize the DOI first. Failure to do this is the most common source of false duplicates.

### Filename-to-DOI mapping

`fetch_fulltext.py` saves files as `pdfs/<doi_safe>.pdf` where:

```python
def doi_safe(doi):
    return re.sub(r"[^\w\-.]", "_", doi)[:120]
```

To reverse-map a filename stem to a DOI, build a lookup dict from all known DOIs:
```python
stem_to_doi = {doi_safe(norm_doi(d)): norm_doi(d) for d in all_known_dois}
```

This mapping is not lossless, so always verify against known DOI lists rather than trying to invert the transform directly.

### Preprint vs. peer-reviewed classification

Preprint DOI prefixes:
```python
PREPRINT_PREFIXES = (
    "10.26434/",   # ChemRxiv
    "10.48550/",   # arXiv
    "10.1101/",    # bioRxiv / medRxiv
    "10.2139/",    # SSRN
    "10.21203/",   # Research Square
    "10.5281/",    # Zenodo
    "10.7302/",    # Deep Blue (U Michigan)
    "10.64898/",   # Janelia / figshare
    "10.69761/",   # misc preprint
)
```

Always apply `norm_doi()` before calling `is_preprint()` — DOIs with `https://doi.org/` prefixes will not match the prefix check.

### Grant ID search pattern

```python
GRANT_PAT = re.compile(r"2309043|DMR-?2309043|DMR\s*23-?09043", re.IGNORECASE)
```

This covers all known variants: `2309043`, `DMR-2309043`, `DMR2309043`, `DMR 2309043`, `DMR23-09043`, `DMR 23-09043`.

---

## Scripts Reference

### `openalex_grant_miner.ipynb`

- **Run as:** `nbconvert --execute --inplace` (not cell-by-cell) to avoid `NameError` from out-of-order cell execution
- **Key cell:** `cell-11` handles WoS cross-reference; auto-detects old (row 11 header) vs. new (row 1 header) WoS export format
- **Output:** `grant_publications.csv`, `crossref_report.csv`

### `lrsm_scholar_scraper.py`

- Has progress tracking via `lrsm_scraper_progress.json`
- Known disambiguation issues:
  - **Liang Wu** — OpenAlex matches "Hui Wu" at NIST instead of Liang Wu (UPenn condensed matter). Results are unreliable; treat with skepticism.
  - **Andrew Rappe** — OpenAlex profile may be fragmented (only ~8 works matched). Verify manually.
- To add new PIs without a full rerun, import functions directly:
  ```python
  from lrsm_scholar_scraper import fetch_openalex_works, save_results
  ```

### `search_acknowledgments.py`

- Has progress tracking via `ack_search_progress.json`
- "Blocked" entries in `ack_search_log.csv` mean the DOI landing page returned a login/paywall redirect — not a negative result
- `blocked_not_fetched.csv` = blocked entries not yet in `crossref_report.csv` and not in `pdfs/`

### `fetch_fulltext.py`

- **Input CSV:** `lrsm_all_pubs_not_in_crossref.csv` (default) — must have a `doi` column
- **Progress:** `fetch_progress.json` — delete to reprocess previously-attempted DOIs
- **Playwright tier (4b):** Uses `page.route()` to inject EZproxy cookies directly into HTTP request headers, bypassing Playwright's cookie-matching restrictions. This avoids the issue where cookies stored via `add_cookies()` are not transmitted in outgoing requests.
- **ChemRxiv:** Blocked by Cloudflare — skip automatically, download manually
- **EZproxy session:** Refresh with `ezproxy_login.py` when cookies expire (~8-24 hours)

### `ezproxy_login.py`

- Opens a real Chromium browser window for the user to complete PennKey + Duo 2FA
- Saves all session cookies to `ezproxy_cookies.json`
- Run whenever `fetch_fulltext.py` fails with auth redirects

### `fill_missing_dois.py`

- Queries Crossref API by title to back-fill DOIs for rows in `lrsm_all_pubs.csv` that have none
- Also fills `journal` and `year` if missing

---

## Repeatable Scripts

These are inline scripts (not standalone files) used for Steps 5-6. Copy-paste into a terminal.

### Step 5 — Mine PDFs for grant acknowledgment

```python
import csv, re
from pathlib import Path

def pdf_to_text(path):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path)) or ""
    except Exception:
        return ""

def read_file(path):
    if path.suffix.lower() == ".pdf":
        return pdf_to_text(path)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def norm_doi(d):
    return (d or "").lower().replace("https://doi.org/","").replace("http://doi.org/","").strip()

def doi_safe(d):
    return re.sub(r"[^\w\-.]", "_", d)[:120]

GRANT_PAT = re.compile(r"2309043|DMR-?2309043|DMR\s*23-?09043", re.IGNORECASE)
DOI_PAT   = re.compile(r"\b(10\.\d{4,}/[^\s,;\"\'<>]{3,})", re.IGNORECASE)

# Load metadata from source CSVs
meta = {}
for src, doi_col, pi_col in [
    ("lrsm_missing_pubs.csv",            "doi", "pi_name"),
    ("lrsm_all_pubs_not_in_crossref.csv","doi", "pi_name"),
    ("blocked_not_fetched.csv",           "doi", "pi_name"),
    ("grant_publications.csv",            "doi", None),
]:
    try:
        with open(src, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = norm_doi(row.get(doi_col, ""))
                if d and doi_safe(d) not in meta:
                    meta[doi_safe(d)] = {
                        "doi":     d,
                        "title":   row.get("title",""),
                        "pi_name": row.get(pi_col,"") if pi_col else "",
                        "year":    row.get("year",""),
                        "journal": row.get("journal",""),
                    }
    except FileNotFoundError:
        pass

stem_to_doi = {k: v["doi"] for k, v in meta.items()}

# Load existing crossref
crossref_dois = set()
crossref_rows = []
crossref_fields = []
with open("crossref_report.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    crossref_fields = list(reader.fieldnames)
    for row in reader:
        d = norm_doi(row.get("doi",""))
        crossref_dois.add(d)
        crossref_rows.append(row)

hits = {}

def try_add(doi_found, label=""):
    doi_found = norm_doi(doi_found.rstrip(".,);:"))
    if not doi_found or len(doi_found) < 5 or doi_found in hits:
        return
    m = meta.get(doi_safe(doi_found), {"doi":doi_found,"title":"","pi_name":"","year":"","journal":""})
    hits[doi_found] = m
    print(f"  HIT [{label}]: {doi_found}")

# Mine pdfs/
for path in sorted(Path("pdfs").iterdir()):
    if path.suffix.lower() not in (".pdf", ".txt"):
        continue
    text = read_file(path)
    if not GRANT_PAT.search(text):
        continue
    doi_found = stem_to_doi.get(path.stem)
    if not doi_found:
        m = DOI_PAT.search(text[:6000])
        doi_found = m.group(1) if m else None
    if doi_found:
        try_add(doi_found, "pdfs")

# Mine Manual Downloads/
for path in sorted(Path("Manual Downloads").iterdir()):
    if path.suffix.lower() != ".pdf":
        continue
    text = read_file(path)
    if not GRANT_PAT.search(text):
        continue
    m = DOI_PAT.search(text[:8000])
    if m:
        try_add(m.group(1).rstrip(".,);:"), "manual")

# Merge new hits
new_entries = [d for d in hits if d not in crossref_dois]
print(f"\nNew entries: {len(new_entries)}")
for d in sorted(new_entries):
    m = hits[d]
    row = {f: "" for f in crossref_fields}
    row.update({"doi": d, "title": m["title"], "pi_name": m["pi_name"],
                "year": m["year"], "journal": m["journal"], "source": "ack_fulltext"})
    crossref_rows.append(row)

with open("crossref_report.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=crossref_fields)
    writer.writeheader()
    writer.writerows(crossref_rows)
```

---

### Step 6a — Fuzzy deduplication

```python
import csv, re
from rapidfuzz import fuzz
from collections import defaultdict

PREPRINT_PREFIXES = (
    "10.26434/","10.48550/","10.1101/","10.2139/",
    "10.21203/","10.5281/","10.7302/","10.64898/","10.69761/",
)

def norm_doi(d):
    return (d or "").lower().replace("https://doi.org/","").replace("http://doi.org/","").strip()

def is_preprint(doi):
    return any(doi.startswith(p) for p in PREPRINT_PREFIXES)

def peer_score(row):
    doi = norm_doi(row.get("doi",""))
    if is_preprint(doi):
        return 0 if re.search(r"[-/]v\d+$", doi) else 1
    return 2

def norm_title(t):
    t = re.sub(r"<[^>]+>", " ", t.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def base_doi(doi):
    return re.sub(r"[-/]v\d+$", "", norm_doi(doi))

with open("crossref_report.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fields = list(reader.fieldnames)
    rows = list(reader)

# 1. Normalize DOIs
for row in rows:
    row["doi"] = norm_doi(row.get("doi",""))

# 2. Exact-DOI dedup
seen = {}
for row in rows:
    d = row["doi"]
    if d not in seen or peer_score(row) > peer_score(seen[d]):
        seen[d] = row
rows = list(seen.values())

# 3. Version dedup
base_groups = defaultdict(list)
for row in rows:
    base_groups[base_doi(row["doi"])].append(row)
rows = [max(g, key=peer_score) for g in base_groups.values()]

# 4. Fuzzy title dedup
titled = [(norm_title(r.get("title","")), r) for r in rows]
parent = list(range(len(titled)))
def find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]; x = parent[x]
    return x
def union(a, b):
    pa, pb = find(a), find(b)
    if pa != pb: parent[pa] = pb

THRESHOLD = 88
for i in range(len(titled)):
    if len(titled[i][0]) < 15: continue
    for j in range(i+1, len(titled)):
        if len(titled[j][0]) < 15: continue
        if fuzz.token_sort_ratio(titled[i][0], titled[j][0]) >= THRESHOLD:
            union(i, j)

clusters = defaultdict(list)
for i in range(len(titled)): clusters[find(i)].append(i)
final = [max([rows[m] for m in members], key=peer_score) for members in clusters.values()]

with open("crossref_report.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(final)
print(f"Deduped: {len(rows)} -> {len(final)} rows")
```

---

### Step 6b — Text cleanup (HTML tags and Unicode)

```python
import csv, re, html

def clean(text):
    if not text: return text
    text = re.sub(r"<[^>]+>", "", text)      # strip HTML tags
    text = html.unescape(text)                 # decode HTML entities
    for ch in "\u2010\u2011\u2012\u2013\u2014\u2212":
        text = text.replace(ch, "-")           # normalize dashes
    text = text.replace("\u2018","'").replace("\u2019","'")
    text = text.replace("\u201c",'"').replace("\u201d",'"')
    text = text.replace("\u2550","=")
    return re.sub(r"  +", " ", text).strip()

with open("crossref_report.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fields = list(reader.fieldnames)
    rows = list(reader)

for row in rows:
    for col in ("title","authors","journal"):
        row[col] = clean(row.get(col,""))

with open("crossref_report.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
```

---

### Step 6c — Fill missing authors from Crossref

```python
import csv, re, requests, time

def norm_doi(d):
    return (d or "").lower().replace("https://doi.org/","").replace("http://doi.org/","").strip()

def fmt_authors(author_list, max_authors=8):
    parts = []
    for a in author_list[:max_authors]:
        name = f"{a.get('given','')} {a.get('family','') or a.get('name','')}".strip()
        if name: parts.append(name)
    return "; ".join(parts)

def crossref_authors(doi):
    try:
        r = requests.get(f"https://api.crossref.org/works/{doi}",
                         params={"mailto":"ghollyer@seas.upenn.edu"}, timeout=12)
        if r.status_code != 200: return None
        return fmt_authors(r.json().get("message",{}).get("author",[]))
    except Exception: return None
    finally: time.sleep(0.4)

def openalex_authors(doi):
    try:
        r = requests.get("https://api.openalex.org/works",
                         params={"filter":f"doi:{doi}","mailto":"ghollyer@seas.upenn.edu"}, timeout=12)
        results = r.json().get("results",[])
        if not results: return None
        names = [s["author"]["display_name"] for s in results[0].get("authorships",[])[:8] if s.get("author")]
        return "; ".join(names) if names else None
    except Exception: return None
    finally: time.sleep(0.3)

with open("crossref_report.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fields = list(reader.fieldnames)
    rows = list(reader)

for row in rows:
    if row.get("authors","").strip(): continue
    doi = norm_doi(row.get("doi",""))
    if not doi: continue
    row["authors"] = crossref_authors(doi) or openalex_authors(doi) or ""

with open("crossref_report.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
```

---

## crossref_report.csv Schema

| Column | Description |
|---|---|
| `title` | Paper title (HTML-stripped, Unicode-normalized) |
| `doi` | Normalized DOI (no `https://doi.org/` prefix, lowercase) |
| `journal` | Journal or venue name |
| `year` | Publication year |
| `authors` | Semicolon-separated author names (up to 8), fetched from Crossref |
| `pi_name` | LRSM PI associated with this paper |
| `in_openalex` | `True` if found via OpenAlex grant search |
| `in_wos` | `True` if found in the WoS export |
| `wos_citations` | Citation count from Web of Science |
| `oa_citations` | Citation count from OpenAlex |
| `open_access` | Open access status from Unpaywall/OpenAlex |
| `source` | How this paper was identified (e.g., `OpenAlex`, `WoS only`, `ack_fulltext`) |

---

## Known Issues and Gotchas

- **DOI prefix bug in dedup:** `peer_score()` calls `is_preprint(doi)` which checks `doi.startswith(prefix)`. If DOIs still have `https://doi.org/` prefix, preprints will be misclassified as peer-reviewed. Always normalize DOIs before scoring.
- **Liang Wu OpenAlex mismatch:** OpenAlex matches "Hui Wu" at NIST instead of Liang Wu (UPenn condensed matter physicist). Filter or exclude Liang Wu results from automated OpenAlex queries.
- **Andrew Rappe fragmented profile:** OpenAlex may only return ~8 works for A. M. Rappe. Add the correct OpenAlex author ID to `HARDCODED_IDS` in `lrsm_scholar_scraper.py` to fix.
- **ChemRxiv Cloudflare block:** All `10.26434/` DOIs fail in `fetch_fulltext.py`. Place manually downloaded PDFs in `Manual Downloads/`.
- **Playwright cookie injection:** Standard `add_cookies()` in Playwright does not reliably transmit cookies in outgoing HTTP requests. The fix in `fetch_fulltext.py` uses `page.route()` to inject the `Cookie` header directly via `_inject_cookies()`.
- **WoS sort TypeError:** WoS records parsed from xlsx have integer years; OpenAlex records have string years. Cast with `str()` before sorting. This is fixed in the current notebook.
- **Google Scholar NameError in notebook:** `fetch_scholar_works` is not defined if notebook cells run out of order. Always execute via `nbconvert --execute`, never cell-by-cell.
- **Fuzzy dedup false positives:** Some Dryad/figshare dataset DOIs (`10.5061/`, `10.17615/`) will fuzzy-match their companion journal papers. This is correct behavior — drop the dataset, keep the paper.
