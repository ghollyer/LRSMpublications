"""
lrsm_scholar_scraper.py
-----------------------
1. Scrapes all MRSEC PIs from https://www.lrsm.upenn.edu/mrsec-participants/
2. Finds each PI's OpenAlex author profile (by name + UPenn affiliation)
3. Retrieves all their works since MIN_YEAR
4. Flags works that mention the grant in OpenAlex's structured grants field
5. Cross-references against grant_publications.csv (OpenAlex pipeline output)
   to surface PI papers NOT yet captured by the grant query

Outputs:
    lrsm_pi_pubs.csv       — all works per PI since MIN_YEAR
    lrsm_grant_matches.csv — works that carry any GRANT_VARIANTS in grants field
    lrsm_missing_pubs.csv  — PI works NOT in grant_publications.csv (potential gaps)
"""

import csv
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

LRSM_URL      = "https://www.lrsm.upenn.edu/mrsec-participants/"
MIN_YEAR      = 2023
EMAIL         = "ghollyer@seas.upenn.edu"
OPENALEX_BASE = "https://api.openalex.org"
UPENN_ROR     = "https://ror.org/00b30xv10"   # University of Pennsylvania

# Hardcoded OpenAlex IDs for PIs that are hard to disambiguate by name search
HARDCODED_IDS: dict[str, str] = {
    "Yoichiro Mori":   "A5057292858",   # UPenn mathematical biologist
    "Doug Durian":     "A5052862439",   # D. J. Durian, soft matter/granular
    "Matthew Good":    "A5088939845",   # UPenn cell biology / phase separation
    "Daniel A. Hammer":"A5059067804",   # UPenn bioengineering (Polymersomes)
    "Kevin T. Turner": "A5022108747",   # UPenn mechanical engineering
}

GRANT_VARIANTS = [
    "2309043",
    "DMR-2309043",
    "DMR2309043",
    "23-09043",
    "DMR23-09043",
]
_GRANT_RE = [re.compile(re.escape(v), re.IGNORECASE) for v in GRANT_VARIANTS]

EXISTING_PUBS_CSV = "grant_publications.csv"   # output from openalex_grant_miner.ipynb
WOS_FILE          = "savedrecs (2).xlsx"

ALL_PUBS_CSV    = "lrsm_pi_pubs.csv"
MATCHES_CSV     = "lrsm_grant_matches.csv"
MISSING_CSV     = "lrsm_missing_pubs.csv"
PROGRESS_FILE   = "lrsm_scraper_progress.json"

SLEEP = 0.12   # polite-pool rate limit (10 req/s)

# ── Helpers ───────────────────────────────────────────────────────────────────

