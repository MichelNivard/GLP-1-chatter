#!/usr/bin/env python3
"""Slow, polite Reddit/PullPush crawler for GLP-1 user-report candidates."""

from __future__ import annotations

import argparse
import email.utils
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
    utc_now_iso,
)

PULLPUSH_BASE = "https://api.pullpush.io/reddit/search"
REDDIT_BASE = "https://www.reddit.com"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    payload: dict[str, Any] | None
    status_code: int | None
    error: str | None = None
    retry_after_seconds: float | None = None

    @property
    def rate_limited(self) -> bool:
        return self.status_code == 429


@dataclass
class SearchPage:
    backend: str
    rows: list[dict[str, Any]]
    status_code: int | None
    next_pullpush_before: int | None = None
    next_reddit_after: str | None = None
    exhausted: bool = False
    rate_limited: bool = False
    error: str | None = None


@dataclass
class CrawlStats:
    considered: int = 0
    inserted_or_changed: int = 0
    content_changed: int = 0
    fetched_rows: int = 0
    requests: int = 0
    retries: int = 0
    rate_limited_responses: int = 0
    http_errors: int = 0
    network_errors: int = 0
    decode_errors: int = 0
    backed_off_seconds: float = 0.0
    polite_sleep_seconds: float = 0.0
    combinations_started: int = 0
    combinations_completed: int = 0
    combinations_skipped_exhausted: int = 0
    combinations_rate_limited: int = 0
    combinations_failed: int = 0
    combinations_exhausted: int = 0
    stopped_reason: str | None = None


