const NS = "http://www.w3.org/2000/svg";

function displayText(value) {
  return String(value ?? "").replaceAll("—", ", ");
}

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = displayText(text);
  return node;
}

function htmlEscape(value) {
  return displayText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmt(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

const FAMILY_NAMES = {
  reta: "Retatrutide",
  tirz: "Tirzepatide",
  sema: "Semaglutide",
};

const COMPOUND_LABEL_OVERRIDES = {
  "amphetamine/dextroamphetamine": "(dextro)amphetamine",
  "human growth hormone": "HGH",
  "hormone replacement therapy": "HRT",
};

const MAX_PLOTTED_WEIGHT_GAIN_KG = 10;

function familyName(family) {
  return FAMILY_NAMES[family] || family || "unknown drug";
}

function compoundDisplayName(value) {
  const text = String(value || "");
  return COMPOUND_LABEL_OVERRIDES[text] || text;
}

function reportDrugLabel(report) {
  const name = familyName(report.drug_family);
  const mentioned = report.drug_name_mentioned;
  if (!mentioned || String(mentioned).toLowerCase() === String(name).toLowerCase()) return name;
  return `${name} · mentioned as ${mentioned}`;
}

function testimonialHtml(text, label = "Qualitative report") {
  return `
    <figure class="testimonial">
      <figcaption>${htmlEscape(label)}</figcaption>
      <blockquote>&ldquo;${htmlEscape(text || "No Reddit text available.")}&rdquo;</blockquote>
    </figure>
  `;
}

async function loadPageData() {
  const jsonPath = document.body.dataset.json;
  const response = await fetch(jsonPath);
  if (!response.ok) throw new Error(`Could not load ${jsonPath}`);
  return response.json();
}

function extent(values, pad = 0.08) {
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const span = max - min;
  return [min - span * pad, max + span * pad];
}

function pathFrom(points, xScale, yScale, xKey, yKey) {
  return points
    .map((point, index) => `${index ? "L" : "M"}${xScale(point[xKey]).toFixed(2)},${yScale(point[yKey]).toFixed(2)}`)
    .join(" ");
}

function niceStep(span, targetTicks) {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const raw = span / Math.max(1, targetTicks);
  const exponent = Math.floor(Math.log10(raw));
  const base = 10 ** exponent;
  const fraction = raw / base;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return niceFraction * base;
}

function niceTicks(min, max, targetTicks = 6) {
  let lower = min;
  let upper = max;
  if (lower === upper) {
    lower -= 1;
    upper += 1;
  }
  const step = niceStep(upper - lower, targetTicks);
  const start = Math.floor(lower / step) * step;
  const end = Math.ceil(upper / step) * step;
  const values = [];
  for (let value = start, guard = 0; value <= end + step * 0.5 && guard < 50; value += step, guard += 1) {
    values.push(Math.abs(value) < step / 1000 ? 0 : Number(value.toFixed(10)));
  }
  return values;
}

function formatTick(value) {
  if (Math.abs(value - Math.round(value)) < 0.01) return String(Math.round(value));
  return value.toFixed(1).replace(/\.0$/, "");
}

function renderDetail(point) {
  const detail = document.getElementById("detail");
  const compounds = (point.other_compounds_concurrent || []).join(", ") || "none stated";
  const effects = (point.side_effects || []).join("; ") || "none extracted";
  const drift = point.content_changed_after_processing
    ? "<p class=\"note\">Latest stored Reddit text changed after this item was processed; the text below is the processed version used for extraction.</p>"
    : "";
  detail.innerHTML = `
    <h2>${htmlEscape(reportDrugLabel(point))}</h2>
    ${drift}
    ${testimonialHtml(point.processed_full_text || point.full_text || "", "Qualitative Reddit text")}
    <a class="reddit-link" href="${htmlEscape(point.url || "#")}" target="_blank" rel="noopener">Open Reddit URL</a>
    <details class="extracted-fields">
      <summary>Extracted fields</summary>
      <dl class="detail-list">
        <div><dt>Post date</dt><dd>${htmlEscape(point.created_iso || "n/a")}</dd></div>
        <div><dt>Subreddit</dt><dd>r/${htmlEscape(point.subreddit)}</dd></div>
        <div><dt>Drug family</dt><dd>${htmlEscape(familyName(point.drug_family))}</dd></div>
        <div><dt>Mentioned as</dt><dd>${htmlEscape(point.drug_name_mentioned || "n/a")}</dd></div>
        <div><dt>Dose</dt><dd>${htmlEscape(point.dose_strong || "n/a")}</dd></div>
        <div><dt>Duration</dt><dd>${htmlEscape(point.duration_raw || "n/a")} (${fmt(point.duration_weeks, 1, " weeks")})</dd></div>
        <div><dt>Start</dt><dd>${fmt(point.weight_start_value)} ${htmlEscape(point.weight_start_unit || "")} (${fmt(point.weight_start_kg, 1, " kg")})</dd></div>
        <div><dt>End</dt><dd>${fmt(point.weight_end_value)} ${htmlEscape(point.weight_end_unit || "")} (${fmt(point.weight_end_kg, 1, " kg")})</dd></div>
        <div><dt>Lost</dt><dd>${fmt(point.weight_lost_value)} ${htmlEscape(point.weight_lost_unit || "")} (${fmt(point.weight_lost_kg, 1, " kg")})</dd></div>
        <div><dt>Weight change</dt><dd>${fmt(point.weight_change_kg, 1, " kg")}</dd></div>
        <div><dt>Attribution</dt><dd>${htmlEscape(point.attribution || "n/a")}</dd></div>
        <div><dt>Concurrent compounds</dt><dd>${htmlEscape(compounds)}</dd></div>
        <div><dt>Side effects</dt><dd>${htmlEscape(effects)}</dd></div>
        <div><dt>Confidence</dt><dd>${fmt(point.confidence, 2)}</dd></div>
        <div><dt>Evidence</dt><dd>${htmlEscape(point.evidence || "n/a")}</dd></div>
        <div><dt>Notes</dt><dd>${htmlEscape(point.notes || "n/a")}</dd></div>
      </dl>
    </details>
  `;
}

function renderScatter(data) {
  const svg = document.getElementById("scatterplot");
  const status = document.getElementById("plot-status");
  const points = data.points || [];
  const plottedPoints = points.filter((point) => {
    const weeks = Number(point.duration_weeks);
    const change = Number(point.weight_change_kg);
    return Number.isFinite(weeks) && Number.isFinite(change) && change <= MAX_PLOTTED_WEIGHT_GAIN_KG;
  });
  const rctSeries = data.rct?.series?.length
    ? data.rct.series
    : (data.rct?.rows?.length ? [{ label: "RCT", rows: data.rct.rows }] : []);
  const rctPalette = [
    { line: "#5a9ed6", band: "rgba(90, 158, 214, 0.14)" },
    { line: "#6fba7d", band: "rgba(111, 186, 125, 0.14)" },
    { line: "#d88a4a", band: "rgba(216, 138, 74, 0.14)" },
    { line: "#9c77c7", band: "rgba(156, 119, 199, 0.14)" },
  ];
  svg.innerHTML = "";

  if (!plottedPoints.length && !rctSeries.length) {
    status.textContent = "No plottable reports yet.";
    svg.setAttribute("viewBox", "0 0 900 520");
    return;
  }
  status.textContent = plottedPoints.length ? "" : "No plottable Reddit reports within the plotted range yet; showing trial overlay.";

  const width = 920;
  const height = 560;
  const margin = { top: 84, right: 38, bottom: 70, left: 86 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  let xValues = plottedPoints.map((point) => Number(point.duration_weeks));
  let yValues = plottedPoints.map((point) => Number(point.weight_change_kg));
  data.curve?.forEach((point) => {
    xValues.push(Number(point.weeks));
    yValues.push(Number(point.weight_change_kg));
  });
  rctSeries.forEach((series) => {
    (series.rows || []).forEach((point) => {
      xValues.push(Number(point.weeks));
      yValues.push(Number(point.lower), Number(point.upper), Number(point.mean));
    });
  });
  xValues = xValues.filter(Number.isFinite);
  yValues = yValues.filter(Number.isFinite);
  if (!xValues.length || !yValues.length) {
    status.textContent = "No plottable numeric reports yet.";
    svg.setAttribute("viewBox", "0 0 900 520");
    return;
  }
  let xDomainMin = Math.min(0, ...xValues);
  let xDomainMax = Math.max(...xValues);
  let yDomainMin = Math.min(0, ...yValues);
  let yDomainMax = MAX_PLOTTED_WEIGHT_GAIN_KG;
  if (xDomainMin === xDomainMax) xDomainMax += 1;
  if (yDomainMin === yDomainMax) {
    yDomainMin -= 1;
  }
  xDomainMax += (xDomainMax - xDomainMin) * 0.04;
  const yPad = (yDomainMax - yDomainMin) * 0.06;
  yDomainMin -= yPad;
  const xTickValues = niceTicks(xDomainMin, xDomainMax, 6);
  const yTickValues = niceTicks(yDomainMin, yDomainMax, 7)
    .filter((value) => value <= MAX_PLOTTED_WEIGHT_GAIN_KG);
  if (!yTickValues.includes(MAX_PLOTTED_WEIGHT_GAIN_KG)) {
    yTickValues.push(MAX_PLOTTED_WEIGHT_GAIN_KG);
    yTickValues.sort((a, b) => a - b);
  }
  const xMin = Math.min(xDomainMin, ...xTickValues);
  const xMax = Math.max(xDomainMax, ...xTickValues);
  const yMin = Math.min(yDomainMin, ...yTickValues);
  const yMax = MAX_PLOTTED_WEIGHT_GAIN_KG;
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const xScale = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * plotW;
  const yScale = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * plotH;

  const clipId = `plot-clip-${data.family || "drug"}`;
  const defs = el("defs");
  const clipPath = el("clipPath", { id: clipId });
  clipPath.appendChild(el("rect", { x: margin.left, y: margin.top, width: plotW, height: plotH }));
  defs.appendChild(clipPath);
  svg.appendChild(defs);
  svg.appendChild(el("rect", { x: margin.left, y: margin.top, width: plotW, height: plotH, class: "plot-bg", fill: "#f3f2ec" }));

  yTickValues.forEach((value) => {
    const y = yScale(value);
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, class: "grid grid-y" }));
    svg.appendChild(el("text", { x: margin.left - 13, y: y + 4, "text-anchor": "end", class: "tick y-tick" }, formatTick(value)));
  });
  xTickValues.forEach((value) => {
    const x = xScale(value);
    svg.appendChild(el("line", { x1: x, x2: x, y1: margin.top, y2: margin.top + plotH, class: "grid grid-x" }));
    svg.appendChild(el("line", { x1: x, x2: x, y1: margin.top + plotH, y2: margin.top + plotH + 6, class: "axis-tick" }));
    svg.appendChild(el("text", { x, y: height - 34, "text-anchor": "middle", class: "tick x-tick" }, formatTick(value)));
  });
  svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: yScale(0), y2: yScale(0), class: "zero-line" }));
  svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: margin.top + plotH, y2: margin.top + plotH, class: "plot-frame" }));
  svg.appendChild(el("line", { x1: margin.left, x2: margin.left, y1: margin.top, y2: margin.top + plotH, class: "plot-frame" }));
  svg.appendChild(el("text", { x: margin.left, y: 34, "text-anchor": "start", class: "axis-title" }, "Weight change (kg)"));
  svg.appendChild(el("text", { x: margin.left + plotW, y: height - 10, "text-anchor": "end", class: "axis-title" }, "Duration (weeks)"));

  const dataLayer = el("g", { "clip-path": `url(#${clipId})` });
  rctSeries.forEach((series, index) => {
    const rows = series.rows || [];
    if (rows.length < 2) return;
    const colors = rctPalette[index % rctPalette.length];
    const upper = rows.map((point) => `${xScale(point.weeks).toFixed(2)},${yScale(point.upper).toFixed(2)}`).join(" ");
    const lower = [...rows].reverse().map((point) => `${xScale(point.weeks).toFixed(2)},${yScale(point.lower).toFixed(2)}`).join(" ");
    dataLayer.appendChild(el("polygon", { points: `${upper} ${lower}`, class: "rct-band", style: `fill:${colors.band}` }));
    dataLayer.appendChild(el("path", { d: pathFrom(rows, xScale, yScale, "weeks", "mean"), class: "rct-line", style: `stroke:${colors.line}` }));
  });

  if (data.curve?.length >= 2) {
    dataLayer.appendChild(el("path", { d: pathFrom(data.curve, xScale, yScale, "weeks", "weight_change_kg"), class: "fit-line" }));
  }

  plottedPoints.forEach((point) => {
    const circle = el("circle", {
      cx: xScale(point.duration_weeks),
      cy: yScale(point.weight_change_kg),
      r: 4.7,
      tabindex: 0,
      class: `point point-${data.family}`,
    });
    circle.addEventListener("click", () => renderDetail(point));
    circle.addEventListener("mouseenter", () => renderDetail(point));
    circle.addEventListener("focus", () => renderDetail(point));
    circle.appendChild(el("title", {}, `${fmt(point.duration_weeks, 1)} weeks, ${fmt(point.weight_change_kg, 1)} kg`));
    dataLayer.appendChild(circle);
  });
  svg.appendChild(dataLayer);

  const legend = el("g", { class: "legend" });
  legend.appendChild(el("circle", { cx: width - 288, cy: 18, r: 4.7, class: `point point-${data.family}` }));
  legend.appendChild(el("text", { x: width - 275, y: 22 }, "Reddit reports"));
  legend.appendChild(el("line", { x1: width - 288, x2: width - 262, y1: 39, y2: 39, class: "fit-line" }));
  legend.appendChild(el("text", { x: width - 253, y: 43 }, "Reddit smoothed fit"));
  rctSeries.forEach((series, index) => {
    const rows = series.rows || [];
    if (rows.length < 2) return;
    const y = 60 + index * 20;
    const colors = rctPalette[index % rctPalette.length];
    legend.appendChild(el("line", { x1: width - 288, x2: width - 262, y1: y, y2: y, class: "rct-line", style: `stroke:${colors.line}` }));
    legend.appendChild(el("text", { x: width - 253, y: y + 4 }, `${series.label} RCT mean +/- 1.96 SD`));
  });
  svg.appendChild(legend);
}

