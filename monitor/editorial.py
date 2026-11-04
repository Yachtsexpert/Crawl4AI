"""Conservative, inspectable headline screening for OK-Boats. Not a fact checker."""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

RULES = {
    "PROPULSION": (
        r"\b(?:engines?|powertrains?|propulsion|outboards?|sterndrives?|"
        r"ips\s*\d*|dpi|pod\s*drives?|hybrid\s+(?:drive|propulsion|engine|power)|"
        r"electric\s+(?:drive|propulsion|powertrain)|powered\s+by|"
        r"mercury\s+(?:v\d+|\d+)|yamaha\s+\d+|volvo\s+penta)\b",
    ),
    "PREMIERE": (
        r"\b(?:world|global|european|europe|american|u\.?s\.?|usa|regional)\s+(?:premieres?|debuts?)\b",
        r"\b(?:premieres?|debuts?|unveils?|unveiled|introduces?|introduced|launches?|launched|all[- ]new)\b",
        r"\bnew\s+(?:[\w-]+\s+){0,3}(?:models?|ranges?|yachts?|boats?|cabins?|lounges?)\b",
    ),
    "TECHNICAL_UPDATE": (
        r"\b(?:upgraded?|redesigned|reengineered|refitted|revised|technical\s+update|"
        r"technological\s+update|new\s+(?:hull|helm|radar|navigation|stabilizers?|"
        r"systems?|battery|layout|electronics|autopilot))\b",
    ),
    "CORPORATE": (
        r"\b(?:dealers?|distributors?|partners?|partnership|appoints?|appointed|"
        r"ceos?|management|acquires?|acquisition|awards?|wins?|won|sponsors?|"
        r"sponsorship|opens?\s+(?:new\s+)?(?:office|dealership)|expansion)\b",
    ),
}
PRIORITY = {"PREMIERE": "P1", "PROPULSION": "P1", "TECHNICAL_UPDATE": "P2",
            "CORPORATE": "P3", "NEEDS_REVIEW": "REVIEW"}


