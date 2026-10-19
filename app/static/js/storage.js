import { api, confirmDialog, STATUS_LABELS, toast } from "./core.js?v=58a36b0fc94d0be8";

const CATEGORY_LABELS = {
  inputs: "输入", trajectories: "轨迹", checkpoints: "Checkpoint", topology_and_parameters: "拓扑与参数",
  logs: "日志", reports_and_other: "报告及其他",
};

function size(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  const power = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** power).toFixed(1)} ${["B", "KiB", "MiB", "GiB", "TiB"][power]}`;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function bindStorage() {
  const dialog = document.querySelector("#storage-dialog");
  const opener = document.querySelector("#open-storage");
  if (!dialog || !opener || dialog.dataset.bound) return;
  dialog.dataset.bound = "true";
  const find = (id) => dialog.querySelector(`#storage-${id}`);
  let overview = null;
  let preview = null;
  let busy = false;
  const selected = new Set();

  function message(text = "") {
    find("error").textContent = text;
    find("error").hidden = !text;
  }

  function invalidate() {
    preview = null;
    find("preview").hidden = true;
    find("execute").disabled = true;
    find("status").textContent = "";
  }

  function setBusy(value) {
    busy = value;
    find("options").disabled = value;
    for (const id of ["refresh", "select-visible", "preview-button", "search"]) find(id).disabled = value;
    find("jobs").querySelectorAll("input").forEach((input) => { input.disabled = value || input.dataset.protected === "true"; });
    find("execute").disabled = value || !preview?.jobs.length;
    document.querySelector("#close-storage").disabled = value;
  }

  function visibleJobs() {
    const filter = find("search").value.trim().toLocaleLowerCase();
    return (overview?.jobs || []).filter((job) => `${job.name} ${job.id}`.toLocaleLowerCase().includes(filter));
  }

  function renderJobs() {
    const container = find("jobs");
    container.replaceChildren();
    for (const job of visibleJobs()) {
      const label = element("label", undefined, "storage-job");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.value = job.id;
      checkbox.checked = selected.has(job.id);
      checkbox.disabled = !job.eligible || busy;
      checkbox.dataset.protected = String(!job.eligible);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked && selected.size >= 200) {
          checkbox.checked = false;
          message("一次最多选择 200 个任务，请分批清理。");
          return;
        }
        if (checkbox.checked) selected.add(job.id); else selected.delete(job.id);
        invalidate();
      });
      const details = element("span", undefined, "storage-job-details");
      details.append(element("strong", job.name), element("small", `${STATUS_LABELS[job.status] || job.status} · ${size(job.bytes)} · 常规可回收 ${size(job.reclaimable_bytes)}`));
      if (job.protected_reason) details.append(element("small", job.protected_reason));
      label.append(checkbox, details);
      container.append(label);
    }
    if (!container.childElementCount) container.append(element("p", "没有符合筛选的任务。", "storage-note"));
  }

  async function refresh() {
    setBusy(true);
    message();
    invalidate();
    find("summary").textContent = "正在统计占用…";
    try {
      overview = await api("/api/storage");
      for (const id of selected) if (!overview.jobs.some((job) => job.id === id && job.eligible)) selected.delete(id);
      const summary = find("summary");
      summary.replaceChildren(
        element("strong", `运行数据 ${size(overview.runtime.bytes)}`),
        element("span", `磁盘剩余 ${size(overview.disk.free_bytes)} / ${size(overview.disk.total_bytes)}`),
        element("span", `任务 ${size(overview.runtime.task_bytes)} · 运行目录其他文件 ${size(overview.runtime.other_bytes)}`),
        element("span", Object.entries(overview.categories).map(([key, bytes]) => `${CATEGORY_LABELS[key] || key} ${size(bytes)}`).join(" · ")),
      );
      renderJobs();
    } catch (error) {
      find("summary").textContent = "占用统计失败。";
      message(error.message);
    } finally { setBusy(false); }
  }

  function request() {
    return { job_ids: [...selected], mode: dialog.querySelector("[name='storage-mode']:checked").value, include_logs: find("logs").checked };
  }

  async function showPreview() {
    if (!selected.size) { message("请先选择要清理的任务。"); return; }
    setBusy(true);
    message();
    invalidate();
    try {
      const selection = request();
      const result = await api("/api/storage/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection) });
      preview = { ...result, selection };
      const container = find("preview");
      container.replaceChildren(element("strong", `${result.jobs.length} 个任务 · ${result.file_count} 个文件 · 预计回收 ${size(result.total_bytes)}`));
      const list = element("ul");
      for (const job of result.jobs) list.append(element("li", `${job.name}：${job.file_count} 个文件，${size(job.bytes)}`));
      for (const job of result.protected_jobs) list.append(element("li", `${job.id}：已保护，${job.reason}`));
      container.append(list);
      if (result.files.length) {
        const details = element("details");
        details.append(element("summary", `查看文件明细${result.files_truncated ? "（前 500 项）" : ""}`));
        const fileList = element("ul", undefined, "storage-file-list");
        for (const file of result.files) fileList.append(element("li", `${file.job_id}/${file.path} · ${size(file.bytes)}`));
        details.append(fileList);
        container.append(details);
      }
      container.hidden = false;
      find("status").textContent = result.jobs.length ? "预览有效期 10 分钟；清理前将再次检查任务状态和文件。" : "所选任务没有可清理的文件。";
    } catch (error) { message(error.message); }
    finally { setBusy(false); }
  }

  async function execute() {
    if (!preview?.jobs.length || busy) return;
    const saved = preview;
    const deleting = saved.selection.mode === "delete_jobs";
    setBusy(true);
    try {
      const confirmed = await confirmDialog({
        title: deleting ? "删除任务及全部结果" : "确认清理所选文件",
        subtitle: `预计回收 ${size(saved.total_bytes)}，操作完成后无法撤销`,
        items: [deleting ? "将删除所选任务的输入、checkpoint、生产轨迹、分析报告和全部其他结果。" : "将删除预览列出的中间轨迹与备份文件。",
          saved.selection.include_logs && !deleting ? "任务日志及日志备份也将被删除。" : "",
          ...saved.jobs.map((job) => `${job.name}：${size(job.bytes)}`)],
        confirmText: deleting ? "永久删除所选任务" : "确认清理", cancelText: "返回检查", tone: "danger",
      });
      if (!confirmed) return;
      const result = await api("/api/storage/cleanup", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...saved.selection, preview_token: saved.preview_token }) });
      selected.clear();
      invalidate();
      await refresh();
      find("status").textContent = `已清理 ${result.deleted_files} 个文件，释放约 ${size(result.deleted_bytes)}，删除 ${result.deleted_jobs.length} 个任务。`;
      if (result.skipped_jobs.length) message(result.skipped_jobs.map((job) => `${job.id}：${job.reason}`).join("；"));
      toast("空间清理完成");
      document.dispatchEvent(new CustomEvent("storage-cleaned", { detail: result }));
    } catch (error) { invalidate(); message(error.message); }
    finally { setBusy(false); }
  }

  opener.addEventListener("click", () => { dialog.showModal(); refresh(); });
  document.querySelector("#close-storage").addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  find("refresh").addEventListener("click", refresh);
  find("search").addEventListener("input", renderJobs);
  find("options").addEventListener("change", () => {
    find("logs").disabled = dialog.querySelector("[name='storage-mode']:checked").value === "delete_jobs";
    invalidate();
  });
  find("select-visible").addEventListener("click", () => {
    for (const job of visibleJobs()) if (job.eligible && selected.size < 200) selected.add(job.id);
    renderJobs();
    invalidate();
  });
  find("preview-button").addEventListener("click", showPreview);
  find("execute").addEventListener("click", execute);
}
