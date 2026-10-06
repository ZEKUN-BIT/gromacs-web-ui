// 任务模块：任务列表、活动任务详情（日志/文件/图谱）、任务操作。

import {
  STATUS_LABELS,
  WORKFLOW_LABELS,
  ACTIVE_STATES,
  MAX_LOG_CHARS,
  $,
  els,
  state,
  escapeHtml,
  updateText,
  toast,
  api,
  confirmDialog,
  confirmSubmitWarnings,
  replaySwapAnimation,
  showWorkspaceView,
  showDetailTab,
} from "./core.js?v=20261006-026";
import { renderPlots, resetPlotFilters, updatePlotOptions } from "./plots.js?v=20261006-026";
import { renderResearch } from "./research.js?v=20261006-026";
import {
  formObject,
  selectedUploadFiles,
  requiredSubmitIssues,
  submitWarnings,
  friendlyPreviewError,
  friendlyJobActionError,
  refreshPreview,
} from "./form.js?v=20261006-026";

/* ---------- 任务列表（签名对比，按需渲染 + 事件委托） ---------- */

function computeJobsSignature() {
  return (
    state.jobs.map((job) => `${job.id}:${job.status}:${job.step_index}:${job.name}`).join("|") +
    `#${state.activeJobId}:${state.jobFilter}:${state.jobsOffset}:${state.jobsTotal}:${state.jobsHasMore}:${state.jobsBusy}`
  );
}

function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function renderJobs() {
  const signature = computeJobsSignature();
  if (signature === state.jobsSignature) return;
  state.jobsSignature = signature;

  // Search and pagination are applied to the complete history by the server.
  const visibleJobs = state.jobs;
  if (els.jobsPrevious) els.jobsPrevious.disabled = state.jobsBusy || state.jobsOffset === 0;
  if (els.jobsNext) els.jobsNext.disabled = state.jobsBusy || !state.jobsHasMore;
  if (els.jobsPageStatus) {
    updateText(els.jobsPageStatus, state.jobsBusy ? "正在读取…"
      : `${state.jobsTotal ? Math.floor(state.jobsOffset / 50) + 1 : 0} / ${Math.ceil(state.jobsTotal / 50)} · ${state.jobsTotal} 项`);
  }

  if (!state.jobs.length) {
    const title = state.jobsBusy ? "正在读取任务…" : state.jobFilter.trim() ? "没有匹配任务" : "暂无任务";
    els.jobsList.innerHTML = `<div class="empty-jobs"><strong>${title}</strong><small>${state.jobFilter.trim() ? "换个名称、状态或工作流试试" : "提交后会出现在这里"}</small></div>`;
    return;
  }
  els.jobsList.innerHTML = visibleJobs
    .map((job) => {
      const isActive = job.id === state.activeJobId;
      const isRunning = ACTIVE_STATES.has(job.status);
      const canDelete = !isRunning;
      return `
        <div class="job-item ${isActive ? "active" : ""}" data-job-id="${escapeHtml(job.id)}" data-status="${escapeHtml(job.status)}">
          <button class="job-select" type="button" data-action="select" title="${escapeHtml(job.name)}&#10;结果目录：${escapeHtml(job.directory_name || job.id)}">
            <strong>${escapeHtml(job.name)}</strong>
            <span class="job-meta">
              <span class="badge ${escapeHtml(job.status)} ${state.changedJobIds.has(job.id) ? "status-changed" : ""}">${escapeHtml(STATUS_LABELS[job.status] || job.status)}</span>
              <small>${escapeHtml(WORKFLOW_LABELS[job.workflow] || job.workflow || "")}</small>
            </span>
            <small class="job-directory" title="结果目录">${escapeHtml(job.directory_name || job.id)}</small>
          </button>
          <div class="job-footer">
          <small class="job-time">${escapeHtml(formatTime(job.created_at))}</small>
          <div class="job-actions" aria-label="${escapeHtml(job.name)} 操作">
            <button class="job-action icon-only" type="button" data-action="select" data-ui-icon="open" title="查看任务详情">查看</button>
            ${
              isRunning
                ? `<button class="job-action warn icon-only" type="button" data-action="cancel" data-ui-icon="stop" title="取消任务">取消</button>`
                : `<button class="job-action danger icon-only" type="button" data-action="delete" data-ui-icon="trash" title="删除任务" ${canDelete ? "" : "disabled"}>删除</button>`
            }
          </div>
          </div>
        </div>
      `;
    })
    .join("");
  if (state.changedJobIds.size) {
    window.setTimeout(() => {
      els.jobsList.querySelectorAll(".badge.status-changed").forEach((badge) => badge.classList.remove("status-changed"));
    }, 650);
  }
}

