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

// Run the real organization form against a small DOM and controlled PATCH calls.
// Drafts must survive task switches, reloads, storage failure and saves in flight.
class TestElement extends FakeEvents {
  constructor() { super(); this.value = ""; this.checked = false; this.disabled = false; this.textContent = ""; this.children = []; this.dataset = {}; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  append(...nodes) { this.children.push(...nodes); }
}
const organizationSource = readFileSync(new URL("../app/static/js/result-summary.js", import.meta.url), "utf8");
function organizationHarness(storage = new Map(), failStorage = false) {
  const ids = ["organization-form", "organization-summary", "job-project", "job-tags", "job-notes", "job-pinned", "download-result-bundle", "result-summary-content"];
  const elements = new Map(ids.map(id => [id, new TestElement()]));
  const form = elements.get("organization-form");
  const submit = new TestElement(); submit.type = "submit";
  form.elements = [...["job-project", "job-tags", "job-notes", "job-pinned"].map(id => elements.get(id)), submit];
  form.querySelector = () => submit;
  form.reset = () => { for (const element of form.elements) { element.value = ""; element.checked = false; } };
  const events = new FakeEvents();
  const requests = [];
  const messages = [];
  const sandbox = {
    state: { activeJobId: null }, product: { available: true },
    document: { querySelector(selector) { return elements.get(selector.slice(1)) || form.children.find(element => element.id === selector.slice(1)); }, createElement() { return new TestElement(); } },
    window: events,
    localStorage: {
      getItem(key) { if (failStorage) throw new Error("Storage blocked"); return storage.get(key) ?? null; },
      setItem(key, value) { if (failStorage) throw new Error("Quota exceeded"); storage.set(key, value); },
      removeItem(key) { if (failStorage) throw new Error("Storage blocked"); storage.delete(key); },
    },
    api(url, options) { const request = deferred(); requests.push({ ...request, url, payload: JSON.parse(options.body) }); return request.promise; },
    loadProduct() { return Promise.resolve(); }, toast(message) { messages.push(message); },
    applyJobDetail(job) { sandbox.renderResultSummary(job); },
  };
  vm.createContext(sandbox);
  vm.runInContext(organizationSource.replace(/^import .*\n/gm, "").replaceAll("export ", ""), sandbox);
  sandbox.bindResultSummary();
  return {
    elements, storage, events, requests, messages,
    field(id) { return elements.get(id); },
    status() { return sandbox.document.querySelector("#organization-draft-status").textContent; },
    async render(job) { sandbox.state.activeJobId = job?.id ?? null; await sandbox.renderResultSummary(job); },
    input(values) {
      for (const [name, value] of Object.entries(values)) elements.get(`job-${name}`)[name === "pinned" ? "checked" : "value"] = value;
      form.emit("input");
    },
    submit() { return form.listeners.get("submit")[0]({ preventDefault() {} }); },
  };
}
const organizationA = { id: "a", status: "running", project: "project-a", tags: ["WT"], notes: "server-a", pinned: false };
const organizationB = { id: "b", status: "running", project: "project-b", tags: [], notes: "server-b", pinned: false };
const drafts = organizationHarness();
await drafts.render(organizationA);
drafts.input({ project: "<img src=x onerror=alert(1)>", tags: "WT，300K", notes: "unsaved-a", pinned: true });
assert.match(drafts.status(), /未保存.*本机草稿已保留/);
await drafts.render(organizationB);
assert.equal(drafts.field("job-notes").value, "server-b");
drafts.input({ notes: "unsaved-b" });
await drafts.render(organizationA);
assert.equal(drafts.field("job-project").value, "<img src=x onerror=alert(1)>");
assert.equal(drafts.field("job-tags").value, "WT，300K");
assert.equal(drafts.field("job-notes").value, "unsaved-a");
assert.equal(drafts.field("job-pinned").checked, true);
const leaving = { preventDefault() { this.prevented = true; } };
drafts.events.emit("beforeunload", leaving);
assert.equal(leaving.prevented, true);
const restored = organizationHarness(drafts.storage);
await restored.render({ ...organizationA, notes: "edited-elsewhere" });
assert.equal(restored.field("job-notes").value, "unsaved-a");
assert.match(restored.status(), /服务器记录已有更新/);
const saving = restored.submit();
assert.equal(restored.requests.length, 1);
assert.deepEqual(restored.requests[0].payload.tags, ["WT", "300K"]);
await restored.render(organizationB);
restored.input({ notes: "newer-b" });
await restored.render(organizationA);
restored.input({ notes: "typed-during-save" });
restored.requests[0].resolve({ ...organizationA, ...restored.requests[0].payload });
await saving;
assert.equal(restored.field("job-notes").value, "typed-during-save");
assert.match(restored.status(), /未保存/);
assert.doesNotMatch(restored.status(), /服务器记录已有更新/);
const savingAgain = restored.submit();
restored.requests[1].resolve({ ...organizationA, ...restored.requests[1].payload });
await savingAgain;
assert.equal(restored.storage.has("gromacs-organization-draft-v1:a"), false);
await restored.render(organizationB);
assert.equal(restored.field("job-notes").value, "newer-b");
const failedSave = restored.submit();
restored.requests[2].reject(new Error("Connection interrupted"));
await failedSave;
assert.equal(restored.field("job-notes").value, "newer-b");
assert.match(restored.status(), /未保存/);
restored.events.emit("job-deleted", { detail: "b" });
assert.equal(restored.storage.has("gromacs-organization-draft-v1:b"), false);
const cleanedLeaving = { preventDefault() { this.prevented = true; } };
restored.events.emit("beforeunload", cleanedLeaving);
assert.equal(cleanedLeaving.prevented, undefined);
for (const malformed of ["not JSON", JSON.stringify({ version: 0 }), JSON.stringify({ version: 1, jobId: "a", values: { notes: [] } })]) {
  const old = organizationHarness(new Map([["gromacs-organization-draft-v1:a", malformed]]));
  await old.render(organizationA);
  assert.equal(old.field("job-notes").value, "server-a");
}
const unavailable = organizationHarness(new Map(), true);
await unavailable.render(organizationA);
unavailable.input({ notes: "memory-only" });
assert.match(unavailable.status(), /本机存储不可用/);
await unavailable.render(organizationB);
await unavailable.render(organizationA);
assert.equal(unavailable.field("job-notes").value, "memory-only");
console.log("Organization drafts preserve all fields across task switches/reloads, warn on stale records, tolerate unavailable storage, and retain edits across pending/failed saves.");

// Exercise real artifact request coordination. Failures must release the cache,
// and an older response must not win after returning to the same task and tab.
const artifactStart = source.indexOf("let artifactRequestGeneration = 0;");
const artifactEnd = source.indexOf("let logHighlightTimer", artifactStart);
assert(artifactStart >= 0 && artifactEnd > artifactStart);
const artifactRequests = [];
const artifactRenders = [];
const artifacts = {
  state: { activeJobId: "a", detailTab: "files", artifactKey: "", filesSignature: "", plotFilters: { replica: "", metric: "", begin: "", end: "" } },
  URLSearchParams,
  els: { outputFiles: new TestElement(), fileCount: new TestElement(), qualityReports: new TestElement(), plotFilterError: new TestElement(), analysisPlots: new TestElement() },
  document: { createElement() { return new TestElement(); } },
  api(url) { const request = deferred(); artifactRequests.push({ ...request, url }); return request.promise; },
  updateText(element, text) { element.textContent = text; },
  renderFiles(jobId, files) { artifactRenders.push({ jobId, files }); artifacts.els.outputFiles.textContent = files.map(file => file.path).join(","); },
  renderActiveJob() { return artifacts.refreshJobArtifacts({ id: artifacts.state.activeJobId, status: "completed" }); },
  renderResearch() {}, updatePlotOptions() {}, renderPlots() {},
};
vm.createContext(artifacts);
vm.runInContext(source.slice(artifactStart, artifactEnd), artifacts);
const failedFiles = artifacts.renderActiveJob();
artifactRequests.at(-1).reject(new Error("temporary outage <script>"));
await failedFiles;
assert.equal(artifacts.state.artifactKey, "");
assert.equal(artifacts.els.fileCount.textContent, "读取失败");
assert.match(artifacts.els.outputFiles.children[0].textContent, /文件列表加载失败/);
const retry = artifacts.els.outputFiles.children[1];
retry.emit("click");
assert.equal(retry.disabled, true);
artifactRequests.at(-1).resolve({ files: [{ path: "recovered.xtc" }] });
await new Promise(resolve => setImmediate(resolve));
assert.equal(artifacts.els.outputFiles.textContent, "recovered.xtc");
// A -> B -> A produces an identical artifact key but a new request generation.
artifacts.state.artifactKey = "";
const obsolete = artifacts.renderActiveJob();
const obsoleteRequest = artifactRequests.at(-1);
artifacts.state.activeJobId = "b";
const secondJob = artifacts.renderActiveJob();
artifactRequests.at(-1).resolve({ files: [{ path: "b.xtc" }] });
await secondJob;
artifacts.state.activeJobId = "a";
const newest = artifacts.renderActiveJob();
artifactRequests.at(-1).resolve({ files: [{ path: "new-a.xtc" }] });
await newest;
obsoleteRequest.resolve({ files: [{ path: "obsolete-a.xtc" }] });
await obsolete;
assert.equal(artifacts.els.outputFiles.textContent, "new-a.xtc");
// The same protection applies to switching away and back to the same tab.
artifacts.state.artifactKey = "";
const obsoleteTab = artifacts.renderActiveJob();
const obsoleteTabRequest = artifactRequests.at(-1);
artifacts.state.detailTab = "research";
await artifacts.renderActiveJob();
artifacts.state.detailTab = "files";
const latestTab = artifacts.renderActiveJob();
artifactRequests.at(-1).resolve({ files: [{ path: "latest-tab.xtc" }] });
await latestTab;
obsoleteTabRequest.reject(new Error("obsolete failure"));
await obsoleteTab;
assert.equal(artifacts.els.outputFiles.textContent, "latest-tab.xtc");
assert.notEqual(artifacts.state.artifactKey, "");
artifacts.state.detailTab = "quality";
const failedQuality = artifacts.renderActiveJob();
artifactRequests.at(-1).reject(new Error("quality file discovery failed"));
await failedQuality;
assert.match(artifacts.els.qualityReports.children[0].textContent, /加载失败/);
assert.equal(artifacts.state.artifactKey, "");
console.log("File list failures show retry controls and recover; stale task/tab responses cannot replace newer results or invalidate their cache.");
