// 入口模块：事件绑定、轮询、启动。

import {
  FILE_FIELD_NAMES,
  MAX_LOG_CHARS,
  els,
  state,
  cacheElements,
  updateText,
  toast,
  closeConfirmDialog,
  showWorkspaceView,
  showDetailTab,
} from "./core.js?v=20261006-026";
import {
  saveSettings,
  setMdpPanelExpanded,
  updateMdpPanelState,
  fillStdinOverridesFromPreview,
  syncFileFieldsFromUploads,
  selectedFileSummary,
  renderWorkflowGuide,
  renderForceFieldSource,
  renderWaterModelOptions,
  refreshHealth,
  refreshPreview,
  debouncedPreview,
  toggleSections,
  ensureDefaultLigandRow,
  addLigandRow,
  removeLigandRow,
  detectLigandsFromPdb,
  refreshRowMode,
  applyHardwareRecommendation,
  updateResourceAdvice,
} from "./form.js?v=20261006-026";
import {
  submitJob,
  cancelActiveJob,
  adoptActiveJob,
  resumeActiveJob,
  retryActiveJob,
  skipActiveJob,
  selectJob,
  cancelJob,
  deleteJob,
  cloneActiveJob,
  benchmarkActiveJob,
  compareActiveJob,
  renderJobs,
  refreshJobs,
  applyFileFilter,
  applyJobsPayload,
  applyJobDetail,
  appendLogChunk,
  renderActiveJob,
  changeJobsPage,
  forgetDeletedJob,
} from "./jobs.js?v=20261006-026";
import { bindDraftSaving, restoreDraft } from "./drafts.js?v=20261006-026";
import { bindPlotControls } from "./plots.js?v=20261006-026";
import { bindExistingAnalysis } from "./existing-analysis.js?v=20261006-026";
import { bindResearch } from "./research.js?v=20261006-026";

function disconnectEventStream() {
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  state.eventSource?.close();
  state.eventSource = null;
}

function connectEventStream() {
  disconnectEventStream();
  if (document.hidden) return;
  const query = new URLSearchParams();
  query.set("jobs_offset", String(state.jobsOffset));
  query.set("jobs_search", state.jobFilter.trim());
  if (state.activeJobId) query.set("job_id", state.activeJobId);
  if (state.logOffset > 0) {
    // Resume where we left off after a reconnect.
    query.set("log_offset", String(state.logOffset));
  } else {
    // Fresh panel open: jump straight to the tail instead of streaming the
    // whole run.log from byte 0 (a multi-MB log used to take minutes to load).
    query.set("log_tail", String(MAX_LOG_CHARS));
  }
  const source = new EventSource(`/api/events?${query}`);
  state.eventSource = source;
  source.addEventListener("jobs", (event) => {
    if (state.eventSource !== source) return;
    const payload = JSON.parse(event.data);
    if (state.jobsOffset > 0 && state.jobsOffset >= payload.total) {
      refreshJobs().catch((error) => toast(error.message, "error")).finally(connectEventStream);
      return;
    }
    const previousActive = state.activeJobId;
    applyJobsPayload(payload);
    if (state.activeJobId !== previousActive) connectEventStream();
  });
  source.addEventListener("job", (event) => applyJobDetail(JSON.parse(event.data)));
  source.addEventListener("log", (event) => appendLogChunk(JSON.parse(event.data)));
  source.addEventListener("deleted", () => {
    if (state.eventSource !== source) return;
    forgetDeletedJob(query.get("job_id"));
    refreshJobs().catch((error) => toast(error.message, "error")).finally(connectEventStream);
  });
  source.onerror = () => {
    source.close();
    if (state.eventSource === source) state.eventSource = null;
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = window.setTimeout(connectEventStream, 1500);
  };
}

/* ---------- 事件绑定 ---------- */

