#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "sources.json"
UA = "Inazawa-Tsunagaru-Hub/1.0 (+https://yasu1738.github.io/inazawa-tsunagaru/)"

SKIP_TEXT = {
    "ホーム", "現在位置", "サイトマップ", "ページの先頭へ", "メインメニュー",
    "本文へ", "文字サイズ", "検索", "閉じる", "戻る"
}


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_candidate(text: str, href: str, base_url: str) -> bool:
    if not text or text in SKIP_TEXT or len(text) < 3:
        return False
    if text.startswith(("〒", "電話", "ファクス")):
        return False
    absolute = urljoin(base_url, href)
    u = urlparse(absolute)
    b = urlparse(base_url)
    if u.netloc != b.netloc:
        return False
    if absolute == base_url or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    return True


def extract_items(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("header, footer, nav, script, style, noscript"):
        tag.decompose()

    candidates = []
    seen = set()
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True))
        href = a.get("href", "")
        if not is_candidate(text, href, base_url):
            continue
        url = urljoin(base_url, href)
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"title": text, "url": url})
    return candidates[:80]


def stable_payload(source: dict, items: list[dict]) -> dict:
    digest = hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "source_id": source["id"],
        "source_label": source["label"],
        "source_url": source["url"],
        "content_hash": digest,
        "items": items,
    }


def load_existing(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def main() -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.7"})
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = []

    for source in cfg["sources"]:
        response = session.get(source["url"], timeout=30)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        items = extract_items(response.text, source["url"])
        if not items:
            raise RuntimeError(f"No items extracted from {source['url']}")

        payload = stable_payload(source, items)
        out = ROOT / source["output"]
        old = load_existing(out)
        old_hash = (old or {}).get("content_hash")

        if old_hash != payload["content_hash"]:
            payload["last_changed_at"] = checked_at
            payload["last_checked_at"] = checked_at
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed.append(source["id"])
            print(f"CHANGED {source['id']}: {len(items)} items")
        else:
            # 差分がない日はファイル自体を書き換えない。
            print(f"UNCHANGED {source['id']}: {len(items)} items")

    print("changed=" + ",".join(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
