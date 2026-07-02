#!/usr/bin/env python3
"""Build the static GitHub Pages site from SQLite."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import shutil
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from glp1_common import (
    DEFAULT_DB,
    DRUG_FAMILIES,
    ROOT,
    canonical_side_effects,
    connect_db,
    ensure_schema,
    load_side_effect_normalization,
    read_json,
    row_json,
    utc_now_iso,
    write_json,
)

FAMILY_NAMES = {
    "reta": "Retatrutide",
    "tirz": "Tirzepatide",
    "sema": "Semaglutide",
}

DOSE_MG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mg\b", re.IGNORECASE)

FALLBACK_FAMILY_COMPOUNDS = {
    "reta": {"canonical_name": "retatrutide", "family": "reta", "confidence": 1.0, "source": "drug_family"},
    "tirz": {"canonical_name": "tirzepatide", "family": "tirz", "confidence": 1.0, "source": "drug_family"},
    "sema": {"canonical_name": "semaglutide", "family": "sema", "confidence": 1.0, "source": "drug_family"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--site-dir", type=Path, default=ROOT / "site", help="Output directory")
    parser.add_argument("--dry-run", action="store_true", help="Read and summarize without writing")
    return parser.parse_args()


def fmt_number(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def median(values: list[float]) -> float | None:
    cleaned = [value for value in values if value is not None and math.isfinite(value)]
    if not cleaned:
        return None
    return float(statistics.median(cleaned))


def load_reports(conn) -> list[Any]:
    return list(
        conn.execute(
            """
            SELECT
              r.*,
              p.reddit_id,
              p.source_type,
              p.subreddit,
              p.thread_id,
              p.created_utc,
              p.created_iso,
              p.title,
              p.body,
              p.full_text,
              p.processed_full_text,
              p.url,
              p.matched_drug_families,
              p.matched_terms,
              p.processed_content_hash,
              p.content_changed_after_processing,
              p.parsed_model,
              p.parsed_at,
              p.rescreen_status,
              p.rescreen_model,
              p.rescreened_at
            FROM extracted_reports r
            JOIN raw_posts p ON p.post_id = r.post_id
            WHERE r.canonical = 1
              AND r.drug_family IN ('reta', 'tirz', 'sema')
              AND p.parse_status = 'parsed'
            ORDER BY p.created_utc DESC, r.report_id DESC
            """
        ).fetchall()
    )


def load_parsed_post_counts(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT r.drug_family, COUNT(DISTINCT r.post_id) AS n
        FROM extracted_reports r
        JOIN raw_posts p ON p.post_id = r.post_id
        WHERE r.canonical = 1
          AND p.parse_status = 'parsed'
          AND r.drug_family IN ('reta', 'tirz', 'sema')
        GROUP BY r.drug_family
        """
    ).fetchall()
    return {row["drug_family"]: int(row["n"]) for row in rows}


def report_point(row: Any) -> dict[str, Any]:
    side_effects = row_json(row, "side_effects", [])
    other_compounds = row_json(row, "other_compounds_concurrent", [])
    return {
        "report_id": row["report_id"],
        "post_id": row["post_id"],
        "reddit_id": row["reddit_id"],
        "source_type": row["source_type"],
        "subreddit": row["subreddit"],
        "created_iso": row["created_iso"],
        "url": row["url"],
        "title": row["title"],
        "full_text": row["full_text"],
        "processed_full_text": row["processed_full_text"] or row["full_text"],
        "processed_content_hash": row["processed_content_hash"],
        "content_changed_after_processing": bool(row["content_changed_after_processing"]),
        "drug_family": row["drug_family"],
        "drug_name_mentioned": row["drug_name_mentioned"],
        "use_status": row["use_status"],
        "attribution": row["attribution"],
        "duration_raw": row["duration_raw"],
        "duration_weeks": row["duration_weeks"],
        "duration_days": row["duration_days"],
        "weight_change_kg": row["weight_change_kg"],
        "weight_start_value": row["weight_start_value"],
        "weight_start_unit": row["weight_start_unit"],
        "weight_start_kg": row["weight_start_kg"],
        "weight_end_value": row["weight_end_value"],
        "weight_end_unit": row["weight_end_unit"],
        "weight_end_kg": row["weight_end_kg"],
        "weight_lost_value": row["weight_lost_value"],
        "weight_lost_unit": row["weight_lost_unit"],
        "weight_lost_kg": row["weight_lost_kg"],
        "weight_goal_value": row["weight_goal_value"],
        "weight_goal_unit": row["weight_goal_unit"],
        "weight_goal_kg": row["weight_goal_kg"],
        "dose_strong": row["dose_strong"],
        "dose_current_mg": row["dose_current_mg"],
        "interval_per_week_value": row["interval_per_week_value"],
        "gender": row["gender"],
        "age_value": row["age_value"],
        "other_compounds_concurrent": other_compounds,
        "side_effects": side_effects,
        "side_effects_semicolon": row["side_effects_semicolon"],
        "confidence": row["confidence"],
        "evidence": row["evidence"],
        "notes": row["notes"],
        "parsed_model": row["parsed_model"],
        "parsed_at": row["parsed_at"],
        "rescreen_status": row["rescreen_status"],
        "rescreen_model": row["rescreen_model"],
        "rescreened_at": row["rescreened_at"],
    }