def article_path(url: str, source: dict) -> bool:
    """Restrict editorial candidates to a source's known article subpaths."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    parsed = urlsplit(url)
    base = urlsplit(source["url"])
    if (parsed.hostname or "").removeprefix("www.").lower() != (base.hostname or "").removeprefix("www.").lower():
        return False
    path = parsed.path.rstrip("/").lower()
    # A configured prefix can be broader than the section URL (e.g. BENETEAU).
    if path == base.path.rstrip("/").lower():
        return False
    prefixes = source.get("article_path_prefixes") or [(base.path.rstrip("/") + "/")]
    return any(path.startswith(prefix.rstrip("/").lower() + "/") for prefix in prefixes)


def publish_date_from_url(url: str) -> str | None:
    """URL-derived date is a hint only: not a verified publication date."""
    match = re.search(r"/(20\d\d)-(\d\d)-(\d\d)(?:/|-)", url)
    if not match:
        return None
    value = "-".join(match.groups())
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return None


def classify(title: str, url: str, source: dict, basis: str = "PAGE_TITLE") -> dict:
    """Headline-only classification, never a confirmed premiere or equipment fact."""
    if not article_path(url, source):
        return {"category": "SECTION_INDEX", "priority": "EXCLUDE", "signals": [], "matched_categories": [],
                "basis": basis, "verification_status": "NOT_A_PUBLICATION",
                "premiere_scope": "NOT_DETERMINED", "date_hint_from_url": None}
    lowered = html.unescape(title or "").lower()
    found = {category: [m.group(0) for rule in patterns for m in list(re.finditer(rule, lowered, re.I))[:2]]
             for category, patterns in RULES.items()}
    found = {key: list(dict.fromkeys(values))[:4] for key, values in found.items() if values}
    if "CORPORATE" in found and "PROPULSION" not in found and "PREMIERE" not in found:
        category = "CORPORATE"
    elif "PROPULSION" in found:
        category = "PROPULSION"
    elif "TECHNICAL_UPDATE" in found and not re.search(
            r"\b(?:premieres?|debuts?|all[- ]new|new\s+(?:models?|yachts?|boats?))\b", lowered):
        category = "TECHNICAL_UPDATE"
    elif "PREMIERE" in found:
        category = "PREMIERE"
    elif "TECHNICAL_UPDATE" in found:
        category = "TECHNICAL_UPDATE"
    elif "CORPORATE" in found:
        category = "CORPORATE"
    else:
        category = "NEEDS_REVIEW"

    scope = "NOT_DETERMINED"
    if re.search(r"\b(?:world|global)\s+(?:premieres?|debuts?)\b", lowered):
        scope = "WORLD_CLAIM_IN_TITLE"
    elif re.search(r"\b(?:u\.?s\.?|usa|american)\s+(?:premieres?|debuts?)\b", lowered):
        scope = "REGIONAL_US_CLAIM_IN_TITLE"
    elif re.search(r"\b(?:european|europe)\s+(?:premieres?|debuts?)\b", lowered):
        scope = "REGIONAL_EU_CLAIM_IN_TITLE"
    elif category == "PREMIERE":
        scope = "UNSPECIFIED"
    return {"category": category, "priority": PRIORITY[category],
            "matched_categories": sorted(found),
            "signals": found.get(category, [])[:4], "basis": basis,
            "verification_status": "NEEDS_EDITOR_REVIEW",
            "premiere_scope": scope,
            "date_hint_from_url": publish_date_from_url(url),
            "technical_significance": "NOT_VERIFIED"}


def extract_article_links(markdown: str, page_url: str, source: dict, limit: int = 40) -> list[dict]:
    """Discover article links cheaply from already-collected Markdown; do not visit them."""
    if not isinstance(markdown, str):
        return []
    result, seen = [], set()
    pattern = re.compile(r"(?<!!)\[([^\]\n]{3,180})\]\(([^\s)]+)(?:\s+['\"][^)]*['\"])?\)")
    for label, target in pattern.findall(markdown):
        target = html.unescape(target).strip("<>")
        if target.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        candidate = urljoin(page_url, target)
        parsed = urlsplit(candidate)
        if not article_path(candidate, source):
            continue
        clean_url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
        if clean_url in seen:
            continue
        seen.add(clean_url)
        cleaned_label = re.sub(r"[\*_]+", "", html.unescape(label)).strip()[:180]
        if cleaned_label.lower() in {"read more", "more", "learn more", "continue reading"}:
            continue
        result.append({"page_url": clean_url, "title": cleaned_label, "source_id": source["id"],
                       "source_url": source["url"], "discovery_status": "LINK_ONLY_UNFETCHED"})
        if len(result) >= limit:
            break
    return result


def editorial_item(record: dict, source: dict, event: str, basis: str) -> dict | None:
    info = classify(record.get("title", ""), record["page_url"], source, basis=basis)
    if info["category"] == "SECTION_INDEX":
        return None
    return {"source_id": source["id"], "page_url": record["page_url"],
            "title": record.get("title", ""), "detected_at_utc": record.get("observed_at_utc"),
            "event": event, "content_sha256": record.get("content_sha256"),
            "actor_run_id": record.get("actor_run_id"), **info}


def render_report(items: list[dict], runs: list[dict], mode: str = "LIVE") -> str:
    """Daily operational queue; scores are heuristic and NEVER publication approvals."""
    counts = Counter(it["category"] for it in items)
    lines = ["# OK-Boats — редакционная очередь (автоматическая предварительная классификация)", "",
             f"Режим формирования: **{mode}**. Все категории определены по заголовкам или анкорам ссылок, без проверки технических фактов.",
             "**NEW_PAGE** — впервые увидено монитором, а не обязательно опубликовано сегодня. "
             "**CONTENT_CHANGED** может означать изменение меню или футера.", "",
             "## Охват и ошибки", "",
             f"Источники: {len(runs)}; успешных: {sum(r.get('status') == 'SUCCEEDED' for r in runs)}; "
             f"ошибок: {sum(r.get('status') == 'FAILED' for r in runs)}.", "",
             "## Кандидаты для редактора", ""]
    if not items:
        lines.append("Новых кандидатных публикаций в этом запуске не обнаружено (это не доказывает отсутствие новостей).")
    else:
        sorted_items = sorted(items, key=lambda r: ({"P1": 0, "P2": 1, "REVIEW": 2, "P3": 3}.get(r["priority"], 9), r["source_id"], r["page_url"]))
        for it in sorted_items:
            title = it["title"].replace("\n", " ").replace("|", "-")
            title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            lines.append(f"- **{it['priority']} / {it['category']}** — [{title}]({it['page_url']}) "
                         f"({it['source_id']}; {it['event']}; {it['basis']}; "
                         f"scope={it['premiere_scope']}; **требует проверки**)" )
    if mode == "OFFLINE_HISTORICAL_BACKFILL":
        lines[7] = f"Офлайн-пересчёт сохранённых данных: {len(runs)} источников в конфигурации; новые запросы к сайтам и Apify не выполнялись."
    lines.extend(["", "## Сводка категорий", ""])
    for key in ("PREMIERE", "PROPULSION", "TECHNICAL_UPDATE", "CORPORATE", "NEEDS_REVIEW"):
        lines.append(f"- {key}: {counts[key]}")
    lines.extend(["", "Ни цена, ни моторизация, ни премьера, ни доступность модели не подтверждены автоматической классификацией.",
                  "Редактор обязан открыть первоисточник и проверить дату, модель, рынок дебюта и технические подробности перед публикацией.", ""])
    return "\n".join(lines)


def backfill_existing(data_dir: Path, sources: list[dict]) -> int:
    """Reclassify existing change history offline with no Apify calls or charges."""
    source_by_id = {s["id"]: s for s in sources}
    changes_path = data_dir / "changes.jsonl"
    if not changes_path.exists():
        raise FileNotFoundError("No change history to classify")
    seen, items = set(), []
    for line in changes_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        source = source_by_id.get(row.get("source_id"))
        if not source:
            continue
        item = editorial_item(row, source, event=row.get("event", "HISTORICAL"), basis="PAGE_TITLE")
        if item is None:
            continue
        key = (item["source_id"], item["page_url"], item["content_sha256"], item["event"])
        if key not in seen:
            seen.add(key)
            items.append(item)
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_text(data_dir / "editorial_candidates.jsonl", "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in items))
    runs = [{"status": "HISTORICAL", "source_id": s["id"]} for s in sources]
    _atomic_text(data_dir / "editorial_latest.md", render_report(items, runs, mode="OFFLINE_HISTORICAL_BACKFILL"))
    return len(items)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
