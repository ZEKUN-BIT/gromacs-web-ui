// 入口模块：事件绑定、轮询、启动。

import {
  FILE_FIELD_NAMES,
  els,
  state,
  cacheElements,
  updateText,
  toast,
  closeConfirmDialog,
  showWorkspaceView,
  showDetailTab,
  setSidebarOpen,
} from "./core.js?v=6cd8594e2b063155";
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
} from "./form.js?v=6cd8594e2b063155";
import {
  submitJob,
  bindSubmissionRecovery,
  selectJob,
  renderJobs,
  refreshJobs,
  applyFileFilter,
  renderActiveJob,
  changeJobsPage,
  forgetDeletedJob,
} from "./jobs.js?v=6cd8594e2b063155";
import {
  cancelActiveJob,
  adoptActiveJob,
  resumeActiveJob,
  retryActiveJob,
  skipActiveJob,
  cancelJob,
  deleteJob,
  cloneActiveJob,
  benchmarkActiveJob,
  compareActiveJob,
} from "./job-actions.js?v=6cd8594e2b063155";
import { bindDraftSaving, restoreDraft } from "./drafts.js?v=6cd8594e2b063155";
import { bindPlotControls } from "./plots.js?v=6cd8594e2b063155";
import { bindExistingAnalysis } from "./existing-analysis.js?v=6cd8594e2b063155";
import { bindResearch } from "./research.js?v=6cd8594e2b063155";
import { bindStorage } from "./storage.js?v=6cd8594e2b063155";
import { bindAgent } from "./agent.js?v=6cd8594e2b063155";
import { prepareWorkspace, bindWorkspace, refreshComposerSummary } from "./workspace.js?v=6cd8594e2b063155";
import { bindResultSummary } from "./result-summary.js?v=6cd8594e2b063155";

import { connectEventStream, disconnectEventStream, bindConnectionStatus } from "./event-stream.js?v=6cd8594e2b063155";
import { bindNavigation } from "./navigation.js?v=6cd8594e2b063155";

/* ---------- 事件绑定 ---------- */

function bindUI() {
  bindSubmissionRecovery();
  bindConnectionStatus();
  bindStorage();
  bindAgent();
  document.addEventListener("storage-cleaned", async (event) => {
    for (const jobId of event.detail.deleted_jobs) forgetDeletedJob(jobId);
    state.artifactKey = "";
    state.filesSignature = "";
    await refreshJobs().catch((error) => toast(error.message, "error"));
    await renderActiveJob().catch((error) => toast(error.message, "error"));
    connectEventStream();
  });
  bindExistingAnalysis();
  bindResearch();
  document.querySelectorAll("[data-workspace-view]").forEach((button) => {
    button.addEventListener("click", () => {
      showWorkspaceView(button.dataset.workspaceView);
      if (button.hasAttribute("data-manual-compose")) {
        document.querySelector("#manual-compose-title").scrollIntoView({ block: "start" });
      }
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
    setSidebarOpen(!document.body.classList.contains("sidebar-open"), true);
  });
  els.sidebarBackdrop?.addEventListener("click", () => setSidebarOpen(false, true));
  window.matchMedia("(max-width: 1180px)").addEventListener("change", (event) => {
    if (!event.matches) setSidebarOpen(false);
  });
  els.saveSettings.addEventListener("click", saveSettings);
  for (const field of [els.gmxBin, els.maxParallel, document.querySelector("#analysis-parallel")]) {
    field?.addEventListener("input", () => { field.dataset.userEdited = "true"; });
  }
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
    if (event.key === "Escape" && !document.querySelector("dialog[open]") && document.body.classList.contains("sidebar-open")) {
      setSidebarOpen(false, true);
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
      toast(state.logTailOmittedBytes || state.logTrimmedChars ? "当前显示的末尾日志已复制；可下载已保留日志，服务端轮转可能省略更早内容。" : "日志已复制到剪贴板", "success");
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
    if (event.target?.name) event.target.dataset.userEdited = "true";
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
  els.jobForm.addEventListener("change", (event) => {
    if (event.target?.name) event.target.dataset.userEdited = "true";
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
  showWorkspaceView("compose", false);
  const workspaceReady = prepareWorkspace();
  bindUI();
  bindWorkspace();
  bindResultSummary();
  const navigationReady = bindNavigation();
  window.addEventListener("open-source-job", event => selectJob(event.detail));
  bindPlotControls(renderActiveJob);
  ensureDefaultLigandRow();
  restoreDraft();
  bindDraftSaving();
  toggleSections();
  setMdpPanelExpanded(false);
  updateMdpPanelState();
  renderWorkflowGuide();
  refreshComposerSummary();
  connectEventStream();
  void refreshJobs().catch((error) => toast(error.message, "error")).finally(() => {
    if (state.jobsTotal > 0) document.querySelector("#start-guide").open = false;
    connectEventStream();
    navigationReady();
  });
  void refreshPreview();
  // Revisit a selected task once capability discovery completes, even if an
  // idle task has no further SSE updates to trigger its result summary.
  void workspaceReady.then(() => renderActiveJob());
  // Diagnostics and product defaults load independently of history and SSE.
  void Promise.allSettled([workspaceReady, refreshHealth().catch((error) => toast(error.message, "error"))]).then(() => {
    toggleSections();
    renderWorkflowGuide();
    refreshComposerSummary();
    return refreshPreview();
  });
  window.setInterval(() => {
    if (!document.hidden && state.workspaceView === "detail" && ["files", "plots", "quality"].includes(state.detailTab)) {
      void renderActiveJob();
    }
  }, 5000);
}

boot();