class CrawlStop(Exception):
    """Gracefully stop the crawl after committing any work already done."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        "--subreddits",
        dest="subreddits_csv",
        help="Comma-separated subreddit allowlist. May be combined with repeated --subreddit.",
    )
    parser.add_argument(
        "--term",
        action="append",
        dest="terms",
        help="Restrict to one or more search terms. Defaults to config/search_terms.json.",
    )
    parser.add_argument(
        "--terms",
        dest="terms_csv",
        help="Comma-separated search-term allowlist. May be combined with repeated --term.",
    )
    parser.add_argument(
        "--source-type",
        action="append",
        choices=("submission", "comment"),
        dest="source_types",
        help="Restrict to a source type. May be repeated.",
    )
    parser.add_argument(
        "--source-types",
        dest="source_types_csv",
        help="Comma-separated source types: submission,comment.",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="Search submissions only. Ignored when --source-type/--source-types is set.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=float(config.get("request_delay_seconds", 6.0)),
        help="Base delay between requests.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(config.get("request_max_retries", 4)),
        help="Retries per request for 429/5xx/network failures.",
    )
    parser.add_argument(
        "--backoff-base-seconds",
        type=float,
        default=float(config.get("backoff_base_seconds", 20.0)),
        help="Initial retry backoff for rate limits and transient failures.",
    )
    parser.add_argument(
        "--max-backoff-seconds",
        type=float,
        default=float(config.get("max_backoff_seconds", 300.0)),
        help="Maximum wait between retry attempts.",
    )
    parser.add_argument(
        "--rate-limit-cooldown-seconds",
        type=float,
        default=float(config.get("rate_limit_cooldown_seconds", 0.0)),
        help="Cooldown after retries are exhausted for a 429 response.",
    )
    parser.add_argument(
        "--stop-on-rate-limit",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("stop_on_rate_limit", True)),
        help="Stop the run after an unrecovered 429 so a later run can resume.",
    )
    parser.add_argument(
        "--resume-checkpoints",
        action=argparse.BooleanOptionalAction,
        default=bool(config.get("resume_checkpoints", False)),
        help="Resume historical PullPush/Reddit cursors from the crawl_state table.",
    )
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=0,
        help="Maximum subreddit/term/source-type combinations to attempt in this run.",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Maximum HTTP requests before stopping gracefully.",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=0.0,
        help="Maximum runtime before stopping gracefully.",
    )
    return parser.parse_args()


def split_list(*values: str | Iterable[str] | None) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            pieces = value.split(",")
        else:
            pieces = []
            for item in value:
                pieces.extend(str(item).split(","))
        for piece in pieces:
            piece = piece.strip()
            if piece:
                result.append(piece)
    return result


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def jittered(seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return seconds + random.uniform(0.0, min(3.0, seconds * 0.1))


def polite_sleep(seconds: float, stats: CrawlStats) -> None:
    if seconds <= 0:
        return
    stats.polite_sleep_seconds += seconds
    time.sleep(seconds)


def backoff_sleep(seconds: float, stats: CrawlStats) -> None:
    if seconds <= 0:
        return
    stats.backed_off_seconds += seconds
    time.sleep(seconds)


def check_stop_limits(args: argparse.Namespace, stats: CrawlStats, started: float) -> None:
    if args.max_requests and stats.requests >= args.max_requests:
        raise CrawlStop(f"max_requests reached ({args.max_requests})")
    if args.max_minutes and (time.monotonic() - started) >= args.max_minutes * 60.0:
        raise CrawlStop(f"max_minutes reached ({args.max_minutes:g})")


def fetch_json(
    url: str,
    params: dict[str, Any],
    user_agent: str,
    *,
    stats: CrawlStats,
    max_retries: int,
    backoff_base_seconds: float,
    max_backoff_seconds: float,
) -> FetchResult:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full_url = f"{url}?{query}"

    for attempt in range(max_retries + 1):
        request = urllib.request.Request(
            full_url,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
        )
        stats.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    return FetchResult(
                        payload=json.loads(response.read().decode(charset, errors="replace")),
                        status_code=getattr(response, "status", 200),
                    )
                except json.JSONDecodeError as exc:
                    stats.decode_errors += 1
                    return FetchResult(None, getattr(response, "status", 200), f"JSON decode error: {exc}")
        except urllib.error.HTTPError as exc:
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
            error = f"HTTP Error {exc.code}: {exc.reason}"
            if exc.code == 429:
                stats.rate_limited_responses += 1
            else:
                stats.http_errors += 1
            if exc.code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                wait = retry_after
                if wait is None:
                    wait = backoff_base_seconds * (2**attempt)
                wait = jittered(min(wait, max_backoff_seconds))
                stats.retries += 1
                print(
                    f"warning: {error}; retrying in {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                backoff_sleep(wait, stats)
                continue
            print(f"warning: failed {url}: {error}", file=sys.stderr)
            return FetchResult(None, exc.code, error, retry_after)
        except (urllib.error.URLError, TimeoutError) as exc:
            stats.network_errors += 1
            error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries:
                wait = jittered(min(backoff_base_seconds * (2**attempt), max_backoff_seconds))
                stats.retries += 1
                print(
                    f"warning: {error}; retrying in {wait:.1f}s "
                    f"(attempt {attempt + 1}/{max_retries})",
                    file=sys.stderr,
                )
                backoff_sleep(wait, stats)
                continue
            print(f"warning: failed {url}: {error}", file=sys.stderr)
            return FetchResult(None, None, error)

    return FetchResult(None, None, "request failed without a result")


def pullpush_pages(
    *,
    kind: str,
    subreddit: str,
    term: str,
    after_epoch: int | None,
    start_before: int | None,
    page_size: int,
    pages: int,
    user_agent: str,
    args: argparse.Namespace,
    stats: CrawlStats,
    started: float,
) -> Iterable[SearchPage]:
    endpoint = f"{PULLPUSH_BASE}/{kind}/"
    before = start_before
    for _page in range(max(1, pages)):
        check_stop_limits(args, stats, started)
        params = {
            "q": term,
            "subreddit": subreddit,
            "after": after_epoch,
            "before": before,
            "size": min(max(page_size, 1), 100),
            "sort": "desc",
            "sort_type": "created_utc",
        }
        result = fetch_json(
            endpoint,
            params,
            user_agent,
            stats=stats,
            max_retries=max(args.max_retries, 0),
            backoff_base_seconds=max(args.backoff_base_seconds, 0.0),
            max_backoff_seconds=max(args.max_backoff_seconds, 0.0),
        )
        polite_sleep(max(args.delay_seconds, 0.0), stats)
        if not result.payload:
            yield SearchPage(
                backend="pullpush",
                rows=[],
                status_code=result.status_code,
                rate_limited=result.rate_limited,
                error=result.error,
            )
            return
        rows = result.payload.get("data") or []
        if not rows:
            yield SearchPage(backend="pullpush", rows=[], status_code=result.status_code, exhausted=True)
            return
        created_values = [int(row["created_utc"]) for row in rows if row.get("created_utc")]
        next_before = min(created_values) if created_values else None
        yield SearchPage(
            backend="pullpush",
            rows=rows,
            status_code=result.status_code,
            next_pullpush_before=next_before,
        )
        if next_before is None or next_before == before:
            return
        before = next_before


def reddit_submission_pages(
    *,
    subreddit: str,
    term: str,
    start_after: str | None,
    page_size: int,
    pages: int,
    user_agent: str,
    args: argparse.Namespace,
    stats: CrawlStats,
    started: float,
) -> Iterable[SearchPage]:
    endpoint = f"{REDDIT_BASE}/r/{urllib.parse.quote(subreddit)}/search.json"
    after = start_after
    for _page in range(max(1, pages)):
        check_stop_limits(args, stats, started)
        params = {
            "q": term,
            "restrict_sr": "on",
            "sort": "new",
            "t": "all",
            "limit": min(max(page_size, 1), 100),
            "after": after,
        }
        result = fetch_json(
            endpoint,
            params,
            user_agent,
            stats=stats,
            max_retries=max(args.max_retries, 0),
            backoff_base_seconds=max(args.backoff_base_seconds, 0.0),
            max_backoff_seconds=max(args.max_backoff_seconds, 0.0),
        )
        polite_sleep(max(args.delay_seconds, 0.0), stats)
        if not result.payload:
            yield SearchPage(
                backend="reddit_json",
                rows=[],
                status_code=result.status_code,
                rate_limited=result.rate_limited,
                error=result.error,
            )
            return
        data = result.payload.get("data") or {}
        children = data.get("children") or []
        if not children:
            yield SearchPage(backend="reddit_json", rows=[], status_code=result.status_code, exhausted=True)
            return
        rows = [(child.get("data") or {}) for child in children]
        after = data.get("after")
        yield SearchPage(
            backend="reddit_json",
            rows=rows,
            status_code=result.status_code,
            next_reddit_after=after,
            exhausted=after is None,
        )
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


def source_pages(
    *,
    source: str,
    source_type: str,
    subreddit: str,
    term: str,
    after_epoch: int | None,
    start_pullpush_before: int | None,
    start_reddit_after: str | None,
    args: argparse.Namespace,
    user_agent: str,
    stats: CrawlStats,
    started: float,
) -> Iterable[SearchPage]:
    if source_type == "comment":
        yield from pullpush_pages(
            kind="comment",
            subreddit=subreddit,
            term=term,
            after_epoch=after_epoch,
            start_before=start_pullpush_before,
            page_size=args.page_size,
            pages=args.pages,
            user_agent=user_agent,
            args=args,
            stats=stats,
            started=started,
        )
        return

    if source == "reddit_json":
        yield from reddit_submission_pages(
            subreddit=subreddit,
            term=term,
            start_after=start_reddit_after,
            page_size=args.page_size,
            pages=args.pages,
            user_agent=user_agent,
            args=args,
            stats=stats,
            started=started,
        )
        return

    if source == "auto":
        reddit_pages = list(
            reddit_submission_pages(
                subreddit=subreddit,
                term=term,
                start_after=start_reddit_after,
                page_size=args.page_size,
                pages=args.pages,
                user_agent=user_agent,
                args=args,
                stats=stats,
                started=started,
            )
        )
        if any(page.rows for page in reddit_pages):
            yield from reddit_pages
            return

    yield from pullpush_pages(
        kind="submission",
        subreddit=subreddit,
        term=term,
        after_epoch=after_epoch,
        start_before=start_pullpush_before,
        page_size=args.page_size,
        pages=args.pages,
        user_agent=user_agent,
        args=args,
        stats=stats,
        started=started,
    )


def crawl_key(
    *,
    source: str,
    source_type: str,
    subreddit: str,
    term: str,
    window_kind: str,
    after_epoch: int | None,
) -> str:
    return "|".join(
        [
            source.lower(),
            source_type.lower(),
            subreddit.lower(),
            term.lower(),
            window_kind,
            str(after_epoch or ""),
        ]
    )


def load_crawl_checkpoint(conn: Any, key: str) -> Any | None:
    return conn.execute("SELECT * FROM crawl_state WHERE crawl_key = ?", (key,)).fetchone()


def mark_crawl_started(
    conn: Any,
    *,
    key: str,
    source: str,
    source_type: str,
    subreddit: str,
    term: str,
    window_kind: str,
    after_epoch: int | None,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO crawl_state (
          crawl_key, source_backend, source_type, subreddit, term, window_kind,
          after_epoch, last_started_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(crawl_key) DO UPDATE SET
          source_backend = excluded.source_backend,
          last_started_at = excluded.last_started_at,
          updated_at = excluded.updated_at
        """,
        (key, source, source_type, subreddit, term, window_kind, after_epoch, now, now),
    )


