// GROMACS 控制台前端逻辑 - 核心模块
// 常量、全局状态、DOM 缓存、通用工具、toast / confirm 对话框、API 封装。

export const STATUS_LABELS = {
  preparing: "准备输入中",
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export const WORKFLOW_LABELS = {
  protein_md: "蛋白 MD",
  protein_ligand_md: "复合物 MD",
  em_only: "能量最小化",
  run_tpr: "运行 TPR",
  analysis_rmsd: "RMSD 分析",
  postprocess: "轨迹后处理",
  analysis_suite: "分析套件",
  benchmark: "性能基准",
  custom: "自定义",
};

export const ACTIVE_STATES = new Set(["preparing", "queued", "running", "interrupted"]);
export const MAX_LOG_CHARS = 300_000;

export const FILE_FIELD_NAMES = new Set([
  "structure_file",
  "protein_file",
  "ligand_gro_file",
  "ligand_itp_file",
  "ligand_structure_file",
  "ligand_chemistry_file",
  "tpr_file",
  "trajectory_file",
  "index_file",
  "edr_file",
]);

export const state = {
  jobs: [],
  activeJobId: null,
  activeJob: null,
  eventSource: null,
  reconnectTimer: null,
  jobsSignature: "",
  previewSignature: "",
  previewCommands: [],
  mdpSignature: "",
  logKey: "",
  logSignature: "",
  logText: "",
  logOffset: 0,
  artifactKey: "",
  filesSignature: "",
  plotsSignature: "",
  fileFilter: "",
  jobFilter: "",
  jobsOffset: 0,
  jobsTotal: 0,
  jobsHasMore: false,
  jobsBusy: false,
  changedJobIds: new Set(),
  localForceFields: [],
  performanceRecommendation: null,
  workspaceView: "compose",
  detailTab: "overview",
  viewChosen: false,
  plotFilters: { metric: "", replica: "", begin: "", end: "", mode: "separate" },
};

export const $ = (selector) => document.querySelector(selector);
export const els = {};

export function cacheElements() {
  Object.assign(els, {
    toast: $("#toast"),
    composeView: $("#compose-view"),
    detailView: $("#detail-view"),
    toggleSidebar: $("#toggle-sidebar"),
    jobError: $("#job-error"),
    skippedStepsNotice: $("#skipped-steps-notice"),
    qualityReports: $("#quality-reports"),
    protocolSummary: $("#protocol-summary"),
    draftStatus: $("#draft-status"),
    clearDraft: $("#clear-draft"),
    plotControls: $("#plot-controls"),
    plotMetric: $("#plot-metric"),
    plotReplica: $("#plot-replica"),
    plotBegin: $("#plot-begin"),
    plotEnd: $("#plot-end"),
    plotFilterError: $("#plot-filter-error"),
    plotWindowNotice: $("#plot-window-notice"),
    gmxBin: $("#gmx-bin"),
    maxParallel: $("#max-parallel"),
    resourceAdvice: $("#resource-advice"),
    applyHardwareRecommendation: $("#apply-hardware-recommendation"),
    runtimePath: $("#runtime-path"),
    healthCard: $("#health-card"),
    environmentDiagnostics: $("#environment-diagnostics"),
    forceFieldOptions: $("#force-field-options"),
    forceFieldSource: $("#force-field-source"),
    waterModel: $("select[name='water_model']"),
    jobsList: $("#jobs-list"),
    jobFilter: $("#job-filter"),
    jobsPrevious: $("#jobs-previous"),
    jobsNext: $("#jobs-next"),
    jobsPageStatus: $("#jobs-page-status"),
    jobForm: $("#job-form"),
    files: $("#files"),
    ligandList: $("#ligand-list"),
    addLigand: $("#add-ligand"),
    detectLigands: $("#detect-ligands"),
    ligandReferenceFile: $("#ligand-reference-file"),
    ligandReferenceSummary: $("#ligand-reference-summary"),
    fileSummary: $("#file-summary"),
    dropzone: $("#dropzone"),
    submitJob: $("#submit-job"),
    saveSettings: $("#save-settings"),
    previewCount: $("#preview-count"),
    commandPreview: $("#command-preview"),
    mdpPreview: $("#mdp-preview"),
    mdpPanelToggle: $("#mdp-panel-toggle"),
    mdpPanelBody: $("#mdp-panel-body"),
    mdpPanelState: $("#mdp-panel-state"),
    customStdin: $("#custom-stdin"),
    stdinOverrides: $("#stdin-overrides"),
    fillStdinOverrides: $("#fill-stdin-overrides"),
    workflowGuide: $("#workflow-guide"),
    guideTitle: $("#guide-title"),
    guideSteps: $("#guide-steps"),
    guideReady: $("#guide-ready"),
    guideBarFill: $("#guide-bar-fill"),
    guideHint: $("#guide-hint"),
    guideRequirements: $("#guide-requirements"),
    guideNoteTitle: $("#guide-note-title"),
    guideNotes: $("#guide-notes"),
    activeJobTitle: $("#active-job-title"),
    copyJobDirectory: $("#copy-job-directory"),
    cloneJob: $("#clone-job"),
    benchmarkJob: $("#benchmark-job"),
    compareJob: $("#compare-job"),
    compareJobSelect: $("#compare-job-select"),
    jobDirectory: $("#job-directory"),
    cancelJob: $("#cancel-job"),
    adoptJob: $("#adopt-job"),
    resumeJob: $("#resume-job"),
    retryJob: $("#retry-job"),
    skipJob: $("#skip-job"),
    copyLog: $("#copy-log"),
    logBottom: $("#log-bottom"),
    jobStatus: $("#job-status"),
    jobStep: $("#job-step"),
    jobExit: $("#job-exit"),
    jobPercent: $("#job-percent"),
    jobDuration: $("#job-duration"),
    jobPeakMemory: $("#job-peak-memory"),
    jobPerformance: $("#job-performance"),
    jobEta: $("#job-eta"),
    progress: $("#job-progress"),
    progressFill: $("#progress-fill"),
    logNotice: $("#log-notice"),
    jobLog: $("#job-log"),
    outputFiles: $("#output-files"),
    analysisPlots: $("#analysis-plots"),
    downloadPlots: $("#download-plots"),
    plotCount: $("#plot-count"),
    fileCount: $("#file-count"),
    fileFilter: $("#file-filter"),
    terminalTitle: $("#terminal-title"),
    submitWarning: $("#submit-warning"),
    submitWarningDialog: $("#submit-warning .run-dialog"),
    submitWarningMark: $("#submit-warning-mark"),
    submitWarningTitle: $("#submit-warning-title"),
    submitWarningSubtitle: $("#submit-warning-subtitle"),
    submitWarningList: $("#submit-warning-list"),
    submitWarningConfirm: $("#submit-warning-confirm"),
    submitWarningCancel: $("#submit-warning-cancel"),
  });
}

export function showWorkspaceView(view, chosen = true) {
  state.workspaceView = view === "detail" ? "detail" : "compose";
  if (chosen) state.viewChosen = true;
  els.composeView.hidden = state.workspaceView !== "compose";
  els.detailView.hidden = state.workspaceView !== "detail";
  document.querySelectorAll("[data-workspace-view]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.workspaceView === state.workspaceView));
  });
  document.body.classList.remove("sidebar-open");
  els.toggleSidebar.setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "instant" });
}