export function applyJobsPayload(payload) {
  const previousStatuses = new Map(state.jobs.map((job) => [job.id, job.status]));
  state.jobs = payload.jobs || [];
  state.jobsTotal = payload.total ?? state.jobs.length;
  state.jobsHasMore = Boolean(payload.has_more);
  state.changedJobIds = new Set(
    state.jobs.filter((job) => previousStatuses.has(job.id) && previousStatuses.get(job.id) !== job.status).map((job) => job.id),
  );
  // A task absent from this page may still exist elsewhere in the history.
  if (!state.activeJobId) {
    state.activeJobId = state.jobs[0]?.id || null;
    state.activeJob = null;
    state.logKey = "";
    state.logText = "";
    state.logOffset = 0;
    state.artifactKey = "";
    state.filesSignature = "";
    state.plotsSignature = "";
  }
  renderJobs();
  if (!state.viewChosen) showWorkspaceView(state.activeJobId ? "detail" : "compose");
  renderActiveJob();
}

export function forgetDeletedJob(jobId) {
  if (state.activeJobId !== jobId) return;
  state.activeJobId = null;
  state.activeJob = null;
  resetActiveJobPanel();
}

export function applyJobDetail(job) {
  if (!job || job.id !== state.activeJobId) return;
  state.activeJob = job;
  const summary = state.jobs.find((item) => item.id === job.id);
  if (summary) Object.assign(summary, {
    status: job.status,
    updated_at: job.updated_at,
    current_step: job.current_step,
    step_index: job.step_index,
    step_count: job.step_count,
    exit_code: job.exit_code,
    error: job.error,
    progress_percent: job.progress_percent,
    simulation_progress: job.simulation_progress,
    performance_ns_per_day: job.performance_ns_per_day,
  });
  renderJobs();
  renderActiveJob();
}

let jobsRequestGeneration = 0;

export async function refreshJobs() {
  const generation = ++jobsRequestGeneration;
  const search = state.jobFilter.trim();
  const query = new URLSearchParams({ limit: "50", offset: String(state.jobsOffset), search });
  state.jobsBusy = true;
  renderJobs();
  try {
    let payload = await api(`/api/jobs?${query}`);
    if (generation !== jobsRequestGeneration || search !== state.jobFilter.trim()) return;
    if (state.jobsOffset > 0 && state.jobsOffset >= payload.total) {
      state.jobsOffset = Math.max(0, Math.floor((payload.total - 1) / 50) * 50);
      query.set("offset", String(state.jobsOffset));
      window.dispatchEvent(new CustomEvent("jobs-page-change"));
      payload = await api(`/api/jobs?${query}`);
      if (generation !== jobsRequestGeneration || search !== state.jobFilter.trim()) return;
    }
    applyJobsPayload(payload);
    const jobId = state.activeJobId;
    if (!jobId) return;
    try {
      const detail = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (generation === jobsRequestGeneration) applyJobDetail(detail);
    } catch (error) {
      if (generation !== jobsRequestGeneration || search !== state.jobFilter.trim() || jobId !== state.activeJobId) return;
      if (error.status !== 404) throw error;
      forgetDeletedJob(jobId);
      applyJobsPayload(payload);
      window.dispatchEvent(new CustomEvent("active-job-change"));
    }
  } finally {
    if (generation === jobsRequestGeneration && search === state.jobFilter.trim()) {
      state.jobsBusy = false;
      renderJobs();
    }
  }
}

export async function changeJobsPage(direction) {
  if (state.jobsBusy || (direction > 0 && !state.jobsHasMore)) return;
  state.jobsOffset = Math.max(0, state.jobsOffset + direction * 50);
  window.dispatchEvent(new CustomEvent("jobs-page-change"));
  await refreshJobs();
}

/* ---------- 活动任务详情（日志/文件智能拉取） ---------- */

