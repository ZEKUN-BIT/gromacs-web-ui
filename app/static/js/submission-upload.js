import { api, formatApiError, updateText } from "./core.js?v=a501fb3828ebf5d3";

const PENDING_KEY = "gromacs.pending-submission";
const INACTIVITY_MS = 120_000;
let pendingId = null;
try { pendingId = sessionStorage.getItem(PENDING_KEY); } catch { /* Recovery remains available in this tab. */ }
if (pendingId && !/^[a-f0-9]{32}$/.test(pendingId)) pendingId = null;

function remember(id) {
  pendingId = id;
  try {
    if (id) sessionStorage.setItem(PENDING_KEY, id);
    else sessionStorage.removeItem(PENDING_KEY);
  } catch { /* Browser storage is optional. */ }
}

function controls() {
  return {
    panel: document.querySelector("#upload-status"), message: document.querySelector("#upload-message"),
    cancel: document.querySelector("#cancel-upload"), resolve: document.querySelector("#resolve-upload"),
  };
}

function announce(message, { cancel = false, resolve = false } = {}) {
  const ui = controls();
  ui.panel.hidden = !message;
  ui.message.textContent = message;
  ui.cancel.hidden = !cancel;
  ui.resolve.hidden = !resolve;
}

async function resolvePending() {
  if (!pendingId) return null;
  const requestId = pendingId;
  // Resolving is idempotent: a timed-out acknowledgement can safely be checked
  // again with the same ID, without resending any simulation input.
  const result = await api(`/api/submissions/${requestId}/resolve`, { method: "POST", timeoutMs: 30_000 });
  if (!["submitted", "cancelled", "deleted"].includes(result.state)) throw new Error("提交结果仍未确认，请再次核对。");
  if (pendingId === requestId) remember(null);
  return result;
}

export function bindUploadRecovery(onRecovered) {
  if (pendingId) announce("上次提交的结果尚未确认，请先核对；已保存的参数草稿可继续使用。", { resolve: true });
  controls().resolve.addEventListener("click", async event => {
    event.target.disabled = true;
    try {
      const result = await resolvePending();
      if (pendingId) return; // Another submit already started while this check was pending.
      announce(result?.state === "submitted" ? "已找到上次提交的任务。" : "已确认不会继续创建上次任务，可以重新提交。");
      if (result?.job) await onRecovered(result.job);
    } catch (error) { announce(`核对失败：${error.message}。请恢复连接后再次核对。`, { resolve: true }); }
    finally { event.target.disabled = false; }
  });
}

function sendUpload(data, button) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const ui = controls();
    let lastProgress = Date.now(), lastLoaded = 0, uploaded = false, settled = false, interruption = "";
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearInterval(watchdog);
      ui.cancel.removeEventListener("click", cancel);
      ui.cancel.hidden = true;
      callback(value);
    };
    const cancel = () => {
      interruption = uploaded ? "已停止等待，正在核对任务。" : "已取消上传，正在核对任务。";
      request.abort();
    };
    const preparation = () => {
      uploaded = true; lastProgress = Date.now();
      ui.cancel.textContent = "停止等待并核对";
      updateText(button.querySelector(".button-label"), "正在校验并准备输入…");
      announce("上传完成，服务正在校验并准备输入。停止等待后会核对是否已创建任务。", { cancel: true });
    };
    const watchdog = setInterval(() => {
      if (Date.now() - lastProgress < INACTIVITY_MS) return;
      if (uploaded) {
        announce("输入准备耗时较长，可继续等待，或停止等待并核对任务。", { cancel: true });
      } else {
        interruption = "上传已连续 2 分钟没有进展，正在核对提交结果。";
        request.abort();
      }
    }, 5000);
    request.open("POST", "/api/jobs");
    request.responseType = "json";
    ui.cancel.textContent = "取消上传";
    ui.cancel.addEventListener("click", cancel);
    announce("正在上传输入文件…", { cancel: true });
    request.upload.addEventListener("progress", event => {
      if (uploaded) return;
      if (event.loaded > lastLoaded) { lastProgress = Date.now(); lastLoaded = event.loaded; }
      if (event.lengthComputable && event.loaded >= event.total) { preparation(); return; }
      const label = event.lengthComputable ? `正在上传 ${Math.round(event.loaded / event.total * 100)}%…` : "正在上传…";
      updateText(button.querySelector(".button-label"), label);
      announce(label, { cancel: true });
    });
    request.upload.addEventListener("load", preparation);
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300 && request.response?.id) return finish(resolve, request.response);
      const problem = request.response?.problem;
      const detail = problem?.code && !["operation_failed", "invalid_request"].includes(problem.code)
        ? [problem.message, problem.recovery].filter(Boolean).join("；")
        : request.response?.detail || request.statusText || "任务提交失败";
      const error = Object.assign(new Error(formatApiError(detail)), {
        status: request.status, detail: request.response?.detail, problem,
      });
      finish(reject, error);
    });
    request.addEventListener("error", () => finish(reject, new Error("连接中断，正在核对提交结果。")));
    request.addEventListener("abort", () => finish(reject, new Error(interruption || "上传已中断。")));
    try { request.send(data); } catch (error) { finish(reject, error); }
  });
}

export async function uploadJob(data, button) {
  // Resolve any earlier ambiguous request before allocating another identity.
  // The server seals unknown IDs, so a delayed multipart request cannot create
  // a task after this check has said it is safe to retry.
  if (pendingId) {
    try {
      const previous = await resolvePending();
      if (previous?.job) { announce("已找到上次提交的任务。请核对后再创建其他任务。"); return previous.job; }
    } catch (error) {
      announce("上次提交结果尚未确认，请恢复连接后核对。", { resolve: true });
      throw error;
    }
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  remember(Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join(""));
  data.set("submission_id", pendingId);
  try {
    const job = await sendUpload(data, button);
    remember(null); announce("");
    return job;
  } catch (error) {
    // Validation rejects before a task is created; keep detailed field errors.
    if (error.status === 422) { remember(null); announce(""); throw error; }
    announce(error.message);
    try {
      const result = await resolvePending();
      if (result?.job) { announce("已确认任务创建成功，无需重复提交。"); return result.job; }
      announce("本次提交已停止，参数和已选文件已保留，可以重新提交。");
    } catch (resolutionError) {
      announce(`提交结果尚未确认：${resolutionError.message}。恢复连接后请核对，避免重复提交。`, { resolve: true });
    }
    throw error;
  }
}
