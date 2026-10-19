"""
search_acknowledgments.py
-------------------------
Searches for grant acknowledgments in the candidate pool (lrsm_missing_pubs.csv)
by two methods, tried in order for each paper:

  1. Publisher DOI landing page  — fetches HTML, looks for grant IDs in
     acknowledgment sections. Works for OA papers and many paywalled ones
     (acknowledgments are usually in visible HTML even behind a paywall).

  2. arXiv preprint              — searches arXiv by paper title to find a
     preprint version, then searches its abstract/full listing for grant IDs.

Also runs a bulk arXiv search for each grant ID variant to catch papers
that may not be in the candidate pool at all.

Outputs:
    ack_matches.csv          — papers where a grant ID was found
    ack_search_log.csv       — full log (one row per paper, result of each method)

Usage:
    python search_acknowledgments.py
"""

import csv
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

CANDIDATE_CSV  = "lrsm_missing_pubs.csv"
MATCHES_CSV    = "ack_matches.csv"
LOG_CSV        = "ack_search_log.csv"
PROGRESS_FILE  = "ack_search_progress.json"

GRANT_VARIANTS = [
    "2309043",
    "DMR-2309043",
    "DMR2309043",
    "23-09043",
    "DMR23-09043",
]
_GRANT_RE = [re.compile(re.escape(v), re.IGNORECASE) for v in GRANT_VARIANTS]

