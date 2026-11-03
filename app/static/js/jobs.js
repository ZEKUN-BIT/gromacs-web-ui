// 任务模块：任务列表、活动任务详情（日志/文件/图谱）、提交与选择任务。任务操作见 job-actions.js。

import {
  STATUS_LABELS,
  WORKFLOW_LABELS,
  ACTIVE_STATES,
  MAX_LOG_CHARS,
  els,
  state,
  escapeHtml,
  updateText,
  toast,
  api,
  confirmSubmitWarnings,
  replaySwapAnimation,
  showWorkspaceView,
  showDetailTab,
} from "./core.js?v=b71b8f0b4a932c44";
import { renderPlots, resetPlotFilters, updatePlotOptions } from "./plots.js?v=b71b8f0b4a932c44";
import { renderResearch } from "./research.js?v=b71b8f0b4a932c44";
import {
  captureSubmissionSnapshot,
  isSubmissionSnapshotReady,
  requiredSubmitIssues,
  submitWarnings,
  friendlyPreviewError,
  refreshPreview,
} from "./form.js?v=b71b8f0b4a932c44";
import { apiSubmissionIssues, clearSubmissionIssues, showSubmissionIssues } from "./submission-validation.js?v=b71b8f0b4a932c44";
import { uploadJob, bindUploadRecovery } from "./submission-upload.js?v=b71b8f0b4a932c44";

/* ---------- 任务列表（签名对比，按需渲染 + 事件委托） ---------- */

