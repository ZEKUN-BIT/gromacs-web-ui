// 图谱绘制模块：canvas 折线图与渲染。

import { els, state, escapeHtml, updateText } from "./core.js?v=20261006-026";

export const PLOT_COLORS = ["#087f8c", "#e07a1f", "#7259b5", "#2f855a", "#d14d72", "#3578c4", "#b18b16", "#9b4dca"];
let plotObserver = null;
let plotResizeObserver = null;
let displayedPlots = [];

function redrawVisiblePlots() {
  els.analysisPlots?.querySelectorAll("canvas").forEach((canvas) => {
    if (canvas.getClientRects().length && canvas.clientWidth > 0) {
      drawPlot(canvas, displayedPlots[Number(canvas.dataset.plotIndex)]);
    }
  });
}

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawVisiblePlots);

export function resetPlotFilters() {
  state.plotFilters = { metric: "", replica: "", begin: "", end: "", mode: "separate" };
  els.plotControls?.reset();
  if (els.plotFilterError) els.plotFilterError.hidden = true;
}

export function updatePlotOptions(payload) {
  els.plotMetric.innerHTML = '<option value="">全部指标</option>' + (payload.metrics || []).map((metric) =>
    `<option value="${escapeHtml(metric)}">${escapeHtml(metric)}</option>`).join("");
  els.plotReplica.innerHTML = '<option value="">全部副本</option>' + (payload.replicas || []).map((replica) =>
    `<option value="${replica}">副本 ${replica}</option>`).join("");
  els.plotMetric.value = state.plotFilters.metric;
  els.plotReplica.value = state.plotFilters.replica;
  els.plotWindowNotice.hidden = state.plotFilters.begin === "" && state.plotFilters.end === "";
}

export function bindPlotControls(refresh) {
  function apply() {
    const begin = els.plotBegin.value, end = els.plotEnd.value;
    if ((begin !== "" && (!Number.isFinite(Number(begin)) || Number(begin) < 0)) ||
        (end !== "" && (!Number.isFinite(Number(end)) || Number(end) < 0)) ||
        (begin !== "" && end !== "" && Number(begin) >= Number(end))) {
      els.plotFilterError.textContent = "请输入有效区间，结束时间必须大于开始时间。";
      els.plotFilterError.hidden = false;
      return;
    }
    state.plotFilters = { metric: els.plotMetric.value, replica: els.plotReplica.value, begin, end,
      mode: els.plotControls.querySelector("[name='plot-mode']:checked").value };
    state.plotsSignature = "";
    state.artifactKey = "";
    refresh();
  }
  els.plotControls.addEventListener("submit", (event) => { event.preventDefault(); apply(); });
  els.plotControls.addEventListener("change", (event) => {
    if (event.target.tagName === "SELECT" || event.target.type === "radio") apply();
  });
  els.plotControls.addEventListener("reset", () => {
    state.plotFilters = { metric: "", replica: "", begin: "", end: "", mode: "separate" };
    state.plotsSignature = "";
    state.artifactKey = "";
    setTimeout(refresh, 0);
  });
}

function plotTraces(plot, offset = 0) {
  return plot.traces || (plot.series || []).map((name, index) => ({
    name: plot.replica != null ? `副本 ${plot.replica} · ${name}` : name,
    points: plot.points.map((row) => [row[0], row[index + 1]]),
    color: plot.replica != null ? (plot.replica - 1) % PLOT_COLORS.length : (offset + index) % PLOT_COLORS.length,
    dash: plot.replica != null && plot.replica > PLOT_COLORS.length,
    path: plot.path, mean: plot.mean, last: plot.last, sample_count: plot.sample_count,
  }));
}

