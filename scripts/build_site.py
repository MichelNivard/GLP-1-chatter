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

FAMILY_COPY = {
    "reta": {
        "aliases": "Matched terms include retatrutide and common spelling variants.",
        "description": "Reports involving Retatrutide, including stack and switch intervals when attribution is stated.",
    },
    "tirz": {
        "aliases": "Includes Mounjaro and Zepbound when the report attributes outcomes clearly.",
        "description": "Reports involving Tirzepatide, with brand-name mentions retained in report details.",
    },
    "sema": {
        "aliases": "Includes Ozempic, Wegovy, and Rybelsus when the report attributes outcomes clearly.",
        "description": "Reports involving Semaglutide, with brand-name mentions retained in report details.",
    },
}

WEIGHT_PAGE_COPY = {
    "reta": [
        (
            "This page follows retatrutide where it surfaces in Reddit's faster, more risk-tolerant "
            "corners. The crawler looks for retatrutide, reta, and the common misspelling "
            "retaglutide across selected subreddits, then keeps the original post or comment "
            "attached to every extracted point."
        ),
        (
            "Each candidate is read one at a time by gpt-5.4-nano, which is asked to identify "
            "starting weight, current weight, duration, dose narrative, side effects, and whether "
            "the loss belongs to retatrutide itself, a stack, a switch interval, or an earlier "
            "GLP-1 history. The prompt explicitly warns the model not to treat GW as current "
            "weight, not to confuse dose units with body weight, and not to turn a user's broader "
            "journey into retatrutide loss."
        ),
    ],
    "tirz": [
        (
            "This page tracks the tirzepatide layer of Reddit's weight-loss conversation: "
            "Mounjaro and Zepbound, but also shorthand such as tirz and user language like MJ in "
            "the source text. The crawler searches the configured terms slowly and politely, "
            "stores the full Reddit text, and revisits recent material on a schedule while the "
            "historical backfill works through older posts."
        ),
        (
            "Each candidate is read one at a time by gpt-5.4-nano. The extraction asks for SW "
            "and CW when Reddit users write in abbreviations, separates current weight from goal "
            "weight, and tries not to assign prior Ozempic, Wegovy, semaglutide, or later "
            "retatrutide history to tirzepatide unless the interval is stated."
        ),
    ],
    "sema": [
        (
            "This page reads the semaglutide archive: Ozempic, Wegovy, Rybelsus, semaglutide, "
            "and sema. These communities contain some of the earliest mass-market GLP-1 "
            "self-reports, mixing careful diaries with panic, celebration, dosing confusion, "
            "shortages, compounding questions, and ordinary Reddit noise."
        ),
        (
            "Each candidate is read one at a time by gpt-5.4-nano. The model is instructed to "
            "extract reported starting and current weight, duration, dose narrative, and side "
            "effects, while avoiding common traps: goal weight is not an end weight, a pregnancy "
            "or historical high is not automatically a drug baseline, and a loss from a whole "
            "GLP-1 journey should not be credited to semaglutide unless the post says so."
        ),
    ],
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


def nav_link(label: str, href: str, key: str, active: str) -> str:
    class_attr = ' class="active"' if key == active else ""
    return f'<a href="{html.escape(href)}"{class_attr}>{html.escape(label)}</a>'


def site_header(asset_prefix: str = "", active: str = "overview", *, show_brand: bool = True) -> str:
    home = asset_prefix or "./"
    brand = (
        f"""
      <a href="{html.escape(home)}" class="brand">
        <strong>GLP-1 Chatter</strong>
        <span>Reddit, weight-loss drugs, and the new medical consumerism.</span>
      </a>"""
        if show_brand
        else ""
    )
    header_class = "header-inner" if show_brand else "header-inner header-inner-nav-only"
    return f"""
  <header class="site-header">
    <div class="{header_class}">
      {brand}
      <nav class="nav" aria-label="Primary">
        {nav_link("Overview", home, "overview", active)}
        {nav_link("Weight Change", f"{home}weight-change/", "weight", active)}
        {nav_link("Side Effects", f"{home}side-effects/", "effects", active)}
        {nav_link("Stacking/polypharmacy", f"{home}concurrent/", "stacking", active)}
        {nav_link("Methods", f"{home}methods/", "methods", active)}
        {nav_link("Data Status", f"{home}data-status/", "status", active)}
      </nav>
    </div>
  </header>
"""


def family_action_links(family: str, *, asset_prefix: str = "") -> str:
    return f"""
        <div class="actions">
          <a class="button primary" href="{asset_prefix}{family}/">Weight Change</a>
          <a class="button" href="{asset_prefix}{family}/side-effects.html">Side Effects</a>
        </div>
"""


def family_tabs(current_family: str, *, current_view: str) -> str:
    tabs = []
    for family in DRUG_FAMILIES:
        href = f"../{family}/side-effects.html" if current_view == "effects" else f"../{family}/"
        active = " active" if family == current_family else ""
        tabs.append(
            f'<a class="tab{active}" href="{href}">{html.escape(FAMILY_NAMES[family])}</a>'
        )
    return f'<div class="tabs" aria-label="Drug family pages">{"".join(tabs)}</div>'


def _plot_scale(value: float, domain_min: float, domain_max: float, range_min: float, range_max: float) -> float:
    if domain_min == domain_max:
        return (range_min + range_max) / 2
    return range_min + (value - domain_min) / (domain_max - domain_min) * (range_max - range_min)


def _nice_step(span: float, target_ticks: int) -> float:
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(1, target_ticks)
    exponent = math.floor(math.log10(raw))
    base = 10**exponent
    fraction = raw / base
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * base


def _nice_ticks(domain_min: float, domain_max: float, target_ticks: int = 5) -> list[float]:
    if domain_min == domain_max:
        domain_min -= 1
        domain_max += 1
    step = _nice_step(domain_max - domain_min, target_ticks)
    start = math.floor(domain_min / step) * step
    end = math.ceil(domain_max / step) * step
    ticks: list[float] = []
    value = start
    guard = 0
    while value <= end + step * 0.5 and guard < 40:
        ticks.append(0.0 if abs(value) < step / 1_000 else round(value, 10))
        value += step
        guard += 1
    return ticks


def _format_axis_tick(value: float) -> str:
    if abs(value - round(value)) < 0.01:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _quantile(values: list[float], probability: float) -> float:
    cleaned = sorted(value for value in values if math.isfinite(value))
    if not cleaned:
        return 0.0
    if len(cleaned) == 1:
        return cleaned[0]
    position = (len(cleaned) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return cleaned[lower_index]
    lower = cleaned[lower_index]
    upper = cleaned[upper_index]
    return lower + (upper - lower) * (position - lower_index)


def render_home_mini_plot(family: str, payload: dict[str, Any]) -> str:
    points = [
        point
        for point in payload.get("points", [])
        if point.get("duration_weeks") is not None
        and point.get("weight_change_kg") is not None
        and math.isfinite(float(point["duration_weeks"]))
        and math.isfinite(float(point["weight_change_kg"]))
    ]
    curve = [
        point
        for point in payload.get("curve", [])
        if point.get("weeks") is not None
        and point.get("weight_change_kg") is not None
        and math.isfinite(float(point["weeks"]))
        and math.isfinite(float(point["weight_change_kg"]))
    ]
    name = FAMILY_NAMES[family]
    if not points:
        return f"""
        <div class="mini-chart mini-chart-empty" role="img" aria-label="{html.escape(name)} mini plot">
          <span>No plottable reports yet</span>
        </div>
"""

    width = 360
    height = 220
    left = 52
    right = 16
    top = 34
    bottom = 46
    x_values = [float(point["duration_weeks"]) for point in points]
    y_values = [float(point["weight_change_kg"]) for point in points]
    x_values.extend(float(point["weeks"]) for point in curve)
    y_values.extend(float(point["weight_change_kg"]) for point in curve)
    y_values.append(0.0)
    x_min = 0.0
    x_max = max(x_values) if len(x_values) < 30 else _quantile(x_values, 0.98)
    y_min = min(y_values) if len(y_values) < 30 else _quantile(y_values, 0.05)
    y_max = max(y_values) if len(y_values) < 30 else _quantile(y_values, 0.95)
    y_min = min(y_min, 0.0)
    y_max = max(y_max, 0.0)
    x_span = x_max - x_min
    y_span = y_max - y_min
    if x_span == 0:
        x_max += 1
    else:
        x_max += x_span * 0.06
    if y_span == 0:
        y_min -= 1
        y_max += 1
    else:
        y_min -= y_span * 0.10
        y_max += y_span * 0.10
    x_ticks = _nice_ticks(x_min, x_max, 4)
    y_ticks = _nice_ticks(y_min, y_max, 5)
    x_min = min(x_min, *x_ticks)
    x_max = max(x_max, *x_ticks)
    y_min = min(y_min, *y_ticks)
    y_max = max(y_max, *y_ticks)

    def x_pos(value: float) -> float:
        return _plot_scale(value, x_min, x_max, left, width - right)

    def y_pos(value: float) -> float:
        return _plot_scale(value, y_min, y_max, height - bottom, top)

    zero_y = y_pos(0.0)
    plot_width = width - left - right
    plot_height = height - top - bottom
    clip_id = f"mini-clip-{family}"
    y_grid_nodes = "\n".join(
        f'<line class="mini-grid mini-grid-y" x1="{left}" x2="{width - right}" y1="{y_pos(tick):.2f}" y2="{y_pos(tick):.2f}" />'
        f'<text class="mini-tick mini-y-tick" x="{left - 7}" y="{y_pos(tick) + 3.5:.2f}" text-anchor="end">{html.escape(_format_axis_tick(tick))}</text>'
        for tick in y_ticks
    )
    x_tick_nodes = "\n".join(
        f'<line class="mini-axis-tick" x1="{x_pos(tick):.2f}" x2="{x_pos(tick):.2f}" y1="{height - bottom}" y2="{height - bottom + 5}" />'
        f'<text class="mini-tick mini-x-tick" x="{x_pos(tick):.2f}" y="{height - bottom + 20}" text-anchor="middle">{html.escape(_format_axis_tick(tick))}</text>'
        for tick in x_ticks
    )
    point_nodes = "\n".join(
        f'<circle class="mini-point" cx="{x_pos(float(point["duration_weeks"])):.2f}" '
        f'cy="{y_pos(float(point["weight_change_kg"])):.2f}" r="2.6" />'
        for point in points
    )
    curve_path = ""
    if len(curve) >= 2:
        path = " ".join(
            f'{"M" if index == 0 else "L"}{x_pos(float(point["weeks"])):.2f},{y_pos(float(point["weight_change_kg"])):.2f}'
            for index, point in enumerate(curve)
        )
        curve_path = f'<path class="mini-fit" d="{path}" />'
    return f"""
        <svg class="mini-chart mini-plot" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(name)} weight-change mini plot">
          <title>{html.escape(name)} Reddit reports: duration by weight change</title>
          <defs>
            <clipPath id="{clip_id}">
              <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" />
            </clipPath>
          </defs>
          <rect class="mini-panel" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" />
          {y_grid_nodes}
          <line class="mini-zero" x1="{left}" y1="{zero_y:.2f}" x2="{width - right}" y2="{zero_y:.2f}" />
          <line class="mini-axis" x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" />
          <line class="mini-axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" />
          {x_tick_nodes}
          <g clip-path="url(#{clip_id})">
            {curve_path}
            {point_nodes}
          </g>
          <text class="mini-axis-label mini-y-label" x="{left}" y="18">Weight change (kg)</text>
          <text class="mini-axis-label mini-x-label" x="{width - right}" y="{height - 7}" text-anchor="end">Duration (weeks)</text>
        </svg>
"""


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


def load_side_effect_screenings(conn) -> dict[int, dict[str, dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT
          report_id,
          side_effect_phrase,
          severity,
          confidence,
          evidence,
          rationale,
          model,
          screened_at
        FROM side_effect_screenings
        """
    ).fetchall()
    screenings: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        screenings[int(row["report_id"])][row["side_effect_phrase"]] = {
            "severity": row["severity"],
            "source": "llm",
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "rationale": row["rationale"],
            "model": row["model"],
            "screened_at": row["screened_at"],
        }
    return screenings


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


def side_effect_report_card(row: Any, effects: list[str], severity_by_effect: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "report_id": row["report_id"],
        "post_id": row["post_id"],
        "created_iso": row["created_iso"],
        "subreddit": row["subreddit"],
        "url": row["url"],
        "title": row["title"],
        "drug_family": row["drug_family"],
        "drug_name_mentioned": row["drug_name_mentioned"],
        "use_status": row["use_status"],
        "attribution": row["attribution"],
        "dose_strong": row["dose_strong"],
        "duration_raw": row["duration_raw"],
        "duration_weeks": row["duration_weeks"],
        "weight_change_kg": row["weight_change_kg"],
        "effects": effects,
        "severity_by_effect": severity_by_effect,
        "confidence": row["confidence"],
        "evidence": row["evidence"],
        "notes": row["notes"],
        "text_excerpt": text_excerpt(row, limit=680),
        "full_text": row["processed_full_text"] or row["full_text"],
        "content_changed_after_processing": bool(row["content_changed_after_processing"]),
    }


def clipped_quote(value: str | None, limit: int = 185) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'")
    text = text.replace('"', "").replace("“", "").replace("”", "")
    text = re.sub(r"\s*\.\.\.\s*", " ... ", text)
    if not text or text.lower() in {"n/a", "none", "null"}:
        return ""
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{clipped}..."


def side_effect_severity_examples(explorer: dict[str, Any]) -> dict[str, dict[str, str]]:
    examples: dict[str, dict[str, str]] = {}
    reports = explorer.get("reports", {})
    for report in reports.values():
        for effect, severity_info in (report.get("severity_by_effect") or {}).items():
            severity = severity_info.get("severity")
            if severity not in {"mild", "severe"} or severity in examples:
                continue
            quote = clipped_quote(report.get("evidence")) or clipped_quote(report.get("text_excerpt"))
            if not quote:
                continue
            examples[severity] = {
                "effect": str(effect),
                "quote": quote,
                "url": str(report.get("url") or "#"),
                "subreddit": str(report.get("subreddit") or "unknown"),
            }
        if {"mild", "severe"}.issubset(examples):
            break
    return examples


def side_effect_examples_html(explorer: dict[str, Any]) -> str:
    examples = side_effect_severity_examples(explorer)
    cards = []
    for severity in ("mild", "severe"):
        example = examples.get(severity)
        if not example:
            continue
        cards.append(
            f"""
        <figure class="severity-example severity-example-{severity}">
          <figcaption>{severity.capitalize()} LLM example - {html.escape(example["effect"])} - r/{html.escape(example["subreddit"])}</figcaption>
          <blockquote>{html.escape(example["quote"])}</blockquote>
          <a class="reddit-link" href="{html.escape(example["url"])}" target="_blank" rel="noopener">Open Reddit URL</a>
        </figure>
"""
        )
    if not cards:
        return ""
    return f'      <div class="severity-examples">{"".join(cards)}      </div>'


def side_effect_explorer_payload(
    rows: list[Any],
    mapping: dict[str, str],
    screenings: dict[int, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    effect_counter: Counter[str] = Counter()
    severity_counts: dict[str, Counter[str]] = defaultdict(Counter)
    effect_report_ids: dict[str, set[int]] = defaultdict(set)
    pair_counter: Counter[tuple[str, str]] = Counter()
    pair_report_ids: dict[tuple[str, str], set[int]] = defaultdict(set)
    reports: dict[int, dict[str, Any]] = {}

    for row in rows:
        effects = canonical_side_effects(row_json(row, "side_effects", []), mapping)
        if not effects:
            continue
        report_id = int(row["report_id"])
        severity_by_effect = {
            effect: screenings.get(report_id, {}).get(
                effect,
                {"severity": "unscreened", "source": "not_screened"},
            )
            for effect in effects
        }
        reports[report_id] = side_effect_report_card(row, effects, severity_by_effect)
        for effect in effects:
            effect_counter[effect] += 1
            effect_report_ids[effect].add(report_id)
            severity_counts[effect][severity_by_effect[effect]["severity"]] += 1
        for i, source in enumerate(effects):
            for target in effects[i + 1 :]:
                pair = tuple(sorted((source, target)))
                pair_counter[pair] += 1
                pair_report_ids[pair].add(report_id)

    effects_payload = []
    for effect, count in effect_counter.most_common():
        effects_payload.append(
            {
                "phrase": effect,
                "count": count,
                "report_ids": sorted(effect_report_ids[effect]),
                "severity_counts": {
                    severity: severity_counts[effect].get(severity, 0)
                    for severity in ("mild", "moderate", "severe", "unscreened")
                },
            }
        )
    links_payload = [
        {
            "source": source,
            "target": target,
            "count": count,
            "report_ids": sorted(pair_report_ids[(source, target)]),
        }
        for (source, target), count in pair_counter.most_common()
    ]
    return {
        "effects": effects_payload,
        "links": links_payload,
        "reports": {str(report_id): report for report_id, report in sorted(reports.items())},
        "severity_method": {
            "status": "llm",
            "source": "side_effect_screenings",
            "levels": ["mild", "moderate", "severe", "unscreened"],
        },
        "summary": {
            "reports_with_side_effects": len(reports),
            "unique_effects": len(effects_payload),
            "cooccurrence_links": len(links_payload),
        },
    }


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


def render_home(summary: dict[str, Any], generated_at: str, family_payloads: dict[str, dict[str, Any]]) -> str:
    snapshot_notes = {
        "sema": (
            "Drug marketed as Ozempic, Wegovy, and Rybelsus, semaglutide was the GLP-1 drug "
            "that first made this new weight-loss era visible at mass scale, with Ozempic becoming "
            "a cultural and economic phenomenon in the early 2020s. The impact was such that "
            "Novo Nordisk became a large part of the Danish economic story, only to be quickly "
            "challenged by Eli Lilly."
        ),
        "tirz": (
            "Marketed as Mounjaro and Zepbound, tirzepatide turned the second wave into a rivalry: "
            "a dual GIP/GLP-1 drug from Eli Lilly that many Reddit users discuss as the stronger, "
            "more expensive, harder-to-access benchmark for appetite suppression and weight loss."
        ),
        "reta": (
            "Retatrutide is not approved as a weight-loss drug. On Reddit it appears as a frontier "
            "compound for higher-risk-tolerance communities: people unwilling to wait for regulators "
            "and hoping for faster loss, more metabolic force, and less muscle sacrifice."
        ),
    }
    snapshot_eras = {
        "sema": "early 2020s",
        "tirz": "second wave",
        "reta": "investigational",
    }

    def snapshot(family: str, index: int, align: str) -> str:
        item = summary["families"][family]
        return f"""
        <aside class="story-snapshot story-snapshot-{align} family-{family}" aria-label="{html.escape(FAMILY_NAMES[family])} snapshot">
          <div class="snapshot-kicker">Figure {index}<span>{html.escape(snapshot_eras[family])}</span></div>
          <h2>{html.escape(FAMILY_NAMES[family])}</h2>
          <p class="snapshot-note">{html.escape(snapshot_notes[family])}</p>
          {render_home_mini_plot(family, family_payloads[family])}
          <div class="metrics compact-metrics">
            <div class="metric"><span>Parsed posts</span><strong>{item["parsed_posts"]}</strong></div>
            <div class="metric"><span>Plottable</span><strong>{item["plottable_reports"]}</strong></div>
            <div class="metric"><span>Median weeks</span><strong>{fmt_number(item["median_duration_weeks"])}</strong></div>
            <div class="metric"><span>Median change</span><strong>{fmt_number(item["median_weight_change_kg"])} kg</strong></div>
          </div>
          {family_action_links(family)}
        </aside>
"""

    body = f"""
<body>
  {site_header(active="overview", show_brand=False)}
  <main class="home">
    <article class="home-essay">
      <header class="essay-header">
        <h1>GLP-1 Chatter</h1>
        <p class="essay-deck">Reddit, weight-loss drugs, and the new medical consumerism.</p>
      </header>

      <p class="essay-lede">My interest in the rise of the new wave of highly effective weight-loss drugs, GLP-1-based medicines and related incretin drugs, is both scientific and personal. I first used a GLP-1 drug for weight loss in 2022, after reading promising trial evidence. It worked. The effect was immediate enough, and convincing enough, that I also bought shares in Novo Nordisk and Eli Lilly. My own experience was positive, and I still think these drugs are remarkable. For many people they are not merely another diet aid, but a profound intervention in appetite, weight, health, self-image, and agency.</p>

      {snapshot("sema", 1, "right")}

      <p>But their success has also exposed a much stranger and more difficult reality. A critical segment of medicine is being reorganized around motivated consumers, uneven access, online knowledge, and variable risk tolerance. Some people obtain these drugs through conventional medical care. Some go through brief online prescribing pathways. Some use compounding pharmacies. Others enter the much murkier world of research peptides, gray-market suppliers, and drugs that have not yet completed regulatory approval.</p>

      {snapshot("tirz", 2, "left")}

      <p>Reddit has become one of the places where this transition is visible in real time. On communities such as r/Semaglutide, r/Zepbound, r/Tirzepatide, and r/Peptides, people compare doses, side effects, weight-loss trajectories, plateaus, hunger, nausea, constipation, fatigue, hair loss, gallbladder worries, mood changes, and combinations with other medications. Some users are scientifically literate and deliberately experimental. Some are desperate, anxious, under-informed, or unable to access care through ordinary channels. Some posts are careful self-reports. Others may be contaminated by hype, misinformation, commercial interests, or outright peptide sales activity.</p>

      <p>This creates a new kind of medical purgatory. People are using powerful metabolic drugs to change themselves, often with real benefit, but also with uncertain guidance. Medical professionals generally do not view prescription medication as a simple consumer choice. They work within systems built around indication, regulation, risk management, monitoring, and need. Many users, by contrast, experience these drugs as tools of self-directed improvement - more like fixing a lawn mower or installing a modem after watching a YouTube tutorial, part of a broader do-it-yourself culture now reaching into medicine. The result is a culture clash: medicine wants these drugs to move through a slow, cautious, regulated process; consumers often want access, autonomy, information, and practical advice now.</p>

      {snapshot("reta", 3, "right")}

      <p>GLP-1 Chatter tries to make that online landscape more legible.</p>

      <p>The site uses large language models to extract structured information from Reddit discussions about GLP-1 and related weight-loss drugs. It indexes reported weight loss, side effects, co-occurring symptoms, medication combinations, dosing patterns, and user experiences across different communities. The aim is descriptive rather than prescriptive: to surface a broader sociological shift in how people relate to medicine. Across these discussions, individuals are not only receiving care but actively seeking, comparing, and directing it, often with a level of agency that feels new in scale and speed. This emerging pattern - of patients acting as informed, motivated consumers - appears to be unfolding faster than traditional medical institutions are accustomed to accommodating.</p>

      <p>Reddit is messy, biased, incomplete, and vulnerable to manipulation. But it is also a vast archive of lived experience: a place where people describe what they are actually doing, what they think is happening to them, what they fear, what they tolerate, and what they recommend to others.</p>

      <p>Importantly, the website is designed to keep the data close to the underlying stories. Interactive widgets allow users to move from aggregate summaries back toward the posts and experiences that generated them. A table of side effects should not float free from the people reporting them. A weight-loss estimate should be traceable to the messy narrative from which it came. The aim is to quantify without fully flattening the human context.</p>

      <p>This website is a first attempt to map that change from the ground up: through the stories people tell while trying to navigate one of the most consequential medical consumer movements of the decade.</p>

      <p>This is still a work in progress. The extraction is imperfect. The communities are not representative. The data should be interpreted cautiously. But the phenomenon itself is too important to ignore. GLP-1 drugs are changing obesity treatment, diabetes care, pharmaceutical markets, online medicine, and the relationship between patients, consumers, physicians, and platforms.</p>

      <footer class="home-footnotes" aria-label="Site caveats and shortcuts">
        <p>This is observational social-media text mining, not medical advice, clinical evidence, or proof of causality. Each plot point remains linked back to the underlying Reddit text.</p>
        <p class="note-links"><a href="weight-change/">Choose a weight-change view</a><a href="side-effects/">Choose a side-effect view</a><a href="methods/">Read the methods</a></p>
      </footer>
    </article>
  </main>
</body>
"""
    return html_page("GLP-1 Reddit Reports", body)


def render_weight_choice_page(summary: dict[str, Any], generated_at: str) -> str:
    cards = []
    for family in DRUG_FAMILIES:
        item = summary["families"][family]
        cards.append(
            f"""
      <a class="route choice-card family-{family}" href="../{family}/">
        <strong>{html.escape(FAMILY_NAMES[family])}</strong>
        <span>{item["plottable_reports"]} plottable reports; median {fmt_number(item["median_duration_weeks"])} weeks and {fmt_number(item["median_weight_change_kg"])} kg.</span>
      </a>
"""
        )
    body = f"""
<body>
  {site_header("../", active="weight")}
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">Weight Change</p>
      <h1>Choose a drug family.</h1>
      <div class="page-copy">
        <p>This site turns a messy Reddit archive into a cautious set of weight-change plots. The crawler searches selected GLP-1 communities for configured terms: retatrutide, reta, and retaglutide; tirzepatide, tirz, Mounjaro, and Zepbound; semaglutide, sema, Ozempic, Wegovy, and Rybelsus. During the current historical catch-up period, a backfill workflow runs every 12 hours; regular scheduled crawls keep checking recent material.</p>
        <p>Each candidate post or comment is stored with its original text and URL, then read once by gpt-5.4-nano as a single-item extraction. The prompt asks for Reddit abbreviations such as SW and CW, tries not to mistake GW for current weight, and leaves unit conversion to code. On the drug pages, weight loss is plotted as negative kilograms, and points with large losses, gains over 5 kg, or very long durations are reread by gpt-5.4-mini before they become canonical.</p>
      </div>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="route-grid route-grid-wide choice-grid" aria-label="Weight-change drug choices">
      {''.join(cards)}
    </section>
  </main>
</body>
"""
    return html_page("Weight Change Choices", body, asset_prefix="../")


def render_side_effect_choice_page(summary: dict[str, Any], generated_at: str) -> str:
    cards = []
    for family in DRUG_FAMILIES:
        item = summary["families"][family]
        effects = ", ".join(effect["phrase"] for effect in item["most_common_side_effects"][:3]) or "No side effects extracted yet"
        cards.append(
            f"""
      <a class="route choice-card family-{family}" href="../{family}/side-effects.html">
        <strong>{html.escape(FAMILY_NAMES[family])}</strong>
        <span>Common extracted phrases: {html.escape(effects)}.</span>
      </a>
"""
        )
    body = f"""
<body>
  {site_header("../", active="effects")}
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">Side Effects</p>
      <h1>Choose a drug family.</h1>
      <p>Each side-effect page shows normalized phrase counts, co-mentions, report excerpts, and the original Reddit text. The normalization is auditable and conservative; these are user reports, not verified clinical adverse-event rates.</p>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="route-grid route-grid-wide choice-grid" aria-label="Side-effect drug choices">
      {''.join(cards)}
    </section>
  </main>
</body>
"""
    return html_page("Side Effect Choices", body, asset_prefix="../")


def render_methods_page(generated_at: str) -> str:
    body = f"""
<body>
  {site_header("../", active="methods")}
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">Methods</p>
      <h1>How the site is built.</h1>
      <p>This project stores raw Reddit candidate posts and comments, parses one item per LLM call, validates strict JSON, converts units in code, and rescreens reports with large losses, notable gains, or long durations. The LLM extracts raw values only; plotting and unit conversion are computational.</p>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="method-grid">
      <article class="section-panel"><h2>Crawling</h2><p>Relevant Reddit sources are crawled slowly and politely. Raw candidate text, URLs, matched terms, timestamps, and source metadata are retained in SQLite.</p></article>
      <article class="section-panel"><h2>Extraction</h2><p>Each post or comment is parsed independently with a structured JSON schema. Previously processed post IDs are not reparsed just because text or hash metadata changes.</p></article>
      <article class="section-panel"><h2>Rescreening</h2><p>Large extracted values are reviewed by a stronger model, including weight loss over 25 kg, weight gain over 5 kg, or duration over 365 days.</p></article>
      <article class="section-panel"><h2>Plotting</h2><p>Weight loss is plotted as negative weight change in kilograms. Reddit fitted curves are computed from Reddit reports only and do not use optional RCT overlays.</p></article>
    </section>
  </main>
</body>
"""
    return html_page("Methods", body, asset_prefix="../")


def render_data_status_page(summary: dict[str, Any], generated_at: str) -> str:
    rows = []
    for family in DRUG_FAMILIES:
        item = summary["families"][family]
        rows.append(
            f"""
        <tr>
          <td>{html.escape(FAMILY_NAMES[family])}</td>
          <td>{item["parsed_posts"]}</td>
          <td>{item["plottable_reports"]}</td>
          <td>{fmt_number(item["median_duration_weeks"])} weeks</td>
          <td>{fmt_number(item["median_weight_change_kg"])} kg</td>
        </tr>
"""
        )
    body = f"""
<body>
  {site_header("../", active="status")}
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">Data Status</p>
      <h1>Current generated dataset.</h1>
      <p>GitHub Actions crawls Reddit candidates, parses pending items, rescreens flagged reports, rebuilds the static JSON bundles, and publishes the Pages site. Counts below reflect the SQLite database at build time.</p>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="table-section">
      <table>
        <thead><tr><th>Drug family</th><th>Parsed posts</th><th>Plottable reports</th><th>Median duration</th><th>Median change</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </section>
  </main>
</body>
"""
    return html_page("Data Status", body, asset_prefix="../")


def render_scatter_page(family: str, generated_at: str, has_rct: bool) -> str:
    name = FAMILY_NAMES[family]
    rct_note = (
        '<p class="note">Trial overlay is external uploaded aggregate data, not mined Reddit data.</p>'
        if has_rct
        else ""
    )
    rct_context = (
        "The pale blue line and band show uploaded aggregate clinical-trial data for comparison: "
        "a controlled setting behind the messier Reddit reports. That overlay is never used in "
        "the Reddit fitted curve."
        if has_rct
        else (
            "If an external clinical-trial CSV is uploaded for this drug, it will appear as a "
            "pale blue comparison line and uncertainty band. Trial data are kept out of the "
            "Reddit fitted curve."
        )
    )
    page_copy = "\n".join(
        f"        <p>{html.escape(paragraph)}</p>"
        for paragraph in [
            *WEIGHT_PAGE_COPY[family],
            (
                "The dots below are the site's best current reading of Reddit reports with at "
                "least 21 days of duration. Weight loss is plotted as negative weight change in "
                "kilograms, so a 10 kg loss appears as -10 kg. Hover or click a point to see the "
                "original text, Reddit URL, extracted fields, confidence, evidence, and notes. "
                "Large losses, gains over 5 kg, and very long durations are sent through "
                "gpt-5.4-mini for a second read before the canonical extraction is shown."
            ),
            rct_context,
        ]
    )
    body = f"""
<body data-view="scatter" data-family="{family}" data-json="../data/{family}.json">
  {site_header("../", active="weight")}
  <main class="page">
    <section class="page-heading">
      {family_tabs(family, current_view="weight")}
      <p class="eyebrow">{html.escape(name)}</p>
      <h1>Weight change over time</h1>
      <div class="page-copy">
{page_copy}
      </div>
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


def render_side_effect_page(family: str, generated_at: str, explorer: dict[str, Any]) -> str:
    name = FAMILY_NAMES[family]
    examples_html = side_effect_examples_html(explorer)
    body = f"""
<body data-view="side-effects" data-family="{family}" data-json="../data/{family}.json">
  {site_header("../", active="effects")}
  <main class="page">
    <section class="page-heading">
      {family_tabs(family, current_view="effects")}
      <p class="eyebrow">{html.escape(name)}</p>
      <h1>Side-effect mentions</h1>
      <div class="page-copy side-effect-page-copy">
{examples_html}
        <p>The side-effect view starts with the same single-item reading process as the weight plots. Each Reddit post or comment gets a first pass from gpt-5.4-nano, with flagged records eligible for a gpt-5.4-mini reread, and the extraction pulls concise side-effect phrases from the user's account. A separate one-report LLM screen then labels each extracted phrase as mild, moderate, or severe. Code normalizes obvious variants, so "sulfur burps" and "sulphur burps" can be counted together while the original Reddit text remains one click away.</p>
        <p>The labels are not clinical adverse-event grades. They are reader-facing triage for a noisy archive: mild when the report sounds limited or manageable, moderate when it becomes disruptive or persistent, and severe when the text points to danger, drug stopping, urgent care, inability to keep food or fluids down, or major impairment. "Unscreened" means the report has not yet received that severity pass.</p>
        <p>Use the frequency list to select a specific side effect, the circular co-occurrence view to select pairs of symptoms that appear in the same report, and the severity buttons to narrow the archive to mild, moderate, or severe accounts. The report cards below then let you browse the source posts and full Reddit text, preserving the lived context people chose to share: excitement, fear, reassurance, practical advice, and sometimes vulnerability in a place that may or may not give them reliable support.</p>
      </div>
      <p class="meta">Generated {html.escape(generated_at)}</p>
    </section>
    <section class="effect-toolbar" aria-label="Side-effect filters">
      <input id="effect-search" class="effect-search" type="search" placeholder="Search reports, notes, evidence">
      <div class="network-controls severity-controls">
        <button type="button" class="segmented active" data-severity="all">All severities</button>
        <button type="button" class="segmented" data-severity="mild">Mild</button>
        <button type="button" class="segmented" data-severity="moderate">Moderate</button>
        <button type="button" class="segmented" data-severity="severe">Severe</button>
        <button type="button" class="segmented" data-severity="unscreened">Unscreened</button>
      </div>
    </section>
    <section class="side-effect-grid">
      <div class="effect-story">
        <div>
          <h2>Frequency</h2>
          <p id="effect-status" class="status">Loading side effects...</p>
        </div>
        <div id="effect-bars" class="effect-bars effect-bars-large"></div>
      </div>
      <div class="effect-network-card">
        <h2>Co-occurrence</h2>
        <svg id="effect-network" class="effect-network" role="img" aria-label="{html.escape(name)} side-effect co-occurrence graph"></svg>
      </div>
      <aside id="effect-detail" class="detail-panel effect-detail" aria-live="polite">
        <h2>Effect detail</h2>
      </aside>
    </section>
    <section class="effect-feed-section">
      <div class="feed-heading">
        <h2>User reports</h2>
        <p id="effect-feed-count" class="meta"></p>
      </div>
      <div id="effect-feed" class="effect-feed"></div>
      <div id="effect-feed-sentinel" class="feed-sentinel"></div>
    </section>
  </main>
  <script src="../assets/app.js"></script>
</body>
"""
    return html_page(f"{name} Side Effects", body, asset_prefix="../")


def render_concurrent_page(generated_at: str) -> str:
    body = f"""
<body data-view="concurrent" data-json="../data/concurrent.json">
  {site_header("../", active="stacking")}
  <main class="page">
    <section class="page-heading">
      <p class="eyebrow">All drug families</p>
      <h1>Stacking/polypharmacy</h1>
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
    return html_page("Stacking/polypharmacy", body, asset_prefix="../")


def build_site(db_path: Path, site_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    conn = connect_db(db_path)
    ensure_schema(conn)
    reports = load_reports(conn)
    side_effect_screenings = load_side_effect_screenings(conn)
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
            "side_effect_explorer": side_effect_explorer_payload(rows, mapping, side_effect_screenings),
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
            render_side_effect_page(family, generated_at, payload["side_effect_explorer"]),
            encoding="utf-8",
        )
    concurrent_dir = site_dir / "concurrent"
    concurrent_dir.mkdir(parents=True, exist_ok=True)
    (concurrent_dir / "index.html").write_text(render_concurrent_page(generated_at), encoding="utf-8")
    weight_dir = site_dir / "weight-change"
    weight_dir.mkdir(parents=True, exist_ok=True)
    (weight_dir / "index.html").write_text(render_weight_choice_page(summary, generated_at), encoding="utf-8")
    effects_dir = site_dir / "side-effects"
    effects_dir.mkdir(parents=True, exist_ok=True)
    (effects_dir / "index.html").write_text(render_side_effect_choice_page(summary, generated_at), encoding="utf-8")
    methods_dir = site_dir / "methods"
    methods_dir.mkdir(parents=True, exist_ok=True)
    (methods_dir / "index.html").write_text(render_methods_page(generated_at), encoding="utf-8")
    status_dir = site_dir / "data-status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "index.html").write_text(render_data_status_page(summary, generated_at), encoding="utf-8")
    (site_dir / "index.html").write_text(render_home(summary, generated_at, family_payloads), encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    summary = build_site(args.db, args.site_dir, args.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