# Acknowledgment section markers — we'll search these areas first
ACK_MARKERS = re.compile(
    r"acknowledg|funding|supported by|grant|award|nsf|national science",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SLEEP_DOI   = 1.5   # between DOI fetches
SLEEP_ARXIV = 1.0   # between arXiv API calls

MATCH_FIELDS = ["pi_name", "group", "title", "doi", "journal", "year",
                "cited_by", "matched_variant", "found_via", "match_context", "url"]

LOG_FIELDS   = ["pi_name", "title", "doi", "year",
                "doi_page_result", "arxiv_result", "matched_variant", "found_via"]


# ── Grant ID search ───────────────────────────────────────────────────────────

def find_grant_in_text(text: str) -> tuple[str, str] | tuple[None, None]:
    """Return (matched_variant, context_snippet) or (None, None)."""
    for pat, variant in zip(_GRANT_RE, GRANT_VARIANTS):
        m = pat.search(text or "")
        if m:
            start = max(0, m.start() - 80)
            end   = min(len(text), m.end() + 80)
            return variant, text[start:end].replace("\n", " ").strip()
    return None, None


def find_grant_in_soup(soup: BeautifulSoup) -> tuple[str, str] | tuple[None, None]:
    """
    Search page HTML for grant IDs, prioritising acknowledgment sections.
    Returns (variant, context) or (None, None).
    """
    # 1. Look for dedicated ack sections by common HTML patterns
    ack_candidates = []
    for tag in soup.find_all(
        ["section", "div", "p", "h2", "h3", "h4"],
        string=ACK_MARKERS,
    ):
        ack_candidates.append(tag)
    # Also grab parents of matching strings
    for tag in soup.find_all(string=ACK_MARKERS):
        parent = tag.parent
        if parent and parent not in ack_candidates:
            ack_candidates.append(parent)
            # grab siblings / following elements for context
            for sib in parent.find_next_siblings()[:3]:
                ack_candidates.append(sib)

    for tag in ack_candidates:
        text = tag.get_text(" ", strip=True)
        v, ctx = find_grant_in_text(text)
        if v:
            return v, ctx

    # 2. Full page text fallback
    return find_grant_in_text(soup.get_text(" ", strip=True))


# ── Method 1: DOI landing page ────────────────────────────────────────────────

def fetch_doi_page(doi: str) -> tuple[str, str] | tuple[None, None]:
    """
    Fetch the publisher page for a DOI and search for grant IDs.
    Returns (variant, context) or (None, None).
    Also returns ("blocked", "") if the page seems paywalled/JS-only.
    """
    url = f"https://doi.org/{doi}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code in (403, 401, 429):
            return "blocked", f"HTTP {resp.status_code}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"

        ct = resp.headers.get("Content-Type", "")
        if "html" not in ct:
            return None, f"non-HTML content-type: {ct}"

        soup = BeautifulSoup(resp.text, "html.parser")

        # Detect JS-only pages (minimal text content)
        visible = soup.get_text(" ", strip=True)
        if len(visible) < 200:
            return "js-only", ""

        return find_grant_in_soup(soup)

    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.ConnectionError as e:
        return None, f"connection error: {e}"
    except Exception as e:
        return None, f"error: {e}"


# ── Method 2: arXiv lookup by title ──────────────────────────────────────────

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_NS  = "http://www.w3.org/2005/Atom"

def _arxiv_query(search_query: str, max_results: int = 5) -> list[dict]:
    """Raw arXiv API query; returns list of entry dicts."""
    params = {
        "search_query": search_query,
        "max_results":  max_results,
        "sortBy":       "relevance",
    }
    try:
        resp = requests.get(ARXIV_API, params=params, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        entries = []
        for entry in root.findall(f"{{{ARXIV_NS}}}entry"):
            title   = (entry.findtext(f"{{{ARXIV_NS}}}title") or "").strip()
            summary = (entry.findtext(f"{{{ARXIV_NS}}}summary") or "").strip()
            link    = next(
                (l.get("href","") for l in entry.findall(f"{{{ARXIV_NS}}}link")
                 if l.get("type") == "text/html"),
                ""
            )
            entries.append({"title": title, "summary": summary, "link": link})
        return entries
    except Exception:
        return []


def fetch_arxiv_by_title(title: str) -> tuple[str, str] | tuple[None, None]:
    """
    Search arXiv for a paper by title and check its summary for grant IDs.
    Returns (variant, context) or (None, None).
    """
    # Use first ~60 chars of title as query (arXiv title search)
    short = re.sub(r"[^\w\s]", " ", title)[:80].strip()
    entries = _arxiv_query(f'ti:"{short}"', max_results=3)
    time.sleep(SLEEP_ARXIV)

    for entry in entries:
        # Only accept if titles are similar (avoid false positives)
        entry_title_norm = re.sub(r"\W+", " ", entry["title"].lower()).strip()
        query_title_norm = re.sub(r"\W+", " ", title.lower()).strip()
        # Check overlap of first 6 words
        et_words = entry_title_norm.split()[:6]
        qt_words = query_title_norm.split()[:6]
        overlap  = sum(1 for w in et_words if w in qt_words)
        if overlap < 4:
            continue

        combined = f"{entry['title']} {entry['summary']}"
        v, ctx = find_grant_in_text(combined)
        if v:
            return v, ctx

    return None, None


# ── Bulk arXiv grant search ───────────────────────────────────────────────────

def bulk_arxiv_search(existing_dois: set[str]) -> list[dict]:
    """
    Search arXiv directly for each grant variant.
    Returns new matches not already in existing_dois.
    """
    print("\n── Bulk arXiv grant search ──────────────────────────────────────")
    new_matches = []

    for variant in GRANT_VARIANTS:
        print(f"  Searching arXiv for: \"{variant}\"")
        entries = _arxiv_query(f'all:"{variant}"', max_results=50)
        time.sleep(SLEEP_ARXIV)

        for entry in entries:
            # Extract arXiv DOI equivalent (link → arxiv ID)
            link = entry.get("link", "")
            arxiv_doi = ""
            m = re.search(r"arxiv\.org/abs/(.+)", link)
            if m:
                arxiv_doi = f"10.48550/arxiv.{m.group(1)}"

            v, ctx = find_grant_in_text(f"{entry['title']} {entry['summary']}")
            if not v:
                continue

            if arxiv_doi and arxiv_doi in existing_dois:
                continue

            new_matches.append({
                "pi_name":        "arXiv search",
                "group":          "",
                "title":          entry["title"],
                "doi":            arxiv_doi,
                "journal":        "arXiv",
                "year":           "",
                "cited_by":       "",
                "matched_variant": v,
                "found_via":      "arXiv bulk search",
                "match_context":  ctx,
                "url":            link,
            })
            print(f"    + {entry['title'][:70]}")

    return new_matches


# ── Progress helpers ──────────────────────────────────────────────────────────

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done_dois": []}


def save_progress(p: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f)


def append_rows(filepath: str, rows: list[dict], fieldnames: list[str]):
    write_header = not Path(filepath).exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not Path(CANDIDATE_CSV).exists():
        print(f"Candidate pool not found: {CANDIDATE_CSV}")
        print("Run lrsm_scholar_scraper.py first.")
        return

    with open(CANDIDATE_CSV, newline="", encoding="utf-8") as f:
        candidates = [r for r in csv.DictReader(f) if r.get("doi")]

    print(f"Candidate papers with DOIs: {len(candidates)}")

    progress    = load_progress()
    done_dois   = set(progress["done_dois"])

    total_checked = len(done_dois)
    total_matched = 0

    # Collect existing matched DOIs for bulk arXiv dedup
    existing_match_dois: set[str] = set()
    if Path(MATCHES_CSV).exists():
        with open(MATCHES_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("doi"):
                    existing_match_dois.add(row["doi"])

    print("\n── Per-paper DOI + arXiv search ─────────────────────────────────")
    for row in candidates:
        doi   = row.get("doi", "").strip()
        title = row.get("title", "").strip()

        if not doi or doi in done_dois:
            continue

        total_checked += 1
        doi_result = arxiv_result = ""
        matched_variant = found_via = match_context = ""

        # ── Method 1: DOI landing page
        print(f"  [{total_checked}] {title[:65]}")
        v, ctx = fetch_doi_page(doi)
        time.sleep(SLEEP_DOI)

        if v and v not in ("blocked", "js-only"):
            matched_variant = v
            found_via       = "DOI landing page"
            match_context   = ctx or ""
            doi_result      = f"MATCH: {v}"
        else:
            doi_result = v or "no match"

            # ── Method 2: arXiv title lookup
            if title:
                v2, ctx2 = fetch_arxiv_by_title(title)
                if v2:
                    matched_variant = v2
                    found_via       = "arXiv preprint"
                    match_context   = ctx2 or ""
                    arxiv_result    = f"MATCH: {v2}"
                else:
                    arxiv_result = "no match"

        if matched_variant:
            total_matched += 1
            print(f"    *** MATCH ({matched_variant}) via {found_via}")
            match_row = {
                "pi_name":        row.get("pi_name", ""),
                "group":          row.get("group", ""),
                "title":          title,
                "doi":            doi,
                "journal":        row.get("journal", ""),
                "year":           row.get("year", ""),
                "cited_by":       row.get("cited_by", ""),
                "matched_variant": matched_variant,
                "found_via":      found_via,
                "match_context":  match_context,
                "url":            f"https://doi.org/{doi}",
            }
            append_rows(MATCHES_CSV, [match_row], MATCH_FIELDS)
            existing_match_dois.add(doi)

        log_row = {
            "pi_name":        row.get("pi_name", ""),
            "title":          title,
            "doi":            doi,
            "year":           row.get("year", ""),
            "doi_page_result": doi_result,
            "arxiv_result":   arxiv_result,
            "matched_variant": matched_variant,
            "found_via":      found_via,
        }
        append_rows(LOG_CSV, [log_row], LOG_FIELDS)

        done_dois.add(doi)
        progress["done_dois"] = list(done_dois)
        save_progress(progress)

    # ── Bulk arXiv search for grant variants
    bulk_matches = bulk_arxiv_search(existing_match_dois)
    if bulk_matches:
        append_rows(MATCHES_CSV, bulk_matches, MATCH_FIELDS)
        total_matched += len(bulk_matches)

    print(f"""
── Complete ──────────────────────────────────────────────────────
  Papers checked   : {total_checked}
  Grant matches    : {total_matched}

  Matches  → {MATCHES_CSV}
  Full log → {LOG_CSV}
──────────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
