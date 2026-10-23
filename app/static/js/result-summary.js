import { state, api, toast, escapeHtml } from "./core.js?v=621bb939e2b97a80";
import { applyJobDetail } from "./jobs.js?v=621bb939e2b97a80";
import { product, loadProduct } from "./library.js?v=621bb939e2b97a80";

const DRAFT_PREFIX = "gromacs-organization-draft-v1:";
const drafts = new Map();
const loadedDrafts = new Set();
let organizationId = null;
let currentOrganization = null;
let savingJobId = null;
let summaryKey = "";
let summaryGeneration = 0;

function organizationValues(job) {
  return { project: job.project || "", tags: (job.tags || []).join(", "), notes: job.notes || "", pinned: Boolean(job.pinned) };
}

function readOrganizationForm() {
  return { project: document.querySelector("#job-project").value, tags: document.querySelector("#job-tags").value,
    notes: document.querySelector("#job-notes").value, pinned: document.querySelector("#job-pinned").checked };
}

function organizationPayload(values) {
  return { project: values.project.trim(), tags: [...new Set(values.tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean))],
    notes: values.notes.trim(), pinned: values.pinned };
}

function sameOrganization(left, right) {
  return JSON.stringify(organizationPayload(left)) === JSON.stringify(organizationPayload(right));
}

function validDraftValues(values) {
  return values && typeof values.project === "string" && typeof values.tags === "string"
    && typeof values.notes === "string" && typeof values.pinned === "boolean";
}

function loadDraft(jobId) {
  if (loadedDrafts.has(jobId)) return;
  loadedDrafts.add(jobId);
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_PREFIX + jobId) || "null");
    if (draft?.version === 1 && draft.jobId === jobId && validDraftValues(draft.values) && validDraftValues(draft.base)) {
      drafts.set(jobId, { ...draft, persisted: true });
    }
  } catch { /* Unavailable storage or an old, malformed draft must not block the form. */ }
}

function saveDraft(jobId, values, base) {
  const draft = { version: 1, jobId, values, base, persisted: false };
  drafts.set(jobId, draft);
  try {
    localStorage.setItem(DRAFT_PREFIX + jobId, JSON.stringify(draft));
    draft.persisted = true;
  } catch { /* Keep the in-memory draft and make the storage failure visible. */ }
}

function discardDraft(jobId) {
  drafts.delete(jobId);
  try { localStorage.removeItem(DRAFT_PREFIX + jobId); } catch { /* A matching saved draft is discarded when next restored. */ }
}

function renderDraftStatus() {
  const draft = drafts.get(organizationId);
  const status = document.querySelector("#organization-draft-status");
  if (!status) return;
  status.textContent = draft
    ? `未保存 · ${draft.persisted ? "本机草稿已保留，可切换任务或刷新后继续编辑" : "本机存储不可用，草稿仅在当前页面保留，请及时保存"}${currentOrganization && !sameOrganization(draft.base, currentOrganization) ? "；服务器记录已有更新，请核对后保存" : ""}`
    : savingJobId === organizationId && organizationId ? "正在保存实验记录…" : "";
  status.hidden = !status.textContent;
  const summary = document.querySelector("#organization-summary");
  if (currentOrganization) summary.textContent = [currentOrganization.pinned ? "已置顶" : "", currentOrganization.project || "未归入项目",
    ...organizationPayload(currentOrganization).tags, draft ? "未保存" : "编辑实验记录"].filter(Boolean).join(" · ");
}