def save_crawl_checkpoint(conn: Any, key: str, page: SearchPage) -> None:
    now = utc_now_iso()
    success = page.status_code is not None and 200 <= page.status_code < 300
    page_increment = 1 if success else 0
    error_increment = 1 if page.error else 0
    conn.execute(
        """
        UPDATE crawl_state
        SET pullpush_before = COALESCE(?, pullpush_before),
            reddit_after = COALESCE(?, reddit_after),
            pages_fetched = pages_fetched + ?,
            exhausted = ?,
            consecutive_errors = CASE WHEN ? THEN consecutive_errors + 1 ELSE 0 END,
            last_status_code = ?,
            last_error = ?,
            last_finished_at = ?,
            updated_at = ?
        WHERE crawl_key = ?
        """,
        (
            page.next_pullpush_before,
            page.next_reddit_after,
            page_increment,
            int(page.exhausted),
            error_increment,
            page.status_code,
            page.error,
            now,
            now,
            key,
        ),
    )


def selected_source_types(args: argparse.Namespace) -> list[str]:
    explicit = split_list(args.source_types, args.source_types_csv)
    if explicit:
        invalid = sorted(set(explicit) - {"submission", "comment"})
        if invalid:
            raise SystemExit(f"invalid source type(s): {', '.join(invalid)}")
        return unique_preserving_order(explicit)
    return ["submission"] if args.no_comments else ["submission", "comment"]


