import csv
import re
import sys
from datetime import date
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def digits_to_int(s: str):
    if s is None:
        return None
    n = re.sub(r"[^\d]", "", str(s))
    return int(n) if n else None


def extract_id_and_slug(url: str):
    m = re.search(r"/details/([0-9a-fA-F-]{36})/([^/]+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def normalize_platform(value):
    if value is None:
        return None
    v = str(value).strip().lower()
    if any(x in v for x in ["computer", "pc", "windows", "steam"]):
        return "PC"
    if "xbox" in v:
        return "Xbox"
    return None


def find_first_int(data, keys):
    if not isinstance(data, dict):
        return None
    for k in keys:
        if k in data:
            n = digits_to_int(data.get(k))
            if n is not None:
                return n
    return None


def extract_rows_from_api_payload(payload, run_date, creation_id, slug, url):
    """
    Primary extraction path: parse structured API payload returned by the app.
    Tries known platform/stat layouts first, then a generic recursive fallback.
    """
    rows_by_platform = {}

    def put(platform, likes, bookmarks, plays):
        if platform not in ("PC", "Xbox"):
            return
        if likes is None and bookmarks is None and plays is None:
            return
        rows_by_platform[platform] = {
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": platform,
            "plays": plays,
            "likes": likes,
            "bookmarks": bookmarks,
            "url": url,
        }

    def stats_from(d):
        if not isinstance(d, dict):
            return (None, None, None)
        likes = find_first_int(d, ["likes", "likeCount", "totalLikes"])
        bookmarks = find_first_int(d, ["bookmarks", "bookmarkCount", "favoriteCount", "favorites"])
        plays = find_first_int(d, ["plays", "playCount", "totalPlays", "uses", "downloadCount"])
        return (likes, bookmarks, plays)

    # Known-ish sections
    if isinstance(payload, dict):
        for key in ["platformStats", "statsByPlatform", "platforms", "stats"]:
            section = payload.get(key)
            if isinstance(section, list):
                for item in section:
                    if not isinstance(item, dict):
                        continue
                    platform = normalize_platform(
                        item.get("platform")
                        or item.get("platformName")
                        or item.get("hardware")
                        or item.get("name")
                        or item.get("code")
                    )
                    source = item.get("stats") if isinstance(item.get("stats"), dict) else item
                    likes, bookmarks, plays = stats_from(source)
                    put(platform, likes, bookmarks, plays)
            elif isinstance(section, dict):
                for k, v in section.items():
                    platform = normalize_platform(k)
                    source = v if isinstance(v, dict) else {}
                    likes, bookmarks, plays = stats_from(source)
                    put(platform, likes, bookmarks, plays)

    # Generic recursive scan for platform+stats dictionaries
    def walk(node):
        if isinstance(node, dict):
            platform = normalize_platform(
                node.get("platform")
                or node.get("platformName")
                or node.get("hardware")
                or node.get("name")
                or node.get("code")
            )
            source = node.get("stats") if isinstance(node.get("stats"), dict) else node
            likes, bookmarks, plays = stats_from(source)
            put(platform, likes, bookmarks, plays)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for i in node:
                walk(i)

    walk(payload)
    return [rows_by_platform[p] for p in ["PC", "Xbox"] if p in rows_by_platform]


def find_platform_block(text: str, platform_label: str):
    """
    Secondary fallback path: parse visible text blocks like:
    Xbox. Likes. 52. Bookmarks. 683. ... Plays. 142,488
    Computer. Likes. 16. Bookmarks. 159. ... Plays. 75,599
    """
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

    if likes is None and bookmarks is None and plays is None:
        return None

    return {"likes": likes, "bookmarks": bookmarks, "plays": plays}


def scrape_one(url: str):
    parsed = urlparse(url)
    if parsed.netloc != "creations.bethesda.net":
        raise ValueError(f"Unexpected domain: {parsed.netloc}")

    creation_id, slug = extract_id_and_slug(url)
    run_date = date.today().isoformat()

    api_payload = None
    text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        failed_requests = []

        def on_request_failed(req):
            failed_requests.append((req.resource_type, req.url, req.failure))

        page.on("requestfailed", on_request_failed)
        page.goto(url, wait_until="networkidle", timeout=60000)

        # Keep text parsing only as a fallback strategy.
        text = page.inner_text("body")
        context.close()
        browser.close()

    if not text.strip():
        blocked = [u for _t, u, _f in failed_requests if "cdn01.bethesda.net" in u]
        if blocked:
            print(
                "Warning: page data did not render because required CDN assets were blocked "
                "(ERR_BLOCKED_BY_ORB). Falling back to Unknown row.",
                file=sys.stderr,
            )
        else:
            print(
                "Warning: page rendered no text content; stats could not be parsed. "
                "Falling back to Unknown row.",
                file=sys.stderr,
            )

    # “Computer” is commonly used on the site; some pages may say “PC”
    pc = find_platform_block(text, "Computer") or find_platform_block(text, "PC")
    xbox = find_platform_block(text, "Xbox")

    rows = []

    # Primary strategy: structured API payload
    if api_payload is not None:
        rows = extract_rows_from_api_payload(api_payload, run_date, creation_id, slug, url)

    # Secondary strategy: legacy text parsing
    if not rows:
        pc = find_platform_block(text, "Computer") or find_platform_block(text, "PC")
        xbox = find_platform_block(text, "Xbox")

        if pc:
            rows.append({
                "date": run_date,
                "creation_id": creation_id,
                "slug": slug,
                "platform": "PC",
                "plays": pc["plays"],
                "likes": pc["likes"],
                "bookmarks": pc["bookmarks"],
                "url": url,
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
                "url": url,
            })

    if not rows:
        rows.append({
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": "Unknown",
            "plays": None,
            "likes": None,
            "bookmarks": None,
            "url": url,
        })

    if not rows:
        rows.append({
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": "Unknown",
            "plays": None,
            "likes": None,
            "bookmarks": None,
            "url": url
        })

    if not rows:
        rows.append({
            "date": run_date,
            "creation_id": creation_id,
            "slug": slug,
            "platform": "Unknown",
            "plays": None,
            "likes": None,
            "bookmarks": None,
            "url": url
        })

    return rows


def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts/scrape_creations_stats_to_csv.py <output_csv> <url1> [url2 ...]")
        sys.exit(1)

    out_csv = sys.argv[1]
    urls = sys.argv[2:]

    fieldnames = ["date", "creation_id", "slug", "platform", "plays", "likes", "bookmarks", "url"]

    all_rows = []
    for u in urls:
        all_rows.extend(scrape_one(u))

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
