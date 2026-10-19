"""
fetch_fulltext.py
-----------------
Downloads PDFs for papers in a CSV with a 'doi' column using a tiered strategy:

  Tier 1 — Unpaywall        : legal open-access PDF URLs
  Tier 2 — bioRxiv/medRxiv : preprint PDFs via direct DOI URL
  Tier 3 — arXiv            : preprint PDFs
  Tier 4 — EZproxy PDF      : Penn-licensed content via saved cookies
  Tier 5 — EZproxy HTML     : BeautifulSoup full-text extraction fallback
                              (run ezproxy_login.py first to create the cookie file)

Note: ChemRxiv (10.26434) is blocked by Cloudflare and cannot be fetched
      programmatically. Those papers must be downloaded manually.

PDFs are saved to pdfs/<doi_safe>.pdf.
Full-text fallbacks are saved to pdfs/<doi_safe>.txt.

Usage:
    python ezproxy_login.py          # one-time browser login
    python fetch_fulltext.py         # batch download
    python fetch_fulltext.py --limit 50   # process first N papers only
    python fetch_fulltext.py --no-playwright  # skip Playwright tier
"""

import argparse
import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

INPUT_CSV   = "lrsm_all_pubs_not_in_crossref.csv"
COOKIE_FILE = "ezproxy_cookies.json"
PDF_DIR     = Path("pdfs")
PROGRESS    = "fetch_progress.json"
EMAIL       = "ghollyer@seas.upenn.edu"

