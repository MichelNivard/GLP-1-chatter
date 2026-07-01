#!/usr/bin/env python3
"""Parse pending Reddit candidates into structured GLP-1 drug reports."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import hashlib
from pathlib import Path
from typing import Any

from glp1_common import (
    DEFAULT_DB,
    PROMPT_VERSION,
    cache_lookup,
    connect_db,
    converted_result,
    delete_reports_for_post,
    ensure_schema,
    insert_extracted_reports,
    report_needs_rescreen,
    save_parse_cache,
    set_existing_reports_canonical,
    utc_now_iso,
)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
DEFAULT_RESCREEN_MODEL = "gpt-5.4-mini"

RESCREEN_WARNING = (
    "This was flagged because the extracted weight loss or duration is large. "
    "Carefully check whether the loss/duration is truly attributable to the focal drug, "
    "or whether it belongs to prior Tirz/Sema/Ozempic/Wegovy/Mounjaro/Zepbound history, "
    "total GLP journey, age, goal weight, dose, or another confound."
)

REPORT_REQUIRED_FIELDS = [
    "drug_family",
    "drug_name_mentioned",
    "is_user_report",
    "use_status",
    "attribution",
    "include_in_plots",
    "weight_start_value",
    "weight_start_unit",
    "weight_end_value",
    "weight_end_unit",
    "weight_lost_value",
    "weight_lost_unit",
    "weight_goal_value",
    "weight_goal_unit",
    "duration_value",
    "duration_unit",
    "duration_raw",
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
]


def report_json_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    nullable_number = {"type": ["number", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reports": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "drug_family": {
                            "type": "string",
                            "enum": ["reta", "tirz", "sema", "other", "unclear"],
                        },
                        "drug_name_mentioned": nullable_string,
                        "is_user_report": {"type": "boolean"},
                        "use_status": {
                            "type": "string",
                            "enum": [
                                "active_use",
                                "prior_use",
                                "planned",
                                "considering",
                                "maintenance",
                                "stopped",
                                "unclear",
                            ],
                        },
                        "attribution": {
                            "type": "string",
                            "enum": [
                                "clear_single_drug",
                                "stack",
                                "switch_interval",
                                "prior_history",
                                "future_plan",
                                "unclear",
                            ],
                        },
                        "include_in_plots": {"type": "boolean"},
                        "weight_start_value": nullable_number,
                        "weight_start_unit": nullable_string,
                        "weight_end_value": nullable_number,
                        "weight_end_unit": nullable_string,
                        "weight_lost_value": nullable_number,
                        "weight_lost_unit": nullable_string,
                        "weight_goal_value": nullable_number,
                        "weight_goal_unit": nullable_string,
                        "duration_value": nullable_number,
                        "duration_unit": {
                            "type": ["string", "null"],
                            "enum": ["day", "week", "month", "year", None],
                        },
                        "duration_raw": nullable_string,
                        "start_date_raw": nullable_string,
                        "dose_strong": nullable_string,
                        "dose_current_mg": nullable_number,
                        "interval_per_week_value": nullable_number,
                        "gender": nullable_string,
                        "age_value": nullable_number,
                        "other_compounds_concurrent": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "side_effects": {"type": "array", "items": {"type": "string"}},
                        "side_effects_semicolon": nullable_string,
                        "confidence": {"type": "number"},
                        "evidence": nullable_string,
                        "notes": nullable_string,
                    },
                    "required": REPORT_REQUIRED_FIELDS,
                },
            }
        },
        "required": ["reports"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=25, help="Maximum raw posts/comments to parse")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="First-pass model")
    parser.add_argument("--rescreen-model", default=DEFAULT_RESCREEN_MODEL, help="Large-value rescreen model")
    parser.add_argument("--dry-run", action="store_true", help="List pending rows without API calls or DB writes")
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="Retry raw rows and parse-cache rows currently marked error.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=2400,
        help="OpenAI max output tokens for each single-item call.",
    )
    parser.add_argument(
        "--prompt-cache-key",
        default=None,
        help="Stable OpenAI prompt_cache_key prefix. Defaults to this project and prompt version.",
    )
    parser.add_argument(
        "--prompt-cache-retention",
        choices=("default", "in_memory", "24h"),
        default="24h",
        help="OpenAI prompt cache retention. Use 'default' to omit the parameter.",
    )
    return parser.parse_args()


def load_prompt(rescreen: bool = False) -> str:
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "extract_glp1_report.md").read_text(
        encoding="utf-8"
    )
    if rescreen:
        return f"{prompt}\n\nAdditional rescreening warning:\n{RESCREEN_WARNING}\n"
    return prompt


def prompt_cache_key(pass_type: str, model: str, explicit_prefix: str | None = None) -> str:
    base = explicit_prefix or "glp1-reddit-extraction"
    digest = hashlib.sha256(PROMPT_VERSION.encode("utf-8")).hexdigest()[:12]
    safe_model = "".join(char if char.isalnum() or char in "-_" else "-" for char in model)
    return f"{base}-{PROMPT_VERSION}-{pass_type}-{safe_model}-{digest}"[:128]


def build_user_input(row: sqlite3.Row, *, prefer_processed_text: bool = False) -> str:
    full_text = row["full_text"]
    if prefer_processed_text and row["processed_full_text"]:
        full_text = row["processed_full_text"]
    return (
        "Parse this single Reddit item only.\n\n"
        f"source_type: {row['source_type']}\n"
        f"subreddit: r/{row['subreddit']}\n"
        f"created_iso: {row['created_iso']}\n"
        f"url: {row['url']}\n\n"
        "full_text:\n"
        f"{full_text}"
    )


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for output in payload.get("output", []) or []:
        for content in output.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks).strip()


def call_openai(
    *,
    model: str,
    prompt: str,
    user_input: str,
    max_output_tokens: int,
    cache_key: str,
    prompt_cache_retention: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "prompt_cache_key": cache_key,
        "input": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_input},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "glp1_reddit_extraction",
                "strict": True,
                "schema": report_json_schema(),
            }
        },
        "max_output_tokens": max_output_tokens,
    }
    if prompt_cache_retention != "default":
        payload["prompt_cache_retention"] = prompt_cache_retention
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI request failed: {exc}") from exc

    response_payload = json.loads(body)
    text = extract_response_text(response_payload)
    if not text:
        raise RuntimeError(f"OpenAI response did not include output text: {body[:1000]}")
    try:
        return json.loads(text), response_payload.get("usage")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI output was not valid JSON: {exc}: {text[:1000]}") from exc


def validate_extraction(result: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise ValueError("root is not an object")
    reports = result.get("reports")
    if not isinstance(reports, list):
        raise ValueError("reports is not an array")
    for index, report in enumerate(reports):
        if not isinstance(report, dict):
            raise ValueError(f"reports[{index}] is not an object")
        missing = [field for field in REPORT_REQUIRED_FIELDS if field not in report]
        if missing:
            raise ValueError(f"reports[{index}] missing fields: {', '.join(missing)}")
        if report["drug_family"] not in {"reta", "tirz", "sema", "other", "unclear"}:
            raise ValueError(f"reports[{index}] invalid drug_family")
        if report["use_status"] not in {
            "active_use",
            "prior_use",
            "planned",
            "considering",
            "maintenance",
            "stopped",
            "unclear",
        }:
            raise ValueError(f"reports[{index}] invalid use_status")
        if report["attribution"] not in {
            "clear_single_drug",
            "stack",
            "switch_interval",
            "prior_history",
            "future_plan",
            "unclear",
        }:
            raise ValueError(f"reports[{index}] invalid attribution")
        if not isinstance(report["is_user_report"], bool):
            raise ValueError(f"reports[{index}] is_user_report is not boolean")
        if not isinstance(report["include_in_plots"], bool):
            raise ValueError(f"reports[{index}] include_in_plots is not boolean")
        if not isinstance(report["other_compounds_concurrent"], list):
            raise ValueError(f"reports[{index}] other_compounds_concurrent is not array")
        if not isinstance(report["side_effects"], list):
            raise ValueError(f"reports[{index}] side_effects is not array")
        confidence = report["confidence"]
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            raise ValueError(f"reports[{index}] confidence is outside 0..1")


def select_rows(conn: sqlite3.Connection, limit: int, retry_errors: bool) -> list[sqlite3.Row]:
    statuses = ["pending"]
    if retry_errors:
        statuses.append("error")
    placeholders = ", ".join(["?"] * len(statuses))
    query = f"""
        SELECT *
        FROM raw_posts
        WHERE parse_status IN ({placeholders})
           OR rescreen_status = 'pending'
        ORDER BY COALESCE(created_utc, 0) DESC, post_id DESC
    """
    if limit:
        query += " LIMIT ?"
        params: list[Any] = [*statuses, limit]
    else:
        params = [*statuses]
    return list(conn.execute(query, params).fetchall())


def cached_result(
    conn: sqlite3.Connection,
    *,
    content_hash_value: str,
    pass_type: str,
    retry_errors: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    cached = cache_lookup(conn, content_hash_value, pass_type)
    if cached is None:
        return None, None
    if cached["status"] == "parsed":
        return json.loads(cached["result_json"]), None
    if retry_errors:
        return None, None
    return None, cached["error"] or f"cached {pass_type} parse error"


def run_pass(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    pass_type: str,
    model: str,
    prompt: str,
    dry_run: bool,
    retry_errors: bool,
    max_output_tokens: int,
    prompt_cache_key_prefix: str | None,
    prompt_cache_retention: str,
) -> tuple[dict[str, Any] | None, str | None, str]:
    if pass_type == "rescreen" and row["processed_content_hash"]:
        content_hash_value = row["processed_content_hash"]
    else:
        content_hash_value = row["content_hash"]
    cache_key = prompt_cache_key(pass_type, model, prompt_cache_key_prefix)
    cached, cached_error = cached_result(
        conn,
        content_hash_value=content_hash_value,
        pass_type=pass_type,
        retry_errors=retry_errors,
    )
    if cached is not None:
        return cached, None, "cache"
    if cached_error is not None:
        return None, cached_error, "cache-error"
    if dry_run:
        return None, None, "dry-run"

    try:
        result, usage = call_openai(
            model=model,
            prompt=prompt,
            user_input=build_user_input(row, prefer_processed_text=pass_type == "rescreen"),
            max_output_tokens=max_output_tokens,
            cache_key=cache_key,
            prompt_cache_retention=prompt_cache_retention,
        )
        validate_extraction(result)
        converted = converted_result(result)
    except Exception as exc:  # noqa: BLE001 - store parse failures exactly.
        error = str(exc)
        save_parse_cache(
            conn,
            content_hash_value=content_hash_value,
            pass_type=pass_type,
            model=model,
            prompt_cache_key=cache_key,
            status="error",
            error=error,
        )
        return None, error, "api-error"

    save_parse_cache(
        conn,
        content_hash_value=content_hash_value,
        pass_type=pass_type,
        model=model,
        prompt_cache_key=cache_key,
        status="parsed",
        result=result,
        converted=converted,
        usage=usage,
    )
    return result, None, "api"


def mark_parse_error(conn: sqlite3.Connection, post_id: int, model: str, error: str) -> None:
    conn.execute(
        """
        UPDATE raw_posts
        SET parse_status = 'error',
            parsed_model = ?,
            parsed_at = ?,
            parse_error = ?,
            processed_content_hash = content_hash,
            processed_full_text = full_text,
            content_changed_after_processing = 0
        WHERE post_id = ?
        """,
        (model, utc_now_iso(), error[:4000], post_id),
    )


def mark_parsed(conn: sqlite3.Connection, post_id: int, model: str) -> None:
    conn.execute(
        """
        UPDATE raw_posts
        SET parse_status = 'parsed',
            parsed_model = ?,
            parsed_at = ?,
            parse_error = NULL,
            processed_content_hash = content_hash,
            processed_full_text = full_text,
            content_changed_after_processing = 0
        WHERE post_id = ?
        """,
        (model, utc_now_iso(), post_id),
    )


def mark_rescreen(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    status: str,
    model: str | None = None,
) -> None:
    if status == "rescreened":
        conn.execute(
            """
            UPDATE raw_posts
            SET rescreen_status = 'rescreened',
                rescreen_model = ?,
                rescreened_at = ?
            WHERE post_id = ?
            """,
            (model, utc_now_iso(), post_id),
        )
    elif status == "error":
        conn.execute(
            """
            UPDATE raw_posts
            SET rescreen_status = 'error',
                rescreen_model = ?,
                rescreened_at = ?
            WHERE post_id = ?
            """,
            (model, utc_now_iso(), post_id),
        )
    else:
        conn.execute(
            "UPDATE raw_posts SET rescreen_status = ? WHERE post_id = ?",
            (status, post_id),
        )


def process_row(conn: sqlite3.Connection, row: sqlite3.Row, args: argparse.Namespace) -> dict[str, Any]:
    post_id = int(row["post_id"])
    dry_label = "dry-run" if args.dry_run else "write"
    print(f"{dry_label}: post_id={post_id} {row['source_type']} r/{row['subreddit']} {row['reddit_id']}")

    if row["parse_status"] == "parsed" and row["rescreen_status"] == "pending":
        nano_source = "already-parsed"
        if args.dry_run:
            return {"post_id": post_id, "status": "dry-run", "source": nano_source, "rescreen": "pending"}
    else:
        nano_prompt = load_prompt(rescreen=False)
        nano_result, nano_error, nano_source = run_pass(
            conn,
            row=row,
            pass_type="nano",
            model=args.model,
            prompt=nano_prompt,
            dry_run=args.dry_run,
            retry_errors=args.retry_errors,
            max_output_tokens=args.max_output_tokens,
            prompt_cache_key_prefix=args.prompt_cache_key,
            prompt_cache_retention=args.prompt_cache_retention,
        )
        if args.dry_run:
            return {"post_id": post_id, "status": "dry-run", "source": nano_source}
        if nano_error:
            mark_parse_error(conn, post_id, args.model, nano_error)
            conn.commit()
            return {"post_id": post_id, "status": "error", "error": nano_error, "source": nano_source}
        if nano_result is None:
            return {"post_id": post_id, "status": "skipped"}

        delete_reports_for_post(conn, post_id)
        insert_extracted_reports(
            conn,
            post_id=post_id,
            content_hash_value=row["content_hash"],
            result=nano_result,
            pass_type="nano",
            canonical=True,
        )
        mark_parsed(conn, post_id, args.model)
        converted = converted_result(nano_result)
        needs_rescreen = any(report_needs_rescreen(report) for report in converted.get("reports", []))
        if not needs_rescreen:
            mark_rescreen(conn, post_id, status="not_needed")
            conn.commit()
            return {"post_id": post_id, "status": "parsed", "source": nano_source, "rescreen": "not_needed"}

        mark_rescreen(conn, post_id, status="pending")
        conn.commit()

    rescreen_prompt = load_prompt(rescreen=True)
    rescreen_result, rescreen_error, rescreen_source = run_pass(
        conn,
        row=row,
        pass_type="rescreen",
        model=args.rescreen_model,
        prompt=rescreen_prompt,
        dry_run=args.dry_run,
        retry_errors=args.retry_errors,
        max_output_tokens=args.max_output_tokens,
        prompt_cache_key_prefix=args.prompt_cache_key,
        prompt_cache_retention=args.prompt_cache_retention,
    )
    if rescreen_error:
        mark_rescreen(conn, post_id, status="error", model=args.rescreen_model)
        conn.commit()
        return {
            "post_id": post_id,
            "status": "parsed",
            "source": nano_source,
            "rescreen": "error",
            "error": rescreen_error,
        }

    if rescreen_result is not None:
        set_existing_reports_canonical(conn, post_id, False)
        insert_extracted_reports(
            conn,
            post_id=post_id,
            content_hash_value=row["processed_content_hash"] or row["content_hash"],
            result=rescreen_result,
            pass_type="rescreen",
            canonical=True,
        )
        mark_rescreen(conn, post_id, status="rescreened", model=args.rescreen_model)
        conn.commit()
        return {
            "post_id": post_id,
            "status": "parsed",
            "source": nano_source,
            "rescreen": rescreen_source,
        }

    return {"post_id": post_id, "status": "parsed", "source": nano_source}


def main() -> int:
    args = parse_args()
    conn = connect_db(args.db)
    ensure_schema(conn)
    rows = select_rows(conn, args.limit, args.retry_errors)
    if not rows:
        print("no pending rows")
        return 0
    results = []
    try:
        for row in rows:
            results.append(process_row(conn, row, args))
    finally:
        conn.close()
    print(json.dumps({"processed": len(results), "results": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