function resetActiveJobPanel() {
  updateText(els.activeJobTitle, "未选择任务");
  updateText(els.terminalTitle, "run.log");
  updateText(els.jobStatus, "空闲");
  els.jobStatus.dataset.state = "idle";
  updateText(els.jobStep, "-");
  updateText(els.jobExit, "-");
  updateText(els.jobPercent, "0%");
  updateText(els.jobDuration, "-");
  updateText(els.jobPeakMemory, "-");
  updateText(els.jobPerformance, "-");
  updateText(els.jobEta, "-");
  updateText(els.jobDirectory, "-");
  els.progress.dataset.state = "idle";
  els.progressFill.style.transform = "scaleX(0)";
  els.cancelJob.disabled = true;
  els.benchmarkJob.disabled = true;
  document.querySelector("#analyze-job").hidden = true;
  document.querySelector("#analysis-source-job").hidden = true;
  els.adoptJob.hidden = true;
  els.resumeJob.hidden = true;
  els.retryJob.hidden = true;
  els.skipJob.hidden = true;
  els.jobError.hidden = true;
  els.skippedStepsNotice.hidden = true;
  els.qualityReports.textContent = "暂无质量报告";
  state.logKey = "";
  state.logSignature = "";
  state.logText = "";
  state.logOffset = 0;
  window.clearTimeout(logHighlightTimer);
  els.jobLog.textContent = "";
  els.logNotice.hidden = true;
  if (state.filesSignature !== "") {
    state.filesSignature = "";
    els.outputFiles.innerHTML = "";
    updateText(els.fileCount, "0 个");
  }
  if (state.plotsSignature !== "") {
    state.plotsSignature = "";
    els.analysisPlots.innerHTML = `<div class="plot-empty">完成模拟或分析后，曲线会显示在这里。</div>`;
    updateText(els.plotCount, "0 张");
  }
  if (els.downloadPlots) {
    els.downloadPlots.hidden = true;
    els.downloadPlots.href = "#";
  }
}