function renderSideEffects(data) {
  const explorer = data.side_effect_explorer || {};
  const effects = explorer.effects || data.side_effects || [];
  const reports = explorer.reports || {};
  const bars = document.getElementById("effect-bars");
  const network = document.getElementById("effect-network");
  const detail = document.getElementById("effect-detail");
  const feed = document.getElementById("effect-feed");
  const feedCount = document.getElementById("effect-feed-count");
  const sentinel = document.getElementById("effect-feed-sentinel");
  const status = document.getElementById("effect-status");
  const search = document.getElementById("effect-search");
  let selectedEffect = effects[0]?.phrase || null;
  let selectedPair = null;
  let selectedSeverity = "all";
  let query = "";
  let visibleReports = 18;
  let observer = null;
  const graphEffectLimit = 24;

  function reportForId(id) {
    return reports[String(id)] || reports[id];
  }

  function severityFor(report, effect) {
    return report?.severity_by_effect?.[effect]?.severity || "unscreened";
  }

  function severitySourceFor(report, effect) {
    const screening = report?.severity_by_effect?.[effect] || {};
    if (screening.source === "llm") {
      return screening.model ? `llm: ${screening.model}` : "llm";
    }
    return screening.source || "not_screened";
  }

  function severityRationaleFor(report, effect) {
    return report?.severity_by_effect?.[effect]?.rationale || "n/a";
  }

  function severityConfidenceFor(report, effect) {
    const confidence = report?.severity_by_effect?.[effect]?.confidence;
    return Number.isFinite(Number(confidence)) ? Number(confidence).toFixed(2) : "n/a";
  }

  function activeEffects() {
    if (selectedPair) return [selectedPair.source, selectedPair.target];
    return selectedEffect ? [selectedEffect] : [];
  }

  function pairKey(source, target) {
    return [source, target].sort().join("|||");
  }

  function linkForPair(source, target) {
    const key = pairKey(source, target);
    return (explorer.links || []).find((link) => pairKey(link.source, link.target) === key);
  }

  function selectionLabel() {
    if (selectedPair) return `${selectedPair.source} + ${selectedPair.target}`;
    return selectedEffect || "side effects";
  }

  function activeReportIds() {
    if (selectedPair) {
      const link = linkForPair(selectedPair.source, selectedPair.target);
      return link?.report_ids || selectedPair.report_ids || [];
    }
    const effect = effects.find((item) => item.phrase === selectedEffect);
    return effect?.report_ids || [];
  }

  function severityMatches(report) {
    if (selectedSeverity === "all") return true;
    return activeEffects().some((effect) => severityFor(report, effect) === selectedSeverity);
  }

  function selectedReports() {
    const q = query.trim().toLowerCase();
    return activeReportIds()
      .map(reportForId)
      .filter(Boolean)
      .filter(severityMatches)
      .filter((report) => {
        if (!q) return true;
        const haystack = [
          report.subreddit,
          report.drug_name_mentioned,
          report.evidence,
          report.notes,
          report.text_excerpt,
          report.full_text,
          ...(report.effects || []),
        ].join(" ").toLowerCase();
        return haystack.includes(q);
      })
      .sort((a, b) => String(b.created_iso || "").localeCompare(String(a.created_iso || "")));
  }

  function severityBadge(severity) {
    return `<span class="severity-badge severity-${htmlEscape(severity)}">${htmlEscape(severity)}</span>`;
  }

  function effectFrequencyCount(effect) {
    if (selectedSeverity === "all") return Number(effect.count || 0);
    return Number(effect.severity_counts?.[selectedSeverity] || 0);
  }

  function frequencyEffects() {
    return effects
      .map((effect) => ({ ...effect, active_count: effectFrequencyCount(effect) }))
      .filter((effect) => selectedSeverity === "all" || effect.active_count > 0)
      .sort((a, b) => b.active_count - a.active_count || b.count - a.count || a.phrase.localeCompare(b.phrase));
  }

  function reportMatchesAnyEffectSeverity(report, effectNames) {
    return selectedSeverity === "all" || effectNames.some((effect) => severityFor(report, effect) === selectedSeverity);
  }

  function filteredLinkReportIds(link) {
    return (link.report_ids || []).filter((id) => {
      const report = reportForId(id);
      return report && reportMatchesAnyEffectSeverity(report, [link.source, link.target]);
    });
  }

  function linkCountForCurrentSeverity(link) {
    if (selectedSeverity === "all") return Number(link.count || 0);
    return filteredLinkReportIds(link).length;
  }

  function effectColor(index) {
    const palette = [
      "#8bbde8", "#efb189", "#8fcdb6", "#c8abe4", "#f2a5c5", "#cadb91",
      "#9fd6d5", "#e8c18f", "#aaa9ec", "#9bd0ad", "#d3b9a3", "#b8c0ca",
    ];
    return palette[index % palette.length];
  }

  function graphEffects() {
    const rows = frequencyEffects().slice(0, graphEffectLimit);
    const selected = effects.find((item) => item.phrase === selectedEffect);
    if (
      selected &&
      effectFrequencyCount(selected) > 0 &&
      !rows.some((item) => item.phrase === selected.phrase)
    ) {
      rows.splice(Math.max(0, rows.length - 1), rows.length >= graphEffectLimit ? 1 : 0, {
        ...selected,
        active_count: effectFrequencyCount(selected),
      });
    }
    return rows;
  }

  function renderBars() {
    bars.innerHTML = "";
    if (!effects.length) return;
    const rows = frequencyEffects();
    if (!rows.length) {
      bars.innerHTML = `<p class="status">No ${htmlEscape(selectedSeverity)} side-effect labels found.</p>`;
      return;
    }
    const max = Math.max(...rows.map((item) => item.active_count));
    rows.slice(0, 18).forEach((item) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `bar-row effect-bar-row${!selectedPair && item.phrase === selectedEffect ? " active" : ""}`;
      row.innerHTML = `
        <span>${htmlEscape(item.phrase)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.max(4, item.active_count / max * 100)}%"></div></div>
        <strong>${item.active_count}</strong>
      `;
      row.addEventListener("click", () => {
        selectedEffect = item.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      bars.appendChild(row);
    });
  }

  function renderEffectNetwork() {
    network.innerHTML = "";
    const nodes = graphEffects().map((item, index) => ({ ...item, index }));
    if (!nodes.length) {
      network.setAttribute("viewBox", "0 0 760 760");
      return;
    }
    const cell = 24;
    const left = 158;
    const top = 150;
    const width = left + nodes.length * cell + 30;
    const height = top + nodes.length * cell + 52;
    network.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const nodeMap = new Map(nodes.map((item) => [item.phrase, item]));
    const visibleLinks = (explorer.links || [])
      .filter((link) => nodeMap.has(link.source) && nodeMap.has(link.target))
      .map((link) => {
        const reportIds = selectedSeverity === "all" ? (link.report_ids || []) : filteredLinkReportIds(link);
        return {
          ...link,
          active_count: selectedSeverity === "all" ? Number(link.count || reportIds.length || 0) : reportIds.length,
          active_report_ids: reportIds,
        };
      })
      .filter((link) => link.active_count > 0)
      .sort((a, b) => Number(b.active_count || 0) - Number(a.active_count || 0));
    const maxLink = Math.max(1, ...visibleLinks.map((link) => Number(link.active_count || 0)));
    const linkByPair = new Map();
    visibleLinks.forEach((link) => {
      linkByPair.set(pairKey(link.source, link.target), link);
    });

    const defs = el("defs");
    const gradient = el("linearGradient", { id: "effect-matrix-ramp-gradient", x1: "0%", y1: "0%", x2: "100%", y2: "0%" });
    gradient.appendChild(el("stop", { offset: "0%", "stop-color": "#edf3f0" }));
    gradient.appendChild(el("stop", { offset: "55%", "stop-color": "#83bfa8" }));
    gradient.appendChild(el("stop", { offset: "100%", "stop-color": "#1e745f" }));
    defs.appendChild(gradient);
    network.appendChild(defs);

    network.appendChild(el("text", {
      x: left,
      y: 28,
      class: "effect-matrix-note",
    }, "Darker cells indicate more reports mentioning both side effects."));

    const shortLabel = (phrase) => {
      if (phrase.length <= 20) return phrase;
      return `${phrase.slice(0, 18).trim()}...`;
    };

    nodes.forEach((node, index) => {
      const label = shortLabel(node.phrase);
      const y = top + index * cell + cell * 0.65;
      const rowLabel = el("text", {
        x: left - 10,
        y,
        "text-anchor": "end",
        class: `effect-matrix-label${selectedEffect === node.phrase && !selectedPair ? " active" : ""}`,
        tabindex: 0,
      }, label);
      rowLabel.appendChild(el("title", {}, `${node.phrase}: ${node.active_count} reports`));
      rowLabel.addEventListener("click", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      rowLabel.addEventListener("focus", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      network.appendChild(rowLabel);

      const x = left + index * cell + cell * 0.4;
      const columnLabel = el("text", {
        x,
        y: top - 12,
        transform: `rotate(-58 ${x} ${top - 12})`,
        "text-anchor": "start",
        class: `effect-matrix-label${selectedEffect === node.phrase && !selectedPair ? " active" : ""}`,
        tabindex: 0,
      }, label);
      columnLabel.appendChild(el("title", {}, `${node.phrase}: ${node.active_count} reports`));
      columnLabel.addEventListener("click", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      columnLabel.addEventListener("focus", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      network.appendChild(columnLabel);
    });

    nodes.forEach((row, yIndex) => {
      nodes.forEach((column, xIndex) => {
        const x = left + xIndex * cell;
        const y = top + yIndex * cell;
        if (row.phrase === column.phrase) {
          const rect = el("rect", {
            x,
            y,
            width: cell,
            height: cell,
            rx: 3,
            class: `effect-matrix-diagonal${selectedEffect === row.phrase && !selectedPair ? " selected" : ""}`,
            tabindex: 0,
          });
          rect.appendChild(el("title", {}, `${row.phrase}: ${row.active_count} reports`));
          rect.addEventListener("click", () => {
            selectedEffect = row.phrase;
            selectedPair = null;
            visibleReports = 18;
            renderAll();
          });
          rect.addEventListener("focus", () => {
            selectedEffect = row.phrase;
            selectedPair = null;
            visibleReports = 18;
            renderAll();
          });
          network.appendChild(rect);
          return;
        }
        const link = linkByPair.get(pairKey(row.phrase, column.phrase));
        if (!link) {
          network.appendChild(el("rect", { x, y, width: cell, height: cell, rx: 3, class: "effect-matrix-empty" }));
          return;
        }
        const active = selectedPair && selectedPair.source === link.source && selectedPair.target === link.target;
        const count = Number(link.active_count || 0);
        const intensity = Math.sqrt(count / maxLink);
        const cellNode = el("rect", {
          x,
          y,
          width: cell,
          height: cell,
          rx: 3,
          class: `effect-matrix-cell${active ? " selected" : ""}`,
          "data-source": link.source,
          "data-target": link.target,
          fill: `rgba(30, 116, 95, ${(0.15 + intensity * 0.78).toFixed(3)})`,
          tabindex: 0,
        });
        cellNode.appendChild(el("title", {}, `${link.source} + ${link.target}: ${count} reports`));
        const selectPair = () => {
          selectedPair = { source: link.source, target: link.target };
          selectedEffect = link.source;
          visibleReports = 18;
          renderAll();
        };
        cellNode.addEventListener("click", selectPair);
        cellNode.addEventListener("focus", selectPair);
        network.appendChild(cellNode);

        if (count >= Math.max(6, maxLink * 0.16)) {
          network.appendChild(el("text", {
            x: x + cell / 2,
            y: y + cell * 0.64,
            class: "effect-matrix-count",
          }, count));
        }
      });
    });

    const legendY = height - 24;
    network.appendChild(el("text", { x: left, y: legendY, class: "effect-matrix-legend-label" }, "Few reports"));
    network.appendChild(el("rect", { x: left + 74, y: legendY - 10, width: 160, height: 10, rx: 5, class: "effect-matrix-ramp" }));
    network.appendChild(el("text", { x: left + 248, y: legendY, class: "effect-matrix-legend-label" }, "Many reports"));
  }

  function renderDetailPanel(filteredReports) {
    if (selectedPair) {
      const link = linkForPair(selectedPair.source, selectedPair.target);
      const reportsWithPair = link?.report_ids?.length || 0;
      const visibleLabel = selectedSeverity === "all" ? "visible" : `${selectedSeverity} visible`;
      detail.innerHTML = `
        <div class="effect-summary-card">
          <div class="effect-summary-head">
            <span class="effect-summary-kicker">Selected co-occurrence</span>
            <h2>${htmlEscape(selectionLabel())}</h2>
          </div>
          <div class="effect-summary-metrics" aria-label="Co-occurrence summary">
            <span><b>${reportsWithPair}</b> co-occurrence reports</span>
            <span><b>${filteredReports.length}</b> ${htmlEscape(visibleLabel)}</span>
            <span>${htmlEscape(selectedPair.source)}</span>
            <span>${htmlEscape(selectedPair.target)}</span>
          </div>
          <p class="note">Reports below mention both extracted side effects in the same Reddit item.</p>
        </div>
      `;
      return;
    }
    const effect = effects.find((item) => item.phrase === selectedEffect);
    if (!effect) {
      detail.innerHTML = "<h2>Effect detail</h2><p>No side effects extracted yet.</p>";
      return;
    }
    const counts = effect.severity_counts || {};
    const topPartners = (explorer.links || [])
      .filter((link) => link.source === selectedEffect || link.target === selectedEffect)
      .map((link) => ({
        phrase: link.source === selectedEffect ? link.target : link.source,
        count: linkCountForCurrentSeverity(link),
      }))
      .filter((partner) => partner.count > 0)
      .sort((a, b) => b.count - a.count)
      .slice(0, 6);
    const currentEffectCount = selectedSeverity === "all" ? Number(effect.count || 0) : Number(counts[selectedSeverity] || 0);
    const coMentionHtml = topPartners.length
      ? topPartners.map((partner) => `<span>${htmlEscape(partner.phrase)} <b>${partner.count}</b></span>`).join("")
      : "<span>none</span>";
    detail.innerHTML = `
      <div class="effect-summary-card">
        <div class="effect-summary-head">
          <span class="effect-summary-kicker">Selected side effect</span>
          <h2>${htmlEscape(selectedEffect)}</h2>
        </div>
        <div class="effect-summary-metrics" aria-label="Side-effect summary">
          <span><b>${currentEffectCount}</b> ${selectedSeverity === "all" ? "total" : htmlEscape(selectedSeverity)}</span>
          <span><b>${filteredReports.length}</b> visible</span>
          <span><b>${counts.mild || 0}</b> mild</span>
          <span><b>${counts.moderate || 0}</b> moderate</span>
          <span><b>${counts.severe || 0}</b> severe</span>
          <span><b>${counts.unscreened || 0}</b> unscreened</span>
        </div>
        <div class="co-mention-strip" aria-label="Common co-mentions">
          <strong>Co-mentions</strong>
          ${coMentionHtml}
        </div>
        <p class="note">Severity labels appear after the one-report screen; unscreened reports are still waiting.</p>
      </div>
    `;
  }

  function reportCard(report) {
    const primaryEffect = activeEffects()[0] || selectedEffect;
    const severity = severityFor(report, primaryEffect);
    const effectsHtml = (report.effects || [])
      .map((effect) => `<button type="button" class="effect-chip${activeEffects().includes(effect) ? " active" : ""}" data-effect="${htmlEscape(effect)}">${htmlEscape(effect)}</button>`)
      .join("");
    return `
      <article class="effect-report-card">
        <div class="report-topline">
          <h3>${htmlEscape(report.created_iso || "unknown date")} · r/${htmlEscape(report.subreddit || "unknown")}</h3>
          ${severityBadge(severity)}
        </div>
        <p><strong>${htmlEscape(reportDrugLabel(report))}</strong> ${htmlEscape(report.dose_strong || "")}</p>
        ${testimonialHtml(report.text_excerpt || report.full_text || "", "Qualitative excerpt")}
        <a class="reddit-link" href="${htmlEscape(report.url || "#")}" target="_blank" rel="noopener">Open Reddit URL</a>
        <div class="effect-chip-row">${effectsHtml}</div>
        <details class="extracted-fields">
          <summary>Extracted fields</summary>
          <dl class="detail-list compact">
            <div><dt>Severity source</dt><dd>${htmlEscape(severitySourceFor(report, primaryEffect))}</dd></div>
            <div><dt>Severity confidence</dt><dd>${htmlEscape(severityConfidenceFor(report, primaryEffect))}</dd></div>
            <div><dt>Severity rationale</dt><dd>${htmlEscape(severityRationaleFor(report, primaryEffect))}</dd></div>
            <div><dt>Attribution</dt><dd>${htmlEscape(report.attribution || "n/a")}</dd></div>
            <div><dt>Evidence</dt><dd>${htmlEscape(report.evidence || "n/a")}</dd></div>
            <div><dt>Notes</dt><dd>${htmlEscape(report.notes || "n/a")}</dd></div>
          </dl>
        </details>
        <details class="source-text">
          <summary>Full Reddit text</summary>
          ${testimonialHtml(report.full_text || report.text_excerpt || "", "Full qualitative text")}
        </details>
      </article>
    `;
  }

  function bindReportChips() {
    feed.querySelectorAll("[data-effect]").forEach((button) => {
      button.addEventListener("click", () => {
        selectedEffect = button.dataset.effect;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
    });
  }

  function renderFeed(filteredReports) {
    feedCount.textContent = `${filteredReports.length} matching reports for "${selectionLabel()}"`;
    const visible = filteredReports.slice(0, visibleReports);
    feed.innerHTML = visible.map(reportCard).join("") || '<p class="status">No reports match the current filters.</p>';
    bindReportChips();
    sentinel.hidden = visibleReports >= filteredReports.length;
    if (!sentinel.hidden) {
      sentinel.textContent = "Loading more reports...";
    } else {
      sentinel.textContent = "";
    }
  }

  function renderAll() {
    if (!effects.length) {
      status.textContent = "No side effects extracted yet.";
      bars.innerHTML = "";
      network.innerHTML = "";
      feed.innerHTML = "";
      detail.innerHTML = "<h2>Effect detail</h2>";
      return;
    }
    const filteredReports = selectedReports();
    const severityText = selectedSeverity === "all" ? "all severities" : `${selectedSeverity} labels`;
    status.textContent = `${explorer.summary?.reports_with_side_effects || Object.keys(reports).length} reports with side-effect mentions, ${effects.length} normalized phrases; frequency and co-occurrence show ${severityText}.`;
    renderBars();
    renderEffectNetwork();
    renderDetailPanel(filteredReports);
    renderFeed(filteredReports);
  }

  search?.addEventListener("input", () => {
    query = search.value || "";
    visibleReports = 18;
    renderAll();
  });
  document.querySelectorAll("[data-severity]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedSeverity = button.dataset.severity || "all";
      document.querySelectorAll("[data-severity]").forEach((item) => item.classList.toggle("active", item === button));
      visibleReports = 18;
      renderAll();
    });
  });
  if (observer) observer.disconnect();
  observer = new IntersectionObserver((entries) => {
    if (!entries.some((entry) => entry.isIntersecting)) return;
    const filteredReports = selectedReports();
    if (visibleReports < filteredReports.length) {
      visibleReports += 12;
      renderFeed(filteredReports);
    }
  }, { rootMargin: "500px" });
  if (sentinel) observer.observe(sentinel);

  if (!effects.length) {
    status.textContent = "No side effects extracted yet.";
    return;
  }
  renderAll();
}

function networkFamilyClass(family) {
  return `compound-family-${String(family || "unclear").replace(/[^a-z0-9_-]/gi, "-").toLowerCase()}`;
}

const COMPOUND_COLORS = {
  reta: "#8fcdb6",
  tirz: "#8bbde8",
  sema: "#efb189",
  amylin: "#c8abe4",
  glp1_other: "#9fd6d5",
  stimulant: "#f2a5c5",
  diabetes_drug: "#cadb91",
  hormone: "#e8c18f",
  peptide: "#aaa9ec",
  supplement: "#9bd0ad",
  other_drug: "#d3b9a3",
  lifestyle: "#ddd39d",
  unclear: "#b8c0ca",
};

function compoundColor(family) {
  return COMPOUND_COLORS[family] || COMPOUND_COLORS.unclear;
}

function polarPoint(cx, cy, radius, angle) {
  return {
    x: cx + Math.cos(angle) * radius,
    y: cy + Math.sin(angle) * radius,
  };
}

function arcPath(cx, cy, radius, startAngle, endAngle) {
  const start = polarPoint(cx, cy, radius, startAngle);
  const end = polarPoint(cx, cy, radius, endAngle);
  const largeArc = Math.abs(endAngle - startAngle) > Math.PI ? 1 : 0;
  return `M${start.x.toFixed(2)},${start.y.toFixed(2)} A${radius},${radius} 0 ${largeArc} 1 ${end.x.toFixed(2)},${end.y.toFixed(2)}`;
}

function radialTextTransform(cx, cy, radius, angle) {
  const point = polarPoint(cx, cy, radius, angle);
  let rotation = angle * 180 / Math.PI + 90;
  if (angle > Math.PI / 2 && angle < Math.PI * 1.5) rotation += 180;
  return {
    x: point.x,
    y: point.y,
    transform: `rotate(${rotation.toFixed(2)} ${point.x.toFixed(2)} ${point.y.toFixed(2)})`,
  };
}

function tickStep(maxValue) {
  if (maxValue >= 40) return 10;
  if (maxValue >= 18) return 5;
  if (maxValue >= 8) return 2;
  return 1;
}

function attributionText(counts) {
  return Object.entries(counts || {})
    .sort((a, b) => b[1] - a[1])
    .map(([key, value]) => `${key}: ${value}`)
    .join("; ") || "n/a";
}

function reportMiniCard(report) {
  const compounds = (report.compounds || []).map(compoundDisplayName).join(", ");
  const raw = (report.other_compounds_raw || []).map(compoundDisplayName).join(", ");
  return `
    <article class="report-mini">
      <h3>${htmlEscape(report.created_iso || "unknown date")} · r/${htmlEscape(report.subreddit || "unknown")}</h3>
      <p><strong>${htmlEscape(reportDrugLabel(report))}</strong> with ${htmlEscape(raw || compounds || "n/a")}</p>
      ${testimonialHtml(report.text_excerpt || report.full_text || "", "Qualitative excerpt")}
      <a class="reddit-link" href="${htmlEscape(report.url || "#")}" target="_blank" rel="noopener">Open Reddit URL</a>
      <details class="extracted-fields">
        <summary>Extracted fields</summary>
        <dl class="detail-list compact">
          <div><dt>Normalized</dt><dd>${htmlEscape(compounds || "n/a")}</dd></div>
          <div><dt>Attribution</dt><dd>${htmlEscape(report.attribution || "n/a")}</dd></div>
          <div><dt>Dose</dt><dd>${htmlEscape(report.dose_strong || "n/a")}</dd></div>
          <div><dt>Duration</dt><dd>${htmlEscape(report.duration_raw || "n/a")} (${fmt(report.duration_weeks, 1, " weeks")})</dd></div>
          <div><dt>Weight change</dt><dd>${fmt(report.weight_change_kg, 1, " kg")}</dd></div>
          <div><dt>Evidence</dt><dd>${htmlEscape(report.evidence || "n/a")}</dd></div>
          <div><dt>Notes</dt><dd>${htmlEscape(report.notes || "n/a")}</dd></div>
        </dl>
      </details>
      <details class="source-text">
        <summary>Full Reddit text</summary>
        ${testimonialHtml(report.full_text || report.text_excerpt || "", "Full qualitative text")}
      </details>
    </article>
  `;
}

function selectedReportIds(item, mode) {
  return mode === "stack" ? (item.stack_report_ids || []) : (item.report_ids || []);
}

function renderNetworkLinkDetail(link, data, mode) {
  const detail = document.getElementById("network-detail");
  const reportIds = selectedReportIds(link, mode);
  const reports = reportIds.map((id) => data.reports[String(id)] || data.reports[id]).filter(Boolean);
  detail.innerHTML = `
    <h2>${htmlEscape(compoundDisplayName(link.source))} + ${htmlEscape(compoundDisplayName(link.target))}</h2>
    <dl class="detail-list">
      <div><dt>Reports</dt><dd>${reportIds.length}</dd></div>
      <div><dt>All-report count</dt><dd>${link.count}</dd></div>
      <div><dt>Stack-only count</dt><dd>${link.stack_count}</dd></div>
      <div><dt>Attribution mix</dt><dd>${htmlEscape(attributionText(link.attribution_counts))}</dd></div>
    </dl>
    <h3>Contributing reports</h3>
    ${reports.slice(0, 30).map(reportMiniCard).join("") || "<p>No reports in this mode.</p>"}
  `;
}

function renderNetworkNodeDetail(node, data, mode) {
  const detail = document.getElementById("network-detail");
  const reportIds = selectedReportIds(node, mode);
  const reports = reportIds.map((id) => data.reports[String(id)] || data.reports[id]).filter(Boolean);
  detail.innerHTML = `
    <h2>${htmlEscape(compoundDisplayName(node.label))}</h2>
    <dl class="detail-list">
      <div><dt>Family</dt><dd>${htmlEscape(familyName(node.family))}</dd></div>
      <div><dt>Reports</dt><dd>${reportIds.length}</dd></div>
      <div><dt>All-report count</dt><dd>${node.count}</dd></div>
      <div><dt>Stack-only count</dt><dd>${node.stack_count}</dd></div>
    </dl>
    <h3>Contributing reports</h3>
    ${reports.slice(0, 30).map(reportMiniCard).join("") || "<p>No reports in this mode.</p>"}
  `;
}

function renderNormalizationAudit(data) {
  const audit = document.getElementById("normalization-audit");
  if (!audit) return;
  const stats = data.normalization?.stats || {};
  const unresolved = data.normalization?.unresolved_terms || [];
  audit.innerHTML = `
    <div>
      <h3>Normalization source</h3>
      <dl class="detail-list">
        <div><dt>Source</dt><dd>${htmlEscape(stats.source || "alias_only")}</dd></div>
        <div><dt>Raw names</dt><dd>${htmlEscape(stats.raw_names ?? "n/a")}</dd></div>
        <div><dt>Alias normalized</dt><dd>${htmlEscape(stats.alias_normalized ?? "n/a")}</dd></div>
        <div><dt>Nano normalized</dt><dd>${htmlEscape(stats.openai_normalized ?? "n/a")}</dd></div>
        <div><dt>Remaining unresolved</dt><dd>${htmlEscape(stats.unresolved_remaining ?? "n/a")}</dd></div>
      </dl>
    </div>
    <div>
      <h3>Top unresolved terms</h3>
      ${
        unresolved.length
          ? `<table><thead><tr><th>Raw term</th><th>Count</th></tr></thead><tbody>${unresolved
              .slice(0, 20)
              .map((item) => `<tr><td>${htmlEscape(item.raw)}</td><td>${item.count}</td></tr>`)
              .join("")}</tbody></table>`
          : "<p>No unresolved terms in rendered graph data.</p>"
      }
    </div>
  `;
}

function renderConcurrent(data, mode = "all") {
  const svg = document.getElementById("compound-network");
  const status = document.getElementById("network-status");
  svg.innerHTML = "";
  const countKey = mode === "stack" ? "stack_count" : "count";
  const links = (data.links || []).filter((link) => Number(link[countKey] || 0) > 0);
  const linkedNames = new Set();
  links.forEach((link) => {
    linkedNames.add(link.source);
    linkedNames.add(link.target);
  });
  const familyOrder = ["reta", "tirz", "sema", "amylin", "glp1_other", "diabetes_drug", "stimulant", "hormone", "peptide", "supplement", "other_drug", "lifestyle", "unclear"];
  const familyRank = (family) => {
    const index = familyOrder.indexOf(family);
    return index === -1 ? familyOrder.length : index;
  };
  const nodes = (data.nodes || [])
    .filter((node) => linkedNames.has(node.id) && Number(node[countKey] || 0) > 0)
    .sort((a, b) =>
      Number(b[countKey] || 0) - Number(a[countKey] || 0)
      || familyRank(a.family) - familyRank(b.family)
      || String(a.label || a.id).localeCompare(String(b.label || b.id))
    );

  renderNormalizationAudit(data);
  if (!nodes.length || !links.length) {
    status.textContent = "No concurrent-use reports available for this mode.";
    svg.setAttribute("viewBox", "0 0 980 820");
    return;
  }
  const limit = 24;
  const visibleNodes = nodes.slice(0, limit);
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  const visibleLinks = links.filter((link) => visibleIds.has(link.source) && visibleIds.has(link.target));
  status.textContent = `${data.summary?.reports || 0} reports, top ${visibleNodes.length} of ${nodes.length} compounds by ${mode === "stack" ? "stack-only" : "all-concurrent"} report count shown, ${visibleLinks.length} pair connections in the matrix. Darker squares indicate more reports; click a square to inspect the source posts.`;
  const linkByPair = new Map();
  visibleLinks.forEach((link) => {
    linkByPair.set([link.source, link.target].sort().join("|||"), link);
  });

  const cell = 24;
  const left = 150;
  const top = 150;
  const width = left + visibleNodes.length * cell + 28;
  const height = top + visibleNodes.length * cell + 48;
  const maxLink = Math.max(...visibleLinks.map((link) => Number(link[countKey] || 0)), 1);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const defs = el("defs");
  const gradient = el("linearGradient", { id: "matrix-ramp-gradient", x1: "0%", y1: "0%", x2: "100%", y2: "0%" });
  gradient.appendChild(el("stop", { offset: "0%", "stop-color": "#f4e7dc" }));
  gradient.appendChild(el("stop", { offset: "55%", "stop-color": "#d0905e" }));
  gradient.appendChild(el("stop", { offset: "100%", "stop-color": "#7b3518" }));
  defs.appendChild(gradient);
  svg.appendChild(defs);

  svg.appendChild(el("text", {
    x: left,
    y: 28,
    class: "matrix-note",
  }, "Darker cells indicate more Reddit reports mentioning both compounds."));

  visibleNodes.forEach((node, index) => {
    const y = top + index * cell + cell * 0.65;
    const label = compoundDisplayName(node.label || node.id);
    const rowLabel = el("text", {
      x: left - 10,
      y,
      "text-anchor": "end",
      class: "matrix-label matrix-row-label",
      tabindex: 0,
    }, label);
    rowLabel.addEventListener("click", () => renderNetworkNodeDetail(node, data, mode));
    rowLabel.addEventListener("focus", () => renderNetworkNodeDetail(node, data, mode));
    svg.appendChild(rowLabel);

    const x = left + index * cell + cell * 0.4;
    const columnLabel = el("text", {
      x,
      y: top - 12,
      transform: `rotate(-58 ${x} ${top - 12})`,
      "text-anchor": "start",
      class: "matrix-label matrix-column-label",
      tabindex: 0,
    }, label);
    columnLabel.addEventListener("click", () => renderNetworkNodeDetail(node, data, mode));
    columnLabel.addEventListener("focus", () => renderNetworkNodeDetail(node, data, mode));
    svg.appendChild(columnLabel);
  });

  visibleNodes.forEach((row, yIndex) => {
    visibleNodes.forEach((column, xIndex) => {
      const x = left + xIndex * cell;
      const y = top + yIndex * cell;
      const pairKey = [row.id, column.id].sort().join("|||");
      if (row.id === column.id) {
        svg.appendChild(el("rect", { x, y, width: cell, height: cell, rx: 3, class: "matrix-diagonal" }));
        return;
      }
      const link = linkByPair.get(pairKey);
      if (!link) {
        svg.appendChild(el("rect", { x, y, width: cell, height: cell, rx: 3, class: "matrix-empty" }));
        return;
      }
      const count = Number(link[countKey] || 0);
      const intensity = Math.sqrt(count / maxLink);
      const cellNode = el("rect", {
        x,
        y,
        width: cell,
        height: cell,
        rx: 3,
        class: "matrix-cell",
        "data-source": row.id,
        "data-target": column.id,
        fill: `rgba(151, 73, 31, ${(0.16 + intensity * 0.82).toFixed(3)})`,
        tabindex: 0,
      });
      cellNode.appendChild(el("title", {}, `${compoundDisplayName(link.source)} + ${compoundDisplayName(link.target)}: ${count} reports`));
      const activate = () => {
        svg.querySelectorAll(".matrix-cell.selected").forEach((item) => item.classList.remove("selected"));
        cellNode.classList.add("selected");
        renderNetworkLinkDetail(link, data, mode);
      };
      cellNode.addEventListener("click", activate);
      cellNode.addEventListener("focus", activate);
      svg.appendChild(cellNode);

      if (count >= Math.max(8, maxLink * 0.12)) {
        svg.appendChild(el("text", {
          x: x + cell / 2,
          y: y + cell * 0.64,
          class: "matrix-count",
        }, count));
      }
    });
  });

  const legendY = height - 24;
  svg.appendChild(el("text", { x: left, y: legendY, class: "matrix-legend-label" }, "Few reports"));
  svg.appendChild(el("rect", { x: left + 74, y: legendY - 10, width: 170, height: 10, rx: 5, class: "matrix-ramp" }));
  svg.appendChild(el("text", { x: left + 258, y: legendY, class: "matrix-legend-label" }, "Many reports"));

  if (visibleLinks[0]) renderNetworkLinkDetail(visibleLinks[0], data, mode);
}

document.addEventListener("DOMContentLoaded", async () => {
  const view = document.body.dataset.view;
  if (!view) return;
  try {
    const data = await loadPageData();
    if (view === "scatter") renderScatter(data);
    if (view === "side-effects") renderSideEffects(data);
    if (view === "concurrent") {
      let mode = "all";
      document.querySelectorAll("[data-network-mode]").forEach((button) => {
        button.addEventListener("click", () => {
          mode = button.dataset.networkMode;
          document.querySelectorAll("[data-network-mode]").forEach((item) => item.classList.toggle("active", item === button));
          renderConcurrent(data, mode);
        });
      });
      renderConcurrent(data, mode);
    }
  } catch (error) {
    const status = document.getElementById("plot-status") || document.getElementById("network-status") || document.querySelector(".status");
    if (status) status.textContent = error.message;
  }
});