def selected_terms(args: argparse.Namespace, search_terms: dict[str, list[str]]) -> list[str]:
    all_terms = sorted({term for family_terms in search_terms.values() for term in family_terms})
    explicit = split_list(args.terms, args.terms_csv)
    if not explicit:
        return all_terms
    known = {term.lower(): term for term in all_terms}
    selected: list[str] = []
    for term in explicit:
        selected.append(known.get(term.lower(), term.lower()))
    return unique_preserving_order(selected)


def selected_subreddits(args: argparse.Namespace, config: dict[str, Any]) -> list[str]:
    values = split_list(args.subreddits, args.subreddits_csv)
    if values:
        return unique_preserving_order(values)
    return list(config.get("subreddits") or [])


def main() -> int:
    args = parse_args()
    config = load_sources_config()
    search_terms = load_search_terms()
    subreddits = selected_subreddits(args, config)
    terms = selected_terms(args, search_terms)
    source_types = selected_source_types(args)
    after_epoch = None
    window_kind = "historical" if args.seed_historical else "recent"
    if not args.seed_historical:
        since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
        after_epoch = int(since.timestamp())

    conn = None
    if not args.dry_run:
        conn = connect_db(args.db)
        ensure_schema(conn)

    stats = CrawlStats()
    seen_keys: set[tuple[str, str]] = set()
    user_agent = config.get("reddit_user_agent") or "glp1-reddit-user-report-miner/0.1"
    started = time.monotonic()

    try:
        for subreddit in subreddits:
            for term in terms:
                for source_type in source_types:
                    check_stop_limits(args, stats, started)
                    if args.max_combinations and stats.combinations_started >= args.max_combinations:
                        raise CrawlStop(f"max_combinations reached ({args.max_combinations})")
                    key = crawl_key(
                        source=args.source,
                        source_type=source_type,
                        subreddit=subreddit,
                        term=term,
                        window_kind=window_kind,
                        after_epoch=after_epoch,
                    )
                    checkpoint = None
                    if conn is not None:
                        checkpoint = load_crawl_checkpoint(conn, key)
                        if args.resume_checkpoints and checkpoint and checkpoint["exhausted"]:
                            stats.combinations_skipped_exhausted += 1
                            continue
                        mark_crawl_started(
                            conn,
                            key=key,
                            source=args.source,
                            source_type=source_type,
                            subreddit=subreddit,
                            term=term,
                            window_kind=window_kind,
                            after_epoch=after_epoch,
                        )
                        conn.commit()

                    stats.combinations_started += 1
                    start_pullpush_before = (
                        int(checkpoint["pullpush_before"])
                        if args.resume_checkpoints and checkpoint and checkpoint["pullpush_before"]
                        else None
                    )
                    start_reddit_after = (
                        str(checkpoint["reddit_after"])
                        if args.resume_checkpoints and checkpoint and checkpoint["reddit_after"]
                        else None
                    )
                    print(
                        f"crawl r/{subreddit} term={term} source_type={source_type} "
                        f"source={args.source}",
                        file=sys.stderr,
                    )

                    combo_failed = False
                    combo_rate_limited = False
                    combo_exhausted = False
                    for page in source_pages(
                        source=args.source,
                        source_type=source_type,
                        subreddit=subreddit,
                        term=term,
                        after_epoch=after_epoch,
                        start_pullpush_before=start_pullpush_before,
                        start_reddit_after=start_reddit_after,
                        args=args,
                        user_agent=user_agent,
                        stats=stats,
                        started=started,
                    ):
                        stats.fetched_rows += len(page.rows)
                        combo_rate_limited = combo_rate_limited or page.rate_limited
                        combo_failed = combo_failed or bool(page.error)
                        combo_exhausted = combo_exhausted or page.exhausted
                        if conn is not None:
                            save_crawl_checkpoint(conn, key, page)
                            conn.commit()
                        for row in page.rows:
                            if source_type == "submission":
                                candidate = candidate_from_submission(row, search_terms)
                            else:
                                candidate = candidate_from_comment(row, search_terms)
                            if candidate is None:
                                continue
                            if after_epoch and candidate.get("created_utc") and candidate["created_utc"] < after_epoch:
                                continue
                            seen_key = (candidate["source_type"], candidate["reddit_id"])
                            if seen_key in seen_keys:
                                continue
                            seen_keys.add(seen_key)
                            stats.considered += 1
                            if args.dry_run:
                                print(
                                    f"dry-run {candidate['source_type']} r/{candidate['subreddit']} "
                                    f"{candidate['reddit_id']} {candidate['matched_terms']}"
                                )
                            else:
                                changed, hash_changed = upsert_raw_candidate(conn, candidate)
                                stats.inserted_or_changed += int(changed)
                                stats.content_changed += int(hash_changed)
                                conn.commit()
                            if args.limit and stats.considered >= args.limit:
                                raise CrawlStop(f"candidate limit reached ({args.limit})")
                        if page.rate_limited and args.stop_on_rate_limit:
                            stats.combinations_rate_limited += 1
                            stats.combinations_failed += int(bool(page.error))
                            if args.rate_limit_cooldown_seconds > 0:
                                wait = jittered(args.rate_limit_cooldown_seconds)
                                print(
                                    f"warning: unrecovered rate limit; cooling down for {wait:.1f}s",
                                    file=sys.stderr,
                                )
                                backoff_sleep(wait, stats)
                            raise CrawlStop("unrecovered rate limit")

                    stats.combinations_completed += 1
                    stats.combinations_rate_limited += int(combo_rate_limited)
                    stats.combinations_failed += int(combo_failed)
                    stats.combinations_exhausted += int(combo_exhausted)
    except CrawlStop as exc:
        stats.stopped_reason = exc.reason
        print(f"stopping crawl: {exc.reason}", file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()

    summary = {
        "considered": stats.considered,
        "inserted_or_changed": stats.inserted_or_changed,
        "content_changed": stats.content_changed,
        "dry_run": args.dry_run,
        "fetched_rows": stats.fetched_rows,
        "requests": stats.requests,
        "retries": stats.retries,
        "rate_limited_responses": stats.rate_limited_responses,
        "http_errors": stats.http_errors,
        "network_errors": stats.network_errors,
        "decode_errors": stats.decode_errors,
        "backed_off_seconds": round(stats.backed_off_seconds, 3),
        "polite_sleep_seconds": round(stats.polite_sleep_seconds, 3),
        "combinations_started": stats.combinations_started,
        "combinations_completed": stats.combinations_completed,
        "combinations_skipped_exhausted": stats.combinations_skipped_exhausted,
        "combinations_rate_limited": stats.combinations_rate_limited,
        "combinations_failed": stats.combinations_failed,
        "combinations_exhausted": stats.combinations_exhausted,
        "stopped_reason": stats.stopped_reason,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