export async function renderActiveJob() {
  const summary = state.jobs.find((item) => item.id === state.activeJobId);
  const job = state.activeJob?.id === state.activeJobId ? state.activeJob : summary;
  if (!job) {
    resetActiveJobPanel();
    return;
  }

  updateText(els.activeJobTitle, job.name);
  document.querySelector("#analyze-job").hidden = job.status !== "completed" || Boolean(job.dry_run);
  const sourceButton = document.querySelector("#analysis-source-job");
  sourceButton.hidden = !job.analysis_source;
  sourceButton.textContent = job.analysis_source ? `来源：${job.analysis_source.name}` : "";
  sourceButton.dataset.jobId = job.analysis_source?.job_id || "";
  updateText(els.terminalTitle, `${job.name} · run.log`);
  updateText(els.jobStatus, STATUS_LABELS[job.status] || job.status);
  els.jobStatus.dataset.state = job.status;
  updateText(
    els.jobStep,
    `${job.step_index || 0}/${job.step_count || 0}${job.current_step ? ` · ${job.current_step}` : ""}`,
  );
  updateText(els.jobExit, job.exit_code ?? "-");
  updateText(els.jobDirectory, job.workdir || job.directory_name || job.id);
  els.progress.dataset.state = job.status;
  const fallbackPercent = job.step_count ? Math.min(100, ((job.step_index || 0) / job.step_count) * 100) : 0;
  const percent = Number.isFinite(Number(job.progress_percent)) ? Number(job.progress_percent) : fallbackPercent;
  els.progressFill.style.transform = `scaleX(${percent / 100})`;
  const simulation = job.simulation_progress || {};
  const simulationLabel = simulation.step != null ? ` · step ${simulation.step}${simulation.total_steps ? `/${simulation.total_steps}` : ""}` : "";
  updateText(els.jobPercent, `${percent.toFixed(1)}%${simulationLabel}`);
  const currentRun = (job.step_runs || []).at(-1);
  updateText(els.jobDuration, currentRun?.duration_seconds != null ? `${Number(currentRun.duration_seconds).toFixed(2)} s` : currentRun ? "运行中" : "-");
  updateText(els.jobPeakMemory, currentRun?.peak_rss_bytes ? formatSize(currentRun.peak_rss_bytes) : "-");
  const benchmarkBest = job.benchmark_results?.best;
  const nsPerDay = Number(benchmarkBest?.ns_per_day ?? simulation.ns_per_day ?? job.performance_ns_per_day);
  const performanceLabel = Number.isFinite(nsPerDay)
    ? `${nsPerDay.toFixed(2)} ns/day${benchmarkBest ? ` · 1×${benchmarkBest.ntomp}` : ""}`
    : "-";
  updateText(els.jobPerformance, performanceLabel);
  const etaSeconds = Number(simulation.eta_seconds);
  const etaText = Number.isFinite(etaSeconds)
    ? etaSeconds < 3600
      ? `${Math.ceil(etaSeconds / 60)} 分钟`
      : etaSeconds < 86400
        ? `${(etaSeconds / 3600).toFixed(1)} 小时`
        : `${(etaSeconds / 86400).toFixed(1)} 天`
    : "-";
  updateText(els.jobEta, etaText);
  if (els.compareJobSelect) {
    const options = state.jobs.filter((item) => item.id !== job.id);
    const selected = els.compareJobSelect.value;
    els.compareJobSelect.innerHTML = options.length
      ? options.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")
      : `<option value="">暂无其他任务</option>`;
    if (options.some((item) => item.id === selected)) els.compareJobSelect.value = selected;
    els.compareJob.disabled = !options.length;
  }
  els.cancelJob.disabled = !ACTIVE_STATES.has(job.status);
  els.benchmarkJob.disabled = job.status === "preparing" || job.workflow === "benchmark";
  els.adoptJob.hidden = job.status !== "interrupted";
  els.resumeJob.hidden = !job.can_resume_checkpoint;
  els.retryJob.hidden = job.status !== "failed";
  els.skipJob.hidden = !job.can_skip_failed_step;
  els.jobError.hidden = !job.error;
  const reason = job.skip_step_block_reason || "";
  const recoveryHint = job.status === "failed" && !job.can_skip_failed_step
    ? reason.includes("Parallel") ? "请重试整组并行分析。" : "此步骤不可跳过，请修正原因后重试。"
    : "";
  els.jobError.textContent = [job.error, recoveryHint].filter(Boolean).join("\n");
  const skipped = job.skipped_steps || [];
  els.skippedStepsNotice.hidden = !skipped.length;
  els.skippedStepsNotice.textContent = skipped.length
    ? `已跳过 ${skipped.length} 个可选步骤：${skipped.map((step) => step.title).join("；")}。对应输出可能不完整。`
    : "";

  await refreshJobArtifacts(job);
}

async function refreshJobArtifacts(job) {
  const tab = state.detailTab;
  const filters = state.plotFilters;
  const key = `${job.id}:${job.status}:${job.step_index}:${job.exit_code ?? ""}:${tab}:${JSON.stringify(filters)}`;
  if (key === state.artifactKey) return;
  state.artifactKey = key;

  if (tab === "research") {
    await renderResearch(job);
    return;
  }

  const encoded = encodeURIComponent(job.id);
  const query = new URLSearchParams();
  for (const [key, value] of [["replica", filters.replica], ["metric", filters.metric], ["begin_ns", filters.begin], ["end_ns", filters.end]]) {
    if (value !== "") query.set(key, value);
  }
  const [filesResult, plotsResult] = await Promise.allSettled([
    api(`/api/jobs/${encoded}/files`),
    tab === "plots" ? api(`/api/jobs/${encoded}/plots?${query}`) : Promise.resolve(null),
  ]);
  if (state.activeJobId !== job.id || state.artifactKey !== key) return;
  if (filesResult.status === "fulfilled") renderFiles(job.id, filesResult.value.files || [], filesResult.value.truncated);
  if (filesResult.status === "fulfilled" && tab === "quality") {
    await renderQualityReports(job.id, filesResult.value.files || [], key);
  }
  if (plotsResult.status === "fulfilled" && plotsResult.value) {
    els.plotFilterError.hidden = true;
    updatePlotOptions(plotsResult.value);
    renderPlots(job.id, plotsResult.value.plots || [], plotsResult.value.truncated, plotsResult.value.total);
  } else if (plotsResult.status === "rejected") {
    els.plotFilterError.hidden = false;
    els.plotFilterError.textContent = plotsResult.reason.message;
    els.analysisPlots.textContent = "图谱加载失败";
    state.artifactKey = "";
  }
}

