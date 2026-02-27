import csv
import re
import sys
from datetime import date
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

def digits_to_int(s: str):
    if s is None:
        return None
    n = re.sub(r"[^\d]", "", s)
    return int(n) if n else None

def extract_id_and_slug(url: str):
    # /en/starfield/details/<uuid>/<slug>/details
    m = re.search(r"/details/([0-9a-fA-F-]{36})/([^/]+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)

def find_platform_block(text: str, platform_label: str):
    """
    Looks for blocks like:
    Xbox. Likes. 52. Bookmarks. 683. ... Plays. 142,488
    Computer. Likes. 16. Bookmarks. 159. ... Plays. 75,599
    """
    # Grab a window of text after the platform label
    m = re.search(rf"{platform_label}\s*(.*?)(?=(Xbox|Computer|PC)\b|$)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None

    block = m.group(1)

    def after(label):
        mm = re.search(rf"{label}\s*([\d,]+|---)", block, re.IGNORECASE)
        return mm.group(1) if mm else None

    likes_raw = after("Likes")
    bookmarks_raw = after("Bookmarks")
    plays_raw = after("Plays")

    likes = None if likes_raw in (None, "---") else digits_to_int(likes_raw)
    bookmarks = None if bookmarks_raw in (None, "---") else digits_to_int(bookmarks_raw)
    plays = None if plays_raw in (None, "---") else digits_to_int(plays_raw)

    # If we found nothing useful, treat as missing
    if likes is None and bookmarks is None and plays is None:
        return None

    return {"likes": likes, "bookmarks": bookmarks, "plays": plays}

def scrape_one(url: str):
    parsed = urlparse(url)
    if parsed.netloc != "creations.bethesda.net":
        raise ValueError(f"Unexpected domain: {parsed.netloc}")

    creation_id, slug = extract_id_and_slug(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        text = page.inner_text("body")
        browser.close()

    # “Computer” is commonly used on the site; some pages may say “PC”
    pc = find_platform_block(text, "Computer") or find_platform_block(text, "PC")
    xbox = find_platform_block(text, "Xbox")

    rows = []
    run_date = date.today().isoformat()

    if pc:
        rows.append({
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": "PC",
            "plays": pc["plays"],
            "likes": pc["likes"],
            "bookmarks": pc["bookmarks"],
            "url": url
        })

    if xbox:
        rows.append({
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": "Xbox",
            "plays": xbox["plays"],
            "likes": xbox["likes"],
            "bookmarks": xbox["bookmarks"],
            "url": url
        })

    return rows

def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/scrape_creations_stats_to_csv.py <output_csv> <url1> [url2 ...]")
        sys.exit(1)

    out_csv = sys.argv[1]
    urls = sys.argv[2:]

    fieldnames = ["date","creation_id","slug","platform","plays","likes","bookmarks","url"]

    all_rows = []
    for u in urls:
        all_rows.extend(scrape_one(u))

    # Append if exists, otherwise create with header
    try:
        with open(out_csv, "r", newline="", encoding="utf-8"):
            exists = True
    except FileNotFoundError:
        exists = False

    with open(out_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        for r in all_rows:
            w.writerow(r)

    print(f"Wrote {len(all_rows)} rows to {out_csv}")

if __name__ == "__main__":
    main()
