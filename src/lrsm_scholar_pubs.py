"""
lrsm_scholar_pubs.py
--------------------
Scrapes Google Scholar profile pages for all LRSM MRSEC PIs to build a
publication list since MIN_YEAR and search for grant ID acknowledgments.

Fetches each PI's Scholar profile (sorted by date) directly via HTTP,
parses the publication table, and for each paper since MIN_YEAR searches
the title for grant ID variants.

Outputs:
    lrsm_scholar_pubs.csv        — all pubs per PI since MIN_YEAR
    lrsm_scholar_grant_matches.csv — pubs with a grant ID in the title
    lrsm_scholar_missing.csv     — pubs NOT in grant_publications.csv
"""

import csv
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

MIN_YEAR          = 2023
EXISTING_PUBS_CSV = "grant_publications.csv"
ALL_PUBS_CSV      = "lrsm_scholar_pubs.csv"
MATCHES_CSV       = "lrsm_scholar_grant_matches.csv"
MISSING_CSV       = "lrsm_scholar_missing.csv"

GRANT_VARIANTS = [
    "2309043",
    "DMR-2309043",
    "DMR2309043",
    "23-09043",
    "DMR23-09043",
]
_GRANT_RE = [re.compile(re.escape(v), re.IGNORECASE) for v in GRANT_VARIANTS]

