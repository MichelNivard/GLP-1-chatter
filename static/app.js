const NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}, text = "") {
  const node = document.createElementNS(NS, name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text) node.textContent = text;
  return node;
}

function htmlEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmt(value, digits = 1, suffix = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return `${Number(value).toFixed(digits)}${suffix}`;
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

function renderDetail(point) {
  const detail = document.getElementById("detail");
  const compounds = (point.other_compounds_concurrent || []).join(", ") || "none stated";
  const effects = (point.side_effects || []).join("; ") || "none extracted";
  const drift = point.content_changed_after_processing
    ? "<p class=\"note\">Latest stored Reddit text changed after this item was processed; the text below is the processed version used for extraction.</p>"
    : "";
  detail.innerHTML = `
    <h2>${htmlEscape(point.drug_name_mentioned || point.drug_family)}</h2>
    ${drift}
    <h3>Original text</h3>
    <pre>${htmlEscape(point.processed_full_text || point.full_text || "")}</pre>
    <a class="reddit-link" href="${htmlEscape(point.url || "#")}" target="_blank" rel="noopener">Open Reddit URL</a>
    <dl class="detail-list">
      <div><dt>Post date</dt><dd>${htmlEscape(point.created_iso || "n/a")}</dd></div>
      <div><dt>Subreddit</dt><dd>r/${htmlEscape(point.subreddit)}</dd></div>
      <div><dt>Drug</dt><dd>${htmlEscape(point.drug_family)} / ${htmlEscape(point.drug_name_mentioned || "n/a")}</dd></div>
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
  `;
}

function renderScatter(data) {
  const svg = document.getElementById("scatterplot");
  const status = document.getElementById("plot-status");
  const points = data.points || [];
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

  if (!points.length && !rctSeries.length) {
    status.textContent = "No plottable reports yet.";
    svg.setAttribute("viewBox", "0 0 900 520");
    return;
  }
  status.textContent = points.length ? "" : "No plottable Reddit reports yet; showing trial overlay.";

  const width = 920;
  const height = 560;
  const margin = { top: 30, right: 32, bottom: 72, left: 78 };
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const xValues = points.map((point) => Number(point.duration_weeks));
  const yValues = points.map((point) => Number(point.weight_change_kg));
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
  const [xMin, xMax] = extent(xValues, 0.04);
  const [yMin, yMax] = extent(yValues, 0.12);
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const xScale = (value) => margin.left + ((value - xMin) / (xMax - xMin)) * plotW;
  const yScale = (value) => margin.top + (1 - (value - yMin) / (yMax - yMin)) * plotH;

  svg.appendChild(el("rect", { x: margin.left, y: margin.top, width: plotW, height: plotH, class: "plot-bg" }));

  const xTicks = 6;
  const yTicks = 6;
  for (let i = 0; i <= xTicks; i += 1) {
    const value = xMin + (xMax - xMin) * i / xTicks;
    const x = xScale(value);
    svg.appendChild(el("line", { x1: x, x2: x, y1: margin.top, y2: margin.top + plotH, class: "grid" }));
    svg.appendChild(el("text", { x, y: height - 35, "text-anchor": "middle", class: "tick" }, value.toFixed(0)));
  }
  for (let i = 0; i <= yTicks; i += 1) {
    const value = yMin + (yMax - yMin) * i / yTicks;
    const y = yScale(value);
    svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: y, y2: y, class: "grid" }));
    svg.appendChild(el("text", { x: margin.left - 12, y: y + 4, "text-anchor": "end", class: "tick" }, value.toFixed(0)));
  }
  svg.appendChild(el("line", { x1: margin.left, x2: margin.left + plotW, y1: yScale(0), y2: yScale(0), class: "zero-line" }));
  svg.appendChild(el("text", { x: margin.left + plotW / 2, y: height - 8, "text-anchor": "middle", class: "axis-label" }, "Duration (weeks)"));
  const yLabel = el("text", { x: 18, y: margin.top + plotH / 2, "text-anchor": "middle", class: "axis-label", transform: `rotate(-90 18 ${margin.top + plotH / 2})` }, "Weight change (kg)");
  svg.appendChild(yLabel);

  rctSeries.forEach((series, index) => {
    const rows = series.rows || [];
    if (rows.length < 2) return;
    const colors = rctPalette[index % rctPalette.length];
    const upper = rows.map((point) => `${xScale(point.weeks).toFixed(2)},${yScale(point.upper).toFixed(2)}`).join(" ");
    const lower = [...rows].reverse().map((point) => `${xScale(point.weeks).toFixed(2)},${yScale(point.lower).toFixed(2)}`).join(" ");
    svg.appendChild(el("polygon", { points: `${upper} ${lower}`, class: "rct-band", style: `fill:${colors.band}` }));
    svg.appendChild(el("path", { d: pathFrom(rows, xScale, yScale, "weeks", "mean"), class: "rct-line", style: `stroke:${colors.line}` }));
  });

  if (data.curve?.length >= 2) {
    svg.appendChild(el("path", { d: pathFrom(data.curve, xScale, yScale, "weeks", "weight_change_kg"), class: "fit-line" }));
  }

  points.forEach((point) => {
    const circle = el("circle", {
      cx: xScale(point.duration_weeks),
      cy: yScale(point.weight_change_kg),
      r: 5.5,
      tabindex: 0,
      class: `point point-${data.family}`,
    });
    circle.addEventListener("click", () => renderDetail(point));
    circle.addEventListener("mouseenter", () => renderDetail(point));
    circle.addEventListener("focus", () => renderDetail(point));
    circle.appendChild(el("title", {}, `${fmt(point.duration_weeks, 1)} weeks, ${fmt(point.weight_change_kg, 1)} kg`));
    svg.appendChild(circle);
  });

  const legend = el("g", { class: "legend" });
  legend.appendChild(el("circle", { cx: width - 245, cy: 34, r: 5, class: `point point-${data.family}` }));
  legend.appendChild(el("text", { x: width - 232, y: 38 }, "Reddit reports"));
  legend.appendChild(el("line", { x1: width - 245, x2: width - 222, y1: 58, y2: 58, class: "fit-line" }));
  legend.appendChild(el("text", { x: width - 214, y: 62 }, "Reddit smoothed fit"));
  rctSeries.forEach((series, index) => {
    const rows = series.rows || [];
    if (rows.length < 2) return;
    const y = 82 + index * 24;
    const colors = rctPalette[index % rctPalette.length];
    legend.appendChild(el("line", { x1: width - 245, x2: width - 222, y1: y, y2: y, class: "rct-line", style: `stroke:${colors.line}` }));
    legend.appendChild(el("text", { x: width - 214, y: y + 4 }, `${series.label} RCT mean +/- 1.96 SD`));
  });
  svg.appendChild(legend);
}

