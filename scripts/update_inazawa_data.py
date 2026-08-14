#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "sources.json"
TEMPLATE = ROOT / "inazawa-tsunagaru/latest/template.html"
LATEST_HTML = ROOT / "inazawa-tsunagaru/latest/index.html"
JST = ZoneInfo("Asia/Tokyo")
UA = "Inazawa-Tsunagaru-Hub/2.0 (+https://yasu1738.github.io/inazawa-tsunagaru/)"
CANONICAL_URL = "https://yasu1738.github.io/inazawa-tsunagaru/latest/"
OG_IMAGE_URL = "https://yasu1738.github.io/inazawa-tsunagaru/assets/og-latest.svg"
CONTACT_URL = "https://note.com/qa/lumispark"
PAGE_TITLE = "稲沢市の最新情報まとめ（イベント・経営支援）｜稲沢つながる広場"
META_DESCRIPTION = (
    "毎朝6時に稲沢市公式サイトを確認し、イベント・企業支援の新着と更新情報を整理する非公式ページです。"
    "最終確認日時、掲載日、公式リンクを明示し、相談窓口へつなぎます。"
)

SKIP_TEXT = {
    "ホーム", "現在位置", "サイトマップ", "ページの先頭へ", "ページの先頭へ戻る",
    "メインメニュー", "本文へ", "文字サイズ", "検索", "閉じる", "戻る", "アクセス",
    "サイト運営方針", "ページID検索の使い方",
}

DATE_PATTERNS = (
    re.compile(r"(?P<year>20\d{2})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})\s*日?"),
    re.compile(r"令和\s*(?P<era>元|\d+)\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"),
    re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Inazawa official data and generate static latest page.")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Do not fetch remote sources; render latest/index.html from existing JSON files.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the existing latest/index.html and exit.",
    )
    parser.add_argument(
        "--now",
        help="Override current time for testing (ISO 8601, e.g. 2026-08-14T06:00:00+09:00).",
    )
    return parser.parse_args()


def now_jst(override: str | None = None) -> datetime:
    if override:
        parsed = datetime.fromisoformat(override)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST)
    return datetime.now(JST)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def item_key(item: dict) -> str:
    return canonicalize_url(str(item.get("url", ""))) + "\n" + clean(str(item.get("title", "")))