def is_plottable(row: Any) -> bool:
    return (
        bool(row["include_in_plots"])
        and row["duration_days"] is not None
        and row["duration_days"] >= 21
        and row["duration_weeks"] is not None
        and row["weight_change_kg"] is not None
        and math.isfinite(float(row["duration_weeks"]))
        and math.isfinite(float(row["weight_change_kg"]))
    )


def smoothed_curve(points: list[dict[str, Any]]) -> list[dict[str, float]]:
    pairs = sorted(
        (float(point["duration_weeks"]), float(point["weight_change_kg"]))
        for point in points
        if point.get("duration_weeks") is not None and point.get("weight_change_kg") is not None
    )
    n = len(pairs)
    if n < 3:
        return []
    min_x, max_x = pairs[0][0], pairs[-1][0]
    if min_x == max_x:
        return []
    grid_n = min(60, max(12, n * 2))
    window = min(n, max(5, math.ceil(n * 0.35)))
    raw_curve: list[tuple[float, float]] = []
    for i in range(grid_n):
        x = min_x + (max_x - min_x) * i / (grid_n - 1)
        nearest = sorted(pairs, key=lambda pair: abs(pair[0] - x))[:window]
        y = statistics.median([pair[1] for pair in nearest])
        raw_curve.append((x, float(y)))
    smoothed: list[dict[str, float]] = []
    for i, (x, y) in enumerate(raw_curve):
        neighbors = raw_curve[max(0, i - 1) : min(len(raw_curve), i + 2)]
        y_smooth = sum(pair[1] for pair in neighbors) / len(neighbors)
        smoothed.append({"weeks": round(x, 4), "weight_change_kg": round(y_smooth, 4)})
    return smoothed


def parse_primary_dose_mg(label: str) -> float | None:
    match = DOSE_MG_RE.search(label)
    if not match:
        return None
    return float(match.group(1))