def oa_get(path: str, params: dict) -> dict:
    params["mailto"] = EMAIL
    r = requests.get(f"{OPENALEX_BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    time.sleep(SLEEP)
    return r.json()


def norm_doi(doi: str) -> str:
    return (doi or "").lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()


def contains_grant(text: str) -> str | None:
    for pat, variant in zip(_GRANT_RE, GRANT_VARIANTS):
        if pat.search(text or ""):
            return variant
    return None


def abstract_from_index(inv: dict | None) -> str:
    """Reconstruct abstract from OpenAlex inverted index."""
    if not inv:
        return ""
    positions = {}
    for word, locs in inv.items():
        for loc in locs:
            positions[loc] = word
    return " ".join(positions[i] for i in sorted(positions))


# ── Step 1: scrape LRSM page ──────────────────────────────────────────────────

def scrape_lrsm_pis() -> list[dict]:
    resp = requests.get(LRSM_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    pis, seen = [], set()
    current_group = "Leadership / Co-leads"

    for tag in soup.find_all(["h2", "h3", "h4"]):
        if tag.name in ("h2", "h3"):
            current_group = tag.get_text(strip=True)
        elif tag.name == "h4":
            a = tag.find("a")
            if not a:
                continue
            name = a.get_text(strip=True)
            if name and name not in seen:
                seen.add(name)
                pis.append({"name": name, "group": current_group,
                             "profile_url": a.get("href", "")})
    return pis


# ── Step 2: find OpenAlex author ID ──────────────────────────────────────────

def find_openalex_author(name: str) -> dict | None:
    """
    Return the OpenAlex author record for a PI.
    Uses HARDCODED_IDS first; otherwise searches by name, preferring UPenn affiliation.
    """
    if name in HARDCODED_IDS:
        oa_id = HARDCODED_IDS[name]
        data = oa_get(f"authors/{oa_id}", {})
        if "id" in data:
            return data
        print(f"  [!] Hardcoded ID {oa_id} not found — falling back to search")

    data = oa_get("authors", {"search": name, "per_page": 5})
    results = data.get("results", [])
    if not results:
        return None

    def score(a):
        insts = a.get("last_known_institutions") or []
        upenn = any(UPENN_ROR in (i.get("ror") or "") for i in insts)
        return (int(upenn), a.get("works_count", 0))

    return max(results, key=score)


# ── Step 3: fetch works since MIN_YEAR ───────────────────────────────────────

def fetch_works(author_id: str) -> list[dict]:
    """Page through all works for an author since MIN_YEAR."""
    all_works, cursor = [], "*"
    while True:
        data = oa_get("works", {
            "filter": f"author.id:{author_id},publication_year:>{MIN_YEAR - 1}",
            "per_page": 200,
            "cursor": cursor,
        })
        results = data.get("results", [])
        all_works.extend(results)
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or not results:
            break
    return all_works


def parse_work(work: dict, pi_name: str, group: str) -> dict:
    source = (work.get("primary_location") or {}).get("source") or {}
    abstract = ""

    # Check grants field
    grant_match = ""
    for g in work.get("grants") or []:
        award = g.get("award_id") or ""
        m = contains_grant(award)
        if m:
            grant_match = m
            break
    # Also check title + abstract text as fallback
    if not grant_match:
        grant_match = contains_grant(f"{work.get('title','')} {abstract}") or ""

    return {
        "pi_name":      pi_name,
        "group":        group,
        "openalex_id":  work.get("id", "").split("/")[-1],
        "title":        work.get("title", ""),
        "doi":          norm_doi(work.get("doi", "") or ""),
        "journal":      source.get("display_name", ""),
        "year":         work.get("publication_year", ""),
        "date":         work.get("publication_date", ""),
        "cited_by":     work.get("cited_by_count", 0),
        "open_access":  (work.get("open_access") or {}).get("is_oa", False),
        "grant_match":  grant_match,
        "abstract":     abstract[:500],   # truncate for CSV readability
    }


# ── Progress helpers ──────────────────────────────────────────────────────────

def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": []}


def save_progress(p: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, indent=2)


def append_rows(filepath: str, rows: list[dict], fieldnames: list[str]):
    write_header = not Path(filepath).exists()
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Field lists ───────────────────────────────────────────────────────────────

PUB_FIELDS = ["pi_name", "group", "openalex_id", "title", "doi", "journal",
               "year", "date", "cited_by", "open_access", "grant_match", "abstract"]

MATCH_FIELDS = ["pi_name", "group", "title", "doi", "journal", "year",
                "cited_by", "open_access", "grant_match"]

MISSING_FIELDS = ["pi_name", "group", "title", "doi", "journal", "year",
                  "cited_by", "open_access", "openalex_id"]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load existing grant pubs for cross-ref (DOIs already known to acknowledge the grant)
    known_dois: set[str] = set()
    if Path(EXISTING_PUBS_CSV).exists():
        with open(EXISTING_PUBS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = norm_doi(row.get("doi", ""))
                if d:
                    known_dois.add(d)
        print(f"Loaded {len(known_dois)} known grant DOIs from {EXISTING_PUBS_CSV}\n")

    print("── Step 1: scraping LRSM participants page ──────────────────────")
    pis = scrape_lrsm_pis()
    print(f"Found {len(pis)} PIs\n")

    progress = load_progress()
    done_names = set(progress["done"])

    total_pubs = total_matches = total_missing = 0

    print("── Step 2-4: OpenAlex scrape ────────────────────────────────────")
    for pi in pis:
        name = pi["name"]

        if name in done_names:
            print(f"[skip] {name}")
            continue

        print(f"\n[{pi['group']}] {name}")

        author = find_openalex_author(name)
        if author is None:
            print(f"  [!] Not found in OpenAlex")
            progress["done"].append(name)
            save_progress(progress)
            continue

        author_id = author["id"].split("/")[-1]
        insts = [i.get("display_name","") for i in (author.get("last_known_institutions") or [])]
        print(f"  OpenAlex: {author['display_name']} ({', '.join(insts)}) — {author['works_count']} total works")

        works = fetch_works(author_id)
        print(f"  Works since {MIN_YEAR}: {len(works)}")

        pub_rows, match_rows, missing_rows = [], [], []

        for work in works:
            row = parse_work(work, name, pi["group"])
            pub_rows.append(row)

            if row["grant_match"]:
                match_rows.append(row)
                print(f"    GRANT MATCH ({row['grant_match']}): {row['title'][:70]}")

            # Flag PI works not already in our grant pub list
            if row["doi"] and row["doi"] not in known_dois and not row["grant_match"]:
                missing_rows.append(row)

        if pub_rows:
            append_rows(ALL_PUBS_CSV, pub_rows, PUB_FIELDS)
        if match_rows:
            append_rows(MATCHES_CSV, match_rows, MATCH_FIELDS)
        if missing_rows:
            append_rows(MISSING_CSV, missing_rows, MISSING_FIELDS)

        total_pubs    += len(pub_rows)
        total_matches += len(match_rows)
        total_missing += len(missing_rows)

        print(f"  Done — {len(pub_rows)} pubs, {len(match_rows)} grant matches, "
              f"{len(missing_rows)} not in existing grant list")

        progress["done"].append(name)
        save_progress(progress)

    print(f"""
── Complete ─────────────────────────────────────────────────────
  PIs processed        : {len(pis)}
  Total works found    : {total_pubs}
  Grant matches        : {total_matches}
  Potential gaps       : {total_missing}

  All pubs       → {ALL_PUBS_CSV}
  Grant matches  → {MATCHES_CSV}
  Missing pubs   → {MISSING_CSV}
─────────────────────────────────────────────────────────────────
""")
    crossref_wos()


# ── WoS cross-reference ───────────────────────────────────────────────────────

def load_wos() -> tuple[dict, dict]:
    """Load WoS xlsx; return (doi_index, title_index) dicts -> wos record."""
    import openpyxl
    wb = openpyxl.load_workbook(WOS_FILE, read_only=True, data_only=True)
    ws = wb.active
    headers, wos_by_doi, wos_by_title = None, {}, {}
    for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if i == 11:
            headers = list(row)
        elif i > 11 and headers:
            rec = dict(zip(headers, row))
            if not rec.get("Title"):
                continue
            d = norm_doi(rec.get("DOI", "") or "")
            t = re.sub(r"\s+", " ", (rec.get("Title") or "").lower().strip())
            if d:
                wos_by_doi[d] = rec
            if t:
                wos_by_title[t] = rec
    wb.close()
    return wos_by_doi, wos_by_title


def crossref_wos():
    """
    Load lrsm_pi_pubs.csv and annotate each row with in_wos + wos_citations.
    Writes lrsm_pi_pubs_wos.csv.
    """
    if not Path(ALL_PUBS_CSV).exists():
        print("No PI pubs CSV to cross-reference.")
        return
    if not Path(WOS_FILE).exists():
        print(f"WoS file not found: {WOS_FILE}")
        return

    print("── Step 5: WoS cross-reference ──────────────────────────────────")
    wos_by_doi, wos_by_title = load_wos()
    print(f"WoS records: {len(wos_by_doi)} by DOI, {len(wos_by_title)} by title")

    with open(ALL_PUBS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    in_wos = not_in_wos = 0
    out_rows = []
    for row in rows:
        d = norm_doi(row.get("doi", ""))
        t = re.sub(r"\s+", " ", (row.get("title") or "").lower().strip())
        wos_rec = wos_by_doi.get(d) or wos_by_title.get(t)
        row["in_wos"]       = wos_rec is not None
        row["wos_citations"] = wos_rec.get("Total Citations", "") if wos_rec else ""
        out_rows.append(row)
        if wos_rec:
            in_wos += 1
        else:
            not_in_wos += 1

    out_fields = PUB_FIELDS + ["in_wos", "wos_citations"]
    out_file = "lrsm_pi_pubs_wos.csv"
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    print(f"  In WoS     : {in_wos}")
    print(f"  Not in WoS : {not_in_wos}")
    print(f"  Written    → {out_file}\n")


if __name__ == "__main__":
    main()