function bindUI() {
  bindExistingAnalysis();
  bindResearch();
  document.querySelectorAll("[data-workspace-view]").forEach((button) => {
    button.addEventListener("click", () => {
      showWorkspaceView(button.dataset.workspaceView);
      if (state.workspaceView === "detail") {
        state.artifactKey = "";
        renderActiveJob();
      }
    });
  });
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      showDetailTab(button.dataset.detailTab);
      renderActiveJob();
    });
  });
  els.toggleSidebar.addEventListener("click", () => {
    const open = document.body.classList.toggle("sidebar-open");
    els.toggleSidebar.setAttribute("aria-expanded", String(open));
  });
  els.saveSettings.addEventListener("click", saveSettings);
  els.applyHardwareRecommendation?.addEventListener("click", applyHardwareRecommendation);
  els.maxParallel?.addEventListener("input", updateResourceAdvice);
  els.submitJob.addEventListener("click", submitJob);
  els.cancelJob.addEventListener("click", cancelActiveJob);
  els.adoptJob.addEventListener("click", adoptActiveJob);
  els.resumeJob.addEventListener("click", resumeActiveJob);
  els.retryJob.addEventListener("click", retryActiveJob);
  els.skipJob.addEventListener("click", skipActiveJob);
  els.cloneJob?.addEventListener("click", cloneActiveJob);
  els.benchmarkJob?.addEventListener("click", benchmarkActiveJob);
  els.compareJob?.addEventListener("click", compareActiveJob);
  els.mdpPanelToggle?.addEventListener("click", () => {
    setMdpPanelExpanded(els.mdpPanelToggle.getAttribute("aria-expanded") !== "true");
  });
  els.addLigand?.addEventListener("click", () => {
    addLigandRow();
    renderWorkflowGuide();
    debouncedPreview();
  });
  els.detectLigands?.addEventListener("click", detectLigandsFromPdb);
  els.ligandList?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-ligand]");
    if (!button) return;
    removeLigandRow(Number(button.dataset.removeLigand));
    renderWorkflowGuide();
  });
  els.fillStdinOverrides?.addEventListener("click", fillStdinOverridesFromPreview);
  els.submitWarningCancel?.addEventListener("click", () => closeConfirmDialog(false));
  els.submitWarningConfirm?.addEventListener("click", () => closeConfirmDialog(true));
  els.submitWarning?.addEventListener("click", (event) => {
    if (event.target === els.submitWarning) closeConfirmDialog(false);
  });
  els.submitWarning?.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeConfirmDialog(false);
  });
  els.submitWarning?.addEventListener("close", () => {
    if (!els.submitWarning.open) closeConfirmDialog(false);
  });
  els.submitWarning?.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const controls = Array.from(els.submitWarning.querySelectorAll(
      'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((control) => control.getClientRects().length > 0);
    const first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.submitWarning?.open && document.body.classList.contains("sidebar-open")) {
      document.body.classList.remove("sidebar-open");
      els.toggleSidebar.setAttribute("aria-expanded", "false");
      els.toggleSidebar.focus();
    }
  });

  els.copyLog.addEventListener("click", async () => {
    const text = els.jobLog.textContent || "";
    if (!text) {
      toast("当前没有日志");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("日志已复制到剪贴板", "success");
    } catch {
      toast("复制失败，请检查浏览器权限", "error");
    }
  });

  els.copyJobDirectory?.addEventListener("click", async () => {
    const path = els.jobDirectory.textContent || "";
    if (!path || path === "-") return;
    try {
      await navigator.clipboard.writeText(path);
      toast("结果目录已复制", "success");
    } catch {
      toast("复制失败，请检查浏览器权限", "error");
    }
  });

  els.commandPreview.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-command");
    if (!button) return;
    const command = button.closest("li")?.querySelector("code")?.textContent || "";
    try {
      await navigator.clipboard.writeText(command);
      button.textContent = "已复制";
      window.setTimeout(() => { button.textContent = "复制"; }, 1400);
      toast("命令已复制", "success");
    } catch {
      toast("复制失败，请检查浏览器权限", "error");
    }
  });

  els.logBottom.addEventListener("click", () => {
    els.jobLog.scrollTop = els.jobLog.scrollHeight;
  });

  // 任务列表：事件委托，只绑定一次
  els.jobsList.addEventListener("click", (event) => {
    const actionButton = event.target.closest("button[data-action]");
    const item = event.target.closest(".job-item[data-job-id]");
    if (!item || !actionButton) return;
    const jobId = item.dataset.jobId;
    const action = actionButton.dataset.action;
    if (action === "select") {
      selectJob(jobId);
      return;
    }
    if (action === "cancel") {
      cancelJob(jobId);
      return;
    }
    if (action === "delete") {
      deleteJob(jobId);
    }
  });

  els.files.addEventListener("change", () => {
    syncFileFieldsFromUploads({ notify: true });
    updateText(els.fileSummary, selectedFileSummary());
    renderWorkflowGuide();
    refreshPreview();
  });

  let jobSearchTimer = null;
  els.jobFilter?.addEventListener("input", () => {
    state.jobFilter = els.jobFilter.value;
    state.jobsOffset = 0;
    state.jobs = [];
    state.jobsBusy = true;
    state.jobsSignature = "";
    renderJobs();
    disconnectEventStream();
    window.clearTimeout(jobSearchTimer);
    jobSearchTimer = window.setTimeout(() => {
      refreshJobs().catch((error) => toast(error.message, "error")).finally(connectEventStream);
    }, 250);
  });
  els.jobsPrevious?.addEventListener("click", () => changeJobsPage(-1).catch((error) => toast(error.message, "error")));
  els.jobsNext?.addEventListener("click", () => changeJobsPage(1).catch((error) => toast(error.message, "error")));

  els.fileFilter?.addEventListener("input", () => {
    state.fileFilter = els.fileFilter.value;
    applyFileFilter();
  });

  // 输入时防抖刷新预览，避免每次击键都发请求
  els.jobForm.addEventListener("input", (event) => {
    if (event.target?.name?.startsWith("mdp_") && event.target.name !== "mdp_override_enabled") {
      els.jobForm.querySelector("[name='mdp_override_enabled']").checked = true;
    }
    if (event.target?.name && FILE_FIELD_NAMES.has(event.target.name)) {
      delete event.target.dataset.autoUpload;
    }
    if (event.target?.dataset?.ligandField) {
      delete event.target.dataset.autoUpload;
    }
    if (["ntmpi", "ntomp", "pin", "gpu"].includes(event.target?.name)) {
      event.target.dataset.userEdited = "true";
    }
    updateResourceAdvice();
    if (event.target?.name === "force_field") {
      event.target.dataset.userEdited = "true";
      renderForceFieldSource();
    }
    renderWaterModelOptions();
    updateMdpPanelState();
    renderWorkflowGuide();
    debouncedPreview();
  });
  els.jobForm.addEventListener("change", () => {
    toggleSections();
    syncFileFieldsFromUploads();
    renderWaterModelOptions();
    updateMdpPanelState();
    renderWorkflowGuide();
    refreshPreview();
  });
  els.jobForm.addEventListener("change", (event) => {
    if (event.target?.dataset?.ligandField === "mode") {
      refreshRowMode(event.target.closest(".ligand-card"));
    }
  });

  // 拖放：真正把文件赋给 input
  ["dragenter", "dragover"].forEach((eventName) => {
    els.dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      els.dropzone.classList.add("dragging");
    });
  });
  els.dropzone.addEventListener("dragleave", (event) => {
    if (!els.dropzone.contains(event.relatedTarget)) {
      els.dropzone.classList.remove("dragging");
    }
  });
  els.dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    els.dropzone.classList.remove("dragging");
    const dropped = event.dataTransfer?.files;
    if (!dropped?.length) return;
    try {
      els.files.files = dropped;
      syncFileFieldsFromUploads({ notify: true });
      updateText(els.fileSummary, selectedFileSummary());
      renderWorkflowGuide();
      refreshPreview();
    } catch {
      toast("无法读取拖入的文件，请改用点击选择", "error");
    }
  });

  // 页面回到前台时立即刷新一次
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      disconnectEventStream();
    } else {
      refreshJobs().catch((error) => toast(error.message, "error")).finally(connectEventStream);
    }
  });
  window.addEventListener("active-job-change", connectEventStream);
  window.addEventListener("jobs-page-change", connectEventStream);
}

async function boot() {
  cacheElements();
  bindUI();
  bindPlotControls(renderActiveJob);
  ensureDefaultLigandRow();
  restoreDraft();
  bindDraftSaving();
  toggleSections();
  setMdpPanelExpanded(false);
  updateMdpPanelState();
  renderWorkflowGuide();
  await refreshHealth().catch((error) => toast(error.message, "error"));
  await refreshPreview();
  await refreshJobs().catch((error) => toast(error.message, "error"));
  connectEventStream();
}

boot();
