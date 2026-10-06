import { api, escapeHtml, state } from "./core.js?v=20261006-026";
import { drawPlot, plotTheme } from "./plots.js?v=20261006-026";

const labels = {
  ca_rmsd_nm: "C-alpha RMSD (nm)", ca_rg_nm: "C-alpha Rg (nm)",
  ligand_rmsd_nm: "配体 RMSD / 蛋白拟合 (nm)", ligand_self_rmsd_nm: "配体 RMSD / 自身拟合 (nm)",
  contact_residues: "接触残基数", pocket_retention: "初始口袋保持率 (%)",
};
let report = null, mode = "conformation", generation = 0, loadedJob = null;
let drawings = [];
const observer = new ResizeObserver(() => redraw());
const number = (value, digits = 3) => value == null ? "不可计算" : Number(value).toFixed(digits);
const valueLabel = (key, value) => number(value == null ? null : value * (key === "pocket_retention" ? 100 : 1));
const fileLink = (job, path) => `/api/jobs/${encodeURIComponent(job)}/download?path=${encodeURIComponent(path)}`;
const warningList = (items) => `<ul class="research-notes">${items.map((text) => `<li>${escapeHtml(text)}</li>`).join("")}</ul>`;

function table(headers, rows) {
  return `<div class="research-table-scroll"><table><thead><tr>${headers.map((text) => `<th scope="col">${escapeHtml(text)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((text) => `<td>${escapeHtml(text)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function scatter(canvas, points) {
  if (!points.length || !canvas.clientWidth) return;
  const ratio = Math.min(devicePixelRatio || 1, 2), width = Math.max(280, canvas.clientWidth), height = canvas.clientHeight || 250;
  const fontSize = parseFloat(getComputedStyle(canvas).getPropertyValue("--plot-font-size")) || 10;
  canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const theme = plotTheme();
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  let xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(...ys), ymax = Math.max(...ys);
  if (xmax === xmin) xmax = xmin + 1;
  if (ymax === ymin) ymax = ymin + 1;
  const tickLabel = (value) => value === 0 ? "0" : Math.abs(value) < .001 ? value.toExponential(1) : value.toPrecision(3);
  ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
  const yLabelWidth = Math.max(...Array.from({ length: 5 }, (_, tick) =>
    ctx.measureText(tickLabel(ymin + (ymax - ymin) * tick / 4)).width));
  const pad = { left: Math.max(56, Math.ceil(yLabelWidth) + 16), right: 20, top: 16, bottom: fontSize + 24 };
  const chartWidth = width - pad.left - pad.right;
  const px = (x) => pad.left + (x - xmin) / (xmax - xmin) * chartWidth;
  const py = (y) => height - pad.bottom - (y - ymin) / (ymax - ymin) * (height - pad.top - pad.bottom);
  ctx.fillStyle = theme.text; ctx.strokeStyle = theme.grid;
  for (let tick = 0; tick <= 4; tick++) {
    const y = ymin + (ymax - ymin) * tick / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, py(y)); ctx.lineTo(width - pad.right, py(y)); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(tickLabel(y), pad.left - 10, py(y) + 4);
  }
  const xLabelWidth = Math.max(...Array.from({ length: 5 }, (_, tick) =>
    ctx.measureText(tickLabel(xmin + (xmax - xmin) * tick / 4)).width));
  const xIntervals = Math.max(1, Math.min(4, Math.floor(chartWidth / (xLabelWidth + 24))));
  for (let tick = 0; tick <= xIntervals; tick++) {
    const x = xmin + (xmax - xmin) * tick / xIntervals;
    ctx.textAlign = tick === 0 ? "left" : tick === xIntervals ? "right" : "center";
    ctx.fillText(tickLabel(x), px(x), height - 11);
  }
  points.forEach((point, index) => {
    const group = index < points.length * .2 ? 0 : index >= points.length * .8 ? 2 : 1;
    ctx.fillStyle = [theme.series[0], theme.text, theme.series[1]][group];
    const x = px(point[0]), y = py(point[1]);
    ctx.beginPath();
    if (group === 0) ctx.arc(x, y, 2.5, 0, Math.PI * 2);
    else if (group === 1) ctx.rect(x - 2, y - 2, 4, 4);
    else { ctx.moveTo(x, y - 3); ctx.lineTo(x - 3, y + 3); ctx.lineTo(x + 3, y + 3); ctx.closePath(); }
    ctx.fill();
  });
  canvas.dataset.drawn = "true";
}

function redraw() {
  for (const { canvas, data, isScatter } of drawings) {
    if (canvas.getClientRects().length) (isScatter ? scatter : drawPlot)(canvas, data);
  }
}

function chart(parent, title, data, isScatter = false) {
  const figure = document.createElement("figure");
  figure.className = "research-chart";
  const caption = document.createElement("figcaption"); caption.textContent = title;
  const canvas = document.createElement("canvas"); canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", title);
  const readout = document.createElement("output"); readout.className = "plot-readout";
  figure.append(caption, canvas, readout); parent.append(figure);
  drawings.push({ canvas, data, isScatter }); observer.observe(canvas);
}

function residueTable(parent, residues, binding = false) {
  const section = document.createElement("section"); section.className = "research-residues";
  section.innerHTML = `<div class="research-table-tools"><label>残基筛选<input type="search" aria-label="筛选研究残基" placeholder="链 / 残基编号 / 名称" /></label><label>排序<select aria-label="研究残基排序"><option value="${binding ? "contact_occupancy" : "rmsf_nm"}">${binding ? "接触占有率" : "RMSF"}</option><option value="displacement_nm">前后段位移</option><option value="sequence">序列顺序</option></select></label></div><div class="research-residue-rows"></div><div class="research-pager"><button type="button" class="tool-button" data-page="prev">上一页</button><span role="status"></span><button type="button" class="tool-button" data-page="next">下一页</button></div>`;
  parent.append(section);
  let page = 0;
  const search = section.querySelector("input"), sort = section.querySelector("select");
  function render() {
    const query = search.value.trim().toLowerCase();
    const rows = residues.filter((row) => `${row.chain} ${row.resid}${row.icode || ""} ${row.resname}`.toLowerCase().includes(query));
    if (sort.value !== "sequence") rows.sort((a, b) => (b[sort.value] ?? -1) - (a[sort.value] ?? -1));
    const pages = Math.max(1, Math.ceil(rows.length / 50)); page = Math.min(page, pages - 1);
    section.querySelector(".research-residue-rows").innerHTML = table(
      ["链:残基", "名称", "RMSF (nm)", "前后段位移 (nm)", ...(binding ? ["接触占有率 (%)"] : [])],
      rows.slice(page * 50, (page + 1) * 50).map((row) => [`${row.chain}:${row.resid}${row.icode || ""}`, row.resname,
        number(row.rmsf_nm), number(row.displacement_nm), ...(binding ? [number(row.contact_occupancy == null ? null : row.contact_occupancy * 100, 1)] : [])]));
    section.querySelector(".research-pager span").textContent = `${page + 1} / ${pages} · ${rows.length} 个残基`;
    section.querySelector('[data-page="prev"]').disabled = page === 0;
    section.querySelector('[data-page="next"]').disabled = page === pages - 1;
  }
  search.addEventListener("input", () => { page = 0; render(); });
  sort.addEventListener("change", () => { page = 0; render(); });
  section.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => {
    page += button.dataset.page === "next" ? 1 : -1; render();
  }));
  render();
}

function renderResult() {
  observer.disconnect(); drawings = [];
  const root = document.querySelector("#research-results"); root.replaceChildren();
  root.hidden = mode === "comparison";
  document.querySelector("#research-comparison").hidden = mode !== "comparison";
  if (!report || mode === "comparison") return;
  const keys = mode === "binding" ? Object.keys(report.summary).filter((key) => !key.startsWith("ca_")) : ["ca_rmsd_nm", "ca_rg_nm"];
  root.innerHTML = `<div class="research-provenance"><span>${report.frames} 帧 · ${number(report.window_ns[0])}–${number(report.window_ns[1])} ns · 拟合组 ${escapeHtml(report.fit_group)}</span><a class="tool-button" href="${fileLink(report.job.id, "research-report.json")}">报告 JSON</a><a class="tool-button" href="${fileLink(report.job.id, "research-residues.tsv")}">残基 TSV</a></div>${warningList(report.warnings || [])}`;
  if (mode === "binding" && !report.binding) {
    root.insertAdjacentHTML("beforeend", '<p class="research-empty">当前报告未指定配体索引组。</p>'); return;
  }
  root.insertAdjacentHTML("beforeend", table(["指标", "均值", "帧分布 P05–P95", "后 1/4 − 前 1/4"], keys.map((key) => {
    const item = report.summary[key];
    return [labels[key] || key, valueLabel(key, item.mean), `${valueLabel(key, item.p05)} – ${valueLabel(key, item.p95)}`, valueLabel(key, item.late_minus_early)];
  })));
  const charts = document.createElement("div"); charts.className = "research-charts"; root.append(charts);
  const traces = keys.filter((key) => key.endsWith("rmsd_nm"));
  for (const key of traces) {
    chart(charts, `${labels[key]} · 时间 ns`, { title: labels[key], x_label: "Time (ns)", y_label: "RMSD (nm)", series: [labels[key]],
      points: report.timeseries.time_ns.map((time, index) => [time, report.timeseries[key][index]]) });
  }
  if (mode === "conformation") {
    chart(charts, `PCA · PC1 ${number(report.pca.explained_variance[0] * 100, 1)}% / PC2 ${number(report.pca.explained_variance[1] * 100, 1)}% · 坐标 nm`, report.pca.projection_nm, true);
    root.insertAdjacentHTML("beforeend", '<p class="research-method">PCA：前 20% 圆点，中间 60% 方点，后 20% 三角；基于本轨迹的蛋白拟合 C-alpha 坐标。残基位移为前后各 20% 帧的平均位置之差。</p>');
  } else {
    root.insertAdjacentHTML("beforeend", `<p class="research-method">配体 ${escapeHtml(report.binding.resname)} · ${report.binding.heavy_atoms} 个重原子 · 接触阈值 ${number(report.binding.cutoff_nm, 2)} nm · 任意蛋白接触帧比例 ${number(report.binding.contact_frame_fraction * 100, 1)}%。初始口袋含 ${report.binding.initial_contact_residues} 个接触残基。占有率不等同于亲和力或结合自由能。</p>`);
  }
  residueTable(root, report.residues, mode === "binding");
  root.insertAdjacentHTML("beforeend", '<p class="research-method">P05–P95 是帧分布范围，不是置信区间。拟合参考为所选区间的第一帧，单条轨迹不能证明构象或结合已收敛。</p>');
  requestAnimationFrame(redraw);
}

export async function renderResearch(job) {
  const token = ++generation;
  report = null;
  document.querySelector("#research-state").textContent = "正在读取研究报告…";
  document.querySelector("#create-research").disabled = job.status !== "completed" || Boolean(job.dry_run);
  if (loadedJob !== job.id) {
    document.querySelector("#research-compare-results").replaceChildren();
    document.querySelector("#research-compare-error").hidden = true;
    loadedJob = job.id;
  }
  renderResult();
  const [result, catalog] = await Promise.allSettled([api(`/api/jobs/${encodeURIComponent(job.id)}/research`), api("/api/research/jobs")]);
  if (token !== generation || state.activeJobId !== job.id) return;
  if (result.status === "fulfilled") {
    report = result.value; document.querySelector("#research-state").textContent = "";
  } else document.querySelector("#research-state").textContent = result.reason.message;
  if (catalog.status === "fulfilled") {
    for (const id of ["research-reference", "research-other"]) {
      const select = document.getElementById(id);
      const selected = Array.from(select.selectedOptions, (option) => option.value);
      select.replaceChildren();
      catalog.value.jobs.forEach((item) => select.add(new Option(item.name, item.id, false,
        selected.includes(item.id) || (!selected.length && id === "research-reference" && item.id === job.id))));
    }
    if (catalog.value.truncated) document.querySelector("#research-state").textContent += " 候选任务仅覆盖最近 200 个任务。";
  }
  renderResult();
}

export function bindResearch() {
  document.querySelectorAll("[data-research-tab]").forEach((button) => button.addEventListener("click", () => {
    mode = button.dataset.researchTab;
    document.querySelectorAll("[data-research-tab]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    renderResult();
  }));
  document.querySelector("#create-research").addEventListener("click", () => {
    document.querySelector("#analyze-job").click();
    const form = document.querySelector("#existing-analysis-form");
    form.elements.do_research.checked = true;
    form.dispatchEvent(new Event("change", { bubbles: true }));
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
  document.querySelector("#research-compare-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    const error = document.querySelector("#research-compare-error"); error.hidden = true;
    const root = document.querySelector("#research-compare-results"); root.textContent = "正在对齐残基并比较…";
    const request = { reference_jobs: Array.from(document.querySelector("#research-reference").selectedOptions, (item) => item.value),
      comparison_jobs: Array.from(document.querySelector("#research-other").selectedOptions, (item) => item.value) };
    button.disabled = true;
    const jobId = state.activeJobId;
    try {
      const result = await api("/api/research/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
      if (state.activeJobId !== jobId) return;
      root.innerHTML = warningList(result.warnings) + '<p class="research-method">差值方向：对照组 − 参考组。组均值对每条轨迹等权，± 为轨迹均值之间的 SD。</p>' + table(
        ["指标", `参考组 (n=${result.reference.length})`, `对照组 (n=${result.comparison.length})`, "差值"], result.metrics.map((row) => [labels[row.metric] || row.metric,
          `${valueLabel(row.metric, row.reference.mean)}${row.reference.sd == null ? "" : ` ± ${valueLabel(row.metric, row.reference.sd)}`}`,
          `${valueLabel(row.metric, row.comparison.mean)}${row.comparison.sd == null ? "" : ` ± ${valueLabel(row.metric, row.comparison.sd)}`}`,
          valueLabel(row.metric, row.difference)]));
      const residueRows = [...result.residues].sort((a, b) => Math.abs(b.rmsf_nm?.difference || 0) - Math.abs(a.rmsf_nm?.difference || 0));
      root.insertAdjacentHTML("beforeend", `<h4>残基差异 · 按 |ΔRMSF| 排序，前 ${Math.min(50, residueRows.length)}/${residueRows.length}</h4>` + table(
        ["参考残基", "参考 → 对照", "ΔRMSF (nm)", "Δ前后位移 (nm)", "Δ接触占有率 (百分点)"], residueRows.slice(0, 50).map((row) => [
          `${row.chain}:${row.resid}${row.icode || ""}`, `${row.reference_names.join("/")} → ${row.comparison_names.join("/")}`,
          number(row.rmsf_nm?.difference), number(row.displacement_nm?.difference), number(row.contact_occupancy == null ? null : row.contact_occupancy.difference * 100, 1)])));
      root.insertAdjacentHTML("beforeend", '<details class="research-protocol"><summary>模拟条件核对</summary>' + table(
        ["条件", "状态", ...result.reference.map((item) => item.name), ...result.comparison.map((item) => item.name)],
        result.protocol.map((row) => [row.key, {same: "一致", different: "不同", unknown: "记录缺失"}[row.status], ...row.values.map((value) => value ?? "缺失")])) + "</details>");
      const download = document.createElement("button"); download.className = "tool-button"; download.type = "button"; download.textContent = "下载完整比较 JSON";
      download.addEventListener("click", () => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = "research-comparison.json"; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }); root.append(download);
    } catch (failure) {
      if (state.activeJobId === jobId) { root.replaceChildren(); error.hidden = false; error.textContent = failure.message; }
    } finally { button.disabled = false; }
  });
}
