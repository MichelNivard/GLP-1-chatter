#!/usr/bin/env python3
"""Shared utilities for the GLP-1 Reddit mining project."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "glp1_reports.sqlite3"
PROMPT_VERSION = "2026-07-02-v2"

DRUG_FAMILIES = ("reta", "tirz", "sema")
ALL_DRUG_FAMILIES = ("reta", "tirz", "sema", "other", "unclear")
RESIDUAL_RESCREEN_FRACTION = 0.05
MIN_RESIDUAL_FIT_POINTS = 20

DB_SCHEMA = """
PRAGMA journal_mode = DELETE;

CREATE TABLE IF NOT EXISTS raw_posts (
  post_id INTEGER PRIMARY KEY,
  reddit_id TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('submission', 'comment')),
  subreddit TEXT NOT NULL,
  thread_id TEXT,
  created_utc INTEGER,
  created_iso TEXT,
  title TEXT,
  body TEXT,
  full_text TEXT NOT NULL,
  processed_full_text TEXT,
  url TEXT,
  matched_drug_families TEXT NOT NULL DEFAULT '[]',
  matched_terms TEXT NOT NULL DEFAULT '[]',
  content_hash TEXT NOT NULL,
  processed_content_hash TEXT,
  content_changed_after_processing INTEGER NOT NULL DEFAULT 0 CHECK (content_changed_after_processing IN (0, 1)),
  first_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  parse_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (parse_status IN ('pending', 'parsed', 'error')),
  parsed_model TEXT,
  parsed_at TEXT,
  parse_error TEXT,
  rescreen_status TEXT NOT NULL DEFAULT 'not_needed'
    CHECK (rescreen_status IN ('not_needed', 'pending', 'rescreened', 'error')),
  rescreen_model TEXT,
  rescreened_at TEXT,
  UNIQUE (source_type, reddit_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_posts_parse_status
  ON raw_posts(parse_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_raw_posts_content_hash
  ON raw_posts(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_posts_created
  ON raw_posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_raw_posts_subreddit
  ON raw_posts(subreddit);

CREATE TABLE IF NOT EXISTS parse_cache (
  cache_id INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL,
  pass_type TEXT NOT NULL CHECK (pass_type IN ('nano', 'rescreen')),
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_cache_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('parsed', 'error')),
  result_json TEXT,
  converted_json TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_prompt_tokens INTEGER,
  usage_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (content_hash, pass_type)
);

CREATE INDEX IF NOT EXISTS idx_parse_cache_hash
  ON parse_cache(content_hash, pass_type);

CREATE TABLE IF NOT EXISTS extracted_reports (
  report_id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  source_pass TEXT NOT NULL CHECK (source_pass IN ('nano', 'rescreen')),
  canonical INTEGER NOT NULL DEFAULT 1 CHECK (canonical IN (0, 1)),
  drug_family TEXT,
  drug_name_mentioned TEXT,
  is_user_report INTEGER,
  use_status TEXT,
  attribution TEXT,
  include_in_plots INTEGER,
  weight_start_value REAL,
  weight_start_unit TEXT,
  weight_start_kg REAL,
  weight_end_value REAL,
  weight_end_unit TEXT,
  weight_end_kg REAL,
  weight_lost_value REAL,
  weight_lost_unit TEXT,
  weight_lost_kg REAL,
  weight_goal_value REAL,
  weight_goal_unit TEXT,
  weight_goal_kg REAL,
  duration_value REAL,
  duration_unit TEXT,
  duration_raw TEXT,
  duration_days REAL,
  duration_weeks REAL,
  weight_change_kg REAL,
  start_date_raw TEXT,
  dose_strong TEXT,
  dose_current_mg REAL,
  interval_per_week_value REAL,
  gender TEXT,
  age_value REAL,
  other_compounds_concurrent TEXT NOT NULL DEFAULT '[]',
  side_effects TEXT NOT NULL DEFAULT '[]',
  side_effects_semicolon TEXT,
  confidence REAL,
  evidence TEXT,
  notes TEXT,
  raw_report_json TEXT NOT NULL,
  converted_at TEXT NOT NULL,
  FOREIGN KEY (post_id) REFERENCES raw_posts(post_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reports_post
  ON extracted_reports(post_id);
CREATE INDEX IF NOT EXISTS idx_reports_family
  ON extracted_reports(drug_family, canonical, include_in_plots);
CREATE INDEX IF NOT EXISTS idx_reports_hash
  ON extracted_reports(content_hash, source_pass);

CREATE TABLE IF NOT EXISTS side_effect_screening_runs (
  report_id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_cache_key TEXT,
  status TEXT NOT NULL CHECK (status IN ('parsed', 'error', 'no_effects')),
  result_json TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_prompt_tokens INTEGER,
  usage_json TEXT,
  error TEXT,
  screened_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (report_id) REFERENCES extracted_reports(report_id) ON DELETE CASCADE,
  FOREIGN KEY (post_id) REFERENCES raw_posts(post_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_side_effect_screening_runs_status
  ON side_effect_screening_runs(status, updated_at);

CREATE TABLE IF NOT EXISTS side_effect_screenings (
  screening_id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  side_effect_phrase TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
  confidence REAL,
  evidence TEXT,
  rationale TEXT,
  model TEXT NOT NULL,
  screened_at TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  FOREIGN KEY (report_id) REFERENCES extracted_reports(report_id) ON DELETE CASCADE,
  FOREIGN KEY (post_id) REFERENCES raw_posts(post_id) ON DELETE CASCADE,
  UNIQUE (report_id, side_effect_phrase)
);

CREATE INDEX IF NOT EXISTS idx_side_effect_screenings_report
  ON side_effect_screenings(report_id);
CREATE INDEX IF NOT EXISTS idx_side_effect_screenings_phrase
  ON side_effect_screenings(side_effect_phrase, severity);

CREATE TABLE IF NOT EXISTS crawl_state (
  crawl_key TEXT PRIMARY KEY,
  source_backend TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK (source_type IN ('submission', 'comment')),
  subreddit TEXT NOT NULL,
  term TEXT NOT NULL,
  window_kind TEXT NOT NULL CHECK (window_kind IN ('historical', 'recent')),
  after_epoch INTEGER,
  pullpush_before INTEGER,
  reddit_after TEXT,
  pages_fetched INTEGER NOT NULL DEFAULT 0,
  exhausted INTEGER NOT NULL DEFAULT 0 CHECK (exhausted IN (0, 1)),
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  last_status_code INTEGER,
  last_error TEXT,
  last_started_at TEXT,
  last_finished_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_crawl_state_combo
  ON crawl_state(subreddit, term, source_type, window_kind);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_from_utc(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).replace(microsecond=0).isoformat()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def connect_db(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DB_SCHEMA)
    migrate_schema(conn)
    conn.commit()


def migrate_schema(conn: sqlite3.Connection) -> None:
    raw_post_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(raw_posts)").fetchall()
    }
    additions = {
        "processed_full_text": "ALTER TABLE raw_posts ADD COLUMN processed_full_text TEXT",
        "processed_content_hash": "ALTER TABLE raw_posts ADD COLUMN processed_content_hash TEXT",
        "content_changed_after_processing": (
            "ALTER TABLE raw_posts ADD COLUMN content_changed_after_processing "
            "INTEGER NOT NULL DEFAULT 0 CHECK (content_changed_after_processing IN (0, 1))"
        ),
    }
    for column, statement in additions.items():
        if column not in raw_post_columns:
            conn.execute(statement)
    parse_cache_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(parse_cache)").fetchall()
    }
    parse_cache_additions = {
        "prompt_cache_key": "ALTER TABLE parse_cache ADD COLUMN prompt_cache_key TEXT",
        "input_tokens": "ALTER TABLE parse_cache ADD COLUMN input_tokens INTEGER",
        "output_tokens": "ALTER TABLE parse_cache ADD COLUMN output_tokens INTEGER",
        "cached_prompt_tokens": "ALTER TABLE parse_cache ADD COLUMN cached_prompt_tokens INTEGER",
        "usage_json": "ALTER TABLE parse_cache ADD COLUMN usage_json TEXT",
    }
    for column, statement in parse_cache_additions.items():
        if column not in parse_cache_columns:
            conn.execute(statement)


def normalize_space(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def build_full_text(title: str | None, body: str | None) -> str:
    title = normalize_space(title)
    body = (body or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _term_regex(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)


def find_matches(text: str, search_terms: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    families: list[str] = []
    terms: list[str] = []
    for family, family_terms in search_terms.items():
        found_for_family = False
        for term in family_terms:
            if _term_regex(term).search(text):
                terms.append(term.lower())
                found_for_family = True
        if found_for_family:
            families.append(family)
    return sorted(set(families)), sorted(set(terms))


def json_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return json.dumps([value], ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(list(value), ensure_ascii=False)


def upsert_raw_candidate(conn: sqlite3.Connection, item: dict[str, Any]) -> tuple[bool, bool]:
    """Insert or update a raw candidate.

    Returns (inserted_or_changed, content_changed).
    """

    now = utc_now_iso()
    reddit_id = item["reddit_id"]
    source_type = item["source_type"]
    full_text = item["full_text"]
    item_hash = content_hash(full_text)
    existing = conn.execute(
        """
        SELECT post_id, content_hash, parse_status
        FROM raw_posts
        WHERE source_type = ? AND reddit_id = ?
        """,
        (source_type, reddit_id),
    ).fetchone()

    values = {
        "reddit_id": reddit_id,
        "source_type": source_type,
        "subreddit": item.get("subreddit") or "",
        "thread_id": item.get("thread_id"),
        "created_utc": item.get("created_utc"),
        "created_iso": item.get("created_iso"),
        "title": item.get("title"),
        "body": item.get("body"),
        "full_text": full_text,
        "url": item.get("url"),
        "matched_drug_families": json_list(item.get("matched_drug_families", [])),
        "matched_terms": json_list(item.get("matched_terms", [])),
        "content_hash": item_hash,
    }

    if existing is None:
        conn.execute(
            """
            INSERT INTO raw_posts (
              reddit_id, source_type, subreddit, thread_id, created_utc, created_iso,
              title, body, full_text, url, matched_drug_families, matched_terms,
              content_hash, first_seen_at, updated_at
            ) VALUES (
              :reddit_id, :source_type, :subreddit, :thread_id, :created_utc, :created_iso,
              :title, :body, :full_text, :url, :matched_drug_families, :matched_terms,
              :content_hash, :now, :now
            )
            """,
            {**values, "now": now},
        )
        return True, True

    changed = existing["content_hash"] != item_hash
    if changed:
        if existing["parse_status"] == "pending":
            conn.execute(
                """
                UPDATE raw_posts
                SET subreddit = :subreddit,
                    thread_id = :thread_id,
                    created_utc = :created_utc,
                    created_iso = :created_iso,
                    title = :title,
                    body = :body,
                    full_text = :full_text,
                    url = :url,
                    matched_drug_families = :matched_drug_families,
                    matched_terms = :matched_terms,
                    content_hash = :content_hash,
                    content_changed_after_processing = 0,
                    updated_at = :now
                WHERE post_id = :post_id
                """,
                {**values, "now": now, "post_id": existing["post_id"]},
            )
        else:
            conn.execute(
                """
                UPDATE raw_posts
                SET subreddit = :subreddit,
                    thread_id = :thread_id,
                    created_utc = :created_utc,
                    created_iso = :created_iso,
                    title = :title,
                    body = :body,
                    full_text = :full_text,
                    url = :url,
                    matched_drug_families = :matched_drug_families,
                    matched_terms = :matched_terms,
                    content_hash = :content_hash,
                    content_changed_after_processing = 1,
                    updated_at = :now
                WHERE post_id = :post_id
                """,
                {**values, "now": now, "post_id": existing["post_id"]},
            )
        return True, True

    conn.execute(
        """
        UPDATE raw_posts
        SET subreddit = :subreddit,
            thread_id = :thread_id,
            created_utc = :created_utc,
            created_iso = :created_iso,
            title = :title,
            body = :body,
            full_text = :full_text,
            url = :url,
            matched_drug_families = :matched_drug_families,
            matched_terms = :matched_terms,
            updated_at = :now
        WHERE post_id = :post_id
        """,
        {**values, "now": now, "post_id": existing["post_id"]},
    )
    return False, False


def load_search_terms() -> dict[str, list[str]]:
    return read_json(ROOT / "config" / "search_terms.json")


def load_sources_config() -> dict[str, Any]:
    return read_json(ROOT / "config" / "sources.json")


def load_side_effect_normalization() -> dict[str, str]:
    mapping = read_json(ROOT / "config" / "side_effect_normalization.json")
    return {normalize_side_effect_phrase(k): normalize_side_effect_phrase(v) for k, v in mapping.items()}


def normalize_side_effect_phrase(phrase: str) -> str:
    phrase = normalize_space(phrase).lower()
    phrase = phrase.replace("_", " ").replace("/", " ")
    phrase = re.sub(r"[^a-z0-9 +'-]+", " ", phrase)
    phrase = normalize_space(phrase)
    return phrase


def canonical_side_effects(side_effects: list[str], mapping: dict[str, str]) -> list[str]:
    normalized: list[str] = []
    for effect in side_effects or []:
        phrase = normalize_side_effect_phrase(effect)
        if not phrase:
            continue
        normalized.append(mapping.get(phrase, phrase))
    return sorted(set(normalized))


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def weight_to_kg(value: Any, unit: str | None) -> float | None:
    numeric = _as_float(value)
    if numeric is None or not unit:
        return None
    unit_norm = unit.strip().lower().replace(".", "")
    if unit_norm in {"kg", "kgs", "kilogram", "kilograms"}:
        return numeric
    if unit_norm in {"lb", "lbs", "pound", "pounds"}:
        return numeric * 0.45359237
    if unit_norm in {"stone", "stones", "st"}:
        return numeric * 6.35029318
    return None


def duration_to_days(value: Any, unit: str | None) -> float | None:
    numeric = _as_float(value)
    if numeric is None or not unit:
        return None
    unit_norm = unit.strip().lower().rstrip("s")
    factors = {
        "day": 1.0,
        "week": 7.0,
        "month": 30.4375,
        "year": 365.25,
    }
    factor = factors.get(unit_norm)
    if factor is None:
        return None
    return numeric * factor


def compute_converted_report(report: dict[str, Any]) -> dict[str, Any]:
    converted = dict(report)
    start_kg = weight_to_kg(report.get("weight_start_value"), report.get("weight_start_unit"))
    end_kg = weight_to_kg(report.get("weight_end_value"), report.get("weight_end_unit"))
    lost_kg = weight_to_kg(report.get("weight_lost_value"), report.get("weight_lost_unit"))
    goal_kg = weight_to_kg(report.get("weight_goal_value"), report.get("weight_goal_unit"))

    if lost_kg is None and start_kg is not None and end_kg is not None:
        lost_kg = start_kg - end_kg
    if end_kg is None and start_kg is not None and lost_kg is not None:
        end_kg = start_kg - lost_kg

    if start_kg is not None and end_kg is not None:
        weight_change_kg = end_kg - start_kg
    elif lost_kg is not None:
        weight_change_kg = -lost_kg
    else:
        weight_change_kg = None

    duration_days = duration_to_days(report.get("duration_value"), report.get("duration_unit"))
    converted.update(
        {
            "weight_start_kg": start_kg,
            "weight_end_kg": end_kg,
            "weight_lost_kg": lost_kg,
            "weight_goal_kg": goal_kg,
            "duration_days": duration_days,
            "duration_weeks": duration_days / 7.0 if duration_days is not None else None,
            "weight_change_kg": weight_change_kg,
        }
    )
    return converted


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def solve_3x3(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    augmented = [row[:] + [rhs] for row, rhs in zip(matrix, vector, strict=True)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        if pivot != col:
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for item in range(col, 4):
            augmented[col][item] /= pivot_value
        for row in range(3):
            if row == col:
                continue
            factor = augmented[row][col]
            for item in range(col, 4):
                augmented[row][item] -= factor * augmented[col][item]
    return [augmented[row][3] for row in range(3)]


def quadratic_fit(pairs: list[tuple[float, float]]) -> dict[str, Any] | None:
    cleaned = [(x, y) for x, y in pairs if math.isfinite(x) and math.isfinite(y)]
    if len(cleaned) < 3 or len({x for x, _ in cleaned}) < 3:
        return None
    xs = [x for x, _ in cleaned]
    center = sum(xs) / len(xs)
    scale = max(xs) - min(xs)
    if scale <= 0:
        return None
    scaled = [((x - center) / scale, y) for x, y in cleaned]
    s0 = float(len(scaled))
    s1 = sum(z for z, _ in scaled)
    s2 = sum(z * z for z, _ in scaled)
    s3 = sum(z * z * z for z, _ in scaled)
    s4 = sum(z * z * z * z for z, _ in scaled)
    t0 = sum(y for _, y in scaled)
    t1 = sum(z * y for z, y in scaled)
    t2 = sum(z * z * y for z, y in scaled)
    coefficients = solve_3x3(
        [[s0, s1, s2], [s1, s2, s3], [s2, s3, s4]],
        [t0, t1, t2],
    )
    if coefficients is None:
        return None
    return {"coefficients": coefficients, "center": center, "scale": scale}


def quadratic_predict(fit: dict[str, Any], weeks: float) -> float:
    intercept, linear, quadratic = fit["coefficients"]
    z = (weeks - fit["center"]) / fit["scale"]
    return intercept + linear * z + quadratic * z * z


def quadratic_regression_curve(points: list[dict[str, Any]], grid_max: int = 80) -> list[dict[str, float]]:
    pairs = sorted(
        (duration, change)
        for point in points
        if (duration := finite_float(point.get("duration_weeks"))) is not None
        and (change := finite_float(point.get("weight_change_kg"))) is not None
    )
    fit = quadratic_fit(pairs)
    if fit is None:
        return []
    min_x, max_x = pairs[0][0], pairs[-1][0]
    if min_x == max_x:
        return []
    grid_n = min(grid_max, max(16, len(pairs) * 2))
    return [
        {
            "weeks": round(x, 4),
            "weight_change_kg": round(quadratic_predict(fit, x), 4),
        }
        for i in range(grid_n)
        for x in [min_x + (max_x - min_x) * i / (grid_n - 1)]
    ]


def residual_outlier_post_ids(
    rows: list[dict[str, Any]],
    *,
    fraction: float = RESIDUAL_RESCREEN_FRACTION,
    min_points: int = MIN_RESIDUAL_FIT_POINTS,
) -> dict[str, Any]:
    by_family: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        family = str(row.get("drug_family") or "")
        duration = finite_float(row.get("duration_weeks"))
        change = finite_float(row.get("weight_change_kg"))
        if family not in DRUG_FAMILIES or duration is None or change is None:
            continue
        by_family.setdefault(family, []).append({**row, "duration_weeks": duration, "weight_change_kg": change})

    flagged_post_ids: set[int] = set()
    diagnostics: dict[str, dict[str, Any]] = {}
    for family, family_rows in by_family.items():
        pairs = [(row["duration_weeks"], row["weight_change_kg"]) for row in family_rows]
        fit = quadratic_fit(pairs)
        if fit is None or len(family_rows) < min_points:
            diagnostics[family] = {"eligible_reports": len(family_rows), "flagged_posts": 0, "threshold_abs_residual": None}
            continue
        residual_rows = []
        for row in family_rows:
            expected = quadratic_predict(fit, row["duration_weeks"])
            residual = row["weight_change_kg"] - expected
            residual_rows.append(
                {
                    **row,
                    "expected_weight_change_kg": expected,
                    "residual_kg": residual,
                    "abs_residual_kg": abs(residual),
                }
            )
        residual_rows.sort(key=lambda item: (-item["abs_residual_kg"], int(item.get("report_id") or 0)))
        flag_count = max(1, math.ceil(len(residual_rows) * fraction))
        top_rows = residual_rows[:flag_count]
        threshold = top_rows[-1]["abs_residual_kg"] if top_rows else None
        candidate_post_ids = {
            int(row["post_id"])
            for row in top_rows
            if row.get("post_id") is not None
            and row.get("source_pass") == "nano"
            and row.get("rescreen_status") == "not_needed"
        }
        flagged_post_ids.update(candidate_post_ids)
        diagnostics[family] = {
            "eligible_reports": len(family_rows),
            "outlier_reports": flag_count,
            "flagged_posts": len(candidate_post_ids),
            "threshold_abs_residual": round(threshold, 4) if threshold is not None else None,
        }
    return {"post_ids": sorted(flagged_post_ids), "diagnostics": diagnostics}


def converted_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"reports": [compute_converted_report(report) for report in result.get("reports", [])]}


def cache_lookup(conn: sqlite3.Connection, content_hash_value: str, pass_type: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM parse_cache
        WHERE content_hash = ? AND pass_type = ?
        """,
        (content_hash_value, pass_type),
    ).fetchone()


def save_parse_cache(
    conn: sqlite3.Connection,
    *,
    content_hash_value: str,
    pass_type: str,
    model: str,
    prompt_cache_key: str | None = None,
    status: str,
    result: dict[str, Any] | None = None,
    converted: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = utc_now_iso()
    input_tokens = None
    output_tokens = None
    cached_prompt_tokens = None
    if usage:
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        token_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        cached_prompt_tokens = token_details.get("cached_tokens")
    conn.execute(
        """
        INSERT INTO parse_cache (
          content_hash, pass_type, model, prompt_version, prompt_cache_key, status, result_json,
          converted_json, input_tokens, output_tokens, cached_prompt_tokens, usage_json,
          error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(content_hash, pass_type) DO UPDATE SET
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          prompt_cache_key = excluded.prompt_cache_key,
          status = excluded.status,
          result_json = excluded.result_json,
          converted_json = excluded.converted_json,
          input_tokens = excluded.input_tokens,
          output_tokens = excluded.output_tokens,
          cached_prompt_tokens = excluded.cached_prompt_tokens,
          usage_json = excluded.usage_json,
          error = excluded.error,
          updated_at = excluded.updated_at
        """,
        (
            content_hash_value,
            pass_type,
            model,
            PROMPT_VERSION,
            prompt_cache_key,
            status,
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
            json.dumps(converted, ensure_ascii=False, sort_keys=True) if converted is not None else None,
            input_tokens,
            output_tokens,
            cached_prompt_tokens,
            json.dumps(usage, ensure_ascii=False, sort_keys=True) if usage is not None else None,
            error,
            now,
            now,
        ),
    )


def delete_reports_for_post(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("DELETE FROM extracted_reports WHERE post_id = ?", (post_id,))


REPORT_INSERT_COLUMNS = [
    "post_id",
    "content_hash",
    "source_pass",
    "canonical",
    "drug_family",
    "drug_name_mentioned",
    "is_user_report",
    "use_status",
    "attribution",
    "include_in_plots",
    "weight_start_value",
    "weight_start_unit",
    "weight_start_kg",
    "weight_end_value",
    "weight_end_unit",
    "weight_end_kg",
    "weight_lost_value",
    "weight_lost_unit",
    "weight_lost_kg",
    "weight_goal_value",
    "weight_goal_unit",
    "weight_goal_kg",
    "duration_value",
    "duration_unit",
    "duration_raw",
    "duration_days",
    "duration_weeks",
    "weight_change_kg",
    "start_date_raw",
    "dose_strong",
    "dose_current_mg",
    "interval_per_week_value",
    "gender",
    "age_value",
    "other_compounds_concurrent",
    "side_effects",
    "side_effects_semicolon",
    "confidence",
    "evidence",
    "notes",
    "raw_report_json",
    "converted_at",
]


def insert_extracted_reports(
    conn: sqlite3.Connection,
    *,
    post_id: int,
    content_hash_value: str,
    result: dict[str, Any],
    pass_type: str,
    canonical: bool,
) -> None:
    now = utc_now_iso()
    converted = converted_result(result)
    placeholders = ", ".join(["?"] * len(REPORT_INSERT_COLUMNS))
    columns = ", ".join(REPORT_INSERT_COLUMNS)
    for raw_report, report in zip(result.get("reports", []), converted["reports"]):
        row = {
            "post_id": post_id,
            "content_hash": content_hash_value,
            "source_pass": pass_type,
            "canonical": 1 if canonical else 0,
            "drug_family": report.get("drug_family"),
            "drug_name_mentioned": report.get("drug_name_mentioned"),
            "is_user_report": int(bool(report.get("is_user_report"))),
            "use_status": report.get("use_status"),
            "attribution": report.get("attribution"),
            "include_in_plots": int(bool(report.get("include_in_plots"))),
            "weight_start_value": _as_float(report.get("weight_start_value")),
            "weight_start_unit": report.get("weight_start_unit"),
            "weight_start_kg": report.get("weight_start_kg"),
            "weight_end_value": _as_float(report.get("weight_end_value")),
            "weight_end_unit": report.get("weight_end_unit"),
            "weight_end_kg": report.get("weight_end_kg"),
            "weight_lost_value": _as_float(report.get("weight_lost_value")),
            "weight_lost_unit": report.get("weight_lost_unit"),
            "weight_lost_kg": report.get("weight_lost_kg"),
            "weight_goal_value": _as_float(report.get("weight_goal_value")),
            "weight_goal_unit": report.get("weight_goal_unit"),
            "weight_goal_kg": report.get("weight_goal_kg"),
            "duration_value": _as_float(report.get("duration_value")),
            "duration_unit": report.get("duration_unit"),
            "duration_raw": report.get("duration_raw"),
            "duration_days": report.get("duration_days"),
            "duration_weeks": report.get("duration_weeks"),
            "weight_change_kg": report.get("weight_change_kg"),
            "start_date_raw": report.get("start_date_raw"),
            "dose_strong": report.get("dose_strong"),
            "dose_current_mg": _as_float(report.get("dose_current_mg")),
            "interval_per_week_value": _as_float(report.get("interval_per_week_value")),
            "gender": report.get("gender"),
            "age_value": _as_float(report.get("age_value")),
            "other_compounds_concurrent": json.dumps(
                report.get("other_compounds_concurrent") or [], ensure_ascii=False
            ),
            "side_effects": json.dumps(report.get("side_effects") or [], ensure_ascii=False),
            "side_effects_semicolon": report.get("side_effects_semicolon"),
            "confidence": _as_float(report.get("confidence")),
            "evidence": report.get("evidence"),
            "notes": report.get("notes"),
            "raw_report_json": json.dumps(raw_report, ensure_ascii=False, sort_keys=True),
            "converted_at": now,
        }
        conn.execute(
            f"INSERT INTO extracted_reports ({columns}) VALUES ({placeholders})",
            [row[column] for column in REPORT_INSERT_COLUMNS],
        )


def set_existing_reports_canonical(conn: sqlite3.Connection, post_id: int, canonical: bool) -> None:
    conn.execute(
        "UPDATE extracted_reports SET canonical = ? WHERE post_id = ?",
        (1 if canonical else 0, post_id),
    )


def row_json(row: sqlite3.Row, column: str, default: Any) -> Any:
    value = row[column]
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
