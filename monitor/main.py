"""Auditable public-source monitor, not a yacht listing price scraper."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .editorial import (_atomic_text, backfill_existing, editorial_item,
                        extract_article_links, render_report)

ACTOR = "apify/website-content-crawler"


def canonical(url):
    p = urlsplit(url.strip())
    if p.scheme not in ("https", "http") or not p.netloc:
        return ""
    q = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")))
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", q, ""))


def validate(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    sources = cfg.get("sources")
    if cfg.get("schema_version") != 1 or not isinstance(sources, list) or not 1 <= len(sources) <= 5:
        raise ValueError("schema_version=1 and 1..5 sources required")
    ids = set()
    for source in sources:
        key, url = source["id"], source["url"]
        if not re.fullmatch("[a-z][a-z0-9-]{1,50}", key) or key in ids:
            raise ValueError("Invalid or duplicate source ID")
        if not url.startswith("https://") or not canonical(url):
            raise ValueError("HTTPS source URL required")
        if type(source["max_pages"]) is not int or not 1 <= source["max_pages"] <= 3:
            raise ValueError("max_pages must be 1..3")
        prefixes = source.get("article_path_prefixes", [])
        if not isinstance(prefixes, list) or len(prefixes) > 8 or any(
                not isinstance(p, str) or not p.startswith("/") or len(p) > 100 for p in prefixes):
            raise ValueError("Invalid article path prefixes")
        ids.add(key)
    cap = Decimal(str(cfg["max_charge_usd_per_source"]))
    if not Decimal("0") < cap <= Decimal("0.25"):
        raise ValueError("Invalid cost limit")
    return cfg


def append_jsonl(file, records):
    if not records:
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as out:
        for row in records:
            out.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def actor_input(source):
    pages = source["max_pages"]
    return {"startUrls": [{"url": source["url"]}], "crawlerType": "playwright:adaptive",
            "maxCrawlPages": pages, "maxResults": pages, "maxCrawlDepth": 1 if pages > 1 else 0,
            "respectRobotsTxtFile": True, "useSitemaps": False, "useLlmsTxt": False,
            "blockMedia": True, "saveMarkdown": True, "summarize": False,
            "saveScreenshots": False}


def collect(client, cfg, folder, observed_at=None, replay_runs=None):
    folder = Path(folder)
    observed_at = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    state_file = folder / "last_seen.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    observations, changes, runs = [], [], []
    editorial_items, discovered_links = [], []
    discovered_seen = set()
    discovered_file = folder / "discovered_links.jsonl"
    if discovered_file.exists():
        for line in discovered_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                previous = json.loads(line)
                discovered_seen.add((previous["source_id"], previous["page_url"]))
    failures = 0
    for source in cfg["sources"]:
        if not source.get("enabled", True):
            runs.append({"source_id": source["id"], "status": "DISABLED", "observed_at_utc": observed_at})
            continue
        source_id = source["id"]
        run_id = ""
        actor_status = "UNKNOWN"
        stage = "actor_call"
        mode = "REPLAY" if replay_runs is not None else "LIVE"
        try:
            if replay_runs is not None:
                # Read the already completed Actor run. Never start a second paid run.
                stage = "replay_lookup"
                run_id = replay_runs[source_id]
                result = client.run(run_id).get()
            else:
                result = client.actor(ACTOR).call(
                    run_input=actor_input(source), max_items=source["max_pages"],
                    max_total_charge_usd=Decimal(str(cfg["max_charge_usd_per_source"])),
                    run_timeout=timedelta(minutes=3))
            # apify-client >=3 returns a typed Run model, not a dictionary.
            # Status is an enum; preserve its value instead of relying on str(enum).
            if result is None:
                raise RuntimeError("Actor did not return a run")
            stage = "run_status"
            run_id = str(result.id)
            raw_status = result.status
            actor_status = str(getattr(raw_status, "value", raw_status))
            if actor_status != "SUCCEEDED":
                raise RuntimeError("Actor run unsuccessful")
            data_id = result.default_dataset_id
            stage = "dataset_read"
            staged, staged_changes, staged_state = [], [], dict(state)
            staged_links, staged_link_seen = [], set()
            seen = set()
            for row in client.dataset(data_id).iterate_items():
                if not isinstance(row, dict):
                    continue
                url = canonical(str(row.get("url", "")))
                page_host = (urlsplit(url).hostname or "").removeprefix("www.")
                source_host = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
                content = row.get("markdown") or row.get("text") or ""
                if not url or page_host != source_host or not isinstance(content, str) or not content.strip() or url in seen:
                    continue
                seen.add(url)
                # Discover links from already fetched Markdown; no extra Actor requests.
                for link in extract_article_links(content, url, source, limit=35):
                    link_key = (source_id, link["page_url"])
                    if link_key not in discovered_seen and link_key not in staged_link_seen:
                        staged_link_seen.add(link_key)
                        link["observed_at_utc"] = observed_at
                        link["actor_run_id"] = run_id
                        staged_links.append(link)
                normalized = re.sub(r"\s+", " ", content).strip()
                digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                title = str(metadata.get("title") or row.get("title") or "")[:240]
                key = source_id + "|" + url
                prior = staged_state.get(key)
                record = {"source_id": source_id, "source_url": source["url"],
                          "page_url": url, "title": title, "content_sha256": digest,
                          "observed_at_utc": observed_at, "actor_run_id": run_id,
                          "collection_mode": mode,
                          "price_status": "NOT_COLLECTED", "equipment_status": "NOT_COLLECTED"}
                staged.append(record)
                if prior is None or prior["content_sha256"] != digest:
                    staged_changes.append({**record, "event": "NEW_PAGE" if prior is None else "CONTENT_CHANGED",
                                           "previous_sha256": None if prior is None else prior["content_sha256"]})
                staged_state[key] = {"content_sha256": digest,
                                     "first_seen_at_utc": prior.get("first_seen_at_utc", observed_at) if prior else observed_at,
                                     "last_seen_at_utc": observed_at, "title": title}
            if not staged:
                raise RuntimeError("No usable pages returned")
            state = staged_state
            observations.extend(staged)
            changes.extend(staged_changes)
            # The index section is excluded; new page != new publication today.
            for event in staged_changes:
                item = editorial_item(event, source, event["event"], "PAGE_TITLE")
                if item is not None:
                    editorial_items.append(item)
            fetched_urls = {rec["page_url"] for rec in staged}
            for link in staged_links[:35]:
                link_key = (source_id, link["page_url"])
                if link["page_url"] in fetched_urls or source_id + "|" + link["page_url"] in state:
                    continue
                discovered_seen.add(link_key)
                discovered_links.append(link)
                item = editorial_item(link, source, "LINK_DISCOVERED", "LINK_ANCHOR")
                if item is not None:
                    editorial_items.append(item)
            runs.append({"source_id": source_id, "status": "SUCCEEDED", "pages": len(staged),
                         "observed_at_utc": observed_at, "actor_run_id": run_id,
                         "actor_status": actor_status, "collection_mode": mode})
        except Exception as exc:
            failures += 1
            # Never expose arbitrary API exception messages: they can contain credentials.
            runs.append({"source_id": source_id, "status": "FAILED",
                         "error_stage": stage, "error_type": type(exc).__name__,
                         "actor_status": actor_status, "collection_mode": mode,
                         "observed_at_utc": observed_at, "actor_run_id": run_id})
        print(source_id + ": " + runs[-1]["status"], flush=True)
    append_jsonl(folder / "observations.jsonl", observations)
    append_jsonl(folder / "changes.jsonl", changes)
    append_jsonl(folder / "runs.jsonl", runs)
    append_jsonl(folder / "discovered_links.jsonl", discovered_links)
    append_jsonl(folder / "editorial_candidates.jsonl", editorial_items)
    _atomic_text(folder / "editorial_latest.md", render_report(
        editorial_items, runs, mode="REPLAY" if replay_runs is not None else "LIVE"))
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "last_seen.tmp"
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(state_file)
    print(json.dumps({"observations": len(observations), "changes": len(changes), "failures": failures}))
    return 1 if failures else 0



def load_recovery_runs(path, cfg):
    """Read known completed Actor run IDs; never allow an unverified source mapping."""
    mappings = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {source["id"] for source in cfg["sources"] if source.get("enabled", True)}
    if not isinstance(mappings, dict) or set(mappings) != expected:
        raise ValueError("Recovery run IDs must exactly match enabled sources")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{17}", value)
               for value in mappings.values()):
        raise ValueError("Invalid Apify run ID in recovery map")
    return mappings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/sources.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--rebuild-editorial", action="store_true",
                        help="Classify historical change records without calling Apify")
    parser.add_argument("--recover-first-run", action="store_true",
                        help="Reuse completed Apify runs without starting new Actors")
    args = parser.parse_args()
    try:
        cfg = validate(args.config)
        if args.check_config:
            print("VALID:", len(cfg["sources"]), "sources")
            return 0
        if args.rebuild_editorial:
            count = backfill_existing(Path(args.data_dir), cfg["sources"])
            print("Offline editorial classifications:", count)
            return 0
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("Missing GitHub Actions secret APIFY_TOKEN", file=sys.stderr)
            return 2
        from apify_client import ApifyClient
        replay_runs = load_recovery_runs("config/first_run_ids.json", cfg) if args.recover_first_run else None
        return collect(ApifyClient(token), cfg, args.data_dir, replay_runs=replay_runs)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print("Configuration error:", type(exc).__name__, str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