function overlayReplicas(plots) {
  const groups = new Map();
  for (const plot of plots) {
    const key = plot.replica != null
      ? JSON.stringify([plot.metric, plot.series, plot.x_label, plot.y_label])
      : JSON.stringify([plot.path, plot.title]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(plot);
  }
  return Array.from(groups.values(), (members) => members.length < 2 ? members[0] : {
    ...members[0], path: members[0].metric, traces: members.flatMap((plot) => plotTraces(plot)),
    sample_count: members.reduce((total, plot) => total + plot.sample_count, 0),
  });
}

// 从 CSS 变量读取配色，深色模式自动切换；变量缺失时回退到常量
export function plotTheme() {
  const styles = getComputedStyle(document.documentElement);
  const series = PLOT_COLORS.map((color, index) => styles.getPropertyValue(`--series-${index}`).trim() || color);
  return {
    text: styles.getPropertyValue("--plot-text").trim() || "#63716a",
    grid: styles.getPropertyValue("--plot-grid").trim() || "#d8e2dc",
    series,
  };
}

function formatTick(value, span) {
  const magnitude = Math.abs(value);
  if ((magnitude >= 10000 || (magnitude > 0 && magnitude < 0.001)) && span !== 0) return value.toExponential(2);
  const decimals = span < 0.01 ? 4 : span < 1 ? 3 : span < 20 ? 2 : span < 200 ? 1 : 0;
  const fixed = value.toFixed(decimals);
  return fixed.includes(".") ? fixed.replace(/0+$/, "").replace(/\.$/, "") : fixed;
}

export function drawPlot(canvas, plot) {
  if (!plot) return;
  const traces = plotTraces(plot, Number(canvas.dataset.colorOffset || 0));
  const points = traces.flatMap((trace) => trace.points);
  if (!points.length) return;
  const theme = plotTheme();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(280, canvas.clientWidth);
  const height = canvas.clientHeight || 250;
  const fontSize = parseFloat(getComputedStyle(canvas).getPropertyValue("--plot-font-size")) || 10;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const xs = points.map((row) => row[0]);
  const ys = points.flatMap((row) => row.slice(1));
  let xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (xMin === xMax) xMax = xMin + 1;
  if (yMin === yMax) yMax = yMin + 1;
  const yPadding = (yMax - yMin) * 0.06;
  yMin -= yPadding;
  yMax += yPadding;
  ctx.font = `${fontSize}px "JetBrains Mono", monospace`;
  const yLabels = Array.from({ length: 5 }, (_, tick) => formatTick(yMax - tick * (yMax - yMin) / 4, yMax - yMin));
  const widestYLabel = Math.max(...yLabels.map((label) => ctx.measureText(label).width));
  const pad = { left: Math.max(54, Math.ceil(widestYLabel) + 16), right: 18, top: 16, bottom: fontSize + 24 };
  const px = (value) => pad.left + ((value - xMin) / (xMax - xMin)) * (width - pad.left - pad.right);
  const py = (value) => pad.top + (1 - (value - yMin) / (yMax - yMin)) * (height - pad.top - pad.bottom);
  const chartWidth = width - pad.left - pad.right;
  ctx.fillStyle = theme.text;
  ctx.strokeStyle = theme.grid;
  ctx.lineWidth = 1;
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = pad.top + tick * (height - pad.top - pad.bottom) / 4;
    const value = yMax - tick * (yMax - yMin) / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(formatTick(value, yMax - yMin), pad.left - 10, y + 4);
  }
  ctx.textAlign = "center";
  const xLabelWidth = Math.max(...Array.from({ length: 5 }, (_, tick) =>
    ctx.measureText(formatTick(xMin + tick * (xMax - xMin) / 4, xMax - xMin)).width));
  const xIntervals = Math.max(1, Math.min(4, Math.floor(chartWidth / (xLabelWidth + 24))));
  for (let tick = 0; tick <= xIntervals; tick += 1) {
    const x = pad.left + tick * (width - pad.left - pad.right) / xIntervals;
    const value = xMin + tick * (xMax - xMin) / xIntervals;
    ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, height - pad.bottom); ctx.stroke();
    ctx.textAlign = tick === 0 ? "left" : tick === xIntervals ? "right" : "center";
    ctx.fillText(formatTick(value, xMax - xMin), x, height - 11);
  }
  traces.forEach((trace) => {
    ctx.beginPath();
    ctx.strokeStyle = theme.series[trace.color];
    ctx.setLineDash(trace.dash ? [6, 3] : []);
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    trace.points.forEach((row, index) => {
      const x = px(row[0]), y = py(row[1]);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.setLineDash([]);
  canvas.onpointermove = (event) => {
    const bounds = canvas.getBoundingClientRect();
    const position = ((event.clientX - bounds.left) * width / bounds.width - pad.left) / chartWidth;
    const target = xMin + Math.max(0, Math.min(1, position)) * (xMax - xMin);
    const values = traces.map((trace) => {
      const closest = trace.points.reduce((best, row) => Math.abs(row[0] - target) < Math.abs(best[0] - target) ? row : best);
      return `${trace.name}: ${formatPlotValue(closest[1])} (${formatPlotValue(closest[0])})`;
    });
    canvas.nextElementSibling.textContent = values.join(" · ");
  };
  canvas.onpointerleave = () => { canvas.nextElementSibling.textContent = ""; };
  canvas.dataset.drawn = "true";
}

function formatPlotValue(value) {
  if (!Number.isFinite(Number(value))) return "–";
  return Number(Number(value).toPrecision(6)).toString();
}

function plotAnnotation(plot) {
  const text = [plot.title, plot.path, plot.x_label, plot.y_label, ...(plot.series || [])].join(" ").toLowerCase();
  const rules = [
    [/rmsd/, "观察曲线是否在初始调整后进入相对稳定的平台；持续漂移或多次阶跃可能表示构象仍在变化，需结合回转半径、RMSF 和轨迹共同判断。"],
    [/rmsf|fluctuation/, "峰值对应波动较大的残基或原子区域。重点比较口袋、活性位点和末端区域，并结合结构位置判断高波动是否合理。"],
    [/gyrate|gyration|\brg\b/, "回转半径反映体系整体紧致程度。稳定区间内的小幅波动通常更可信；长期单向变化可能提示持续压缩或展开。"],
    [/sasa|solvent.accessible/, "溶剂可及表面积反映体系暴露程度。明显阶跃或长期趋势应与回转半径及构象快照交叉检查。"],
    [/hydrogen|hbond|h-bond/, "关注氢键数量或占有率是否在稳定区间波动。单个瞬时峰值意义有限，持续存在及重复形成更值得关注。"],
    [/distance|dist/, "距离曲线用于判断选定原子、残基或配体是否保持接近。应结合接触定义、周期性边界处理和轨迹快照，避免仅凭均值判断。"],
    [/contact|mindist/, "关注接触数量是否持续以及是否频繁降至零。接触稳定不等同于结合稳定，仍需结合距离、氢键和配体构象。"],
    [/temperature|temp/, "温度应围绕目标值随机波动且无持续漂移。短时尖峰需结合恒温阶段、耦合参数和日志判断。"],
    [/pressure|press/, "瞬时压力波动通常较大，应重点看长期均值和稳定区间，而不是单个峰值；同时检查密度和体积是否已稳定。"],
    [/density/, "密度应在平衡后围绕合理区间波动。持续趋势或明显跳变可能提示 NPT 平衡不足或体系设置需要复查。"],
    [/volume/, "体积在 NPT 阶段趋于稳定可作为盒子平衡的辅助证据；需与压力、密度和温度一起判断。"],
    [/potential|total energy|kinetic|enthalpy|energy/, "关注平衡后的波动带和长期趋势。能量绝对值通常不适合跨不同体系直接比较，持续漂移需要检查平衡和模拟设置。"],
    [/eigen|principal|\bpca\b|projection/, "观察采样区域是否充分覆盖以及是否出现多个构象簇。二维投影只展示部分信息，应结合代表结构和其他收敛指标。"],
  ];
  return rules.find(([pattern]) => pattern.test(text))?.[1]
    || "观察平衡后的波动范围、长期趋势和异常阶跃，并与同栏目其他指标、任务日志及轨迹结构交叉验证。";
}

export function renderPlots(jobId, plots, truncated = false, total = null) {
  const signature = `${jobId}:${JSON.stringify(state.plotFilters)}:${truncated}:${total}:${plots.map((plot) => `${plot.path}:${plot.title}:${plot.sample_count}:${plot.last}:${plot.mean}`).join(",")}`;
  if (signature === state.plotsSignature) return;
  state.plotsSignature = signature;
  if (state.plotFilters.mode === "overlay") plots = overlayReplicas(plots);
  displayedPlots = plots;
  plotResizeObserver?.disconnect();
  const totalShown = `${plots.length} 张${truncated ? "（部分省略）" : ""}`;
  updateText(els.plotCount, totalShown);
  if (els.downloadPlots) {
    els.downloadPlots.hidden = !plots.length;
    els.downloadPlots.href = plots.length ? `/api/jobs/${encodeURIComponent(jobId)}/plots/download` : "#";
  }
  if (!plots.length) {
    els.analysisPlots.innerHTML = `<div class="plot-empty"><strong>当前筛选范围内暂无曲线</strong><p>区间内需至少两个有效数据点。</p></div>`;
    return;
  }
  const categoryMeta = {
    structure: ["结构稳定性", "用于观察整体构象、紧致程度和局部柔性是否进入相对稳定区间。不要只看单条曲线的平台，应结合多项结构指标和轨迹快照。"],
    interaction: ["蛋白–配体相互作用", "用于检查配体是否保持在口袋，以及距离、接触和氢键是否持续。单个指标稳定不能单独证明结合稳定。"],
    sampling: ["构象采样", "用于观察主要构象空间的覆盖和构象簇分布。投影图反映的是降维后的相对关系，需要结合代表结构解读。"],
    quality: ["运行质控", "用于核对温度、压力、密度和能量是否符合预期。运行质控通过是分析结构结果的前提，但不等同于结构已经收敛。"],
  };
  const categories = ["structure", "interaction", "sampling", "quality"];
  const truncationNotice = truncated
    ? `<div class="plot-truncation-notice">部分文件超过读取限制或曲线数量上限。请选择具体指标或副本，或下载完整 XVG。</div>`
    : "";
  els.analysisPlots.innerHTML = truncationNotice + categories.map((category) => {
    const entries = plots.map((plot, index) => ({ plot, index })).filter(({ plot }) => (plot.category || "quality") === category);
    if (!entries.length) return "";
    const [title, description] = categoryMeta[category];
    return `<section class="plot-category"><header><h3>${title}</h3><p>${description}</p></header><div class="plot-category-grid">${entries.map(({ plot, index }) => `
    <article class="plot-card">
      <div class="plot-card-head"><div><strong>${escapeHtml(plot.title)}${plot.traces ? " · 副本对比" : plot.replica != null ? ` · 副本 ${plot.replica}` : ""}</strong><small>${escapeHtml(plot.path)} · ${plot.sample_count} 个原始数据点</small></div></div>
      <canvas data-plot-index="${index}" data-color-offset="${index % PLOT_COLORS.length}" role="img" aria-label="${escapeHtml(plot.title)} 数据曲线"></canvas>
      <output class="plot-readout"></output>
      <div class="plot-axis-labels"><span><b>X</b>${escapeHtml(plot.x_label || "X")}</span><span><b>Y</b>${escapeHtml(plot.y_label || "Value")}</span></div>
      <div class="plot-statistics"><table aria-label="${escapeHtml(plot.title)} 统计">
        <thead><tr><th scope="col">曲线</th><th scope="col">${state.plotFilters.begin !== "" || state.plotFilters.end !== "" ? "区间" : "全程"}均值</th><th scope="col">末值</th><th scope="col">原始数据</th></tr></thead>
        <tbody>${plotTraces(plot, index).map((trace) => `<tr><th scope="row"><span class="plot-series"><i class="series-${trace.color}${trace.dash ? " trace-dashed" : ""}" aria-hidden="true"></i><span>${escapeHtml(trace.name)}${trace.dash ? " (虚线)" : ""}</span></span></th><td>${escapeHtml(formatPlotValue(trace.mean))}</td><td>${escapeHtml(formatPlotValue(trace.last))}</td><td><a href="/api/jobs/${encodeURIComponent(jobId)}/download?path=${encodeURIComponent(trace.path)}" aria-label="${escapeHtml(trace.name)} 下载完整 XVG" download>XVG</a></td></tr>`).join("")}</tbody>
      </table></div>
      <aside class="plot-annotation"><strong>解读提示</strong><p>${escapeHtml(plotAnnotation(plot))}</p></aside>
    </article>`).join("")}</div></section>`;
  }).join("");
  plotObserver?.disconnect();
  const canvases = els.analysisPlots.querySelectorAll("canvas");
  if ("ResizeObserver" in window) {
    plotResizeObserver = new ResizeObserver(redrawVisiblePlots);
    canvases.forEach((canvas) => plotResizeObserver.observe(canvas));
  }
  if (!("IntersectionObserver" in window)) {
    canvases.forEach((canvas) => drawPlot(canvas, plots[Number(canvas.dataset.plotIndex)]));
    return;
  }
  plotObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const canvas = entry.target;
      drawPlot(canvas, plots[Number(canvas.dataset.plotIndex)]);
      observer.unobserve(canvas);
    });
  }, { rootMargin: "240px 0px" });
  canvases.forEach((canvas) => plotObserver.observe(canvas));
}
