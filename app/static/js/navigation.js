// Shareable task locations and browser history; form contents never enter URLs.
import { state, els, showWorkspaceView, showDetailTab, toast } from "./core.js?v=a3c62023f9227a95";
import { refreshJobs, selectJob } from "./jobs.js?v=a3c62023f9227a95";
import { connectEventStream } from "./event-stream.js?v=a3c62023f9227a95";

export function bindNavigation() {
  let ready = false, restoring = false, queued = false;
  const tabs = new Set(["overview", "quality", "plots", "research", "files"]);
  function readLocation() {
    const query = new URL(location.href).searchParams;
    const job = query.get("job") || "";
    return {
      job: /^[A-Za-z0-9_-]{1,128}$/.test(job) ? job : null,
      view: ["compose", "agent", "detail"].includes(query.get("view")) ? query.get("view") : "compose",
      tab: tabs.has(query.get("tab")) ? query.get("tab") : "overview",
      search: (query.get("search") || "").slice(0, 256), project: (query.get("project") || "").slice(0, 128),
      offset: Math.min(1_000_000, Math.max(0, Number.parseInt(query.get("offset"), 10) || 0)),
    };
  }
  function applyLocation(initial = false) {
    const target = readLocation();
    restoring = true;
    state.jobFilter = target.search; state.jobProject = target.project; state.jobsOffset = target.offset;
    els.jobFilter.value = target.search;
    const projects = document.querySelector("#project-filter");
    if (target.project && ![...projects.options].some(option => option.value === target.project)) projects.add(new Option(target.project, target.project));
    projects.value = target.project;
    if (initial) state.activeJobId = target.job;
    else if (target.job) selectJob(target.job);
    else { state.activeJobId = null; state.activeJob = null; }
    showDetailTab(target.tab);
    showWorkspaceView(target.view, Boolean(location.search));
    restoring = false;
  }
  function saveScroll() {
    if (!ready || restoring) return;
    history.replaceState({ ...history.state, gromacsScroll: window.scrollY }, "");
  }
  function write(mode = "push") {
    if (!ready || restoring || queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      const url = new URL(location.href);
      for (const [key, value] of Object.entries({
        job: state.activeJobId, view: state.workspaceView === "compose" ? "" : state.workspaceView,
        tab: state.detailTab === "overview" ? "" : state.detailTab,
        search: state.jobFilter, project: state.jobProject, offset: state.jobsOffset || "",
      })) {
        if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
      }
      if (url.href !== location.href) history[mode === "replace" ? "replaceState" : "pushState"]({ gromacsScroll: window.scrollY }, "", url);
    });
  }
  applyLocation(true);
  window.addEventListener("before-workspace-view-change", saveScroll);
  window.addEventListener("before-detail-tab-change", saveScroll);
  for (const type of ["workspace-view-change", "detail-tab-change", "active-job-change", "jobs-page-change"]) window.addEventListener(type, () => write());
  window.addEventListener("jobs-updated", () => write("replace"));
  els.jobFilter.addEventListener("input", () => write("replace"));
  document.querySelector("#project-filter").addEventListener("change", () => write());
  window.addEventListener("popstate", event => {
    const href = location.href, top = event.state?.gromacsScroll || 0;
    applyLocation(); connectEventStream();
    void refreshJobs().catch(error => toast(error.message, "error")).finally(() => {
      if (location.href === href) requestAnimationFrame(() => window.scrollTo(0, top));
    });
  });
  return () => { ready = true; write("replace"); };
}