# PI name → Google Scholar user ID
SCHOLAR_IDS: dict[str, str] = {
    # Leadership / Co-leads
    "Eric A. Stach":       "OP3retUAAAAJ",
    "Andrea Liu":          "x0NU7KEAAAAJ",
    # IRG 1
    "Eleni Katifori":      "2ETz5HcAAAAJ",
    "John Crocker":        "NLH31KIAAAAJ",
    "Marc Miskin":         "3Y5LfW4AAAAJ",
    "Matthew Good":        "HMZ_caAAAAAJ",
    "Elizabeth Rhoades":   "4vrjUB8AAAAJ",
    "Yi-Wei Chang":        "m8otWusAAAAJ",
    "Doug Durian":         "YgFOewgAAAAJ",
    "David Issadore":      "FnPWUaIAAAAJ",
    "Paul Janmey":         "uwShy2EAAAAJ",
    "Daeyeon Lee":         "isKj1xoAAAAJ",
    "Yoichiro Mori":       "Ojr_cGEAAAAJ",
    "James Pikul":         "4k7aX_oAAAAJ",
    "Robert Riggleman":    "aNlEZRUAAAAJ",
    "Kevin T. Turner":     "j_SWM_UAAAAJ",
    # IRG 2
    "David Chenoweth":     "OtnNOkwAAAAJ",
    "Daniel A. Hammer":    "-VUw0SoAAAAJ",
    "Alex Hughes":         "2I3Y6SgAAAAJ",
    "Tom Lubensky":        "Xu0UksoAAAAJ",
    "Chinedum Osuji":      "yt7S_-YAAAAJ",
    "Amish Patel":         "XllbW4gAAAAJ",
    "Kathleen Stebe":      "3PitVLwAAAAJ",
    "Shu Yang":            "K-qEUmgAAAAJ",
    "Arjun Yodh":          "LWAFHkEAAAAJ",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SLEEP = 3.0   # seconds between Scholar requests (be polite)

PUB_FIELDS     = ["pi_name", "title", "venue", "year", "cited_by",
                   "scholar_url", "grant_match"]
MISSING_FIELDS = ["pi_name", "title", "venue", "year", "cited_by",
                   "scholar_url"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def contains_grant(text: str) -> str | None:
    for pat, variant in zip(_GRANT_RE, GRANT_VARIANTS):
        if pat.search(text or ""):
            return variant
    return None


def norm_title(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def load_known_titles() -> set[str]:
    """Load normalised titles from grant_publications.csv."""
    known: set[str] = set()
    if not Path(EXISTING_PUBS_CSV).exists():
        return known
    with open(EXISTING_PUBS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = norm_title(row.get("title", "") or row.get("Title", ""))
            if t:
                known.add(t)
    return known


def append_rows(filepath: str, rows: list[dict], fieldnames: list[str]):
    write_header = not Path(filepath).exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(rows)


# ── Scholar scraping ──────────────────────────────────────────────────────────

SCHOLAR_BASE = "https://scholar.google.com"

def fetch_scholar_profile(user_id: str, pi_name: str) -> list[dict]:
    """
    Fetch paginated Scholar profile for user_id, return all pubs >= MIN_YEAR.
    Scholar returns up to 100 rows per page; we stop once we see years < MIN_YEAR.
    """
    pubs = []
    cstart = 0
    pagesize = 100

    while True:
        url = (
            f"{SCHOLAR_BASE}/citations"
            f"?user={user_id}&sortby=pubdate&cstart={cstart}&pagesize={pagesize}"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"    [!] Request error: {e}")
            break

        if resp.status_code == 429:
            print("    [!] Rate-limited (429). Sleeping 60s...")
            time.sleep(60)
            continue
        if resp.status_code != 200:
            print(f"    [!] HTTP {resp.status_code}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.gsc_a_tr")
        if not rows:
            break

        found_old = False
        for row in rows:
            title_tag = row.select_one("a.gsc_a_at")
            year_tag  = row.select_one("td.gsc_a_y span")
            cite_tag  = row.select_one("td.gsc_a_c a.gsc_a_ac")
            venue_tags = row.select("div.gs_gray")

            title    = title_tag.get_text(strip=True) if title_tag else ""
            year_str = year_tag.get_text(strip=True)  if year_tag  else ""
            cited_by = cite_tag.get_text(strip=True)  if cite_tag  else "0"
            venue    = venue_tags[0].get_text(strip=True) if venue_tags else ""
            link     = (SCHOLAR_BASE + title_tag["href"]) if title_tag and title_tag.get("href") else ""

            try:
                year = int(year_str)
            except ValueError:
                year = 0

            if year and year < MIN_YEAR:
                found_old = True
                continue   # keep scanning — rows may not be perfectly sorted

            if not title:
                continue

            pubs.append({
                "pi_name":     pi_name,
                "title":       title,
                "venue":       venue,
                "year":        year_str,
                "cited_by":    cited_by,
                "scholar_url": link,
                "grant_match": contains_grant(title) or "",
            })

        time.sleep(SLEEP)

        # If all rows on this page were old, stop paginating
        if found_old and all(
            (int(row.select_one("td.gsc_a_y span").get_text(strip=True) or "0") < MIN_YEAR
             if row.select_one("td.gsc_a_y span") else True)
            for row in rows
        ):
            break

        if len(rows) < pagesize:
            break   # last page

        cstart += pagesize

    return pubs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    known_titles = load_known_titles()
    print(f"Known grant pub titles loaded: {len(known_titles)}")

    total_pubs = total_matches = total_missing = 0

    for pi_name, user_id in SCHOLAR_IDS.items():
        print(f"\n{pi_name}  [{user_id}]")
        pubs = fetch_scholar_profile(user_id, pi_name)
        print(f"  {len(pubs)} pubs since {MIN_YEAR}")

        pub_rows     = []
        match_rows   = []
        missing_rows = []

        for pub in pubs:
            pub_rows.append(pub)
            if pub["grant_match"]:
                match_rows.append(pub)
                print(f"  *** GRANT MATCH ({pub['grant_match']}): {pub['title'][:70]}")
            elif norm_title(pub["title"]) not in known_titles:
                missing_rows.append(pub)

        if pub_rows:
            append_rows(ALL_PUBS_CSV, pub_rows, PUB_FIELDS)
        if match_rows:
            append_rows(MATCHES_CSV, match_rows, PUB_FIELDS)
        if missing_rows:
            append_rows(MISSING_CSV, missing_rows, MISSING_FIELDS)

        total_pubs    += len(pub_rows)
        total_matches += len(match_rows)
        total_missing += len(missing_rows)

        print(f"  {len(pub_rows)} pubs | {len(match_rows)} grant matches | "
              f"{len(missing_rows)} not in existing grant list")

    print(f"""
── Complete ─────────────────────────────────────────────────────
  PIs processed     : {len(SCHOLAR_IDS)}
  Total pubs found  : {total_pubs}
  Grant matches     : {total_matches}
  Not in grant list : {total_missing}

  All pubs     → {ALL_PUBS_CSV}
  Matches      → {MATCHES_CSV}
  Missing      → {MISSING_CSV}
─────────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
