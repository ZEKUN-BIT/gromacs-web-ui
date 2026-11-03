// 任务操作模块：取消、接管、续跑、重试、跳过、删除、复制、性能基准与参数比较。

import { ACTIVE_STATES, els, state, toast, api, confirmDialog } from "./core.js?v=7c195747008d663c";
import { friendlyJobActionError } from "./form.js?v=7c195747008d663c";
import { refreshJobs, renderActiveJob } from "./jobs.js?v=7c195747008d663c";

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
      state.logKey = "";
      state.logText = "";
      state.logOffset = 0;
      state.logTailOmittedBytes = 0;
      state.logTrimmedChars = 0;
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
