#!/usr/bin/env python3
"""LLM-screen extracted side-effect phrases for reader-facing severity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from glp1_common import (
    DEFAULT_DB,
    ROOT,
    canonical_side_effects,
    connect_db,
    ensure_schema,
    load_side_effect_normalization,
    row_json,
    utc_now_iso,
)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
SIDE_EFFECT_PROMPT_VERSION = "2026-07-02-side-effects-v1"
VALID_SEVERITIES = {"mild", "moderate", "severe"}


class TransientOpenAIError(RuntimeError):
    """A retryable API/server condition; stop the run without marking every row error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--limit", type=int, default=50, help="Maximum reports to screen; 0 means no limit")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Severity-screening model")
    parser.add_argument("--dry-run", action="store_true", help="List eligible reports without API calls or DB writes")
    parser.add_argument("--retry-errors", action="store_true", help="Retry reports with prior screening errors")
    parser.add_argument("--max-output-tokens", type=int, default=1200, help="OpenAI max output tokens")
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=19_800,
        help="Gracefully stop after this many seconds so Actions can commit partial progress.",
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


def prompt_cache_key(model: str, explicit_prefix: str | None = None) -> str:
    base = explicit_prefix or "glp1-severity"
    digest = hashlib.sha256(SIDE_EFFECT_PROMPT_VERSION.encode("utf-8")).hexdigest()[:12]
    safe_model = "".join(char if char.isalnum() or char in "-_" else "-" for char in model)
    suffix = f"{safe_model}-{digest}"
    max_base_length = max(1, 63 - len(suffix))
    return f"{base[:max_base_length]}-{suffix}"[:64]


def severity_schema() -> dict[str, Any]:
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "screenings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "side_effect": {"type": "string"},
                        "severity": {"type": "string", "enum": ["mild", "moderate", "severe"]},
                        "confidence": {"type": "number"},
                        "evidence": nullable_string,
                        "rationale": nullable_string,
                    },
                    "required": ["side_effect", "severity", "confidence", "evidence", "rationale"],
                },
            },
            "overall_notes": nullable_string,
        },
        "required": ["screenings", "overall_notes"],
    }


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    chunks: list[str] = []
    for output in payload.get("output", []) or []:
        for content in output.get("content", []) or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "".join(chunks).strip()


def load_prompt() -> str:
    return (ROOT / "prompts" / "screen_side_effect_severity.md").read_text(encoding="utf-8")


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
    payload: dict[str, Any] = {
        "model": model,
        "prompt_cache_key": cache_key,
        "input": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_input},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "glp1_side_effect_severity",
                "strict": True,
                "schema": severity_schema(),
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
        if exc.code in {429, 500, 502, 503, 504}:
            raise TransientOpenAIError(f"OpenAI transient HTTP {exc.code}: {detail}") from exc
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


def usage_fields(usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None, str | None]:
    if not usage:
        return None, None, None, None
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    token_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_prompt_tokens = token_details.get("cached_tokens")
    return (
        input_tokens,
        output_tokens,
        cached_prompt_tokens,
        json.dumps(usage, ensure_ascii=False, sort_keys=True),
    )


def select_reports(conn: sqlite3.Connection, limit: int, retry_errors: bool) -> list[sqlite3.Row]:
    query = """
        SELECT
          r.report_id,
          r.post_id,
          r.content_hash,
          r.drug_family,
          r.drug_name_mentioned,
          r.attribution,
          r.dose_strong,
          r.side_effects,
          r.evidence,
          r.notes,
          p.reddit_id,
          p.source_type,
          p.subreddit,
          p.created_iso,
          p.url,
          p.title,
          p.full_text,
          p.processed_full_text,
          run.status AS screening_status
        FROM extracted_reports r
        JOIN raw_posts p ON p.post_id = r.post_id
        LEFT JOIN side_effect_screening_runs run ON run.report_id = r.report_id
        WHERE r.canonical = 1
          AND p.parse_status = 'parsed'
          AND r.drug_family IN ('reta', 'tirz', 'sema')
          AND r.side_effects IS NOT NULL
          AND r.side_effects NOT IN ('[]', 'null', '')
          AND (
            run.report_id IS NULL
            OR (? = 1 AND run.status = 'error')
          )
        ORDER BY COALESCE(p.created_iso, '') DESC, r.report_id DESC
    """
    params: list[Any] = [1 if retry_errors else 0]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(query, params).fetchall())


def build_user_input(row: sqlite3.Row, effects: list[str]) -> str:
    full_text = row["processed_full_text"] or row["full_text"] or ""
    return (
        "Screen this single extracted Reddit report only.\n\n"
        f"report_id: {row['report_id']}\n"
        f"post_id: {row['post_id']}\n"
        f"source_type: {row['source_type']}\n"
        f"subreddit: r/{row['subreddit']}\n"
        f"created_iso: {row['created_iso']}\n"
        f"url: {row['url']}\n"
        f"drug_family: {row['drug_family']}\n"
        f"drug_name_mentioned: {row['drug_name_mentioned']}\n"
        f"attribution: {row['attribution']}\n"
        f"dose: {row['dose_strong']}\n"
        f"side_effects_to_screen: {json.dumps(effects, ensure_ascii=False)}\n"
        f"extraction_evidence: {row['evidence']}\n"
        f"extraction_notes: {row['notes']}\n\n"
        "full_reddit_text:\n"
        f"{full_text}"
    )


