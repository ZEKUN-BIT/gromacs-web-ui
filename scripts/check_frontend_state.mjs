// Run the real refresh function with interleaved requests and no browser/server.
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
