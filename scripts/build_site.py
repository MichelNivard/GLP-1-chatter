#!/usr/bin/env python3
"""Build the static GitHub Pages site from SQLite."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
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
    row_json,
    utc_now_iso,
    write_json,
)

FAMILY_NAMES = {
    "reta": "Retatrutide",
    "tirz": "Tirzepatide",
    "sema": "Semaglutide",
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
    }


def side_effect_counts(rows: list[Any], mapping: dict[str, str]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        effects = canonical_side_effects(row_json(row, "side_effects", []), mapping)
        counter.update(effects)
    return [{"phrase": phrase, "count": count} for phrase, count in counter.most_common()]


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
        return summary

    if site_dir.exists():
        shutil.rmtree(site_dir)
    (site_dir / "data").mkdir(parents=True, exist_ok=True)
    copy_assets(site_dir)
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")
    write_json(site_dir / "data" / "summary.json", summary)
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
    (site_dir / "index.html").write_text(render_home(summary, generated_at), encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    summary = build_site(args.db, args.site_dir, args.dry_run)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