EZPROXY_BASE = "https://proxy.library.upenn.edu/login?url="

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html,*/*;q=0.8",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def norm_doi(doi: str) -> str:
    return (doi or "").lower().replace("https://doi.org/", "").replace("http://doi.org/", "").strip()

def doi_safe(doi: str) -> str:
    return re.sub(r"[^\w\-.]", "_", doi)[:120]

def load_progress() -> set:
    if Path(PROGRESS).exists():
        with open(PROGRESS) as f:
            return set(json.load(f).get("done", []))
    return set()

def save_progress(done: set):
    with open(PROGRESS, "w") as f:
        json.dump({"done": list(done)}, f)


# ── EZproxy session ───────────────────────────────────────────────────────────

def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if not Path(COOKIE_FILE).exists():
        print(f"[!] {COOKIE_FILE} not found. Run ezproxy_login.py first for paywalled access.")
        return s
    with open(COOKIE_FILE) as f:
        cookies = json.load(f)
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."), path=c.get("path", "/"))
    print(f"Loaded {len(cookies)} EZproxy cookies.")
    return s


# ── Tier 1: Unpaywall ─────────────────────────────────────────────────────────

def unpaywall_pdf_url(doi: str) -> str | None:
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={EMAIL}", timeout=10)
        time.sleep(0.5)
        if r.status_code != 200:
            return None
        loc = r.json().get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url")
    except Exception:
        return None


# ── Tier 2: bioRxiv / medRxiv ────────────────────────────────────────────────

def biorxiv_pdf_bytes(doi: str) -> tuple[bytes, str] | tuple[None, None]:
    """Fetch PDF bytes for bioRxiv/medRxiv DOIs (10.1101/...). Returns (bytes, url) or (None, None)."""
    if not doi.startswith("10.1101/"):
        return None, None
    for base in ("https://www.biorxiv.org", "https://www.medrxiv.org"):
        url = f"{base}/content/{doi}.full.pdf"
        try:
            r = requests.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200 and "pdf" in r.headers.get("Content-Type", ""):
                return r.content, r.url
        except Exception:
            pass
    return None, None


# ── Tier 3: arXiv ─────────────────────────────────────────────────────────────

ARXIV_NS = "http://www.w3.org/2005/Atom"

def arxiv_pdf_url(title: str) -> str | None:
    short = re.sub(r"[^\w\s]", " ", title)[:80].strip()
    try:
        r = requests.get("https://export.arxiv.org/api/query", params={
            "search_query": f'ti:"{short}"', "max_results": 3
        }, timeout=10)
        time.sleep(0.5)
        root = ET.fromstring(r.text)
        for entry in root.findall(f"{{{ARXIV_NS}}}entry"):
            et = (entry.findtext(f"{{{ARXIV_NS}}}title") or "").lower()
            if sum(1 for w in et.split()[:8] if w in title.lower()) >= 5:
                for link in entry.findall(f"{{{ARXIV_NS}}}link"):
                    if link.get("title") == "pdf":
                        return link.get("href")
    except Exception:
        pass
    return None


# ── Tier 4: EZproxy ──────────────────────────────────────────────────────────

def _follow_meta_refresh(r: requests.Response, session: requests.Session, max_hops: int = 3) -> requests.Response:
    """Follow <meta http-equiv='refresh'> redirects that requests doesn't handle."""
    from bs4 import BeautifulSoup
    for _ in range(max_hops):
        ct = r.headers.get("Content-Type", "")
        if "html" not in ct or len(r.text) > 50_000:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if not tag:
            break
        content = tag.get("content", "")
        m = re.search(r"url=['\"]?([^'\">\s]+)", content, re.I)
        if not m:
            break
        url = m.group(1).strip("'\"")
        if not url.startswith("http"):
            url = requests.compat.urljoin(r.url, url)
        time.sleep(1.0)
        r = session.get(url, timeout=20, allow_redirects=True)
    return r


def ezproxy_fetch(doi: str, session: requests.Session) -> tuple[bytes | None, str | None]:
    """
    Fetch content via EZproxy. Returns (pdf_bytes, html_text) where at most one
    is non-None. html_text is returned when we get an article page but no PDF link,
    so the caller can fall through to HTML text extraction.
    """
    proxy_url = f"{EZPROXY_BASE}https://doi.org/{doi}"
    try:
        r = session.get(proxy_url, timeout=20, allow_redirects=True)
        time.sleep(2.0)
        if r.status_code != 200:
            return None, None
        r = _follow_meta_refresh(r, session)
        ct = r.headers.get("Content-Type", "")
        if "pdf" in ct:
            return r.content, None
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf($|\?)", href, re.IGNORECASE):
                pdf_url = href if href.startswith("http") else r.url.rsplit("/", 1)[0] + "/" + href
                r2 = session.get(pdf_url, timeout=20)
                time.sleep(2.0)
                if r2.status_code == 200 and "pdf" in r2.headers.get("Content-Type", ""):
                    return r2.content, None
        for tag in soup.find_all(["iframe", "embed"], src=True):
            src = tag["src"]
            if "pdf" in src.lower():
                pdf_url = src if src.startswith("http") else "https://" + src.lstrip("/")
                r2 = session.get(pdf_url, timeout=20)
                time.sleep(2.0)
                if r2.status_code == 200:
                    return r2.content, None
        # No PDF found — return the HTML for text extraction fallback
        if "html" in ct:
            return None, r.text
    except Exception as e:
        print(f"    EZproxy error: {e}")
    return None, None


# ── Tier 4b: Playwright (JS-rendered EZproxy) ────────────────────────────────

def playwright_fetch(doi: str, cookie_file: str = COOKIE_FILE) -> tuple[bytes | None, str | None]:
    """
    Render the EZproxy page in a headless Chromium browser with saved cookies.
    Handles JavaScript-gated publisher pages (ACS, Wiley, PNAS, etc.).
    Returns (pdf_bytes, article_text) where at most one is non-None.
    """
    if not Path(cookie_file).exists():
        return None, None

    with open(cookie_file) as f:
        raw_cookies = json.load(f)

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                accept_downloads=True,
            )

            # Build per-domain cookie strings for header injection.
            # We bypass Playwright's cookie-matching by routing requests and
            # injecting the Cookie header directly — avoiding secure/SameSite issues.
            def _cookie_str(domain_substr: str) -> str:
                parts = [
                    f"{c['name']}={c['value']}"
                    for c in raw_cookies
                    if domain_substr in c.get("domain", "")
                ]
                return "; ".join(parts)

            ezproxy_cookie_str = _cookie_str("proxy.library.upenn.edu")

            page = context.new_page()

            # Intercept every request to proxy.library.upenn.edu and inject
            # the EZProxy session cookies directly into the Cookie header.
            def _inject_cookies(route):
                headers = dict(route.request.headers)
                if ezproxy_cookie_str:
                    existing = headers.get("cookie", "")
                    headers["cookie"] = (existing + "; " + ezproxy_cookie_str).strip("; ")
                route.continue_(headers=headers)

            page.route("**proxy.library.upenn.edu**", _inject_cookies)

            proxy_url = f"{EZPROXY_BASE}https://doi.org/{doi}"

            # Navigate and wait for network to settle
            try:
                page.goto(proxy_url, timeout=30_000, wait_until="networkidle")
            except PWTimeout:
                page.wait_for_timeout(3000)

            # Look for PDF download links
            pdf_bytes = None
            for selector in [
                "a[href*='.pdf']",
                "a[title*='PDF' i]",
                "a[aria-label*='PDF' i]",
                "a:has-text('Download PDF')",
                "a:has-text('PDF')",
                "button:has-text('PDF')",
            ]:
                try:
                    el = page.query_selector(selector)
                    if not el:
                        continue
                    href = el.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = page.url.rsplit("/", 1)[0] + "/" + href.lstrip("/")
                    if href:
                        with context.expect_page() as new_page_info:
                            try:
                                with page.expect_download(timeout=15_000) as dl_info:
                                    el.click()
                                dl = dl_info.value
                                import tempfile, os
                                tmp = tempfile.mktemp(suffix=".pdf")
                                dl.save_as(tmp)
                                with open(tmp, "rb") as fh:
                                    pdf_bytes = fh.read()
                                os.unlink(tmp)
                                if pdf_bytes:
                                    break
                            except Exception:
                                pass
                        # If no download triggered, try fetching href directly
                        if not pdf_bytes and href.startswith("http"):
                            r2 = page.request.get(href)
                            if "pdf" in (r2.headers.get("content-type") or "").lower():
                                pdf_bytes = r2.body()
                                if pdf_bytes:
                                    break
                except Exception:
                    continue

            if pdf_bytes:
                browser.close()
                return pdf_bytes, None

            # No PDF — extract rendered article text
            article_text = None
            for selector in _ARTICLE_SELECTORS:
                try:
                    el = page.query_selector(selector)
                    if el:
                        text = el.inner_text()
                        if len(text) > 1000:
                            article_text = text
                            break
                except Exception:
                    continue

            if not article_text:
                # Last resort: full body text
                try:
                    body = page.query_selector("body")
                    if body:
                        text = body.inner_text()
                        if len(text) > 1000:
                            article_text = text
                except Exception:
                    pass

            browser.close()
            return None, article_text

    except Exception as e:
        print(f"    Playwright error: {e}")
        return None, None


# ── Tier 5: HTML full-text extraction ────────────────────────────────────────

_ARTICLE_SELECTORS = [
    "article",
    "[class*='article-body']",
    "[class*='article-text']",
    "[class*='article-content']",
    "[class*='fulltext']",
    "[class*='full-text']",
    ".Body",           # Elsevier
    ".c-article-body", # Nature/Springer
    "#article",
    "main",
]

def extract_article_text(html: str) -> str | None:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure"]):
        tag.decompose()
    for selector in _ARTICLE_SELECTORS:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 1000:
                return text
    # Last resort: full body
    body = soup.body
    if body:
        text = body.get_text(separator="\n", strip=True)
        if len(text) > 1000:
            return text
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",      default=INPUT_CSV)
    parser.add_argument("--limit",      type=int, default=0)
    parser.add_argument("--no-ezproxy",    action="store_true")
    parser.add_argument("--no-playwright", action="store_true")
    args = parser.parse_args()

    PDF_DIR.mkdir(exist_ok=True)

    with open(args.input, newline="", encoding="utf-8") as f:
        papers = [r for r in csv.DictReader(f) if r.get("doi")]
    if args.limit:
        papers = papers[:args.limit]

    print(f"Papers to process: {len(papers)}")

    done = load_progress()
    session = None if args.no_ezproxy else build_session()
    downloaded = skipped = failed = 0

    for paper in papers:
        doi   = norm_doi(paper.get("doi", ""))
        title = paper.get("title", "")

        if not doi or doi in done:
            skipped += 1
            continue

        stem     = doi_safe(doi)
        out_path = PDF_DIR / f"{stem}.pdf"
        txt_path = PDF_DIR / f"{stem}.txt"
        if out_path.exists() or txt_path.exists():
            done.add(doi)
            skipped += 1
            continue

        print(f"\n{title[:70]}\n  doi: {doi}")

        pdf_bytes = None
        html_text = None
        via = None

        # Tier 1: Unpaywall
        oa_url = unpaywall_pdf_url(doi)
        if oa_url:
            try:
                r = requests.get(oa_url, headers=HEADERS, timeout=20)
                if r.status_code == 200 and "pdf" in r.headers.get("Content-Type", ""):
                    pdf_bytes = r.content
                    via = f"Unpaywall ({oa_url[:60]})"
            except Exception:
                pass

        # Tier 2: bioRxiv / medRxiv
        if not pdf_bytes:
            pdf_bytes, bx_url = biorxiv_pdf_bytes(doi)
            if pdf_bytes:
                via = f"bioRxiv/medRxiv ({bx_url})"

        # Tier 3: arXiv
        if not pdf_bytes and title:
            ax_url = arxiv_pdf_url(title)
            if ax_url:
                try:
                    r = requests.get(ax_url, headers=HEADERS, timeout=20)
                    if r.status_code == 200:
                        pdf_bytes = r.content
                        via = f"arXiv ({ax_url})"
                except Exception:
                    pass

        # Tier 4: EZproxy PDF/HTML via requests
        article_text = None
        if not pdf_bytes and session:
            pdf_bytes, html_text = ezproxy_fetch(doi, session)
            if pdf_bytes:
                via = "EZproxy PDF"
            elif html_text:
                article_text = extract_article_text(html_text)
                if article_text:
                    via = "EZproxy HTML"

        # Tier 4b: Playwright (JS-rendered EZproxy) — for paywalled JS-gated pages
        if not pdf_bytes and not article_text and not args.no_playwright:
            pdf_bytes, article_text = playwright_fetch(doi)
            if pdf_bytes:
                via = "Playwright PDF"
            elif article_text:
                via = "Playwright HTML"

        if pdf_bytes:
            out_path.write_bytes(pdf_bytes)
            print(f"  Saved {len(pdf_bytes)//1024} KB via {via}")
            downloaded += 1
        elif article_text and via in ("EZproxy HTML", "Playwright HTML"):
            txt_path.write_text(article_text, encoding="utf-8")
            print(f"  Saved {len(article_text)} chars text via {via}")
            downloaded += 1
        else:
            print("  No content found.")
            failed += 1

        done.add(doi)
        save_progress(done)

    print(f"""
── Complete ─────────────────────────────────────
  Downloaded : {downloaded}
  Not found  : {failed}
  Skipped    : {skipped}
  PDFs/text  → {PDF_DIR}/
─────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