def keep_highest_dose_series(
    series_rows: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    if len(series_rows) <= 1:
        return series_rows, []
    dose_values = {label: parse_primary_dose_mg(label) for label in series_rows}
    if any(value is None for value in dose_values.values()):
        return series_rows, []
    max_dose = max(value for value in dose_values.values() if value is not None)
    kept = {
        label: rows
        for label, rows in series_rows.items()
        if dose_values[label] == max_dose
    }
    dropped = [label for label in series_rows if label not in kept]
    return kept, dropped


def load_trial_overlay(family: str) -> dict[str, Any] | None:
    path = ROOT / "trial-data" / f"trial-{family}.csv"
    if not path.exists():
        return None
    series_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"weeks", "loss_kg", "sd_loss_kg"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            dose = (row.get("dose") or row.get("arm") or "Trial").strip() or "Trial"
            weeks = float(row["weeks"])
            loss_kg = float(row["loss_kg"])
            sd_loss_kg = float(row["sd_loss_kg"])
            mean = -abs(loss_kg)
            point: dict[str, Any] = {
                "dose": dose,
                "weeks": weeks,
                "mean": mean,
                "lower": mean - 1.96 * sd_loss_kg,
                "upper": mean + 1.96 * sd_loss_kg,
                "loss_kg": loss_kg,
                "sd_loss_kg": sd_loss_kg,
            }
            for optional in (
                "n",
                "percent_change",
                "se_percent",
                "baseline_weight_kg",
                "body_weight_kg",
                "ci95_half_width_kg",
                "se_kg",
                "sd_weight_kg",
                "change_sd_correlation",
                "pixel_y_mean",
                "pixel_y_top",
                "pixel_y_bottom",
                "source",
                "source_url",
                "method",
                "n_assumption",
            ):
                value = (row.get(optional) or "").strip()
                if not value:
                    continue
                if optional in {"source", "source_url", "method", "n_assumption"}:
                    point[optional] = value
                else:
                    point[optional] = float(value)
            series_rows[dose].append(point)
    series_rows, dropped_series = keep_highest_dose_series(series_rows)
    series: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for label, label_rows in series_rows.items():
        label_rows.sort(key=lambda item: item["weeks"])
        rows.extend(label_rows)
        series.append({"label": label, "rows": label_rows})
    rows.sort(key=lambda item: (str(item.get("dose") or ""), item["weeks"]))
    return {
        "present": True,
        "source_file": f"trial-data/trial-{family}.csv",
        "note": "Trial overlay is external uploaded aggregate data, not mined Reddit data.",
        "series": series,
        "rows": rows,
        "dose_filter": {
            "mode": "highest_mg_only" if dropped_series else "none",
            "dropped_series": dropped_series,
        },
    }


def side_effect_counts(rows: list[Any], mapping: dict[str, str]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        effects = canonical_side_effects(row_json(row, "side_effects", []), mapping)
        counter.update(effects)
    return [{"phrase": phrase, "count": count} for phrase, count in counter.most_common()]


def normalize_compound_key(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower().replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^[\"'`]+|[\"'`]+$", "", value)
    return value.strip()


def split_compound_candidate(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    pieces = re.split(r"\s*(?:,|;|\+|&|\band\b|\bwith\b|\bw\/\b)\s*", text, flags=re.IGNORECASE)
    cleaned = [piece.strip(" .()[]{}") for piece in pieces if piece.strip(" .()[]{}")]
    return cleaned or [text]


def load_compound_aliases() -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    path = ROOT / "config" / "compound_normalization.json"
    if not path.exists():
        return {}, set()
    config = read_json(path)
    aliases: dict[str, list[dict[str, Any]]] = {}
    for item in config.get("aliases", []):
        compounds = [
            {
                "canonical_name": name,
                "family": item.get("family") or "unclear",
                "confidence": 1.0,
                "source": "alias",
            }
            for name in item.get("canonical_names", [])
        ]
        for alias in item.get("aliases", []):
            key = normalize_compound_key(alias)
            if key:
                aliases[key] = compounds
    ignore = {normalize_compound_key(value) for value in config.get("ignore_terms", []) if normalize_compound_key(value)}
    return aliases, ignore


def load_compound_normalization() -> dict[str, Any]:
    aliases, ignore = load_compound_aliases()
    exact: dict[str, list[dict[str, Any]]] = {}
    normalized: dict[str, list[dict[str, Any]]] = dict(aliases)
    stats: dict[str, Any] = {"source": "alias_only", "raw_names": 0, "unresolved_remaining": None}
    cache_path = ROOT / "data" / "compound_normalizations.json"
    if cache_path.exists():
        cache = read_json(cache_path)
        stats = cache.get("stats", stats)
        stats["source"] = "data/compound_normalizations.json"
        for raw, item in cache.get("items", {}).items():
            compounds = []
            for compound in item.get("compounds", []):
                canonical = str(compound.get("canonical_name") or "").strip()
                if not canonical:
                    continue
                compounds.append(
                    {
                        "canonical_name": canonical,
                        "family": compound.get("family") or "unclear",
                        "confidence": compound.get("confidence"),
                        "source": compound.get("source") or item.get("source") or "normalization_cache",
                    }
                )
            exact[raw] = compounds
            normalized[normalize_compound_key(raw)] = compounds
    return {"exact": exact, "normalized": normalized, "ignore": ignore, "stats": stats}


def normalize_compound(raw: str, normalization: dict[str, Any]) -> list[dict[str, Any]]:
    if not raw:
        return []
    key = normalize_compound_key(raw)
    if not key or key in normalization["ignore"]:
        return []
    exact = normalization["exact"].get(raw)
    if exact is not None:
        return [dict(item) for item in exact]
    compounds = normalization["normalized"].get(key)
    if compounds is not None:
        return [dict(item) for item in compounds]

    split_compounds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for piece in split_compound_candidate(raw):
        piece_key = normalize_compound_key(piece)
        if not piece_key or piece_key in normalization["ignore"]:
            continue
        mapped = normalization["normalized"].get(piece_key)
        if mapped is None:
            return []
        for compound in mapped:
            canonical = compound["canonical_name"]
            if canonical in seen:
                continue
            seen.add(canonical)
            split_compounds.append(dict(compound))
    return split_compounds


def focal_compounds(row: Any, normalization: dict[str, Any]) -> list[dict[str, Any]]:
    compounds = normalize_compound(row["drug_name_mentioned"] or "", normalization)
    if compounds:
        return compounds
    fallback = FALLBACK_FAMILY_COMPOUNDS.get(row["drug_family"])
    return [dict(fallback)] if fallback else []


def text_excerpt(row: Any, limit: int = 520) -> str:
    text = row["processed_full_text"] or row["full_text"] or ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def build_concurrent_payload(rows: list[Any], generated_at: str) -> dict[str, Any]:
    normalization = load_compound_normalization()
    nodes: dict[str, dict[str, Any]] = {}
    link_map: dict[tuple[str, str], dict[str, Any]] = {}
    report_lookup: dict[int, dict[str, Any]] = {}
    unresolved_counter: Counter[str] = Counter()

    def add_node(compound: dict[str, Any], report_id: int, is_stack: bool) -> None:
        name = compound["canonical_name"]
        node = nodes.setdefault(
            name,
            {
                "id": name,
                "label": name,
                "family": compound.get("family") or "unclear",
                "count": 0,
                "stack_count": 0,
                "report_ids": [],
                "stack_report_ids": [],
            },
        )
        node["count"] += 1
        node["report_ids"].append(report_id)
        if is_stack:
            node["stack_count"] += 1
            node["stack_report_ids"].append(report_id)

    def add_link(source: str, target: str, report_id: int, is_stack: bool, attribution: str) -> None:
        a, b = sorted((source, target))
        link = link_map.setdefault(
            (a, b),
            {
                "source": a,
                "target": b,
                "count": 0,
                "stack_count": 0,
                "report_ids": [],
                "stack_report_ids": [],
                "attribution_counts": {},
            },
        )
        link["count"] += 1
        link["report_ids"].append(report_id)
        link["attribution_counts"][attribution] = link["attribution_counts"].get(attribution, 0) + 1
        if is_stack:
            link["stack_count"] += 1
            link["stack_report_ids"].append(report_id)

    for row in rows:
        report_id = int(row["report_id"])
        raw_others = [str(value).strip() for value in row_json(row, "other_compounds_concurrent", []) if str(value).strip()]
        if not raw_others:
            continue
        compounds: list[dict[str, Any]] = []
        for compound in focal_compounds(row, normalization):
            compounds.append(compound)
        for raw in raw_others:
            normalized = normalize_compound(raw, normalization)
            if not normalized:
                unresolved_counter[raw] += 1
            compounds.extend(normalized)

        unique: dict[str, dict[str, Any]] = {}
        for compound in compounds:
            canonical = str(compound.get("canonical_name") or "").strip()
            if canonical:
                unique.setdefault(canonical, {**compound, "canonical_name": canonical})
        if len(unique) < 2:
            continue

        is_stack = row["attribution"] == "stack"
        compound_names = sorted(unique)
        for compound in unique.values():
            add_node(compound, report_id, is_stack)
        for i, source in enumerate(compound_names):
            for target in compound_names[i + 1 :]:
                add_link(source, target, report_id, is_stack, row["attribution"] or "unclear")

        report_lookup[report_id] = {
            "report_id": report_id,
            "post_id": row["post_id"],
            "created_iso": row["created_iso"],
            "subreddit": row["subreddit"],
            "url": row["url"],
            "title": row["title"],
            "drug_family": row["drug_family"],
            "drug_name_mentioned": row["drug_name_mentioned"],
            "focal_compounds": [compound["canonical_name"] for compound in focal_compounds(row, normalization)],
            "other_compounds_raw": raw_others,
            "compounds": compound_names,
            "attribution": row["attribution"],
            "use_status": row["use_status"],
            "dose_strong": row["dose_strong"],
            "duration_raw": row["duration_raw"],
            "duration_weeks": row["duration_weeks"],
            "weight_change_kg": row["weight_change_kg"],
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "notes": row["notes"],
            "text_excerpt": text_excerpt(row),
        }

    node_list = sorted(nodes.values(), key=lambda item: (-item["count"], item["label"]))
    link_list = sorted(link_map.values(), key=lambda item: (-item["count"], item["source"], item["target"]))
    for node in node_list:
        node["report_ids"] = sorted(set(node["report_ids"]))
        node["stack_report_ids"] = sorted(set(node["stack_report_ids"]))
    for link in link_list:
        link["report_ids"] = sorted(set(link["report_ids"]))
        link["stack_report_ids"] = sorted(set(link["stack_report_ids"]))

    return {
        "generated_at": generated_at,
        "nodes": node_list,
        "links": link_list,
        "reports": report_lookup,
        "normalization": {
            "stats": normalization["stats"],
            "unresolved_terms": [
                {"raw": raw, "count": count}
                for raw, count in unresolved_counter.most_common(40)
            ],
        },
        "summary": {
            "nodes": len(node_list),
            "links": len(link_list),
            "reports": len(report_lookup),
            "stack_reports": len([report for report in report_lookup.values() if report["attribution"] == "stack"]),
        },
    }


def copy_assets(site_dir: Path) -> None:
    assets_dir = site_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for asset in ("styles.css", "app.js"):
        shutil.copy2(ROOT / "static" / asset, assets_dir / asset)


def html_page(title: str, body: str, asset_prefix: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="{asset_prefix}assets/styles.css">
</head>
{body}
</html>
"""


def render_home(summary: dict[str, Any], generated_at: str) -> str:
    cards = []
    for family in DRUG_FAMILIES:
        item = summary["families"][family]
        effects = ", ".join(effect["phrase"] for effect in item["most_common_side_effects"][:3]) or "n/a"
        cards.append(
            f"""
      <a class="summary-card family-{family}" href="{family}/">
        <span class="card-kicker">{html.escape(FAMILY_NAMES[family])}</span>
        <strong>{item["plottable_reports"]}</strong>
        <span>plottable reports</span>
        <dl>
          <div><dt>Parsed posts</dt><dd>{item["parsed_posts"]}</dd></div>
          <div><dt>Median duration</dt><dd>{fmt_number(item["median_duration_weeks"])} weeks</dd></div>
          <div><dt>Median change</dt><dd>{fmt_number(item["median_weight_change_kg"])} kg</dd></div>
          <div><dt>Common effects</dt><dd>{html.escape(effects)}</dd></div>
        </dl>
      </a>
"""
        )
    body = f"""
<body>
  <header class="site-header">
    <nav class="nav">
      <a href="./" class="brand">GLP-1 Reddit Reports</a>
      <a href="reta/">Reta</a>
      <a href="tirz/">Tirz</a>
      <a href="sema/">Sema</a>
      <a href="concurrent/">Concurrent use</a>
    </nav>
  </header>
  <main class="home">
    <section class="intro-band">
      <div class="intro-copy">
        <h1>Self-updating Reddit text mining for GLP-1/GIP/glucagon user reports</h1>
        <p>This static site summarizes structured extractions from Reddit posts and comments mentioning retatrutide, tirzepatide, and semaglutide families. It is observational social-media text mining, not medical advice, clinical evidence, or proof of causality.</p>
        <p class="meta">Generated {html.escape(generated_at)}. Reddit reports are parsed one post/comment per LLM call, cached by content hash, and rescreened when large extracted values are flagged.</p>
      </div>
    </section>
    <section class="summary-grid" aria-label="Drug family summaries">
      {''.join(cards)}
    </section>
    <section class="link-band">
      <h2>Pages</h2>
      <ul class="page-links">
        <li><a href="reta/">Retatrutide scatterplot</a> <a href="reta/side-effects.html">side effects</a></li>
        <li><a href="tirz/">Tirzepatide scatterplot</a> <a href="tirz/side-effects.html">side effects</a></li>
        <li><a href="sema/">Semaglutide scatterplot</a> <a href="sema/side-effects.html">side effects</a></li>
        <li><a href="concurrent/">Concurrent-use network</a></li>
      </ul>
    </section>
  </main>
</body>
"""
    return html_page("GLP-1 Reddit Reports", body)


def render_scatter_page(family: str, generated_at: str, has_rct: bool) -> str:
    name = FAMILY_NAMES[family]
    rct_note = (
        '<p class="note">Trial overlay is external uploaded aggregate data, not mined Reddit data.</p>'
        if has_rct
        else ""
    )
    body = f"""
<body data-view="scatter" data-family="{family}" data-json="../data/{family}.json">
  <header class="site-header">
    <nav class="nav">
      <a href="../" class="brand">GLP-1 Reddit Reports</a>
      <a href="../reta/">Reta</a>
      <a href="../tirz/">Tirz</a>
      <a href="../sema/">Sema</a>
      <a href="../concurrent/">Concurrent use</a>
      <a href="side-effects.html">Side effects</a>
    </nav>
  </header>
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">{html.escape(name)}</p>
      <h1>Weight change over time</h1>
      <p>Scatterplot of mined Reddit user reports with duration of at least 21 days. Weight loss is plotted as negative weight change in kg.</p>
      {rct_note}
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="plot-layout">
      <div class="plot-area">
        <div id="plot-status" class="status">Loading reports...</div>
        <svg id="scatterplot" class="scatterplot" role="img" aria-label="{html.escape(name)} Reddit report scatterplot"></svg>
      </div>
      <aside id="detail" class="detail-panel" aria-live="polite">
        <h2>Report detail</h2>
        <p>Select a point to inspect the original Reddit text and extracted fields.</p>
      </aside>
    </section>
  </main>
  <script src="../assets/app.js"></script>
</body>
"""
    return html_page(f"{name} Reddit Reports", body, asset_prefix="../")


def render_side_effect_page(family: str, generated_at: str) -> str:
    name = FAMILY_NAMES[family]
    body = f"""
<body data-view="side-effects" data-family="{family}" data-json="../data/{family}.json">
  <header class="site-header">
    <nav class="nav">
      <a href="../" class="brand">GLP-1 Reddit Reports</a>
      <a href="../reta/">Reta</a>
      <a href="../tirz/">Tirz</a>
      <a href="../sema/">Sema</a>
      <a href="../concurrent/">Concurrent use</a>
      <a href="./">Scatterplot</a>
    </nav>
  </header>
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">{html.escape(name)}</p>
      <h1>Side-effect mentions</h1>
      <p>Frequency counts use explicit, auditable phrase normalization from <code>config/side_effect_normalization.json</code>.</p>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="effects-layout">
      <div>
        <h2>Frequency</h2>
        <div id="effect-bars" class="effect-bars"></div>
      </div>
      <div>
        <h2>Phrase cloud</h2>
        <div id="effect-cloud" class="effect-cloud"></div>
      </div>
    </section>
    <section class="table-section">
      <h2>Counts</h2>
      <table>
        <thead><tr><th>Phrase</th><th>Count</th></tr></thead>
        <tbody id="effect-table"></tbody>
      </table>
    </section>
  </main>
  <script src="../assets/app.js"></script>
</body>
"""
    return html_page(f"{name} Side Effects", body, asset_prefix="../")


def render_concurrent_page(generated_at: str) -> str:
    body = f"""
<body data-view="concurrent" data-json="../data/concurrent.json">
  <header class="site-header">
    <nav class="nav">
      <a href="../" class="brand">GLP-1 Reddit Reports</a>
      <a href="../reta/">Reta</a>
      <a href="../tirz/">Tirz</a>
      <a href="../sema/">Sema</a>
    </nav>
  </header>
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">All drug families</p>
      <h1>Concurrent-use network</h1>
      <p>Circular network of normalized compounds mentioned together in parsed Reddit reports. Edges connect compounds appearing in the same extracted report; stack-only mode restricts counts to reports marked as stacks by the parser.</p>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="network-controls" aria-label="Network controls">
      <button type="button" class="segmented active" data-network-mode="all">All concurrent mentions</button>
      <button type="button" class="segmented" data-network-mode="stack">Stack attribution only</button>
    </section>
    <section class="network-layout">
      <div class="network-area">
        <div id="network-status" class="status">Loading network...</div>
        <svg id="compound-network" class="compound-network" role="img" aria-label="Concurrent compound network"></svg>
      </div>
      <aside id="network-detail" class="detail-panel network-detail" aria-live="polite">
        <h2>Connection detail</h2>
        <p>Select an edge or compound to inspect contributing reports.</p>
      </aside>
    </section>
    <section class="table-section">
      <h2>Normalization audit</h2>
      <p>Compound names use <code>config/compound_normalization.json</code> plus optional cached nano normalization in <code>data/compound_normalizations.json</code>.</p>
      <div id="normalization-audit" class="audit-grid"></div>
    </section>
  </main>
  <script src="../assets/app.js"></script>
</body>
"""
    return html_page("Concurrent GLP-1 Use Network", body, asset_prefix="../")


def build_site(db_path: Path, site_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    conn = connect_db(db_path)
    ensure_schema(conn)
    reports = load_reports(conn)
    parsed_counts = load_parsed_post_counts(conn)
    conn.close()

    mapping = load_side_effect_normalization()
    reports_by_family: dict[str, list[Any]] = defaultdict(list)
    for report in reports:
        reports_by_family[report["drug_family"]].append(report)

    generated_at = utc_now_iso()
    summary = {"generated_at": generated_at, "families": {}}
    family_payloads: dict[str, dict[str, Any]] = {}
    concurrent_payload = build_concurrent_payload(reports, generated_at)

    for family in DRUG_FAMILIES:
        rows = reports_by_family.get(family, [])
        points = [report_point(row) for row in rows if is_plottable(row)]
        effects = side_effect_counts(rows, mapping)
        rct = load_trial_overlay(family)
        payload = {
            "generated_at": generated_at,
            "family": family,
            "family_name": FAMILY_NAMES[family],
            "points": points,
            "curve": smoothed_curve(points),
            "side_effects": effects,
            "side_effect_normalization": mapping,
            "rct": rct or {"present": False, "rows": []},
        }
        family_payloads[family] = payload
        summary["families"][family] = {
            "name": FAMILY_NAMES[family],
            "parsed_posts": parsed_counts.get(family, 0),
            "plottable_reports": len(points),
            "median_duration_weeks": median([point["duration_weeks"] for point in points]),
            "median_weight_change_kg": median([point["weight_change_kg"] for point in points]),
            "most_common_side_effects": effects[:5],
        }

    if dry_run:
        return {**summary, "concurrent": concurrent_payload["summary"]}

    if site_dir.exists():
        shutil.rmtree(site_dir)
    (site_dir / "data").mkdir(parents=True, exist_ok=True)
    copy_assets(site_dir)
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    write_json(site_dir / "data" / "summary.json", summary)
    write_json(site_dir / "data" / "concurrent.json", concurrent_payload)
    for family, payload in family_payloads.items():
        write_json(site_dir / "data" / f"{family}.json", payload)
        family_dir = site_dir / family
        family_dir.mkdir(parents=True, exist_ok=True)
        (family_dir / "index.html").write_text(
            render_scatter_page(family, generated_at, bool(payload["rct"].get("present"))),
            encoding="utf-8",
        )
        (family_dir / "side-effects.html").write_text(
            render_side_effect_page(family, generated_at),
            encoding="utf-8",
        )
    concurrent_dir = site_dir / "concurrent"
    concurrent_dir.mkdir(parents=True, exist_ok=True)
    (concurrent_dir / "index.html").write_text(render_concurrent_page(generated_at), encoding="utf-8")
    (site_dir / "index.html").write_text(render_home(summary, generated_at), encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    summary = build_site(args.db, args.site_dir, args.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
