"""
search_acknowledgements.py
--------------------------
Searches every file in pdfs/ (.pdf and .txt) for:
  1. An acknowledgements section
  2. Mentions of the DMR-2309043 grant (several spelling variants)

Matches DOI filenames back to lrsm_all_pubs.csv for metadata.

Output: ack_search_results.csv
"""

import csv
import io
import re
from pathlib import Path

import pdfplumber

# ── Config ────────────────────────────────────────────────────────────────────

PDF_DIR    = Path("pdfs")
INPUT_CSV  = "lrsm_all_pubs.csv"
OUTPUT_CSV = "ack_search_results.csv"

GRANT_VARIANTS = [
    "DMR-2309043", "DMR2309043", "DMR 2309043",
    "23-09043", "DMR23-09043", "2309043",
]
_GRANT_RE = re.compile(
    "|".join(re.escape(v) for v in GRANT_VARIANTS),
    re.IGNORECASE,
)

_ACK_RE = re.compile(
    r"acknowledg(e?ment|ement)s?",
    re.IGNORECASE,
)

CONTEXT_CHARS = 300   # characters of context around a grant match
ACK_EXTRACT   = 1000  # characters to extract after the acknowledgements header

OUTPUT_FIELDS = [
    "filename", "doi", "pi_name", "group", "title", "year", "journal",
    "has_ack_section", "ack_text",
    "grant_match", "matched_variant", "match_context",
    "source_format",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def doi_safe(doi: str) -> str:
    return re.sub(r"[^\w\-.]", "_", doi)[:120]


def extract_pdf_text(path: Path) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            parts = [p.extract_text() for p in pdf.pages if p.extract_text()]
            return "\n".join(parts)
    except Exception as e:
        print(f"  [!] PDF extract failed for {path.name}: {e}")
        return ""


def find_ack_section(text: str) -> tuple[bool, str]:
    """Return (found, extracted_text_after_header)."""
    m = _ACK_RE.search(text)
    if not m:
        return False, ""
    start = m.end()
    snippet = text[start : start + ACK_EXTRACT].strip()
    return True, snippet


def find_grant(text: str) -> tuple[bool, str, str]:
    """Return (found, matched_variant, context_snippet)."""
    m = _GRANT_RE.search(text)
    if not m:
        return False, "", ""
    variant = m.group(0)
    lo = max(0, m.start() - CONTEXT_CHARS)
    hi = min(len(text), m.end() + CONTEXT_CHARS)
    context = text[lo:hi].replace("\n", " ").strip()
    return True, variant, context


def load_metadata() -> dict[str, dict]:
    """Build a doi_safe_stem → row dict from lrsm_all_pubs.csv."""
    meta = {}
    try:
        with open(INPUT_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                doi = row.get("doi", "").strip().lower().replace("https://doi.org/", "")
                if doi:
                    meta[doi_safe(doi)] = row
    except FileNotFoundError:
        print(f"[!] {INPUT_CSV} not found — metadata will be empty")
    return meta


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    meta = load_metadata()
    files = sorted(PDF_DIR.iterdir())
    print(f"Files to search: {len(files)}")

    results = []
    ack_count = grant_count = 0

    for i, path in enumerate(files, 1):
        if path.suffix not in (".pdf", ".txt"):
            continue

        stem = path.stem  # e.g. 10.1021_jacs.3c02705
        row_meta = meta.get(stem, {})

        # ── Extract text ──────────────────────────────────────────────────
        if path.suffix == ".pdf":
            text = extract_pdf_text(path)
            fmt  = "PDF"
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                print(f"  [!] Read failed for {path.name}: {e}")
                text = ""
            fmt = "TXT"

        if not text:
            print(f"[{i}/{len(files)}] {path.name}: empty")
            results.append({
                "filename":       path.name,
                "doi":            row_meta.get("doi", stem),
                "pi_name":        row_meta.get("pi_name", ""),
                "group":          row_meta.get("group", ""),
                "title":          row_meta.get("title", ""),
                "year":           row_meta.get("year", ""),
                "journal":        row_meta.get("journal", ""),
                "has_ack_section": False,
                "ack_text":       "",
                "grant_match":    False,
                "matched_variant": "",
                "match_context":  "",
                "source_format":  fmt,
            })
            continue

        # ── Search ────────────────────────────────────────────────────────
        has_ack, ack_text   = find_ack_section(text)
        has_grant, variant, context = find_grant(text)

        if has_ack:   ack_count   += 1
        if has_grant: grant_count += 1

        status = []
        if has_ack:   status.append("ACK")
        if has_grant: status.append(f"GRANT({variant})")
        print(f"[{i}/{len(files)}] {path.name[:55]}: {', '.join(status) or '-'}")

        results.append({
            "filename":        path.name,
            "doi":             row_meta.get("doi", stem),
            "pi_name":         row_meta.get("pi_name", ""),
            "group":           row_meta.get("group", ""),
            "title":           row_meta.get("title", ""),
            "year":            row_meta.get("year", ""),
            "journal":         row_meta.get("journal", ""),
            "has_ack_section": has_ack,
            "ack_text":        ack_text,
            "grant_match":     has_grant,
            "matched_variant": variant,
            "match_context":   context,
            "source_format":   fmt,
        })

    # ── Write CSV ─────────────────────────────────────────────────────────────
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    print(f"""
── Complete ─────────────────────────────────────────────────────────
  Files searched       : {len(results)}
  Has ack section      : {ack_count}  ({100*ack_count//max(len(results),1)}%)
  Grant match          : {grant_count}
  Output               → {OUTPUT_CSV}
─────────────────────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
