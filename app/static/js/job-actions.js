// 任务操作模块：取消、接管、续跑、重试、跳过、删除、复制、性能基准与参数比较。

import { resetLogState } from "./log-viewer.js?v=5b84b3e1eaca6ab9";
import { ACTIVE_STATES, els, state, toast, api, confirmDialog } from "./core.js?v=5b84b3e1eaca6ab9";
import { friendlyJobActionError } from "./form.js?v=5b84b3e1eaca6ab9";
import { refreshJobs, renderActiveJob } from "./jobs.js?v=5b84b3e1eaca6ab9";

function jobById(jobId) {
  return state.jobs.find((item) => item.id === jobId);
}

// 确认后 POST 一个任务操作，成功后刷新列表；失败时提示原因。
async function confirmJobAction(jobId, dialog, action, successMessage) {
  const confirmed = await confirmDialog(dialog);
  if (!confirmed) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
    toast(successMessage, "success");
    state.jobsSignature = "";
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
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
  const job = jobById(jobId);
  await confirmJobAction(jobId, {
    title: "重新接管后台任务？",
    subtitle: "只接管仍在当前任务目录运行的 mdrun",
    items: [
      `任务：${job?.name || jobId}`,
      "系统会核对进程工作目录和命令；该进程完成后自动继续剩余分析与后处理。",
    ],
    confirmText: "确认接管",
    cancelText: "暂不接管",
    mark: "↻",
  }, "adopt", "已重新接管后台任务");
}

export async function resumeActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = jobById(jobId);
  await confirmJobAction(jobId, {
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
  }, "resume-checkpoint", "检查点续跑已加入队列");
}

export async function retryActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = jobById(jobId);
  await confirmJobAction(jobId, {
    title: "重试失败步骤？",
    subtitle: "从失败的那一步重新执行，已完成的步骤不会重跑",
    items: [`任务：${job?.name || jobId}`, `失败步骤：${job?.current_step || job?.error || "未知"}`],
    confirmText: "重试",
    cancelText: "取消",
    mark: "↻",
    tone: "warn",
  }, "retry-step", "已重新入队，从失败步骤重试");
}

