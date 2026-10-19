// Run real refresh/upload functions with controlled requests and no browser/server.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/static/js/jobs.js", import.meta.url), "utf8");
const start = source.indexOf("let jobsRequestGeneration = 0;");
const end = source.indexOf("/* ---------- 活动任务详情", start);
assert(start >= 0 && end > start);
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const firstDetail = deferred();
const pendingDetail = deferred();
const pages = [];
const context = {
  state: { jobFilter: "old", jobsOffset: 0, activeJobId: "old-job", jobsBusy: false },
  URLSearchParams,
  CustomEvent: class {},
  window: { dispatchEvent() {} },
  renderJobs() {},
  applyJobDetail() {},
  forgetDeletedJob() { throw new Error("A stale 404 must not remove the current job"); },
  applyJobsPayload(payload) { pages.push(payload.marker); },
  api(url) {
    if (url.startsWith("/api/jobs?")) {
      const search = new URL(url, "http://test").searchParams.get("search");
      return Promise.resolve({ marker: search, total: 1, jobs: [] });
    }
    firstDetail.resolve();
    return pendingDetail.promise;
  },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end).replaceAll("export ", ""), context);
const oldRefresh = context.refreshJobs();
await firstDetail.promise;
context.state.jobFilter = "new";
context.state.activeJobId = null;
await context.refreshJobs();
pendingDetail.reject(Object.assign(new Error("deleted"), { status: 404 }));
await oldRefresh;
assert.deepEqual(pages, ["old", "new"]);
assert.equal(context.state.jobFilter, "new");
assert.equal(context.state.jobsBusy, false);
console.log("An old detail 404 cannot replace the latest search results.");

// Exercise the actual upload implementation with explicitly dispatched XHR
// events, including upload completion before the server finishes preparing.
const uploadStart = source.indexOf("function uploadJob(data, button) {");
const uploadEnd = source.indexOf("export async function submitJob()", uploadStart);
assert(uploadStart >= 0 && uploadEnd > uploadStart);
const coreSource = readFileSync(new URL("../app/static/js/core.js", import.meta.url), "utf8");
const formatStart = coreSource.indexOf("export function formatApiError(detail) {");
const formatEnd = coreSource.indexOf("let confirmDialogResolver", formatStart);
assert(formatStart >= 0 && formatEnd > formatStart);

class FakeEvents {
  listeners = new Map();
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  emit(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) callback(event);
  }
}

class ControlledXHR extends FakeEvents {
  static requests = [];
  constructor() {
    super();
    this.upload = new FakeEvents();
    ControlledXHR.requests.push(this);
  }
  open(method, url) { this.method = method; this.url = url; }
  send(data) { this.data = data; }
}

const uploads = {
  XMLHttpRequest: ControlledXHR,
  updateText(element, value) { element.textContent = value; },
};
vm.createContext(uploads);
vm.runInContext(coreSource.slice(formatStart, formatEnd).replaceAll("export ", ""), uploads);
vm.runInContext(source.slice(uploadStart, uploadEnd), uploads);

function startUpload() {
  const data = { params: "synthetic upload; never sent to a server" };
  const label = { textContent: "正在上传…" };
  const button = { querySelector(selector) { assert.equal(selector, ".button-label"); return label; } };
  const pending = uploads.uploadJob(data, button);
  const request = ControlledXHR.requests.at(-1);
  assert.equal(request.method, "POST");
  assert.equal(request.url, "/api/jobs");
  assert.equal(request.responseType, "json");
  assert.equal(request.data, data);
  return { pending, request, label };
}

for (const status of [200, 201, 202, 299]) {
  const { pending, request, label } = startUpload();
  request.upload.emit("progress", { lengthComputable: true, loaded: 25, total: 100 });
  assert.equal(label.textContent, "正在上传 25%…");
  request.upload.emit("progress", { lengthComputable: true, loaded: 100, total: 100 });
  assert.equal(label.textContent, "正在校验并准备输入…");
  request.upload.emit("progress", { lengthComputable: false, loaded: 0, total: 0 });
  assert.equal(label.textContent, "正在校验并准备输入…");
  let settled = false;
  pending.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false, "100% upload must still wait for server preparation");
  const response = { id: `uploaded-${status}`, status: "queued" };
  Object.assign(request, { status, response });
  request.emit("load");
  assert.equal(await pending, response);
}

for (const failure of [
  { status: 409, response: { detail: "Disk free space is low", problem: { code: "disk_space_low", message: "可用空间不足", recovery: "请先清理空间" } }, message: "可用空间不足；请先清理空间" },
  { status: 422, response: { detail: [{ loc: ["body", "tpr_file"], msg: "Field required" }] }, message: "tpr_file: Field required" },
  { status: 400, response: { detail: "Invalid simulation input" }, message: "Invalid simulation input" },
  { status: 503, response: null, statusText: "Service Unavailable", message: "Service Unavailable" },
  { status: 300, response: null, statusText: "", message: "任务提交失败" },
]) {
  const { pending, request } = startUpload();
  Object.assign(request, failure);
  request.emit("load");
  await assert.rejects(pending, (error) => error.status === failure.status && error.message === failure.message);
}

for (const [type, message] of [
  ["error", "连接中断，请检查服务，并先查看任务列表确认是否已提交。"],
  ["abort", "上传已取消。"],
]) {
  const { pending, request } = startUpload();
  request.emit(type);
  await assert.rejects(pending, (error) => error.message === message && error.status === undefined);
}
console.log("Real upload progress, preparation wait, 2xx success, validation/HTTP failure, network error and abort handling passed.");
