#!/usr/bin/env python3
"""Normalize extracted concurrent compound names for the site network page."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from glp1_common import DEFAULT_DB, ROOT, connect_db, ensure_schema, read_json, row_json, utc_now_iso, write_json

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-nano"
NORMALIZATION_VERSION = "2026-07-03-one-by-one-v1"
VALID_FAMILIES = {
    "reta",
    "tirz",
    "sema",
    "amylin",
    "glp1_other",
    "stimulant",
    "diabetes_drug",
    "hormone",
    "peptide",
    "supplement",
    "other_drug",
    "lifestyle",
    "unclear",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "compound_normalizations.json",
        help="Normalization cache JSON path",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model for unresolved names")
    parser.add_argument("--limit", type=int, default=0, help="Maximum unresolved raw names to send; 0 means no limit")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1,
        help="Deprecated compatibility flag; normalization now sends exactly one raw name per API call",
    )
    parser.add_argument("--dry-run", action="store_true", help="Summarize work without writing output")
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Use explicit aliases only; do not call OpenAI for unresolved names",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=1000,
        help="OpenAI max output tokens per one-name normalization call",
    )
    parser.add_argument(
        "--prompt-cache-retention",
        choices=("default", "in_memory", "24h"),
        default="24h",
        help="OpenAI prompt cache retention. Use 'default' to omit the parameter.",
    )
    return parser.parse_args()


def norm_key(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^[\"'`]+|[\"'`]+$", "", value)
    return value.strip()


def split_candidate(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    pieces = re.split(r"\s*(?:,|;|\+|&|\band\b|\bwith\b|\bw\/\b)\s*", text, flags=re.IGNORECASE)
    cleaned = [piece.strip(" .()[]{}") for piece in pieces if piece.strip(" .()[]{}")]
    return cleaned or [text]


def load_alias_config() -> dict[str, Any]:
    return read_json(ROOT / "config" / "compound_normalization.json")


def alias_lookup(config: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    lookup: dict[str, list[dict[str, Any]]] = {}
    for item in config.get("aliases", []):
        compounds = [
            {
                "canonical_name": name,
                "family": item.get("family") or "unclear",
                "confidence": 1.0,
                "note": "explicit alias map",
                "source": "alias",
            }
            for name in item.get("canonical_names", [])
        ]
        for alias in item.get("aliases", []):
            key = norm_key(alias)
            if key:
                lookup[key] = compounds
    ignore = {norm_key(value) for value in config.get("ignore_terms", []) if norm_key(value)}
    return lookup, ignore


def normalize_with_aliases(raw: str, lookup: dict[str, list[dict[str, Any]]], ignore: set[str]) -> list[dict[str, Any]] | None:
    key = norm_key(raw)
    if not key or key in ignore:
        return []
    compounds: list[dict[str, Any]] = []
    seen: set[str] = set()
    matched_all = True
    for piece in split_candidate(raw):
        piece_key = norm_key(piece)
        if not piece_key or piece_key in ignore:
            continue
        mapped = lookup.get(piece_key)
        if not mapped:
            matched_all = False
            break
        for compound in mapped:
            canonical = compound["canonical_name"]
            if canonical in seen:
                continue
            seen.add(canonical)
            compounds.append(dict(compound))
    if matched_all:
        return compounds
    mapped = lookup.get(key)
    if mapped is not None:
        return [dict(compound) for compound in mapped]
    return None


def load_existing(path: Path) -> dict[str, Any]:
    if path.exists():
        return read_json(path)
    return {
        "version": NORMALIZATION_VERSION,
        "generated_at": None,
        "model": None,
        "prompt_cache_key": None,
        "items": {},
    }


def collect_raw_names(conn: sqlite3.Connection) -> Counter[str]:
    counter: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT drug_name_mentioned, other_compounds_concurrent
        FROM extracted_reports
        WHERE canonical = 1
        """
    ).fetchall()
    for row in rows:
        if row["drug_name_mentioned"]:
            counter[str(row["drug_name_mentioned"]).strip()] += 1
        for compound in row_json(row, "other_compounds_concurrent", []):
            if compound:
                counter[str(compound).strip()] += 1
    return +counter


def prompt_cache_key(model: str) -> str:
    digest = hashlib.sha256(NORMALIZATION_VERSION.encode("utf-8")).hexdigest()[:12]
    safe_model = "".join(char if char.isalnum() or char in "-_" else "-" for char in model)
    return f"glp1-compounds-{safe_model}-{digest}"[:64]


def normalization_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "raw": {"type": "string"},
            "compounds": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "canonical_name": {"type": "string"},
                        "family": {
                            "type": "string",
                            "enum": sorted(VALID_FAMILIES),
                        },
                        "confidence": {"type": "number"},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["canonical_name", "family", "confidence", "note"],
                },
            },
        },
        "required": ["raw", "compounds"],
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