function renderSideEffects(data) {
  const effects = data.side_effects || [];
  const bars = document.getElementById("effect-bars");
  const cloud = document.getElementById("effect-cloud");
  const table = document.getElementById("effect-table");
  bars.innerHTML = "";
  cloud.innerHTML = "";
  table.innerHTML = "";
  if (!effects.length) {
    bars.innerHTML = '<p class="status">No side effects extracted yet.</p>';
    return;
  }
  const max = Math.max(...effects.map((item) => item.count));
  effects.slice(0, 25).forEach((item) => {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `
      <span>${htmlEscape(item.phrase)}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(5, item.count / max * 100)}%"></div></div>
      <strong>${item.count}</strong>
    `;
    bars.appendChild(row);
  });
  effects.slice(0, 60).forEach((item) => {
    const token = document.createElement("span");
    token.style.fontSize = `${0.85 + (item.count / max) * 1.65}rem`;
    token.textContent = item.phrase;
    cloud.appendChild(token);
  });
  effects.forEach((item) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${htmlEscape(item.phrase)}</td><td>${item.count}</td>`;
    table.appendChild(tr);
  });
}

function networkFamilyClass(family) {
  return `compound-family-${String(family || "unclear").replace(/[^a-z0-9_-]/gi, "-").toLowerCase()}`;
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
      <p><strong>${htmlEscape(report.drug_name_mentioned || report.drug_family || "unknown focal drug")}</strong> with ${htmlEscape(raw || compounds || "n/a")}</p>
      <dl class="detail-list compact">
        <div><dt>Normalized</dt><dd>${htmlEscape(compounds || "n/a")}</dd></div>
        <div><dt>Attribution</dt><dd>${htmlEscape(report.attribution || "n/a")}</dd></div>
        <div><dt>Dose</dt><dd>${htmlEscape(report.dose_strong || "n/a")}</dd></div>
        <div><dt>Duration</dt><dd>${htmlEscape(report.duration_raw || "n/a")} (${fmt(report.duration_weeks, 1, " weeks")})</dd></div>
        <div><dt>Weight change</dt><dd>${fmt(report.weight_change_kg, 1, " kg")}</dd></div>
        <div><dt>Evidence</dt><dd>${htmlEscape(report.evidence || "n/a")}</dd></div>
        <div><dt>Notes</dt><dd>${htmlEscape(report.notes || "n/a")}</dd></div>
      </dl>
      <p class="excerpt">${htmlEscape(report.text_excerpt || "")}</p>
      <a class="reddit-link" href="${htmlEscape(report.url || "#")}" target="_blank" rel="noopener">Open Reddit URL</a>
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
      <div><dt>Family</dt><dd>${htmlEscape(node.family || "unclear")}</dd></div>
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
    svg.setAttribute("viewBox", "0 0 900 680");
    return;
  }
  status.textContent = `${data.summary?.reports || 0} reports, ${nodes.length} compounds, ${links.length} connections shown (${mode === "stack" ? "stack-only" : "all concurrent mentions"}).`;

  const width = 940;
  const height = 700;
  const cx = width / 2;
  const cy = height / 2;
  const radius = 265;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const positions = new Map();
  nodes.forEach((node, index) => {
    const angle = -Math.PI / 2 + (index / nodes.length) * Math.PI * 2;
    positions.set(node.id, {
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      angle,
    });
  });

  const maxLink = Math.max(...links.map((link) => Number(link[countKey] || 0)));
  const edgeLayer = el("g", { class: "network-edges" });
  links.forEach((link) => {
    const source = positions.get(link.source);
    const target = positions.get(link.target);
    if (!source || !target) return;
    const count = Number(link[countKey] || 0);
    const path = el("path", {
      d: `M${source.x.toFixed(2)},${source.y.toFixed(2)} Q${cx.toFixed(2)},${cy.toFixed(2)} ${target.x.toFixed(2)},${target.y.toFixed(2)}`,
      class: "network-edge",
      "stroke-width": (1.2 + Math.sqrt(count / maxLink) * 7).toFixed(2),
      tabindex: 0,
    });
    path.appendChild(el("title", {}, `${link.source} + ${link.target}: ${count} reports`));
    path.addEventListener("click", () => renderNetworkLinkDetail(link, data, mode));
    path.addEventListener("mouseenter", () => renderNetworkLinkDetail(link, data, mode));
    path.addEventListener("focus", () => renderNetworkLinkDetail(link, data, mode));
    edgeLayer.appendChild(path);
  });
  svg.appendChild(edgeLayer);

  const maxNode = Math.max(...nodes.map((node) => Number(node[countKey] || 0)));
  const nodeLayer = el("g", { class: "network-nodes" });
  nodes.forEach((node) => {
    const position = positions.get(node.id);
    const count = Number(node[countKey] || 0);
    const nodeRadius = 8 + Math.sqrt(count / maxNode) * 14;
    const group = el("g", { class: `network-node ${networkFamilyClass(node.family)}`, tabindex: 0 });
    const circle = el("circle", { cx: position.x, cy: position.y, r: nodeRadius.toFixed(2) });
    const labelRadius = radius + 34;
    const lx = cx + Math.cos(position.angle) * labelRadius;
    const ly = cy + Math.sin(position.angle) * labelRadius;
    const anchor = Math.cos(position.angle) > 0.18 ? "start" : Math.cos(position.angle) < -0.18 ? "end" : "middle";
    const label = el("text", { x: lx.toFixed(2), y: ly.toFixed(2), "text-anchor": anchor }, node.label);
    group.appendChild(circle);
    group.appendChild(label);
    group.appendChild(el("title", {}, `${node.label}: ${count} reports`));
    group.addEventListener("click", () => renderNetworkNodeDetail(node, data, mode));
    group.addEventListener("mouseenter", () => renderNetworkNodeDetail(node, data, mode));
    group.addEventListener("focus", () => renderNetworkNodeDetail(node, data, mode));
    nodeLayer.appendChild(group);
  });
  svg.appendChild(nodeLayer);

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
    legend.appendChild(el("circle", { cx: 24, cy: y, r: 6, class: networkFamilyClass(family) }));
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
