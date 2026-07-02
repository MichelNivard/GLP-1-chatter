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

const MAX_PLOTTED_WEIGHT_GAIN_KG = 10;

function familyName(family) {
  return FAMILY_NAMES[family] || family || "unknown drug";
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
  const graphEffectLimit = 56;

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

  function selectionLabel() {
    if (selectedPair) return `${selectedPair.source} + ${selectedPair.target}`;
    return selectedEffect || "side effects";
  }

  function activeReportIds() {
    if (selectedPair) return selectedPair.report_ids || [];
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

  function effectColor(index) {
    const palette = [
      "#8bbde8", "#efb189", "#8fcdb6", "#c8abe4", "#f2a5c5", "#cadb91",
      "#9fd6d5", "#e8c18f", "#aaa9ec", "#9bd0ad", "#d3b9a3", "#b8c0ca",
    ];
    return palette[index % palette.length];
  }

  function graphEffects() {
    const names = new Set();
    effects.slice(0, graphEffectLimit).forEach((item) => names.add(item.phrase));
    return effects.filter((item) => names.has(item.phrase));
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
      network.setAttribute("viewBox", "0 0 900 720");
      return;
    }
    const width = 960;
    const height = 760;
    const cx = width / 2;
    const cy = height / 2;
    const arcRadius = 280;
    const ribbonRadius = 218;
    const labelRadius = 330;
    network.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const nodeMap = new Map(nodes.map((item) => [item.phrase, item]));
    const visibleLinks = (explorer.links || [])
      .filter((link) => nodeMap.has(link.source) && nodeMap.has(link.target))
      .sort((a, b) => Number(a.count || 0) - Number(b.count || 0));
    const maxLink = Math.max(1, ...visibleLinks.map((link) => Number(link.count || 0)));
    const total = nodes.reduce((sum, item) => sum + Math.max(1, Number(item.count || 0)), 0);
    const gap = nodes.length > 50 ? 0.018 : 0.028;
    const usableAngle = Math.PI * 2 - gap * nodes.length;
    const minSpan = Math.min(0.045, usableAngle / nodes.length * 0.4);
    const weightedAngle = Math.max(0.1, usableAngle - minSpan * nodes.length);
    let cursor = -Math.PI / 2;
    const positions = new Map();

    nodes.forEach((node) => {
      const count = Math.max(1, Number(node.count || 0));
      const span = minSpan + weightedAngle * (count / total);
      const startAngle = cursor + gap / 2;
      const endAngle = cursor + span - gap / 2;
      const angle = (startAngle + endAngle) / 2;
      const point = polarPoint(cx, cy, ribbonRadius, angle);
      positions.set(node.phrase, { x: point.x, y: point.y, angle, startAngle, endAngle, span });
      cursor += span;
    });

    network.appendChild(el("circle", { cx, cy, r: arcRadius + 28, class: "effect-ring effect-ring-outer" }));
    network.appendChild(el("circle", { cx, cy, r: arcRadius - 20, class: "effect-ring effect-ring-inner" }));
    network.appendChild(el("circle", { cx, cy, r: ribbonRadius - 26, class: "effect-core" }));

    const edgeLayer = el("g", { class: "effect-chord-edges" });
    visibleLinks.forEach((link) => {
      const source = positions.get(link.source);
      const target = positions.get(link.target);
      if (!source || !target) return;
      const sourceNode = nodeMap.get(link.source);
      const color = effectColor(sourceNode.index);
      const active = selectedPair && selectedPair.source === link.source && selectedPair.target === link.target;
      const touchesSelection = activeEffects().includes(link.source) || activeEffects().includes(link.target);
      const d = `M${source.x.toFixed(2)},${source.y.toFixed(2)} C${cx.toFixed(2)},${cy.toFixed(2)} ${cx.toFixed(2)},${cy.toFixed(2)} ${target.x.toFixed(2)},${target.y.toFixed(2)}`;
      const strokeWidth = 0.9 + Math.sqrt(Number(link.count || 0) / maxLink) * 9;
      const selectPair = () => {
        selectedPair = { source: link.source, target: link.target, report_ids: link.report_ids || [] };
        selectedEffect = link.source;
        visibleReports = 18;
        renderAll();
      };
      const path = el("path", {
        d,
        class: `effect-chord-edge${active ? " active" : ""}${touchesSelection ? " related" : ""}`,
        style: `stroke:${color}`,
        "stroke-width": strokeWidth.toFixed(2),
      });
      const hitPath = el("path", {
        d,
        class: "effect-chord-hit",
        "stroke-width": Math.max(14, strokeWidth + 10).toFixed(2),
        tabindex: 0,
      });
      hitPath.appendChild(el("title", {}, `${link.source} + ${link.target}: ${link.count} reports`));
      hitPath.addEventListener("mouseenter", () => path.classList.add("hovered"));
      hitPath.addEventListener("mouseleave", () => path.classList.remove("hovered"));
      hitPath.addEventListener("click", selectPair);
      hitPath.addEventListener("focus", selectPair);
      edgeLayer.appendChild(path);
      edgeLayer.appendChild(hitPath);
    });
    network.appendChild(edgeLayer);

    const arcLayer = el("g", { class: "effect-chord-arcs" });
    nodes.forEach((node) => {
      const position = positions.get(node.phrase);
      const color = effectColor(node.index);
      const active = activeEffects().includes(node.phrase);
      const tier = node.index < 12 ? "major" : (node.index < 28 ? "middle" : "tail");
      const group = el("g", {
        class: `effect-chord-node effect-chord-node-${tier}${active ? " active" : ""}`,
      });
      const arc = el("path", {
        d: arcPath(cx, cy, arcRadius, position.startAngle, position.endAngle),
        class: `effect-chord-arc effect-chord-arc-${tier}${active ? " active" : ""}`,
        style: `stroke:${color}`,
        tabindex: 0,
      });
      arc.appendChild(el("title", {}, `${node.phrase}: ${node.count} reports`));
      arc.addEventListener("click", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      arc.addEventListener("focus", () => {
        selectedEffect = node.phrase;
        selectedPair = null;
        visibleReports = 18;
        renderAll();
      });
      group.appendChild(arc);

      const labelPosition = radialTextTransform(cx, cy, labelRadius, position.angle);
      const label = node.phrase.length > 26 ? `${node.phrase.slice(0, 24)}...` : node.phrase;
      group.appendChild(el("text", {
        x: labelPosition.x.toFixed(2),
        y: labelPosition.y.toFixed(2),
        transform: labelPosition.transform,
        "text-anchor": "middle",
        class: "effect-chord-label",
      }, label));
      arcLayer.appendChild(group);
    });
    network.appendChild(arcLayer);
  }

  function renderDetailPanel(filteredReports) {
    if (selectedPair) {
      const reportsWithPair = selectedPair.report_ids?.length || 0;
      detail.innerHTML = `
        <div class="effect-summary-card">
          <div class="effect-summary-head">
            <span class="effect-summary-kicker">Selected co-occurrence</span>
            <h2>${htmlEscape(selectionLabel())}</h2>
          </div>
          <div class="effect-summary-metrics" aria-label="Co-occurrence summary">
            <span><b>${reportsWithPair}</b> co-occurrence reports</span>
            <span><b>${filteredReports.length}</b> visible</span>
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
      .sort((a, b) => b.count - a.count)
      .slice(0, 6)
      .map((link) => ({
        phrase: link.source === selectedEffect ? link.target : link.source,
        count: link.count,
      }));
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
          <span><b>${effect.count}</b> total</span>
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
    status.textContent = `${explorer.summary?.reports_with_side_effects || Object.keys(reports).length} reports with side-effect mentions, ${effects.length} normalized phrases; frequency shows ${severityText}.`;
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
  const compounds = (report.compounds || []).join(", ");
  const raw = (report.other_compounds_raw || []).join(", ");
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
    <h2>${htmlEscape(link.source)} + ${htmlEscape(link.target)}</h2>
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
    <h2>${htmlEscape(node.label)}</h2>
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
  const nodes = (data.nodes || [])
    .filter((node) => linkedNames.has(node.id) && Number(node[countKey] || 0) > 0)
    .sort((a, b) => {
      const familyDiff = familyOrder.indexOf(a.family) - familyOrder.indexOf(b.family);
      if (familyDiff) return familyDiff;
      return b[countKey] - a[countKey] || a.label.localeCompare(b.label);
    });

  renderNormalizationAudit(data);
  if (!nodes.length || !links.length) {
    status.textContent = "No concurrent-use reports available for this mode.";
    svg.setAttribute("viewBox", "0 0 980 820");
    return;
  }
  status.textContent = `${data.summary?.reports || 0} reports, ${nodes.length} compounds, ${links.length} connections shown (${mode === "stack" ? "stack-only" : "all concurrent mentions"}). Hover or focus an outer arc to show its compound name.`;

  const width = 980;
  const height = 820;
  const cx = width / 2;
  const cy = height / 2;
  const arcRadius = 295;
  const ribbonRadius = 246;
  const labelRadius = 365;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  svg.appendChild(el("circle", { cx, cy, r: ribbonRadius - 24, class: "network-core" }));

  const nodeCounts = nodes.map((node) => Math.max(1, Number(node[countKey] || 0)));
  const totalCount = nodeCounts.reduce((sum, value) => sum + value, 0);
  const gap = nodes.length > 14 ? 0.032 : 0.048;
  const usableAngle = Math.PI * 2 - gap * nodes.length;
  const minSpan = Math.min(0.09, usableAngle / nodes.length * 0.35);
  const weightedAngle = Math.max(0.1, usableAngle - minSpan * nodes.length);
  let cursor = -Math.PI / 2;
  const positions = new Map();
  nodes.forEach((node, index) => {
    const count = nodeCounts[index];
    const span = minSpan + weightedAngle * (count / totalCount);
    const startAngle = cursor + gap / 2;
    const endAngle = cursor + span - gap / 2;
    const angle = (startAngle + endAngle) / 2;
    const ribbonPoint = polarPoint(cx, cy, ribbonRadius, angle);
    positions.set(node.id, {
      x: ribbonPoint.x,
      y: ribbonPoint.y,
      angle,
      startAngle,
      endAngle,
      span,
    });
    cursor += span;
  });

  const arcLayer = el("g", { class: "network-arcs" });
  nodes.forEach((node) => {
    const position = positions.get(node.id);
    const count = Number(node[countKey] || 0);
    const color = compoundColor(node.family);
    const group = el("g", { class: "network-node-group" });
    const arc = el("path", {
      d: arcPath(cx, cy, arcRadius, position.startAngle, position.endAngle),
      class: "network-arc",
      style: `stroke:${color}`,
      tabindex: 0,
    });
    arc.appendChild(el("title", {}, `${node.label}: ${count} reports`));
    arc.addEventListener("click", () => renderNetworkNodeDetail(node, data, mode));
    arc.addEventListener("mouseenter", () => renderNetworkNodeDetail(node, data, mode));
    arc.addEventListener("focus", () => renderNetworkNodeDetail(node, data, mode));
    group.appendChild(arc);

    const step = tickStep(count);
    const tickValues = new Set([0, count]);
    if (count >= step * 2) {
      for (let value = step; value < count; value += step) {
        tickValues.add(value);
      }
    }
    [...tickValues].sort((a, b) => a - b).forEach((value) => {
      const valueAngle = position.startAngle + (position.endAngle - position.startAngle) * (count ? value / count : 0);
      const inner = polarPoint(cx, cy, arcRadius + 17, valueAngle);
      const outer = polarPoint(cx, cy, arcRadius + (value === 0 || value === count ? 30 : 24), valueAngle);
      group.appendChild(el("line", {
        x1: inner.x.toFixed(2),
        y1: inner.y.toFixed(2),
        x2: outer.x.toFixed(2),
        y2: outer.y.toFixed(2),
        class: "network-tick",
      }));
    });

    const labelPosition = radialTextTransform(cx, cy, labelRadius + 10, position.angle);
    group.appendChild(el("text", {
      x: labelPosition.x.toFixed(2),
      y: labelPosition.y.toFixed(2),
      transform: labelPosition.transform,
      "text-anchor": "middle",
      class: "network-radial-label",
    }, `${node.label} (${count})`));
    arcLayer.appendChild(group);
  });
  svg.appendChild(arcLayer);

  const maxLink = Math.max(...links.map((link) => Number(link[countKey] || 0)));
  const edgeLayer = el("g", { class: "network-edges" });
  links
    .slice()
    .sort((a, b) => Number(a[countKey] || 0) - Number(b[countKey] || 0))
    .forEach((link) => {
    const source = positions.get(link.source);
    const target = positions.get(link.target);
    if (!source || !target) return;
    const count = Number(link[countKey] || 0);
    const sourceNode = nodes.find((node) => node.id === link.source);
    const color = compoundColor(sourceNode?.family);
    const path = el("path", {
      d: `M${source.x.toFixed(2)},${source.y.toFixed(2)} C${cx.toFixed(2)},${cy.toFixed(2)} ${cx.toFixed(2)},${cy.toFixed(2)} ${target.x.toFixed(2)},${target.y.toFixed(2)}`,
      class: "network-edge",
      style: `stroke:${color}`,
      "stroke-width": (1.8 + Math.sqrt(count / maxLink) * 18).toFixed(2),
      tabindex: 0,
    });
    path.appendChild(el("title", {}, `${link.source} + ${link.target}: ${count} reports`));
    path.addEventListener("click", () => renderNetworkLinkDetail(link, data, mode));
    path.addEventListener("mouseenter", () => renderNetworkLinkDetail(link, data, mode));
    path.addEventListener("focus", () => renderNetworkLinkDetail(link, data, mode));
    edgeLayer.appendChild(path);
  });
  svg.appendChild(edgeLayer);

  const legendItems = [
    ["reta", "Retatrutide"],
    ["tirz", "Tirzepatide"],
    ["sema", "Semaglutide"],
    ["amylin", "Amylin"],
    ["peptide", "Peptide"],
    ["hormone", "Hormone"],
    ["other_drug", "Other drug"],
  ];
  const legend = el("g", { class: "network-legend" });
  legendItems.forEach(([family, label], index) => {
    const y = 26 + index * 22;
    legend.appendChild(el("rect", { x: 16, y: y - 7, width: 16, height: 10, rx: 2, class: networkFamilyClass(family), style: `fill:${compoundColor(family)}` }));
    legend.appendChild(el("text", { x: 38, y: y + 4 }, label));
  });
  svg.appendChild(legend);

  if (links[0]) renderNetworkLinkDetail(links[0], data, mode);
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