def call_openai(
    *,
    model: str,
    prompt: str,
    raw_name: str,
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
            {
                "role": "user",
                "content": (
                    "Normalize this one raw string. Preserve it exactly in the raw field.\n\n"
                    f"{raw_name}"
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "glp1_compound_normalization",
                "strict": True,
                "schema": normalization_schema(),
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


def validate_result(result: dict[str, Any], requested: str) -> None:
    if not isinstance(result, dict):
        raise ValueError("normalization result must be an object")
    if result.get("raw") != requested:
        raise ValueError(f"normalization result raw mismatch: expected {requested!r}, got {result.get('raw')!r}")
    if not isinstance(result.get("compounds"), list):
        raise ValueError(f"{requested}: compounds is not an array")
    for compound in result["compounds"]:
        if not isinstance(compound, dict):
            raise ValueError(f"{requested}: compound is not an object")
        if compound.get("family") not in VALID_FAMILIES:
            raise ValueError(f"{requested}: invalid family {compound.get('family')}")
        confidence = compound.get("confidence")
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            raise ValueError(f"{requested}: invalid confidence")


def entry(raw: str, count: int, compounds: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "count": count,
        "compounds": compounds,
        "source": source,
        "updated_at": utc_now_iso(),
    }


def merge_items(existing: dict[str, Any], items: list[dict[str, Any]], counts: Counter[str], source: str) -> None:
    store = existing.setdefault("items", {})
    for item in items:
        raw = item["raw"]
        compounds = []
        seen: set[str] = set()
        for compound in item.get("compounds", []):
            canonical = str(compound.get("canonical_name") or "").strip()
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            compounds.append(
                {
                    "canonical_name": canonical,
                    "family": compound.get("family") if compound.get("family") in VALID_FAMILIES else "unclear",
                    "confidence": float(compound.get("confidence") or 0),
                    "note": compound.get("note"),
                    "source": compound.get("source") or source,
                }
            )
        store[raw] = entry(raw, counts.get(raw, 0), compounds, source)


def refresh_stats(existing: dict[str, Any], counts: Counter[str], alias_items: list[dict[str, Any]], api_items: list[dict[str, Any]]) -> None:
    store = existing.get("items", {})
    existing["stats"] = {
        "raw_names": len(counts),
        "processed_raw_names": len([raw for raw in counts if raw in store]),
        "alias_normalized": len([raw for raw in counts if store.get(raw, {}).get("source") == "alias"]),
        "openai_normalized": len([raw for raw in counts if store.get(raw, {}).get("source") == "openai"]),
        "empty_normalized": len([raw for raw in counts if raw in store and not store[raw].get("compounds")]),
        "unresolved_remaining": len([raw for raw in counts if raw not in store]),
    }


def main() -> int:
    args = parse_args()
    config = load_alias_config()
    aliases, ignore = alias_lookup(config)
    conn = connect_db(args.db)
    ensure_schema(conn)
    counts = collect_raw_names(conn)
    conn.close()

    existing = load_existing(args.output)
    existing["version"] = NORMALIZATION_VERSION
    existing["generated_at"] = utc_now_iso()
    existing["model"] = args.model
    existing["prompt_cache_key"] = prompt_cache_key(args.model)
    existing["prompt"] = "prompts/normalize_compounds.md"
    existing["alias_config"] = "config/compound_normalization.json"

    alias_items: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for raw in sorted(counts, key=lambda value: (-counts[value], norm_key(value))):
        mapped = normalize_with_aliases(raw, aliases, ignore)
        if mapped is None:
            if raw not in existing.get("items", {}):
                unresolved.append(raw)
            continue
        alias_items.append({"raw": raw, "compounds": mapped})
    merge_items(existing, alias_items, counts, "alias")

    if args.limit:
        unresolved = unresolved[: args.limit]

    api_items: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    if unresolved and not args.no_api:
        prompt = (ROOT / "prompts" / "normalize_compounds.md").read_text(encoding="utf-8")
        cache_key = prompt_cache_key(args.model)
        for index, raw in enumerate(unresolved, start=1):
            print(f"normalizing with {args.model}: {index} of {len(unresolved)}: {raw}")
            result, chunk_usage = call_openai(
                model=args.model,
                prompt=prompt,
                raw_name=raw,
                max_output_tokens=args.max_output_tokens,
                cache_key=cache_key,
                prompt_cache_retention=args.prompt_cache_retention,
            )
            validate_result(result, raw)
            api_items.append(result)
            merge_items(existing, [result], counts, "openai")
            if chunk_usage:
                usage.append(chunk_usage)
            existing["usage"] = usage
            refresh_stats(existing, counts, alias_items, api_items)
            if not args.dry_run:
                write_json(args.output, existing)
    elif unresolved:
        print(f"unresolved_without_api={len(unresolved)}")

    merge_items(existing, api_items, counts, "openai")
    existing["usage"] = usage
    refresh_stats(existing, counts, alias_items, api_items)

    print(json.dumps(existing["stats"], indent=2, sort_keys=True))
    if args.dry_run:
        return 0
    write_json(args.output, existing)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI should show concise operational failures.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
