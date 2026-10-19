"""
fill_missing_dois.py
--------------------
For rows in lrsm_all_pubs.csv that have no DOI, queries the Crossref API by
title and back-fills the DOI (plus journal/year if also missing) when a
confident match is found.

Confidence check: normalised title similarity >= 0.85 via SequenceMatcher.
Updates the CSV in-place.

Usage:
    python fill_missing_dois.py
    python fill_missing_dois.py --threshold 0.80   # looser matching
"""

import argparse
import csv
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

EMAIL    = "ghollyer@seas.upenn.edu"
CROSSREF = "https://api.crossref.org/works"
INPUT    = "lrsm_all_pubs.csv"


def norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def crossref_lookup(title: str) -> dict | None:
    """Return the top Crossref hit for a title, or None on failure."""
    try:
        r = requests.get(CROSSREF, params={
            "query.title": title,
            "rows": 1,
            "mailto": EMAIL,
        }, timeout=15)
        time.sleep(0.2)
        if r.status_code != 200:
            return None
        items = r.json().get("message", {}).get("items", [])
        return items[0] if items else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()

    path = Path(INPUT)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    no_doi = [r for r in rows if not r.get("doi", "").strip() and r.get("title", "").strip()]
    print(f"Rows without DOI: {len(no_doi)}")

    filled = skipped = failed = 0

    for i, row in enumerate(no_doi, 1):
        title = row["title"].strip()
        print(f"\n[{i}/{len(no_doi)}] {title[:70]}")

        hit = crossref_lookup(title)
        if not hit:
            print("  Crossref: no result")
            failed += 1
            continue

        hit_titles = hit.get("title", [])
        hit_title  = hit_titles[0] if hit_titles else ""
        hit_doi    = hit.get("DOI", "")
        score      = similarity(title, hit_title)

        print(f"  Match ({score:.2f}): {hit_title[:70]}")
        print(f"  DOI: {hit_doi}")

        if score < args.threshold:
            print(f"  Skipped (below threshold {args.threshold})")
            skipped += 1
            continue

        # Find and update the matching row in the full list
        for full_row in rows:
            if full_row is row:
                full_row["doi"] = hit_doi
                if not full_row.get("journal", "").strip():
                    container = hit.get("container-title", [])
                    full_row["journal"] = container[0] if container else ""
                if not full_row.get("year", "").strip():
                    issued = hit.get("issued", {}).get("date-parts", [[]])
                    if issued and issued[0]:
                        full_row["year"] = str(issued[0][0])
                break

        filled += 1
        print(f"  Filled!")

    # Write back
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"""
── Complete ──────────────────────────────────────
  DOIs filled  : {filled}
  Below thresh : {skipped}
  No result    : {failed}
  CSV updated  → {INPUT}
─────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