export function showDetailTab(tab) {
  state.detailTab = tab;
  document.querySelectorAll("[data-detail-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.detailPanel !== tab;
  });
  document.querySelectorAll("[data-detail-tab]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.detailTab === tab));
  });
  state.artifactKey = "";
}

export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function updateText(el, value) {
  const text = String(value);
  if (el.textContent !== text) el.textContent = text;
}

export function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
}

export function toast(message, type = "info") {
  els.toast.textContent = message;
  els.toast.dataset.type = type;
  els.toast.classList.add("visible");
  window.clearTimeout(els.toast._timer);
  els.toast._timer = window.setTimeout(() => els.toast.classList.remove("visible"), 3200);
}

export async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      detail = payload.detail || text || detail;
    } catch {
      detail = text || detail;
    }
    const error = new Error(formatApiError(detail));
    error.status = response.status;
    throw error;
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.text();
}

export function formatApiError(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const location = Array.isArray(item?.loc) ? item.loc.filter((part) => !["body", "query", "path"].includes(String(part))).join(".") : "";
        const message = item?.msg || item?.message || JSON.stringify(item);
        return location ? `${location}: ${message}` : message;
      })
      .join("；");
  }
  if (detail && typeof detail === "object") {
    return detail.msg || detail.message || JSON.stringify(detail);
  }
  return String(detail || "参数校验失败");
}

let confirmDialogResolver = null;
let confirmDialogFocus = null;

export function closeConfirmDialog(confirmed) {
  if (!els.submitWarning) return;
  const resolve = confirmDialogResolver;
  const focus = confirmDialogFocus;
  confirmDialogResolver = null;
  confirmDialogFocus = null;
  if (els.submitWarning.open) els.submitWarning.close();
  els.submitWarning.hidden = true;
  if (focus?.isConnected) focus.focus();
  resolve?.(confirmed);
}

export function confirmDialog({ title, subtitle, items, confirmText = "确认", cancelText = "取消", mark = "!", tone = "default" }) {
  // A second caller must not replace an unresolved confirmation.
  if (confirmDialogResolver) return Promise.resolve(false);
  const warnings = items.filter(Boolean);
  if (!warnings.length) return Promise.resolve(true);
  if (!els.submitWarning?.showModal || !els.submitWarningList) {
    return Promise.resolve(window.confirm(warnings.join("\n\n")));
  }
  if (els.submitWarningDialog) els.submitWarningDialog.dataset.tone = tone;
  updateText(els.submitWarningTitle, title);
  updateText(els.submitWarningSubtitle, subtitle);
  updateText(els.submitWarningMark, mark);
  updateText(els.submitWarningConfirm, confirmText);
  updateText(els.submitWarningCancel, cancelText);
  els.submitWarningList.innerHTML = warnings.map((warning) => `<li>${escapeHtml(warning)}</li>`).join("");
  confirmDialogFocus = document.activeElement;
  els.submitWarning.hidden = false;
  els.submitWarning.showModal();
  els.submitWarningCancel?.focus();
  return new Promise((resolve) => {
    confirmDialogResolver = resolve;
  });
}

export function confirmSubmitWarnings(warnings) {
  return confirmDialog({
    title: "提交前确认",
    subtitle: "文件处理记录会写入日志和报告",
    items: warnings,
    confirmText: "确认提交",
    cancelText: "返回修改",
    mark: "!",
    tone: "default",
  });
}

export function replaySwapAnimation(selector) {
  document.querySelectorAll(selector).forEach((el) => {
    el.classList.remove("swap-in");
    void el.offsetWidth;
    el.classList.add("swap-in");
  });
}
