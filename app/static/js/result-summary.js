import { state, api, toast, escapeHtml } from "./core.js?v=58a36b0fc94d0be8";
import { applyJobDetail } from "./jobs.js?v=58a36b0fc94d0be8";
import { product, loadProduct } from "./library.js?v=58a36b0fc94d0be8";

let organizationId = null;
let dirty = false;
let editRevision = 0;
let saving = false;
let summaryKey = "";
let summaryGeneration = 0;

export function bindResultSummary() {
  const form = document.querySelector("#organization-form");
  form.addEventListener("input", () => { dirty = true; editRevision += 1; });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const jobId = state.activeJobId; if (!jobId || saving) return;
    const revision = editRevision;
    const button = form.querySelector('button[type="submit"]'); button.disabled = true;
    saving = true;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}/organization`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        project: document.querySelector("#job-project").value,
        tags: document.querySelector("#job-tags").value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
        notes: document.querySelector("#job-notes").value,
        pinned: document.querySelector("#job-pinned").checked,
      }) });
      if (state.activeJobId === jobId) {
        if (revision === editRevision) dirty = false;
        applyJobDetail(job);
      }
      await loadProduct(); toast(dirty && state.activeJobId === jobId ? "已保存提交时的记录；后续修改尚未保存。" : "实验记录已保存", "success");
    } catch (error) { toast(error.message, "error"); }
    finally { saving = false; button.disabled = !state.activeJobId; }
  });
  window.addEventListener("job-rendered", event => { renderResultSummary(event.detail); });
}

async function renderResultSummary(job) {
  const form = document.querySelector("#organization-form");
  if (!job) {
    organizationId = null; dirty = false; editRevision += 1; summaryKey = ""; summaryGeneration += 1;
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
  for (const field of form.elements) field.disabled = field.type === "submit" && saving;
  if (organizationId !== job.id) { dirty = false; organizationId = job.id; editRevision += 1; }
  if (!dirty) {
    document.querySelector("#job-project").value = job.project || "";
    document.querySelector("#job-tags").value = (job.tags || []).join(", ");
    document.querySelector("#job-notes").value = job.notes || "";
    document.querySelector("#job-pinned").checked = Boolean(job.pinned);
  }
  document.querySelector("#organization-summary").textContent = [job.pinned ? "已置顶" : "", job.project || "未归入项目", ...(job.tags || [])].filter(Boolean).join(" · ") + " · 编辑实验记录";
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