async function renderQualityReports(jobId, files, key) {
  const reports = files.filter((file) => /^(quality-[^/]+\.(txt|json)|structure-preflight\.txt|sampling-summary\.json|structural-convergence\.json)$/.test(file.path)).slice(0, 12);
  const results = await Promise.allSettled(reports.map(async (file) => {
    if (file.size > 262144) return `<p>${escapeHtml(file.path)} 超过预览大小限制，请在输出文件中下载。</p>`;
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/download?path=${encodeURIComponent(file.path)}`);
    if (!response.ok) throw new Error("报告读取失败");
    const raw = await response.text();
    let body = `<pre>${escapeHtml(raw)}</pre>`;
    try {
      const report = JSON.parse(raw);
      if (report.series) {
        const rows = Object.entries(report.series).map(([name, stats]) => `<tr><th scope="row">${escapeHtml(name)}</th><td>${escapeHtml(Number(stats.mean).toPrecision(6))}</td><td>${escapeHtml(Number(stats.drift).toPrecision(4))}</td><td>${escapeHtml(stats.samples)}</td></tr>`).join("");
        body = `<p><strong>${escapeHtml(report.status)}</strong> · 目标温度 ${escapeHtml(report.target_temperature ?? "未记录")} K · ${escapeHtml(report.target_source_mdp || "旧任务参数")}</p><div class="report-table-scroll"><table><thead><tr><th>指标</th><th>均值</th><th>首尾窗口差</th><th>样本数</th></tr></thead><tbody>${rows}</tbody></table></div><p>${escapeHtml((report.failures || []).join("；"))}</p>`;
      }
    } catch { /* Text reports retain their original contents. */ }
    return `<details class="quality-report" open><summary>${escapeHtml(file.path)}</summary>${body}<a href="/api/jobs/${encodeURIComponent(jobId)}/download?path=${encodeURIComponent(file.path)}" download>下载报告</a></details>`;
  }));
  if (state.activeJobId !== jobId || state.artifactKey !== key) return;
  els.qualityReports.innerHTML = results.length
    ? results.map((result) => result.status === "fulfilled" ? result.value : "<p>报告暂时无法读取，请重新打开质量检查。</p>").join("")
    : "<p>尚未生成质量报告。</p>";
}

let logHighlightTimer = null;

export function appendLogChunk(chunk) {
  if (!chunk) return;
  if (chunk.reset) state.logText = "";
  if (Number(chunk.start) < state.logOffset && !chunk.reset) return;
  state.logText += chunk.text || "";
  state.logOffset = Number(chunk.offset) || state.logOffset;
  if (state.logText.length > MAX_LOG_CHARS) {
    state.logText = state.logText.slice(-MAX_LOG_CHARS);
    state.logSignature = "";
  }
  // Fast path: plain textContent update, no HTML parse per chunk.
  renderLog(state.logText, false);
  // Debounced highlight pass: rebuild with colored spans only when the log
  // has been quiet, so streaming updates never re-parse 300 KB every tick.
  window.clearTimeout(logHighlightTimer);
  logHighlightTimer = window.setTimeout(() => renderLog(state.logText, true), 1500);
}

function renderLog(log, highlight = true) {
  const truncated = Math.max(0, log.length - MAX_LOG_CHARS);
  const text = truncated ? log.slice(truncated) : log;
  const signature = `${state.logKey}:${highlight ? "hl" : "plain"}:${text.length}:${text.slice(-512)}`;
  if (signature === state.logSignature) return;
  state.logSignature = signature;

  const terminal = els.jobLog;
  const stickToBottom = terminal.scrollTop + terminal.clientHeight >= terminal.scrollHeight - 30;
  if (highlight) {
    const pattern = /\b(error|fatal|failed|failure|exception|warning|warn|step(?:\s+\d+)?|stage(?:\s+\d+)?)\b/gi;
    terminal.innerHTML = escapeHtml(text).replace(pattern, (match) => {
      const token = match.toLowerCase();
      const tone = /error|fatal|failed|failure|exception/.test(token) ? "error" : /warn/.test(token) ? "warning" : "step";
      return `<span class="log-${tone}">${match}</span>`;
    });
  } else {
    terminal.textContent = text;
  }
  if (truncated) {
    els.logNotice.hidden = false;
    updateText(els.logNotice, `日志过长，仅显示末尾（已折叠约 ${(truncated / 1024).toFixed(0)} KB）`);
  } else {
    els.logNotice.hidden = true;
  }
  if (stickToBottom) terminal.scrollTop = terminal.scrollHeight;
}

function formatSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function fileExt(path) {
  const base = path.split("/").pop() || path;
  if (base.length > 2 && base.startsWith("#") && base.endsWith("#")) return "bak";
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

export function applyFileFilter() {
  const query = state.fileFilter.trim().toLowerCase();
  const rows = els.outputFiles.querySelectorAll(".file-row:not(.empty)");
  if (!rows.length) return;
  let visible = 0;
  rows.forEach((row) => {
    const name = row.querySelector("a")?.textContent || "";
    const match = !query || name.toLowerCase().includes(query);
    row.hidden = !match;
    if (match) visible += 1;
  });
  els.outputFiles.querySelectorAll(".file-group").forEach((group) => {
    const matches = group.querySelectorAll(".file-row:not([hidden])").length;
    group.hidden = Boolean(query) && matches === 0;
    if (query && matches) group.open = true;
  });
  const truncated = els.outputFiles.dataset.truncated === "true";
  updateText(els.fileCount, query ? `${visible}/${rows.length} 个` : `${rows.length}${truncated ? "+" : ""} 个`);
}

function renderFiles(jobId, files, truncated = false) {
  const signature = `${jobId}:${truncated}:${files.map((file) => `${file.path}:${file.size}`).join(",")}`;
  if (signature === state.filesSignature) return;
  state.filesSignature = signature;
  els.outputFiles.dataset.truncated = truncated ? "true" : "false";

  if (!files.length) {
    updateText(els.fileCount, "0 个");
    els.outputFiles.innerHTML = `<div class="file-row empty"><span>暂无输出文件</span></div>`;
    return;
  }
  const groups = [
    { key: "structure", label: "结构", extensions: new Set(["pdb", "gro", "top", "itp", "ndx", "tpr"]) },
    { key: "trajectory", label: "轨迹", extensions: new Set(["xtc", "trr", "cpt"]) },
    { key: "energy", label: "能量", extensions: new Set(["edr", "xvg"]) },
    { key: "log", label: "日志", extensions: null },
  ];
  const grouped = new Map(groups.map((group) => [group.key, []]));
  files.forEach((file) => {
    const ext = fileExt(file.path);
    const group = groups.find((candidate) => candidate.extensions?.has(ext)) || groups[3];
    grouped.get(group.key).push(file);
  });
  const renderFile = (file) => {
      const href = `/api/jobs/${encodeURIComponent(jobId)}/download?path=${encodeURIComponent(file.path)}`;
      const ext = fileExt(file.path) || "file";
      return `<div class="file-row"><i class="file-ext" data-ext="${escapeHtml(ext)}">${escapeHtml(ext)}</i><a href="${href}" title="下载 ${escapeHtml(file.path)}">${escapeHtml(file.path)}</a><span>${formatSize(file.size)}</span></div>`;
  };
  els.outputFiles.innerHTML = groups
    .filter((group) => grouped.get(group.key).length)
    .map((group) => `<details class="file-group" data-group="${group.key}" ${group.key === "structure" ? "open" : ""}>
      <summary><span>${group.label}</span><small>${grouped.get(group.key).length} 个</small></summary>
      <div class="file-group-list">${grouped.get(group.key).map(renderFile).join("")}</div>
    </details>`)
    .join("");
  applyFileFilter();
}

/* ---------- 任务操作 ---------- */

export async function submitJob() {
  const button = els.submitJob;
  if (button.disabled) return;
  const nameInput = els.jobForm.querySelector("input[name='name']");
  if (nameInput && !nameInput.value.trim()) {
    toast("请先填写任务名", "error");
    nameInput.classList.add("input-error");
    nameInput.addEventListener("input", () => nameInput.classList.remove("input-error"), { once: true });
    nameInput.focus();
    return;
  }
  const dataObject = formObject();
  const requiredIssues = requiredSubmitIssues(dataObject);
  if (requiredIssues.length) {
    await confirmDialog({
      title: "需要确认配体电荷",
      subtitle: "真实运行 ACPYPE 前必须先确认关键参数",
      items: requiredIssues,
      confirmText: "返回设置",
      cancelText: "关闭",
      mark: "!",
      tone: "warn",
    });
    els.jobForm.querySelector("[data-ligand-field='charge_confirmed']")?.focus();
    return;
  }
  const warnings = submitWarnings(dataObject);
  const confirmed = await confirmSubmitWarnings(warnings);
  if (!confirmed) return;
  const data = new FormData();
  data.set("params", JSON.stringify(dataObject));
  selectedUploadFiles().forEach((file) => data.append("files", file));

  button.disabled = true;
  button.dataset.busy = "true";
  button.setAttribute("aria-busy", "true");
  updateText(button.querySelector(".button-label"), "正在启动…");
  try {
    const job = await api("/api/jobs", { method: "POST", body: data });
    state.activeJobId = job.id;
    state.activeJob = job;
    state.logKey = "";
    state.logText = "";
    state.logOffset = 0;
    state.artifactKey = "";
    state.filesSignature = "";
    toast(`任务已提交：${job.name}`, "success");
    showDetailTab("overview");
    showWorkspaceView("detail");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    toast(friendlyPreviewError(error.message), "error");
  } finally {
    button.dataset.busy = "false";
    button.removeAttribute("aria-busy");
    button.disabled = false;
    updateText(button.querySelector(".button-label"), "启动模拟");
    refreshPreview().catch(() => {});
  }
}

export async function cancelActiveJob() {
  if (!state.activeJobId) return;
  els.cancelJob.disabled = true;
  await cancelJob(state.activeJobId);
  renderActiveJob();
}

export async function adoptActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "重新接管后台任务？",
    subtitle: "只接管仍在当前任务目录运行的 mdrun",
    items: [
      `任务：${job?.name || jobId}`,
      "系统会核对进程工作目录和命令；该进程完成后自动继续剩余分析与后处理。",
    ],
    confirmText: "确认接管",
    cancelText: "暂不接管",
    mark: "↻",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/adopt`, { method: "POST" });
    toast("已重新接管后台任务", "success");
    state.jobsSignature = "";
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function resumeActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "从检查点继续？",
    subtitle: "确认后台原进程已经停止",
    items: [
      `任务：${job?.name || jobId}`,
      "后端会再次检查活动 mdrun；若仍有进程写入，会拒绝启动第二个进程。",
      "续跑会使用现有 CPT 并追加到原 XTC/EDR，完成后继续剩余步骤。",
    ],
    confirmText: "检查并继续",
    cancelText: "取消",
    mark: "▶",
    tone: "warn",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/resume-checkpoint`, { method: "POST" });
    toast("检查点续跑已加入队列", "success");
    state.jobsSignature = "";
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function retryActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "重试失败步骤？",
    subtitle: "从失败的那一步重新执行，已完成的步骤不会重跑",
    items: [`任务：${job?.name || jobId}`, `失败步骤：${job?.current_step || job?.error || "未知"}`],
    confirmText: "重试",
    cancelText: "取消",
    mark: "↻",
    tone: "warn",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/retry-step`, { method: "POST" });
    toast("已重新入队，从失败步骤重试", "success");
    state.jobsSignature = "";
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function skipActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "跳过失败步骤？",
    subtitle: "跳过当前失败的步骤，直接从下一步继续（可能产生不完整的结果）",
    items: [`任务：${job?.name || jobId}`, `失败步骤：${job?.current_step || job?.error || "未知"}`],
    confirmText: "跳过",
    cancelText: "取消",
    mark: "⏭",
    tone: "warn",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/skip-step`, { method: "POST" });
    toast("已跳过失败步骤并入队", "success");
    state.jobsSignature = "";
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export function selectJob(jobId) {
  if (!jobId) return;
  showWorkspaceView("detail");
  if (jobId === state.activeJobId) {
    state.artifactKey = "";
    renderActiveJob();
    return;
  }
  state.activeJobId = jobId;
  resetPlotFilters();
  state.activeJob = null;
  state.logKey = "";
  state.logText = "";
  state.logOffset = 0;
  state.artifactKey = "";
  state.filesSignature = "";
  state.plotsSignature = "";
  els.analysisPlots.textContent = "正在加载图谱…";
  els.qualityReports.textContent = "正在加载报告…";
  els.outputFiles.textContent = "正在加载文件…";
  renderJobs();
  els.jobLog.textContent = "";
  api(`/api/jobs/${encodeURIComponent(jobId)}`).then(applyJobDetail).catch((error) => toast(error.message, "error"));
  window.dispatchEvent(new CustomEvent("active-job-change"));
  replaySwapAnimation(".job-panel, .command-panel, .files-panel, .plots-panel");
}

export async function cancelJob(jobId) {
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "取消任务？",
    subtitle: "会向正在运行的进程发送停止请求",
    items: [
      `任务：${job?.name || jobId}`,
      "已经写出的中间文件会留在任务目录中；要彻底清理，请等任务停止后再删除。",
    ],
    confirmText: "确认取消",
    cancelText: "继续运行",
    mark: "!",
    tone: "warn",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    toast("取消请求已发送", "success");
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function deleteJob(jobId) {
  if (!jobId) return;
  const job = state.jobs.find((item) => item.id === jobId);
  if (!job) return;
  if (ACTIVE_STATES.has(job.status)) {
    toast("运行中的任务需要先取消，再删除。", "error");
    return;
  }
  const confirmed = await confirmDialog({
    title: "删除任务？",
    subtitle: "会删除日志和输出文件",
    items: [
      `任务：${job.name}`,
      "删除后任务不会再出现在列表里，日志、TPR、XTC、EDR、GRO 等输出文件也会一起移除。",
    ],
    confirmText: "确认删除",
    cancelText: "保留任务",
    mark: "×",
    tone: "danger",
  });
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      state.activeJob = null;
      state.logKey = "";
      state.logText = "";
      state.logOffset = 0;
      state.artifactKey = "";
      state.filesSignature = "";
    }
    state.jobsSignature = "";
    toast("任务已删除", "success");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function cloneActiveJob() {
  if (!state.activeJobId) return;
  try {
    const cloned = await api(`/api/jobs/${encodeURIComponent(state.activeJobId)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    state.activeJobId = cloned.id;
    state.activeJob = cloned;
    state.jobsSignature = "";
    toast(`已复制任务：${cloned.name}`, "success");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}

export async function benchmarkActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = state.activeJob?.id === jobId ? state.activeJob : state.jobs.find((item) => item.id === jobId);
  const confirmed = await confirmDialog({
    title: "创建线程性能基准？",
    subtitle: "复制当前任务的 TPR，不修改正式轨迹",
    items: [
      `来源：${job?.name || jobId}`,
      "自动测试物理核心数、中间值和全部逻辑 CPU，每组 10,000 step。",
      "单 GPU 忙碌时基准任务会留在队列中，避免影响正式模拟。",
    ],
    confirmText: "创建基准任务",
    cancelText: "取消",
    mark: "⚙",
  });
  if (!confirmed) return;
  els.benchmarkJob.disabled = true;
  try {
    const benchmark = await api(`/api/jobs/${encodeURIComponent(jobId)}/benchmark`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    state.activeJobId = benchmark.id;
    state.activeJob = benchmark;
    state.jobsSignature = "";
    toast(`性能基准已入队：${benchmark.name}`, "success");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
    els.benchmarkJob.disabled = false;
  }
}

export async function compareActiveJob() {
  const otherJobId = els.compareJobSelect?.value;
  const jobId = state.activeJobId;
  if (!jobId || !otherJobId) return;
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/diff/${encodeURIComponent(otherJobId)}`);
    const items = result.differences.length
      ? result.differences.slice(0, 30).map((item) => `${item.path}: ${JSON.stringify(item.left)} → ${JSON.stringify(item.right)}`)
      : ["参数完全一致"];
    if (result.differences.length > 30) items.push(`另有 ${result.differences.length - 30} 项未显示`);
    await confirmDialog({
      title: "任务参数差异",
      subtitle: `${result.left.name} ↔ ${result.right.name}`,
      items,
      confirmText: "关闭",
      cancelText: "关闭",
      mark: "Δ",
    });
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  }
}