def parse_date_from_text(text: str, reference: datetime) -> str | None:
    text = clean(text)
    if not text:
        return None
    for index, pattern in enumerate(DATE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        try:
            if index == 0:
                year = int(match.group("year"))
            elif index == 1:
                era = match.group("era")
                year = 2018 + (1 if era == "元" else int(era))
            else:
                year = reference.year
            month = int(match.group("month"))
            day = int(match.group("day"))
            parsed = date(year, month, day)
            if index == 2 and parsed > reference.date() + timedelta(days=180):
                parsed = date(year - 1, month, day)
            return parsed.isoformat()
        except (TypeError, ValueError):
            continue
    return None


def date_near_link(anchor: Tag, reference: datetime) -> str | None:
    time_tag = anchor.find("time")
    if isinstance(time_tag, Tag):
        direct = parse_date_from_text(time_tag.get("datetime", "") or time_tag.get_text(" ", strip=True), reference)
        if direct:
            return direct

    row = anchor.find_parent(["li", "dd", "dt", "tr", "article"]) or anchor.parent
    if not isinstance(row, Tag):
        return None

    candidate_texts: list[str] = []
    for nested_time in row.find_all("time", limit=2):
        if nested_time.get("datetime"):
            candidate_texts.append(str(nested_time.get("datetime")))
        candidate_texts.append(nested_time.get_text(" ", strip=True))

    row_text = clean(row.get_text(" ", strip=True))
    if len(row_text) <= 100:
        candidate_texts.append(row_text)

    previous_count = 0
    for sibling in row.previous_siblings:
        if not isinstance(sibling, Tag):
            continue
        sibling_text = clean(sibling.get_text(" ", strip=True))
        if sibling_text and len(sibling_text) <= 40:
            candidate_texts.append(sibling_text)
        previous_count += 1
        if previous_count >= 2:
            break

    for candidate in candidate_texts:
        parsed = parse_date_from_text(candidate, reference)
        if parsed:
            return parsed
    return None


def is_candidate(text: str, href: str, source_url: str) -> bool:
    if not text or text in SKIP_TEXT or len(text) < 3:
        return False
    if text.startswith(("〒", "電話", "ファクス", "法人番号")):
        return False
    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False

    absolute = canonicalize_url(urljoin(source_url, href))
    source = urlsplit(source_url)
    target = urlsplit(absolute)
    if target.netloc != source.netloc:
        return False
    if absolute == canonicalize_url(source_url):
        return False
    if "/category/" in target.path:
        return False
    if target.path.endswith(("/sitemap.html", "/index.html")) and target.path.count("/") <= 2:
        return False
    return True


def extract_items(page_html: str, source_url: str, reference: datetime) -> list[dict]:
    soup = BeautifulSoup(page_html, "html.parser")
    root = soup.select_one("main, #main, #contents, #content, article") or soup.body or soup
    for tag in root.select("header, footer, nav, script, style, noscript, form, .breadcrumb, #breadcrumb"):
        tag.decompose()

    items: list[dict] = []
    seen: set[str] = set()
    for anchor in root.find_all("a", href=True):
        text = clean(anchor.get_text(" ", strip=True))
        href = str(anchor.get("href", ""))
        if not is_candidate(text, href, source_url):
            continue
        url = canonicalize_url(urljoin(source_url, href))
        key = url + "\n" + text
        if key in seen:
            continue
        seen.add(key)
        item = {"title": text, "url": url}
        published = date_near_link(anchor, reference)
        if published:
            item["published_date"] = published
        items.append(item)

    return sort_items(items)[:60]


def sort_items(items: Iterable[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda item: (
            item.get("published_date") or "0000-00-00",
            item.get("first_seen_at") or "",
            clean(str(item.get("title", ""))),
        ),
        reverse=True,
    )


def stable_hash(items: list[dict]) -> str:
    stable_items = [
        {
            "title": clean(str(item.get("title", ""))),
            "url": canonicalize_url(str(item.get("url", ""))),
            "published_date": item.get("published_date"),
        }
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(stable_items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def normalize_existing_for_render(payload: dict, checked_at: str) -> dict:
    items = []
    for raw in payload.get("items", []):
        if not isinstance(raw, dict):
            continue
        title = clean(str(raw.get("title", "")))
        url = canonicalize_url(str(raw.get("url", "")))
        if not title or not url or "/category/" in urlsplit(url).path:
            continue
        item = {"title": title, "url": url}
        for field in ("published_date", "first_seen_at"):
            if raw.get(field):
                item[field] = raw[field]
        item["is_new"] = bool(raw.get("is_new", False))
        items.append(item)
    items = sort_items(items)
    return {
        "schema_version": max(2, int(payload.get("schema_version", 1) or 1)),
        "source_id": payload.get("source_id", "unknown"),
        "source_label": payload.get("source_label", ""),
        "source_url": payload.get("source_url", ""),
        "content_hash": payload.get("content_hash") or stable_hash(items),
        "items": items,
        "new_count": sum(1 for item in items if item.get("is_new")),
        "last_changed_at": payload.get("last_changed_at") or checked_at,
        "last_checked_at": checked_at,
    }


def merge_payload(source: dict, extracted: list[dict], old: dict | None, checked_at: str) -> tuple[dict, bool]:
    old = old if isinstance(old, dict) else None
    old_items = {
        item_key(item): item
        for item in (old or {}).get("items", [])
        if isinstance(item, dict) and item.get("title") and item.get("url")
    }
    digest = stable_hash(extracted)
    content_changed = old is None or old.get("content_hash") != digest or int(old.get("schema_version", 1) or 1) < 2

    if content_changed:
        merged_items: list[dict] = []
        for raw in extracted:
            key = item_key(raw)
            previous = old_items.get(key, {})
            item = {
                "title": clean(str(raw.get("title", ""))),
                "url": canonicalize_url(str(raw.get("url", ""))),
                "is_new": bool(old is not None and key not in old_items),
                "first_seen_at": previous.get("first_seen_at") if previous else checked_at,
            }
            if raw.get("published_date"):
                item["published_date"] = raw["published_date"]
            elif previous.get("published_date"):
                item["published_date"] = previous["published_date"]
            merged_items.append(item)
        merged_items = sort_items(merged_items)
        last_changed_at = checked_at
    else:
        merged_items = sort_items([dict(item) for item in (old or {}).get("items", []) if isinstance(item, dict)])
        last_changed_at = (old or {}).get("last_changed_at") or checked_at

    payload = {
        "schema_version": 2,
        "source_id": source["id"],
        "source_label": source["label"],
        "source_url": source["url"],
        "content_hash": digest,
        "items": merged_items,
        "new_count": sum(1 for item in merged_items if item.get("is_new")),
        "last_changed_at": last_changed_at,
        "last_checked_at": checked_at,
    }
    return payload, content_changed


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def display_checked(value: str) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return value
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M JST")


def display_updated(value: str) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return value[:10]
    return parsed.astimezone(JST).strftime("%Y-%m-%d")


def display_item_date(value: str | None) -> str:
    if not value:
        return "掲載日不明"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        return value


def render_items(payload: dict, last_checked_display: str) -> str:
    items = payload.get("items", [])
    if not items:
        return (
            '<div class="empty-state">現在、新しい掲載はありません'
            f'（最終確認：{html_lib.escape(last_checked_display)}）。</div>'
        )

    rows: list[str] = ['<ul class="item-list">']
    for item in items:
        title = html_lib.escape(clean(str(item.get("title", ""))))
        url = html_lib.escape(canonicalize_url(str(item.get("url", ""))), quote=True)
        published = item.get("published_date")
        if published:
            date_html = (
                f'<time class="item-date" datetime="{html_lib.escape(str(published), quote=True)}">'
                f'{html_lib.escape(display_item_date(str(published)))}</time>'
            )
        else:
            date_html = '<span class="item-date">掲載日不明</span>'
        new_badge = '<span class="new-badge">NEW</span>' if item.get("is_new") else ""
        rows.append(
            '<li class="item">'
            f'<a class="item-link" href="{url}" target="_blank" rel="noopener noreferrer">'
            f'{date_html}<span class="item-title">{title}</span>'
            f'<span class="item-meta">{new_badge}<span class="external-mark" aria-hidden="true">↗</span>'
            '<span class="visually-hidden">稲沢市公式サイトを新しいタブで開く</span></span>'
            '</a></li>'
        )
    rows.append("</ul>")
    return "".join(rows)


def item_list_json_ld(identifier: str, name: str, payload: dict) -> dict:
    elements = []
    for position, item in enumerate(payload.get("items", []), start=1):
        entry: dict = {
            "@type": "ListItem",
            "position": position,
            "url": item.get("url"),
            "name": item.get("title"),
        }
        if item.get("published_date"):
            entry["datePublished"] = item["published_date"]
        elements.append(entry)
    return {
        "@type": "ItemList",
        "@id": CANONICAL_URL + identifier,
        "name": name,
        "numberOfItems": len(elements),
        "itemListElement": elements,
    }


def render_latest(payloads: dict[str, dict], checked_at: str) -> str:
    if "events" not in payloads or "business_support" not in payloads:
        raise RuntimeError("Both events and business_support payloads are required")
    template = TEMPLATE.read_text(encoding="utf-8")
    events = payloads["events"]
    business = payloads["business_support"]
    checked_display = display_checked(checked_at)

    changed_candidates = [
        parse_iso(events.get("last_changed_at")),
        parse_iso(business.get("last_changed_at")),
    ]
    changed_values = [value for value in changed_candidates if value is not None]
    last_updated = max(changed_values) if changed_values else parse_iso(checked_at)
    if last_updated is None:
        raise RuntimeError("Could not determine last updated timestamp")
    last_updated_iso = last_updated.isoformat(timespec="seconds")

    checked_dt = parse_iso(checked_at)
    if checked_dt is None:
        raise RuntimeError("Invalid checked_at timestamp")
    date_modified = checked_dt.isoformat(timespec="seconds")

    json_ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "@id": CANONICAL_URL + "#webpage",
                "url": CANONICAL_URL,
                "name": PAGE_TITLE,
                "description": META_DESCRIPTION,
                "inLanguage": "ja",
                "dateModified": date_modified,
                "isPartOf": {
                    "@type": "WebSite",
                    "name": "稲沢つながる広場",
                    "url": "https://yasu1738.github.io/inazawa-tsunagaru/",
                },
                "publisher": {
                    "@type": "Organization",
                    "name": "稲沢つながる広場",
                    "url": "https://yasu1738.github.io/inazawa-tsunagaru/",
                },
            },
            item_list_json_ld("#events", "稲沢市公式サイトのイベント情報", events),
            item_list_json_ld("#business-support", "稲沢市公式サイトの経営支援情報", business),
        ],
    }

    replacements = {
        "@@PAGE_TITLE@@": html_lib.escape(PAGE_TITLE, quote=True),
        "@@META_DESCRIPTION@@": html_lib.escape(META_DESCRIPTION, quote=True),
        "@@CANONICAL_URL@@": CANONICAL_URL,
        "@@OG_IMAGE_URL@@": OG_IMAGE_URL,
        "@@JSON_LD@@": json.dumps(json_ld, ensure_ascii=False, indent=2).replace("</", "<\\/"),
        "@@LAST_CHECKED_ISO@@": html_lib.escape(checked_at, quote=True),
        "@@LAST_CHECKED_DISPLAY@@": html_lib.escape(checked_display),
        "@@LAST_UPDATED_ISO@@": html_lib.escape(last_updated_iso, quote=True),
        "@@LAST_UPDATED_DISPLAY@@": html_lib.escape(display_updated(last_updated_iso)),
        "@@EVENTS_COUNT@@": str(len(events.get("items", []))),
        "@@EVENTS_NEW_COUNT@@": str(events.get("new_count", 0)),
        "@@EVENTS_CONTENT@@": render_items(events, checked_display),
        "@@EVENTS_SOURCE_URL@@": html_lib.escape(str(events.get("source_url", "")), quote=True),
        "@@BUSINESS_COUNT@@": str(len(business.get("items", []))),
        "@@BUSINESS_NEW_COUNT@@": str(business.get("new_count", 0)),
        "@@BUSINESS_CONTENT@@": render_items(business, checked_display),
        "@@BUSINESS_SOURCE_URL@@": html_lib.escape(str(business.get("source_url", "")), quote=True),
        "@@CONTACT_URL@@": html_lib.escape(CONTACT_URL, quote=True),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def validate_html(document: str, payloads: dict[str, dict] | None = None) -> None:
    if len(document) < 6000:
        raise RuntimeError("Generated HTML is unexpectedly short")
    if re.search(r"@@[A-Z0-9_]+@@", document):
        raise RuntimeError("Template placeholder remained in generated HTML")
    if "fetch(" in document or "読み込み中" in document:
        raise RuntimeError("Client-side data loading remains in generated HTML")
    if "本ページは非公式の情報整理ページです" not in document:
        raise RuntimeError("Non-official notice is missing")

    soup = BeautifulSoup(document, "html.parser")
    if len(soup.find_all("h1")) != 1:
        raise RuntimeError("Exactly one h1 is required")
    if len(soup.find_all("h2")) < 2:
        raise RuntimeError("At least two h2 headings are required")
    canonical = soup.find("link", rel="canonical")
    if not canonical or canonical.get("href") != CANONICAL_URL:
        raise RuntimeError("Canonical URL is missing or invalid")

    for link in soup.find_all("a", target="_blank"):
        rel = set(link.get("rel") or [])
        if not {"noopener", "noreferrer"}.issubset(rel):
            raise RuntimeError(f"External link is missing rel protection: {link.get('href')}")

    ld_tag = soup.find("script", attrs={"type": "application/ld+json"})
    if not ld_tag or not ld_tag.string:
        raise RuntimeError("JSON-LD is missing")
    json.loads(ld_tag.string)

    if payloads:
        visible_text = soup.get_text(" ", strip=True)
        for payload in payloads.values():
            for item in payload.get("items", []):
                title = clean(str(item.get("title", "")))
                if title and title not in visible_text:
                    raise RuntimeError(f"Item title is not present in static HTML: {title}")


def atomic_write_files(files: dict[Path, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="inazawa-build-", dir=ROOT) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        staged: dict[Path, Path] = {}
        for target, content in files.items():
            relative = target.relative_to(ROOT)
            temp_path = temp_dir / relative
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(content, encoding="utf-8")
            staged[target] = temp_path
        for target, temp_path in staged.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, target)


def load_payloads_from_config(config: dict, checked_at: str) -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for source in config["sources"]:
        path = ROOT / source["output"]
        existing = load_json(path)
        if existing is None:
            raise RuntimeError(f"Existing data file is missing or invalid: {path}")
        normalized = normalize_existing_for_render(existing, checked_at)
        normalized["source_id"] = source["id"]
        normalized["source_label"] = source["label"]
        normalized["source_url"] = source["url"]
        payloads[source["id"]] = normalized
    return payloads


def run_fetch(config: dict, current: datetime) -> tuple[dict[str, dict], list[str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.7"})
    checked_at = current.isoformat(timespec="seconds")
    payloads: dict[str, dict] = {}
    changed_sources: list[str] = []

    for source in config["sources"]:
        response = session.get(source["url"], timeout=35)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        extracted = extract_items(response.text, source["url"], current)
        if not extracted:
            raise RuntimeError(f"No usable information items extracted from {source['url']}")
        old = load_json(ROOT / source["output"])
        payload, content_changed = merge_payload(source, extracted, old, checked_at)
        payloads[source["id"]] = payload
        if content_changed:
            changed_sources.append(source["id"])
        print(
            f"{'CHANGED' if content_changed else 'UNCHANGED'} {source['id']}: "
            f"{len(payload['items'])} items, {payload['new_count']} new"
        )
    return payloads, changed_sources


def main() -> int:
    args = parse_args()
    try:
        if args.validate_only:
            document = LATEST_HTML.read_text(encoding="utf-8")
            validate_html(document)
            print("VALID latest/index.html")
            return 0

        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        current = now_jst(args.now)
        checked_at = current.isoformat(timespec="seconds")

        if args.render_only:
            payloads = load_payloads_from_config(config, checked_at)
            document = render_latest(payloads, checked_at)
            validate_html(document, payloads)
            atomic_write_files({LATEST_HTML: document})
            print(f"RENDERED {LATEST_HTML.relative_to(ROOT)}")
            return 0

        payloads, changed_sources = run_fetch(config, current)
        document = render_latest(payloads, checked_at)
        validate_html(document, payloads)

        outputs: dict[Path, str] = {LATEST_HTML: document}
        for source in config["sources"]:
            payload = payloads[source["id"]]
            outputs[ROOT / source["output"]] = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        atomic_write_files(outputs)
        print("content_changed=" + ",".join(changed_sources))
        print("STATIC_HTML_READY")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Previous public HTML and data files were left untouched.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