export function bindResultSummary() {
  const form = document.querySelector("#organization-form");
  const status = document.createElement("p"); status.id = "organization-draft-status"; status.hidden = true;
  status.className = "wide-field scientific-note"; status.setAttribute("role", "status"); form.append(status);
  form.addEventListener("input", () => {
    if (!organizationId || organizationId !== state.activeJobId || !currentOrganization) return;
    const values = readOrganizationForm();
    if (savingJobId !== organizationId && sameOrganization(values, currentOrganization)) discardDraft(organizationId);
    else saveDraft(organizationId, values, drafts.get(organizationId)?.base || currentOrganization);
    renderDraftStatus();
  });
  window.addEventListener("active-job-change", () => {
    if (organizationId === state.activeJobId) return;
    for (const field of form.elements) field.disabled = true;
  });
  window.addEventListener("beforeunload", event => {
    if (!drafts.size) return;
    event.preventDefault(); event.returnValue = "";
  });
  window.addEventListener("job-deleted", event => {
    discardDraft(event.detail);
    loadedDrafts.delete(event.detail);
    renderDraftStatus();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const jobId = state.activeJobId; if (!jobId || jobId !== organizationId || savingJobId) return;
    const submittedDraft = drafts.get(jobId);
    const values = readOrganizationForm();
    const button = form.querySelector('button[type="submit"]'); button.disabled = true;
    savingJobId = jobId; renderDraftStatus();
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}/organization`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(organizationPayload(values)) });
      const latest = drafts.get(jobId);
      if (latest === submittedDraft || latest && sameOrganization(latest.values, organizationValues(job))) discardDraft(jobId);
      else if (latest) saveDraft(jobId, latest.values, organizationValues(job));
      if (state.activeJobId === jobId) applyJobDetail(job);
      await loadProduct();
      toast(drafts.has(jobId) ? "已保存提交时的记录；后续修改尚未保存。" : "实验记录已保存", "success");
    } catch (error) { toast(error.message, "error"); }
    finally { savingJobId = null; button.disabled = !state.activeJobId; renderDraftStatus(); }
  });
  window.addEventListener("job-rendered", event => { renderResultSummary(event.detail); });
}

async function renderResultSummary(job) {
  const form = document.querySelector("#organization-form");
  if (!job) {
    organizationId = null; currentOrganization = null; summaryKey = ""; summaryGeneration += 1;
    renderDraftStatus();
    form.reset();
    for (const field of form.elements) field.disabled = true;
    document.querySelector("#organization-summary").textContent = "选择任务后编辑实验记录";
    const download = document.querySelector("#download-result-bundle"); download.hidden = true; download.removeAttribute("href");
    document.querySelector("#result-summary-content").textContent = "选择任务后查看。";
    return;
  }
  if (job.id !== state.activeJobId) return;
  if (!product.available) {
    document.querySelector("#download-result-bundle").hidden = true;
    document.querySelector("#result-summary-content").textContent = "当前服务版本尚未启用结果摘要。现有日志、文件与分析仍可查看。";
    return;
  }
  for (const field of form.elements) field.disabled = field.type === "submit" && Boolean(savingJobId);
  organizationId = job.id;
  currentOrganization = organizationValues(job);
  loadDraft(job.id);
  let draft = drafts.get(job.id);
  if (draft && savingJobId !== job.id && sameOrganization(draft.values, currentOrganization)) {
    discardDraft(job.id); draft = null;
  }
  const values = draft?.values || currentOrganization;
  document.querySelector("#job-project").value = values.project;
  document.querySelector("#job-tags").value = values.tags;
  document.querySelector("#job-notes").value = values.notes;
  document.querySelector("#job-pinned").checked = values.pinned;
  renderDraftStatus();
  const canExport = ["completed", "failed", "cancelled", "interrupted"].includes(job.status);
  const download = document.querySelector("#download-result-bundle");
  download.hidden = !canExport;
  download.href = `/api/jobs/${encodeURIComponent(job.id)}/result-bundle`;
  const target = document.querySelector("#result-summary-content");
  const key = `${job.id}:${job.status}:${job.updated_at || ""}`;
  if (key === summaryKey) return;
  summaryKey = key;
  const generation = ++summaryGeneration;
  if (!canExport) {
    target.textContent = job.status === "running" ? "计算进行中。可查看模拟时间与原生日志更新；结果生成后可继续分析和导出。" : "任务准备或排队中。开始运行后会显示进度。";
    return;
  }
  target.textContent = "正在读取结果摘要…";
  try {
    const summary = await api(`/api/jobs/${encodeURIComponent(job.id)}/summary`);
    if (generation !== summaryGeneration || state.activeJobId !== job.id) return;
    const label = job.dry_run ? "命令检查已结束，未执行模拟" : job.is_example ? "教学示例结果" : job.status === "completed" ? "计算已完成，请继续检查结果" : "任务已停止，可检查已保存的结果";
    const checks = summary.quality.map(item => `<li><strong>${escapeHtml(item.file)}</strong><span>${escapeHtml(item.status)}</span>${item.notes?.length ? `<p>${escapeHtml(item.notes.join("；"))}</p>` : ""}</li>`).join("");
    target.innerHTML = `<strong>${escapeHtml(label)}</strong><p>${summary.input_count} 个输入文件 · ${summary.manifest ? "已记录实验清单与输入哈希" : "此任务未记录实验清单"}</p><ul class="quality-summary-list">${checks || "<li>尚无可读取的质量检查报告。可使用“继续分析”或“研究分析”生成相应结果。</li>"}</ul><p class="scientific-note">程序执行完成、质量检查结果与科学结论分别判断；采样充分性仍需结合研究问题复核。</p>`;
    const sourceId = summary.analysis_source?.job_id || summary.cloned_from;
    if (sourceId) {
      const button = document.createElement("button"); button.type = "button"; button.className = "tool-button";
      button.textContent = summary.analysis_source ? "查看分析来源任务" : "查看复制来源任务";
      button.addEventListener("click", () => window.dispatchEvent(new CustomEvent("open-source-job", { detail: sourceId })));
      target.append(button);
    }
  } catch (error) {
    if (generation === summaryGeneration && state.activeJobId === job.id) { target.textContent = `摘要暂时无法读取：${error.message}`; summaryKey = ""; }
  }
}