export async function skipActiveJob() {
  const jobId = state.activeJobId;
  if (!jobId) return;
  const job = jobById(jobId);
  await confirmJobAction(jobId, {
    title: "跳过失败步骤？",
    subtitle: "跳过当前失败的步骤，直接从下一步继续（可能产生不完整的结果）",
    items: [`任务：${job?.name || jobId}`, `失败步骤：${job?.current_step || job?.error || "未知"}`],
    confirmText: "跳过",
    cancelText: "取消",
    mark: "⏭",
    tone: "warn",
  }, "skip-step", "已跳过失败步骤并入队");
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
    window.dispatchEvent(new CustomEvent("job-deleted", { detail: jobId }));
    if (state.activeJobId === jobId) {
      state.activeJobId = null;
      state.activeJob = null;
      resetLogState();
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

let clonePending = false;
export async function cloneActiveJob() {
  const source = state.activeJobId, button = els.cloneJob;
  if (!source || clonePending) return;
  clonePending = true;
  try {
    const confirmed = await confirmDialog({ title: "复制并运行任务？", subtitle: "沿用原始输入和参数，创建一个新任务",
      items: [`来源：${state.activeJob?.name || jobById(source)?.name || source}`, "沿用源任务的执行模式；非试运行任务将加入运行队列。"], confirmText: "复制并运行" });
    if (!confirmed || source !== state.activeJobId) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在复制…";
    const cloned = await api(`/api/jobs/${encodeURIComponent(source)}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    if (state.activeJobId === source) {
      state.activeJobId = cloned.id;
      state.activeJob = cloned;
      resetLogState();
      window.dispatchEvent(new CustomEvent("active-job-change"));
    }
    state.jobsSignature = "";
    toast(`已复制任务：${cloned.name}`, "success");
    await refreshJobs();
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
  } finally {
    clonePending = false;
    button.removeAttribute("aria-busy");
    button.textContent = "复制并运行";
    button.disabled = !state.activeJobId;
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
    resetLogState();
    state.jobsSignature = "";
    toast(`性能基准已入队：${benchmark.name}`, "success");
    await refreshJobs();
    window.dispatchEvent(new CustomEvent("active-job-change"));
  } catch (error) {
    toast(friendlyJobActionError(error.message), "error");
    els.benchmarkJob.disabled = false;
  }
}

export function bindJobComparison() {
  const dialog = document.querySelector("#job-diff-dialog");
  const part = name => dialog.querySelector(`#job-diff-${name}`);
  const search = part("search"), rows = part("rows"), clear = part("clear");
  const targetSearch = part("target-search"), targets = els.compareJobSelect, more = part("more");
  let leftJob, pair, request, listing, timer, nextOffset, differences = [];

  function showState(status, message = "", action = "") {
    part("status").textContent = status;
    part("message").textContent = message;
    part("empty").hidden = !message;
    part("reset").hidden = action !== "reset";
    part("retry").hidden = action !== "retry";
  }

  function setHeaders(left, right) {
    const name = job => left.name && left.name === right.name ? `${job.name} · ${job.id}` : job.name || job.id;
    part("left").textContent = name(left);
    part("right").textContent = name(right);
  }

  function filterRows() {
    const query = search.value.trim().toLowerCase();
    search.disabled = !differences.length;
    clear.hidden = !search.value;
    let count = 0;
    for (const { row, terms } of differences) {
      row.hidden = !!query && !terms.some(term => term.includes(query));
      if (!row.hidden) count++;
    }
    rows.closest(".diff-scroll").scrollTop = 0;
    const total = differences.length;
    showState(query ? `匹配 ${count} / ${total} 项` : `共 ${total} 项差异`,
      !total ? "两个任务没有参数差异。" : !count ? "没有匹配的差异项，试试其他参数名或值。" : "", count || !total ? "" : "reset");
  }

  function makeRow({ path, left, right }) {
    const row = document.createElement("tr");
    const heading = document.createElement("th");
    heading.scope = "row";
    heading.textContent = path;
    row.append(heading);
    const terms = [path.toLowerCase()];
    for (const value of [left, right]) {
      const cell = row.insertCell();
      cell.textContent = value === null ? "未设置" : JSON.stringify(value);
      if (value === null) {
        cell.className = "diff-unset";
        cell.title = "该侧没有此参数，或值为 null";
        terms.push("null");
      }
      terms.push(cell.textContent.toLowerCase());
    }
    return { row, terms };
  }

  async function load() {
    request?.abort();
    const controller = request = new AbortController();
    const [left, right] = pair;
    differences = [];
    rows.replaceChildren();
    search.value = "";
    search.disabled = clear.hidden = true;
    showState("正在比较…", "正在读取两个任务的参数。");
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(left)}/diff/${encodeURIComponent(right)}`, { signal: controller.signal });
      if (controller !== request || !dialog.open) return;
      setHeaders(result.left, result.right);
      differences = result.differences.map(makeRow);
      const fragment = document.createDocumentFragment();
      for (const { row } of differences) fragment.append(row);
      rows.append(fragment);
      filterRows();
    } catch (error) {
      if (controller === request && dialog.open) showState("比较失败", friendlyJobActionError(error.message), "retry");
    }
  }

  function compareSelected() {
    const option = targets.selectedOptions[0];
    if (!option?.value) return;
    pair = [leftJob.id, option.value];
    setHeaders(leftJob, { id: option.value, name: option.text });
    void load();
  }

  async function listTargets() {
    const controller = listing = new AbortController();
    const params = new URLSearchParams({ search: targetSearch.value.trim(), limit: "50", offset: String(nextOffset) });
    more.disabled = true;
    try {
      const page = await api(`/api/jobs?${params}`, { signal: controller.signal });
      if (controller !== listing || !dialog.open) return;
      const seen = new Set(Array.from(targets.options, option => option.value));
      for (const job of page.jobs) {
        if (job.id !== leftJob.id && !seen.has(job.id)) targets.add(new Option(job.name || job.id, job.id));
      }
      nextOffset += page.jobs.length;
      more.hidden = !page.has_more || !page.jobs.length;
      more.textContent = "更多任务";
      targets.disabled = !targets.length;
      if (!pair && targets.length) compareSelected();
      else if (!targets.length) showState("无对照任务", targetSearch.value.trim() ? "没有匹配的其他任务，请更换搜索词。" : "需要至少两个任务才能比较参数。");
    } catch (error) {
      if (controller !== listing || !dialog.open) return;
      more.hidden = false;
      more.textContent = "重试读取任务";
      if (!pair) showState("任务列表读取失败", friendlyJobActionError(error.message));
    } finally {
      if (controller === listing) more.disabled = false;
    }
  }

  function resetTargets(delay = 0) {
    clearTimeout(timer);
    listing?.abort(); request?.abort();
    listing = request = pair = null;
    nextOffset = 0;
    targets.replaceChildren(); rows.replaceChildren(); differences = [];
    search.value = "";
    targets.disabled = more.hidden = search.disabled = clear.hidden = true;
    setHeaders(leftJob, { id: "—" });
    showState("正在查找对照任务…", "正在读取历史任务。");
    timer = setTimeout(listTargets, delay);
  }
  more.addEventListener("click", listTargets);
  targets.addEventListener("change", compareSelected);
  targetSearch.addEventListener("input", () => resetTargets(200));
  window.addEventListener("active-job-change", () => { if (dialog.open && leftJob.id !== state.activeJobId) dialog.close(); });
  els.compareJob.addEventListener("click", () => {
    const id = state.activeJobId;
    if (!id || dialog.open) return;
    leftJob = state.activeJob?.id === id ? state.activeJob : jobById(id) || { id };
    targetSearch.value = "";
    dialog.showModal();
    resetTargets();
  });
  dialog.addEventListener("close", () => {
    if (dialog.open) return; // A queued close event may follow a new comparison.
    clearTimeout(timer);
    listing?.abort(); request?.abort();
    listing = request = null;
    differences = [];
    rows.replaceChildren();
  });
  search.addEventListener("input", filterRows);
  function resetSearch() { search.value = ""; filterRows(); search.focus(); }
  clear.addEventListener("click", resetSearch);
  part("reset").addEventListener("click", resetSearch);
  part("retry").addEventListener("click", () => { part("title").focus(); load(); });
}
