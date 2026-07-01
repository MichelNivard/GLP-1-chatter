#!/usr/bin/env python3
"""Slow, polite Reddit/PullPush crawler for GLP-1 user-report candidates."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from glp1_common import (
    DEFAULT_DB,
    build_full_text,
    connect_db,
    ensure_schema,
    find_matches,
    iso_from_utc,
    load_search_terms,
    load_sources_config,
    upsert_raw_candidate,
)

PULLPUSH_BASE = "https://api.pullpush.io/reddit/search"
REDDIT_BASE = "https://www.reddit.com"


def parse_args() -> argparse.Namespace:
    config = load_sources_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--source",
        choices=("pullpush", "reddit_json", "auto"),
        default=config.get("default_source", "pullpush"),
        help="Crawler backend. reddit_json searches submissions only; comments use PullPush.",
    )
    parser.add_argument("--since-days", type=int, default=7, help="Recent crawl window in days")
    parser.add_argument(
        "--seed-historical",
        action="store_true",
        help="Search without a recent-window cutoff for one-time historical seeding.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum candidates to insert/update")
    parser.add_argument("--page-size", type=int, default=100, help="Backend page size")
    parser.add_argument("--pages", type=int, default=1, help="Pages per subreddit/term/source type")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and count without writing")
    parser.add_argument(
        "--subreddit",
        action="append",
        dest="subreddits",
        help="Restrict to one or more subreddits. Defaults to config/sources.json.",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Search submissions only. By default comments are searched when the source supports it.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=float(config.get("request_delay_seconds", 1.5)),
        help="Delay between requests.",
    )
    return parser.parse_args()


def fetch_json(url: str, params: dict[str, Any], user_agent: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset, errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"warning: failed {url}: {exc}", file=sys.stderr)
        return None


def pullpush_search(
    *,
    kind: str,
    subreddit: str,
    term: str,
    after_epoch: int | None,
    page_size: int,
    pages: int,
    user_agent: str,
    delay_seconds: float,
) -> Iterable[dict[str, Any]]:
    endpoint = f"{PULLPUSH_BASE}/{kind}/"
    before = None
    for _page in range(max(1, pages)):
        params = {
            "q": term,
            "subreddit": subreddit,
            "after": after_epoch,
            "before": before,
            "size": min(max(page_size, 1), 100),
            "sort": "desc",
            "sort_type": "created_utc",
        }
        payload = fetch_json(endpoint, params, user_agent)
        time.sleep(delay_seconds)
        if not payload:
            return
        rows = payload.get("data") or []
        if not rows:
            return
        for row in rows:
            yield row
        created_values = [row.get("created_utc") for row in rows if row.get("created_utc")]
        if not created_values:
            return
        before = min(created_values)


def reddit_submission_search(
    *,
    subreddit: str,
    term: str,
    page_size: int,
    pages: int,
    user_agent: str,
    delay_seconds: float,
) -> Iterable[dict[str, Any]]:
    endpoint = f"{REDDIT_BASE}/r/{urllib.parse.quote(subreddit)}/search.json"
    after = None
    for _page in range(max(1, pages)):
        params = {
            "q": term,
            "restrict_sr": "on",
            "sort": "new",
            "t": "all",
            "limit": min(max(page_size, 1), 100),
            "after": after,
        }
        payload = fetch_json(endpoint, params, user_agent)
        time.sleep(delay_seconds)
        if not payload:
            return
        data = payload.get("data") or {}
        children = data.get("children") or []
        if not children:
            return
        for child in children:
            row = child.get("data") or {}
            yield row
        after = data.get("after")
        if not after:
            return


def candidate_from_submission(row: dict[str, Any], search_terms: dict[str, list[str]]) -> dict[str, Any] | None:
    reddit_id = str(row.get("id") or "").strip()
    if not reddit_id:
        return None
    title = row.get("title") or ""
    body = row.get("selftext") or ""
    full_text = build_full_text(title, body)
    families, terms = find_matches(full_text, search_terms)
    if not terms:
        return None
    created_utc = row.get("created_utc") or row.get("created")
    permalink = row.get("permalink")
    url = f"https://www.reddit.com{permalink}" if permalink else row.get("full_link") or row.get("url")
    return {
        "reddit_id": reddit_id,
        "source_type": "submission",
        "subreddit": row.get("subreddit") or "",
        "thread_id": reddit_id,
        "created_utc": int(created_utc) if created_utc else None,
        "created_iso": iso_from_utc(created_utc),
        "title": title,
        "body": body,
        "full_text": full_text,
        "url": url,
        "matched_drug_families": families,
        "matched_terms": terms,
    }


def candidate_from_comment(row: dict[str, Any], search_terms: dict[str, list[str]]) -> dict[str, Any] | None:
    reddit_id = str(row.get("id") or "").strip()
    if not reddit_id:
        return None
    body = row.get("body") or ""
    full_text = build_full_text(None, body)
    families, terms = find_matches(full_text, search_terms)
    if not terms:
        return None
    created_utc = row.get("created_utc") or row.get("created")
    link_id = str(row.get("link_id") or "").replace("t3_", "")
    permalink = row.get("permalink")
    if permalink:
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
    elif link_id:
        url = f"https://www.reddit.com/comments/{link_id}/_/{reddit_id}/"
    else:
        url = None
    return {
        "reddit_id": reddit_id,
        "source_type": "comment",
        "subreddit": row.get("subreddit") or "",
        "thread_id": link_id or None,
        "created_utc": int(created_utc) if created_utc else None,
        "created_iso": iso_from_utc(created_utc),
        "title": None,
        "body": body,
        "full_text": full_text,
        "url": url,
        "matched_drug_families": families,
        "matched_terms": terms,
    }


def source_iter(
    *,
    source: str,
    source_type: str,
    subreddit: str,
    term: str,
    after_epoch: int | None,
    args: argparse.Namespace,
    user_agent: str,
) -> Iterable[dict[str, Any]]:
    if source_type == "comment":
        yield from pullpush_search(
            kind="comment",
            subreddit=subreddit,
            term=term,
            after_epoch=after_epoch,
            page_size=args.page_size,
            pages=args.pages,
            user_agent=user_agent,
            delay_seconds=args.delay_seconds,
        )
        return

    if source == "reddit_json":
        yield from reddit_submission_search(
            subreddit=subreddit,
            term=term,
            page_size=args.page_size,
            pages=args.pages,
            user_agent=user_agent,
            delay_seconds=args.delay_seconds,
        )
        return

    if source == "auto":
        reddit_rows = list(
            reddit_submission_search(
                subreddit=subreddit,
                term=term,
                page_size=args.page_size,
                pages=args.pages,
                user_agent=user_agent,
                delay_seconds=args.delay_seconds,
            )
        )
        if reddit_rows:
            yield from reddit_rows
            return

    yield from pullpush_search(
        kind="submission",
        subreddit=subreddit,
        term=term,
        after_epoch=after_epoch,
        page_size=args.page_size,
        pages=args.pages,
        user_agent=user_agent,
        delay_seconds=args.delay_seconds,
    )


def main() -> int:
    args = parse_args()
    config = load_sources_config()
    search_terms = load_search_terms()
    subreddits = args.subreddits or config.get("subreddits") or []
    terms = sorted({term for family_terms in search_terms.values() for term in family_terms})
    after_epoch = None
    if not args.seed_historical:
        since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
        after_epoch = int(since.timestamp())

    conn = None
    if not args.dry_run:
        conn = connect_db(args.db)
        ensure_schema(conn)

    seen_keys: set[tuple[str, str]] = set()
    inserted_or_changed = 0
    content_changed = 0
    considered = 0
    user_agent = config.get("reddit_user_agent") or "glp1-reddit-user-report-miner/0.1"
    source_types = ["submission"] if args.no_comments else ["submission", "comment"]

    try:
        for subreddit in subreddits:
            for term in terms:
                for source_type in source_types:
                    rows = source_iter(
                        source=args.source,
                        source_type=source_type,
                        subreddit=subreddit,
                        term=term,
                        after_epoch=after_epoch,
                        args=args,
                        user_agent=user_agent,
                    )
                    for row in rows:
                        if source_type == "submission":
                            candidate = candidate_from_submission(row, search_terms)
                        else:
                            candidate = candidate_from_comment(row, search_terms)
                        if candidate is None:
                            continue
                        if after_epoch and candidate.get("created_utc") and candidate["created_utc"] < after_epoch:
                            continue
                        key = (candidate["source_type"], candidate["reddit_id"])
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        considered += 1
                        if args.dry_run:
                            print(
                                f"dry-run {candidate['source_type']} r/{candidate['subreddit']} "
                                f"{candidate['reddit_id']} {candidate['matched_terms']}"
                            )
                        else:
                            changed, hash_changed = upsert_raw_candidate(conn, candidate)
                            inserted_or_changed += int(changed)
                            content_changed += int(hash_changed)
                            conn.commit()
                        if args.limit and considered >= args.limit:
                            raise StopIteration
    except StopIteration:
        pass
    finally:
        if conn is not None:
            conn.close()

    print(
        json.dumps(
            {
                "considered": considered,
                "inserted_or_changed": inserted_or_changed,
                "content_changed": content_changed,
                "dry_run": args.dry_run,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