def validate_result(result: dict[str, Any], expected_effects: list[str]) -> None:
    if not isinstance(result, dict):
        raise ValueError("root is not an object")
    screenings = result.get("screenings")
    if not isinstance(screenings, list):
        raise ValueError("screenings is not an array")
    expected = set(expected_effects)
    seen: set[str] = set()
    for index, item in enumerate(screenings):
        if not isinstance(item, dict):
            raise ValueError(f"screenings[{index}] is not an object")
        missing = {"side_effect", "severity", "confidence", "evidence", "rationale"} - set(item)
        if missing:
            raise ValueError(f"screenings[{index}] missing fields: {', '.join(sorted(missing))}")
        effect = item["side_effect"]
        if effect not in expected:
            raise ValueError(f"screenings[{index}] unexpected side_effect: {effect}")
        if effect in seen:
            raise ValueError(f"screenings[{index}] duplicate side_effect: {effect}")
        seen.add(effect)
        if item["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"screenings[{index}] invalid severity")
        confidence = item["confidence"]
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            raise ValueError(f"screenings[{index}] confidence is outside 0..1")
    missing_effects = expected - seen
    if missing_effects:
        raise ValueError(f"missing screenings for: {', '.join(sorted(missing_effects))}")


def save_run(
    conn: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    model: str,
    cache_key: str | None,
    status: str,
    result: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = utc_now_iso()
    input_tokens, output_tokens, cached_prompt_tokens, usage_json = usage_fields(usage)
    conn.execute(
        """
        INSERT INTO side_effect_screening_runs (
          report_id, post_id, content_hash, model, prompt_version, prompt_cache_key, status,
          result_json, input_tokens, output_tokens, cached_prompt_tokens, usage_json,
          error, screened_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_id) DO UPDATE SET
          content_hash = excluded.content_hash,
          model = excluded.model,
          prompt_version = excluded.prompt_version,
          prompt_cache_key = excluded.prompt_cache_key,
          status = excluded.status,
          result_json = excluded.result_json,
          input_tokens = excluded.input_tokens,
          output_tokens = excluded.output_tokens,
          cached_prompt_tokens = excluded.cached_prompt_tokens,
          usage_json = excluded.usage_json,
          error = excluded.error,
          screened_at = excluded.screened_at,
          updated_at = excluded.updated_at
        """,
        (
            row["report_id"],
            row["post_id"],
            row["content_hash"],
            model,
            SIDE_EFFECT_PROMPT_VERSION,
            cache_key,
            status,
            json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
            input_tokens,
            output_tokens,
            cached_prompt_tokens,
            usage_json,
            error[:4000] if error else None,
            now if status == "parsed" else None,
            now,
            now,
        ),
    )


def save_screenings(conn: sqlite3.Connection, row: sqlite3.Row, model: str, result: dict[str, Any]) -> None:
    now = utc_now_iso()
    conn.execute("DELETE FROM side_effect_screenings WHERE report_id = ?", (row["report_id"],))
    for item in result["screenings"]:
        conn.execute(
            """
            INSERT INTO side_effect_screenings (
              report_id, post_id, content_hash, side_effect_phrase, severity, confidence,
              evidence, rationale, model, screened_at, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["report_id"],
                row["post_id"],
                row["content_hash"],
                item["side_effect"],
                item["severity"],
                float(item["confidence"]),
                item.get("evidence"),
                item.get("rationale"),
                model,
                now,
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            ),
        )


def process_report(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    mapping: dict[str, str],
    prompt: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    effects = canonical_side_effects(row_json(row, "side_effects", []), mapping)
    if not effects:
        if not args.dry_run:
            save_run(conn, row=row, model=args.model, cache_key=None, status="no_effects")
            conn.commit()
        return {"report_id": row["report_id"], "status": "no_effects"}
    print(f"screen: report_id={row['report_id']} post_id={row['post_id']} effects={len(effects)}")
    if args.dry_run:
        return {"report_id": row["report_id"], "status": "dry-run", "effects": effects}

    cache_key = prompt_cache_key(args.model, args.prompt_cache_key)
    try:
        result, usage = call_openai(
            model=args.model,
            prompt=prompt,
            user_input=build_user_input(row, effects),
            max_output_tokens=args.max_output_tokens,
            cache_key=cache_key,
            prompt_cache_retention=args.prompt_cache_retention,
        )
        validate_result(result, effects)
    except Exception as exc:  # noqa: BLE001 - persisted for audit.
        error = str(exc)
        save_run(conn, row=row, model=args.model, cache_key=cache_key, status="error", error=error)
        conn.commit()
        return {"report_id": row["report_id"], "status": "error", "error": error}

    save_screenings(conn, row, args.model, result)
    save_run(conn, row=row, model=args.model, cache_key=cache_key, status="parsed", result=result, usage=usage)
    conn.commit()
    return {"report_id": row["report_id"], "status": "parsed", "effects": len(effects)}


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    conn = connect_db(args.db)
    ensure_schema(conn)
    mapping = load_side_effect_normalization()
    prompt = load_prompt()
    rows = select_reports(conn, args.limit, args.retry_errors)
    if not rows:
        print("no side-effect reports pending screening")
        conn.close()
        return 0
    results = []
    try:
        for row in rows:
            if args.max_runtime_seconds and time.monotonic() - started > args.max_runtime_seconds:
                print("stopping side-effect screening: max runtime reached", file=sys.stderr)
                break
            try:
                results.append(process_report(conn, row, mapping=mapping, prompt=prompt, args=args))
            except TransientOpenAIError as exc:
                print(f"stopping side-effect screening: {exc}", file=sys.stderr)
                break
    finally:
        conn.close()
    print(json.dumps({"processed": len(results), "results": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
