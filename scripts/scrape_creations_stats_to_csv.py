import csv
import re
import sys
from datetime import date
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def digits_to_int(s):
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


def fetch_api_payload(context, creation_id: str):
    """
    Primary strategy: directly query content endpoint via Playwright APIRequestContext.
    This avoids depending on the SPA to successfully bootstrap before we can parse stats.
    """
    endpoints = [
        f"https://api.bethesda.net/ugcmods/v2/content/{creation_id}",
        f"https://api.bethesda.net/ugcmods/v2/content/{creation_id}?draft=true",
    ]
    for endpoint in endpoints:
        try:
            resp = context.request.get(endpoint, timeout=30000)
            if resp.status < 400:
                return resp.json(), endpoint
        except Exception:
            pass
    return None, None


def scrape_one(url: str):
    parsed = urlparse(url)
    if parsed.netloc != "creations.bethesda.net":
        raise ValueError(f"Unexpected domain: {parsed.netloc}")

    creation_id, slug = extract_id_and_slug(url)
    run_date = date.today().isoformat()
    api_payload = None
    api_source = None
    text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)

        # Strategy 1 (primary): direct API call.
        api_payload, api_source = fetch_api_payload(context, creation_id)

        # Strategy 2: if direct API call failed, attempt capture from browser responses.
        if api_payload is None:
            page = context.new_page()
            content_url_re = re.compile(rf"https://api\.bethesda\.net/ugcmods/v2/content/{re.escape(creation_id)}(?:\?.*)?$")

            def on_response(resp):
                nonlocal api_payload, api_source
                if api_payload is not None:
                    return
                if not content_url_re.match(resp.url):
                    return
                if resp.status >= 400:
                    return
                try:
                    api_payload = resp.json()
                    api_source = resp.url
                except Exception:
                    api_payload = None

            page.on("response", on_response)
            page.goto(url, wait_until="networkidle", timeout=60000)

            # Strategy 3 (fallback): legacy visible text parsing.
            text = page.inner_text("body")

        context.close()
        browser.close()

    rows = []

    if api_payload is not None:
        rows = extract_rows_from_api_payload(api_payload, run_date, creation_id, slug, url)
        if rows:
            print(f"Info: extracted stats from API payload ({api_source}).", file=sys.stderr)

    if not rows and text:
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

        if rows:
            print("Info: extracted stats from visible text fallback.", file=sys.stderr)

    if not rows:
        print("Warning: API and text extraction yielded no stats; writing Unknown row.", file=sys.stderr)
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