function computeJobsSignature() {
  return (
    state.jobs.map((job) => `${job.id}:${job.status}:${job.step_index}:${job.name}:${job.project}:${job.pinned}:${JSON.stringify(job.tags)}`).join("|") +
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
    if (els.jobsList.contains(document.activeElement)) els.jobFilter?.focus({ preventScroll: true });
    const title = state.jobsBusy ? "正在读取任务…" : state.jobFilter.trim() ? "没有匹配任务" : "暂无任务";
    els.jobsList.innerHTML = `<div class="empty-jobs"><strong>${title}</strong><small>${state.jobFilter.trim() ? "换个名称、状态或工作流试试" : "提交后会出现在这里"}</small></div>`;
    return;
  }
  const focused = els.jobsList.contains(document.activeElement) ? {
    jobId: document.activeElement.closest("[data-job-id]")?.dataset.jobId,
    action: document.activeElement.dataset.action,
    select: document.activeElement.classList.contains("job-select"),
  } : null;
  const scrollTop = els.jobsList.scrollTop;
  els.jobsList.innerHTML = visibleJobs
    .map((job) => {
      const isActive = job.id === state.activeJobId;
      const isRunning = ACTIVE_STATES.has(job.status);
      const canDelete = !isRunning;
      return `
        <div class="job-item ${isActive ? "active" : ""}" data-job-id="${escapeHtml(job.id)}" data-status="${escapeHtml(job.status)}">
          <button class="job-select" type="button" data-action="select" title="${escapeHtml(job.name)}&#10;结果目录：${escapeHtml(job.directory_name || job.id)}">
            <span class="job-titleline"><span data-ui-icon="file" aria-hidden="true"></span><strong>${job.pinned ? "置顶 · " : ""}${escapeHtml(job.name)}</strong></span>
            <span class="job-meta">
              <span class="badge ${escapeHtml(job.status)} ${state.changedJobIds.has(job.id) ? "status-changed" : ""}">${escapeHtml(STATUS_LABELS[job.status] || job.status)}</span>
              <small class="job-time">${escapeHtml(formatTime(job.created_at))}</small>
              <small>${escapeHtml(WORKFLOW_LABELS[job.workflow] || job.workflow || "")}</small>
            </span>
            <small class="job-directory" title="结果目录">${escapeHtml(job.directory_name || job.id)}</small>
          </button>
          <div class="job-footer">
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
  if (focused) {
    const row = [...els.jobsList.querySelectorAll("[data-job-id]")].find(item => item.dataset.jobId === focused.jobId);
    const target = row && (focused.select ? row.querySelector(".job-select")
      : [...row.querySelectorAll("[data-action]")].find(button => button.dataset.action === focused.action));
    (target || row?.querySelector(".job-select") || els.jobFilter)?.focus({ preventScroll: true });
  }
  els.jobsList.scrollTop = scrollTop;
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
    state.logTailOmittedBytes = 0;
    state.logTrimmedChars = 0;
    state.artifactKey = "";
    state.filesSignature = "";
    state.plotsSignature = "";
  }
  renderJobs();
  if (!state.viewChosen) showWorkspaceView("compose");
  renderActiveJob();
  window.dispatchEvent(new CustomEvent("jobs-updated"));
}

export function forgetDeletedJob(jobId) {
  window.dispatchEvent(new CustomEvent("job-deleted", { detail: jobId }));
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
    project: job.project,
    tags: job.tags,
    notes: job.notes,
    pinned: job.pinned,
  });
  renderJobs();
  renderActiveJob();
}

let jobsRequestGeneration = 0;

export async function refreshJobs() {
  const generation = ++jobsRequestGeneration;
  const search = state.jobFilter.trim();
  const query = new URLSearchParams({ limit: "50", offset: String(state.jobsOffset), search, project: state.jobProject || "" });
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
  window.dispatchEvent(new CustomEvent("job-rendered", { detail: null }));
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
  updateText(document.querySelector("#progress-observation"), "");
  els.progress.dataset.state = "idle";
  els.progress.dataset.unknown = "false";
  els.progress.setAttribute("aria-valuenow", "0");
  els.progress.setAttribute("aria-valuetext", "未选择任务");
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
  state.logTailOmittedBytes = 0;
  state.logTrimmedChars = 0;
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
  if (els.downloadLog) els.downloadLog.hidden = true;
}

export async function renderActiveJob() {
  const summary = state.jobs.find((item) => item.id === state.activeJobId);
  const job = state.activeJob?.id === state.activeJobId ? state.activeJob : summary;
  if (!job) {
    resetActiveJobPanel();
    return;
  }
  window.dispatchEvent(new CustomEvent("job-rendered", { detail: job }));

  updateText(els.activeJobTitle, job.name);
  document.querySelector("#analyze-job").hidden = job.status !== "completed" || Boolean(job.dry_run);
  const sourceButton = document.querySelector("#analysis-source-job");
  sourceButton.hidden = !job.analysis_source;
  sourceButton.textContent = job.analysis_source ? `来源：${job.analysis_source.name}` : "";
  sourceButton.dataset.jobId = job.analysis_source?.job_id || "";
  updateText(els.terminalTitle, `${job.name} · run.log`);
  if (els.downloadLog) {
    els.downloadLog.href = `/api/jobs/${encodeURIComponent(job.id)}/download?path=run.log`;
    els.downloadLog.hidden = false;
  }
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
  const hasPercent = job.progress_percent != null && Number.isFinite(Number(job.progress_percent));
  const unknown = job.status === "running" && (!hasPercent || job.progress_observation?.state === "waiting");
  const percent = Math.max(0, Math.min(100, hasPercent ? Number(job.progress_percent) : Number.isFinite(fallbackPercent) ? fallbackPercent : 0));
  els.progress.dataset.unknown = String(unknown);
  if (unknown) els.progress.removeAttribute("aria-valuenow");
  else els.progress.setAttribute("aria-valuenow", String(percent));
  els.progress.setAttribute("aria-valuetext", unknown ? "运行中，暂无法估算进度" : `${STATUS_LABELS[job.status] || job.status}，进度 ${percent.toFixed(1)}%`);
  els.progressFill.style.transform = `scaleX(${unknown ? 0 : percent / 100})`;
  const simulation = job.simulation_progress || {};
  const simulationLabel = simulation.step != null ? ` · step ${simulation.step}${simulation.total_steps ? `/${simulation.total_steps}` : ""}` : "";
  const physicalTime = simulation.time_ps != null ? ` · ${(simulation.time_ps / 1000).toFixed(3)} ns` : "";
  updateText(els.jobPercent, `${unknown ? "暂无法估算" : `${percent.toFixed(1)}%`}${physicalTime || simulationLabel}`);
  const observation = document.querySelector("#progress-observation");
  if (observation) {
    observation.hidden = !job.progress_observation;
    observation.dataset.state = job.progress_observation?.state || "";
    const updated = job.progress_observation?.updated_at ? ` 最后更新 ${new Date(job.progress_observation.updated_at).toLocaleTimeString()}` : "";
    observation.textContent = `${job.progress_observation?.message || ""}${updated}`;
  }
  const currentRun = (job.step_runs || []).at(-1);
  const startedAt = currentRun?.started_at || job.process_started_at || job.started_at;
  const elapsed = startedAt && job.status === "running" ? Math.max(0, (Date.now() - new Date(startedAt).getTime()) / 60000) : null;
  updateText(els.jobDuration, currentRun?.duration_seconds != null ? `${Number(currentRun.duration_seconds).toFixed(2)} s` : elapsed != null ? `${Math.floor(elapsed / 60)} 小时 ${Math.floor(elapsed % 60)} 分钟` : currentRun ? "运行中" : "-");
  updateText(els.jobPeakMemory, currentRun?.peak_rss_bytes ? formatSize(currentRun.peak_rss_bytes) : "-");
  const benchmarkBest = job.benchmark_results?.best;
  const rawSpeed = benchmarkBest?.ns_per_day ?? simulation.ns_per_day ?? job.performance_ns_per_day;
  const nsPerDay = rawSpeed == null ? NaN : Number(rawSpeed);
  const performanceLabel = Number.isFinite(nsPerDay)
    ? `${nsPerDay.toFixed(2)} ns/day${benchmarkBest ? ` · 1×${benchmarkBest.ntomp}` : ""}`
    : "-";
  updateText(els.jobPerformance, performanceLabel);
  const etaSeconds = simulation.eta_seconds == null ? NaN : Number(simulation.eta_seconds);
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
  const diskWarning = job.disk_usage?.warning ? `磁盘空间告警：剩余 ${formatSize(job.disk_usage.free_bytes)}，请及时清理空间。` : "";
  els.jobError.hidden = !job.error && !diskWarning && !job.deletion_pending;
  const reason = job.skip_step_block_reason || "";
  const recoveryHint = job.status === "failed" && !job.can_skip_failed_step
    ? reason.includes("Parallel") ? "请重试整组并行分析。" : "此步骤不可跳过，请修正原因后重试。"
    : "";
  els.jobError.textContent = [job.failure_advice?.message, job.failure_advice?.recovery, job.error, recoveryHint, diskWarning,
    job.deletion_pending ? "任务文件删除未完成，请再次删除该任务以完成清理。" : ""].filter(Boolean).join("\n");
  if (job.deletion_pending) {
    els.resumeJob.hidden = true;
    els.retryJob.hidden = true;
    els.skipJob.hidden = true;
  }
  const skipped = job.skipped_steps || [];
  els.skippedStepsNotice.hidden = !skipped.length;
  els.skippedStepsNotice.textContent = skipped.length
    ? `已跳过 ${skipped.length} 个可选步骤：${skipped.map((step) => step.title).join("；")}。对应输出可能不完整。`
    : "";

  await refreshJobArtifacts(job);
}

let artifactRequestGeneration = 0;
let artifactController = null;
let artifactRequestedAt = 0;
let artifactPendingKey = "";
const ARTIFACT_REFRESH_MS = 5000;

async function refreshJobArtifacts(job) {
  if (document.hidden || state.workspaceView !== "detail") return;
  const tab = state.detailTab;
  if (!["files", "plots", "quality", "research"].includes(tab)) return;
  const filters = state.plotFilters;
  const fileView = fileViews.get(job.id) || { search: "", offset: 0 };
  const key = `${job.id}:${job.status}:${job.step_index}:${job.exit_code ?? ""}:${job.storage_revision || 0}:${tab}:${JSON.stringify(filters)}:${tab === "files" ? JSON.stringify([fileView.search, fileView.offset]) : ""}`;
  if (key === state.artifactKey && (artifactPendingKey === key || !ACTIVE_STATES.has(job.status) || Date.now() - artifactRequestedAt < ARTIFACT_REFRESH_MS)) return;
  state.artifactKey = key;
  artifactPendingKey = key;
  artifactRequestedAt = Date.now();
  const generation = ++artifactRequestGeneration;
  artifactController?.abort();
  artifactController = new AbortController();
  const signal = artifactController.signal;
  const isCurrent = () => generation === artifactRequestGeneration && state.activeJobId === job.id && state.detailTab === tab && state.artifactKey === key;

  if (tab === "research") {
    await renderResearch(job);
    if (isCurrent()) artifactPendingKey = "";
    return;
  }

  const encoded = encodeURIComponent(job.id);
  const query = new URLSearchParams();
  for (const [key, value] of [["replica", filters.replica], ["metric", filters.metric], ["begin_ns", filters.begin], ["end_ns", filters.end]]) {
    if (value !== "") query.set(key, value);
  }
  const [filesResult, plotsResult] = await Promise.allSettled([
    api(`/api/jobs/${encoded}/files${tab === "files" ? `?${new URLSearchParams({limit: "100", offset: String(fileView.offset), search: fileView.search})}` : ""}`, { signal }),
    tab === "plots" ? api(`/api/jobs/${encoded}/plots?${query}`, { signal }) : Promise.resolve(null),
  ]);
  if (!isCurrent()) return;
  if (filesResult.status === "fulfilled") {
    filePage = tab === "files" && Number.isFinite(filesResult.value.total) ? { ...filesResult.value, jobId: job.id, search: fileView.search } : null;
    renderFiles(job.id, filesResult.value.files || [], filesResult.value.truncated);
    renderFilePager();
  }
  else {
    filePage = null;
    renderFilePager();
    state.filesSignature = "";
    els.outputFiles.dataset.truncated = "false";
    updateText(els.fileCount, "读取失败");
    const targets = tab === "quality" ? [els.outputFiles, els.qualityReports] : [els.outputFiles];
    for (const target of targets) {
      target.textContent = "";
      const message = document.createElement("p");
      message.setAttribute("role", "alert");
      message.textContent = `文件列表加载失败：${filesResult.reason.message || "连接中断"}`;
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "tool-button";
      retry.textContent = "重试加载文件";
      retry.addEventListener("click", () => {
        if (state.activeJobId !== job.id || state.detailTab !== tab) return;
        retry.disabled = true;
        state.artifactKey = "";
        renderActiveJob();
      });
      target.append(message, retry);
    }
  }
  if (filesResult.status === "fulfilled" && tab === "quality") {
    await renderQualityReports(job.id, filesResult.value.files || [], isCurrent);
    if (!isCurrent()) return;
  }
  if (plotsResult.status === "fulfilled" && plotsResult.value) {
    els.plotFilterError.hidden = true;
    updatePlotOptions(plotsResult.value);
    renderPlots(job.id, plotsResult.value.plots || [], plotsResult.value.truncated, plotsResult.value.total);
  } else if (plotsResult.status === "rejected") {
    els.plotFilterError.hidden = false;
    els.plotFilterError.textContent = plotsResult.reason.message;
    els.analysisPlots.textContent = "图谱加载失败";
  }
  if (filesResult.status === "rejected" || plotsResult.status === "rejected") state.artifactKey = "";
  artifactPendingKey = "";
}

async function renderQualityReports(jobId, files, isCurrent) {
  const reports = files.filter((file) => /^(quality-[^/]+\.(txt|json)|structure-preflight\.txt|sampling-summary\.json|structural-convergence\.json)$/.test(file.path)).slice(0, 12);
  const results = await Promise.allSettled(reports.map(async (file) => {
    if (file.size > 262144) return `<p>${escapeHtml(file.path)} 超过预览大小限制，请在输出文件中下载。</p>`;
    const raw = await api(`/api/jobs/${encodeURIComponent(jobId)}/download?path=${encodeURIComponent(file.path)}`, { responseType: "text" });
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
  if (!isCurrent()) return;
  els.qualityReports.innerHTML = results.length
    ? results.map((result) => result.status === "fulfilled" ? result.value : "<p>报告暂时无法读取，请重新打开质量检查。</p>").join("")
    : "<p>尚未生成质量报告。</p>";
}

let logHighlightTimer = null;

export function appendLogChunk(chunk) {
  if (!chunk) return;
  if (chunk.reset) {
    state.logText = "";
    state.logOffset = 0;
    state.logTailOmittedBytes = 0;
    state.logTrimmedChars = 0;
  }
  if (Number(chunk.start) < state.logOffset && !chunk.reset) return;
  if (!state.logOffset && !state.logText) state.logTailOmittedBytes = Math.max(0, Number(chunk.start) || 0);
  state.logText += chunk.text || "";
  if (Number.isFinite(Number(chunk.offset))) state.logOffset = Number(chunk.offset);
  if (state.logText.length > MAX_LOG_CHARS) {
    state.logTrimmedChars += state.logText.length - MAX_LOG_CHARS;
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
  const text = log;
  const truncated = state.logTailOmittedBytes > 0 || state.logTrimmedChars > 0;
  const signature = `${state.activeJobId}:${highlight ? "hl" : "plain"}:${state.logOffset}:${state.logTailOmittedBytes}:${state.logTrimmedChars}:${text.length}:${text.slice(-512)}`;
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
    const omitted = [state.logTailOmittedBytes ? `打开时省略 ${(state.logTailOmittedBytes / 1024).toFixed(1)} KB` : "", state.logTrimmedChars ? `后续裁剪 ${state.logTrimmedChars.toLocaleString()} 个字符` : ""].filter(Boolean).join("；");
    updateText(els.logNotice, `仅显示末尾日志（${omitted}）。复制仅包含当前显示内容，可下载已保留日志；服务端轮转可能省略更早内容。`);
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

const fileViews = new Map();
let filePage = null, fileSearchTimer = null;
for (const type of ["workspace-view-change", "detail-tab-change", "active-job-change"]) window.addEventListener(type, () => {
  artifactController?.abort();
  artifactRequestGeneration += 1;
  artifactPendingKey = "";
  state.artifactKey = "";
});

function renderFilePager() {
  const nav = document.querySelector("#file-pagination");
  if (!nav) return;
  nav.hidden = !filePage || state.detailTab !== "files";
  if (!filePage) return;
  updateText(els.fileCount, `${filePage.total} 个`);
  updateText(document.querySelector("#files-page-status"), `${filePage.total ? filePage.offset + 1 : 0}–${Math.min(filePage.offset + filePage.files.length, filePage.total)} / ${filePage.total}`);
  document.querySelector("#files-previous").disabled = filePage.offset === 0;
  document.querySelector("#files-next").disabled = !filePage.has_more;
  for (const [id, direction] of [["files-previous", -1], ["files-next", 1]]) {
    document.getElementById(id).onclick = () => {
      if (filePage?.jobId !== state.activeJobId) return;
      fileViews.set(state.activeJobId, { search: state.fileFilter, offset: Math.max(0, filePage.offset + direction * filePage.limit) });
      state.artifactKey = "";
      renderActiveJob();
    };
  }
}

export function applyFileFilter() {
  if (filePage?.jobId === state.activeJobId && filePage.search !== state.fileFilter) {
    clearTimeout(fileSearchTimer);
    fileViews.set(state.activeJobId, { search: state.fileFilter, offset: 0 });
    const jobId = state.activeJobId;
    fileSearchTimer = setTimeout(() => {
      if (state.activeJobId !== jobId || state.detailTab !== "files") return;
      state.artifactKey = ""; renderActiveJob();
    }, 250);
    return;
  }
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
  if (filePage?.jobId === state.activeJobId) renderFilePager();
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
  const opened = new Map([...els.outputFiles.querySelectorAll(".file-group")].map(group => [group.dataset.group, group.open]));
  const sameJob = els.outputFiles.dataset.jobId === jobId;
  const scrollTop = els.outputFiles.scrollTop;
  els.outputFiles.dataset.jobId = jobId;
  els.outputFiles.innerHTML = groups
    .filter((group) => grouped.get(group.key).length)
    .map((group) => `<details class="file-group" data-group="${group.key}" ${(sameJob && opened.has(group.key) ? opened.get(group.key) : group.key === "structure") ? "open" : ""}>
      <summary><span>${group.label}</span><small>${grouped.get(group.key).length} 个</small></summary>
      <div class="file-group-list">${grouped.get(group.key).map(renderFile).join("")}</div>
    </details>`)
    .join("");
  applyFileFilter();
  els.outputFiles.scrollTop = scrollTop;
}

/* ---------- 任务操作 ---------- */

export function bindSubmissionRecovery() {
  bindUploadRecovery(async job => {
    state.activeJobId = null;
    await selectJob(job.id);
    showDetailTab("overview");
    showWorkspaceView("detail");
  });
}

let submissionPending = false;

export async function submitJob() {
  const button = els.submitJob;
  if (submissionPending || button.dataset.busy === "true") return;
  const snapshot = captureSubmissionSnapshot();
  const dataObject = snapshot.params;
  const requiredIssues = requiredSubmitIssues(dataObject);
  if (!String(dataObject.name || "").trim()) requiredIssues.unshift({ field: "name", message: "请先填写任务名" });
  if (requiredIssues.length) {
    showSubmissionIssues(requiredIssues);
    return;
  }
  if (!isSubmissionSnapshotReady(snapshot)) {
    showSubmissionIssues([{ message: "参数或文件已变化，请等待本次预览更新后重新提交。" }]);
    void refreshPreview();
    return;
  }
  submissionPending = true;
  try {
    clearSubmissionIssues();
    const warnings = submitWarnings(dataObject);
    const confirmed = await confirmSubmitWarnings(warnings);
    if (!confirmed) return;
    if (!isSubmissionSnapshotReady(snapshot)) {
      showSubmissionIssues([{ message: "确认期间参数或文件已变化。本次尚未提交，请查看更新后的预览并重新确认。" }]);
      return;
    }
    const data = new FormData();
    data.set("params", JSON.stringify(dataObject));
    snapshot.files.forEach((file) => data.append("files", file));
    button.disabled = true;
    button.dataset.busy = "true";
    button.setAttribute("aria-busy", "true");
    updateText(button.querySelector(".button-label"), "正在上传…");
    const job = await uploadJob(data, button);
    state.activeJobId = job.id;
    state.activeJob = job;
    state.logKey = "";
    state.logText = "";
    state.logOffset = 0;
    state.logTailOmittedBytes = 0;
    state.logTrimmedChars = 0;
    state.artifactKey = "";
    state.filesSignature = "";
    toast(`任务已提交：${job.name}`, "success");
    showDetailTab("overview");
    showWorkspaceView("detail");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    showSubmissionIssues(apiSubmissionIssues(error, friendlyPreviewError(error.message)));
  } finally {
    submissionPending = false;
    button.dataset.busy = "false";
    button.removeAttribute("aria-busy");
    refreshPreview().catch(() => {});
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
  if (fileViews.size >= 64 && !fileViews.has(jobId)) fileViews.delete(fileViews.keys().next().value);
  const fileView = fileViews.get(jobId) || { search: "", offset: 0 };
  state.fileFilter = fileView.search;
  if (els.fileFilter) els.fileFilter.value = fileView.search;
  filePage = null;
  renderFilePager();
  resetPlotFilters();
  state.activeJob = null;
  state.logKey = "";
  state.logText = "";
  state.logOffset = 0;
  state.logTailOmittedBytes = 0;
  state.logTrimmedChars = 0;
  state.artifactKey = "";
  state.filesSignature = "";
  state.plotsSignature = "";
  els.analysisPlots.textContent = "正在加载图谱…";
  els.qualityReports.textContent = "正在加载报告…";
  els.outputFiles.textContent = "正在加载文件…";
  renderJobs();
  els.jobLog.textContent = "";
  els.logNotice.hidden = true;
  api(`/api/jobs/${encodeURIComponent(jobId)}`).then(applyJobDetail).catch((error) => toast(error.message, "error"));
  window.dispatchEvent(new CustomEvent("active-job-change"));
  replaySwapAnimation(".job-panel, .command-panel, .files-panel, .plots-panel");
}
