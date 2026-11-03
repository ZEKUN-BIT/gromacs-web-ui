// Task updates retain their last known values while connection health is explicit.
import { state, MAX_LOG_CHARS, updateText } from "./core.js?v=a501fb3828ebf5d3";
import { refreshJobs, applyJobsPayload, applyJobDetail, appendLogChunk, forgetDeletedJob } from "./jobs.js?v=a501fb3828ebf5d3";
import { renderReadiness } from "./form-environment.js?v=a501fb3828ebf5d3";

let heartbeatTimer = null;
let lastSynchronized = null;
let failures = 0;
const HEARTBEAT_TIMEOUT = 45_000; // The service sends a heartbeat every 15 seconds.

function showConnection(status) {
  const disconnected = status === "reconnecting" || status === "offline";
  const labels = {
    connecting: "正在连接实时更新…", live: "实时更新已连接",
    reconnecting: "实时更新正在重连", offline: "实时同步已中断", paused: "实时更新已暂停",
  };
  const notice = document.querySelector("#task-sync-notice");
  if (notice) { notice.hidden = !disconnected; notice.dataset.state = status; }
  updateText(document.querySelector("#task-sync-state"), labels[status]);
  updateText(document.querySelector("#task-sync-message"), disconnected
    ? `${labels[status]}，当前进度可能不是最新状态；后台任务不会因此停止。` : "");
  const time = document.querySelector("#task-sync-time");
  if (time) {
    time.hidden = !lastSynchronized;
    if (lastSynchronized) {
      time.dateTime = lastSynchronized.toISOString();
      time.textContent = `最近同步 ${lastSynchronized.toLocaleTimeString()}`;
    }
  }
}

export function disconnectEventStream() {
  window.clearTimeout(state.reconnectTimer);
  window.clearTimeout(heartbeatTimer);
  state.reconnectTimer = null;
  heartbeatTimer = null;
  state.eventSource?.close();
  state.eventSource = null;
  if (document.hidden) showConnection("paused");
}

export function connectEventStream() {
  disconnectEventStream();
  if (document.hidden) return;
  if (navigator.onLine === false) { showConnection("offline"); return; }
  showConnection(failures ? "reconnecting" : "connecting");
  const query = new URLSearchParams({
    jobs_offset: String(state.jobsOffset), jobs_search: state.jobFilter.trim(), jobs_project: state.jobProject || "",
  });
  if (state.activeJobId) query.set("job_id", state.activeJobId);
  if (state.logOffset > 0) query.set("log_offset", String(state.logOffset));
  else query.set("log_tail", String(MAX_LOG_CHARS));
  const source = new EventSource(`/api/events?${query}`);
  state.eventSource = source;

  const failed = () => {
    if (state.eventSource !== source) return;
    disconnectEventStream();
    failures += 1;
    showConnection(navigator.onLine === false || failures >= 3 ? "offline" : "reconnecting");
    if (!document.hidden && navigator.onLine !== false) {
      state.reconnectTimer = window.setTimeout(connectEventStream, Math.min(15_000, 1500 * 2 ** Math.min(failures - 1, 4)));
    }
  };
  const synchronized = () => {
    failures = 0;
    lastSynchronized = new Date();
    showConnection("live");
    window.clearTimeout(heartbeatTimer);
    heartbeatTimer = window.setTimeout(failed, HEARTBEAT_TIMEOUT);
  };
  // A connection opening alone does not certify that fresh task data arrived.
  heartbeatTimer = window.setTimeout(failed, HEARTBEAT_TIMEOUT);
  const receive = (type, apply) => source.addEventListener(type, (event) => {
    if (state.eventSource !== source) return;
    try {
      const payload = JSON.parse(event.data);
      apply(payload);
      if (state.eventSource === source) synchronized();
    } catch { failed(); }
  });
  receive("jobs", payload => {
    if (state.jobsOffset > 0 && state.jobsOffset >= payload.total) {
      void refreshJobs().catch(() => {}).finally(() => { if (state.eventSource === source) connectEventStream(); });
      return;
    }
    const previousActive = state.activeJobId;
    applyJobsPayload(payload);
    if (state.activeJobId !== previousActive) connectEventStream();
  });
  receive("job", applyJobDetail);
  receive("log", appendLogChunk);
  receive("worker", payload => {
    if (!state.healthPayload) return;
    state.healthPayload.worker = payload;
    renderReadiness();
  });
  receive("ping", () => {});
  receive("deleted", () => {
    forgetDeletedJob(query.get("job_id"));
    void refreshJobs().catch(() => {}).finally(() => { if (state.eventSource === source) connectEventStream(); });
  });
  source.onerror = failed;
}

export function bindConnectionStatus() {
  document.querySelector("#retry-task-sync")?.addEventListener("click", connectEventStream);
  window.addEventListener("offline", () => { disconnectEventStream(); showConnection("offline"); });
  window.addEventListener("online", connectEventStream);
  window.addEventListener("pagehide", disconnectEventStream);
  window.addEventListener("pageshow", event => { if (event.persisted) connectEventStream(); });
}
