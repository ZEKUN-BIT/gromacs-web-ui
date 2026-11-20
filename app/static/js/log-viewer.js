import { els, state, MAX_LOG_CHARS, updateText, toast } from "./core.js?v=5b84b3e1eaca6ab9";

let highlightTimer, highlighted = false, highlightDeferred = false;
let matches = [], current = -1;
// Even a one-character search in a full log must not create 300,000 DOM nodes.
const MAX_MARKS = 200;

function updateMatches(previousStart = -1) {
  const query = els.logSearch.value;
  matches = [];
  if (query) {
    const literal = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(literal, "gi");
    let hit;
    while ((hit = pattern.exec(state.logText))) matches.push(hit.index);
  }
  current = matches.length ? Math.max(0, matches.indexOf(previousStart)) : -1;
  syncSearch();
}

function syncSearch() {
  const query = els.logSearch.value;
  updateText(els.logCount, !query ? "" : current < 0 ? "无匹配" : `${current + 1} / ${matches.length}`);
  els.logPrev.disabled = els.logNext.disabled = current < 0;
  els.logClear.hidden = !query;
}

function appendText(fragment, text) {
  if (!highlighted) { fragment.append(text); return; }
  const pattern = /\b(error|fatal|failed|failure|exception|warning|warn|step(?:\s+\d+)?|stage(?:\s+\d+)?)\b/gi;
  let start = 0;
  for (const hit of text.matchAll(pattern)) {
    const token = hit[0], span = document.createElement("span");
    span.className = /^warn/i.test(token) ? "log-warning" : /^st/i.test(token) ? "log-step" : "log-error";
    span.textContent = token;
    fragment.append(text.slice(start, hit.index), span);
    start = hit.index + token.length;
  }
  if (start < text.length) fragment.append(text.slice(start));
}

// When the retained window shifts, subtract the actual wrapped height of its discarded prefix.
function clippedHeight(count) {
  if (!count || els.logFollow.checked || !els.jobLog.scrollTop) return 0;
  const walker = document.createTreeWalker(els.jobLog, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let firstTop;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.length) continue;
    range.setStart(node, 0); range.collapse(true);
    firstTop ??= range.getBoundingClientRect().top;
    if (count < node.length) {
      range.setStart(node, count); range.collapse(true);
      return range.getBoundingClientRect().top - firstTop;
    }
    count -= node.length;
  }
  return els.jobLog.scrollTop;
}

function logSelection() {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed && els.jobLog.contains(selection.anchorNode) && els.jobLog.contains(selection.focusNode) ? selection : null;
}

// Search marks and retention trims can require a rebuild. Map both ends (including
// backwards selections) onto the surviving text, never onto another task's log.
function retainSelection(countClipped) {
  const selection = logSelection();
  if (!selection) return null;
  const terminal = els.jobLog, range = document.createRange();
  function offset(node, end) {
    range.selectNodeContents(terminal);
    range.setEnd(node, end);
    return Math.max(0, range.toString().length - countClipped);
  }
  const anchor = offset(selection.anchorNode, selection.anchorOffset), focus = offset(selection.focusNode, selection.focusOffset);
  function point(index) {
    const walker = document.createTreeWalker(terminal, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (index <= walker.currentNode.length) return [walker.currentNode, index];
      index -= walker.currentNode.length;
    }
    return [terminal, terminal.childNodes.length];
  }
  return () => selection.setBaseAndExtent(...point(anchor), ...point(focus));
}

function paint(countClipped = 0) {
  const terminal = els.jobLog, text = state.logText;
  const top = terminal.scrollTop - clippedHeight(countClipped);
  const restore = retainSelection(countClipped);
  if (!highlighted && !matches.length) terminal.textContent = text;
  else {
    const fragment = document.createDocumentFragment();
    const first = matches.length > MAX_MARKS ? current : 0;
    const end = matches.length > MAX_MARKS ? current + 1 : matches.length;
    let start = 0;
    for (let i = first; i < end; i++) {
      const offset = matches[i], mark = document.createElement("mark");
      appendText(fragment, text.slice(start, offset));
      start = offset + els.logSearch.value.length;
      mark.textContent = text.slice(offset, start);
      if (i === current) mark.className = "log-current";
      fragment.append(mark);
    }
    appendText(fragment, text.slice(start));
    terminal.replaceChildren(fragment);
  }
  restore?.();
  terminal.scrollTop = els.logFollow.checked ? terminal.scrollHeight : top;
}

function highlightWhenIdle() {
  highlightDeferred = Boolean(logSelection());
  if (!highlightDeferred) { highlighted = true; paint(); }
}

function revealCurrent() {
  const terminal = els.jobLog, mark = terminal.querySelector(".log-current");
  if (!mark) return;
  els.logFollow.checked = false;
  terminal.scrollTop += mark.getBoundingClientRect().top - terminal.getBoundingClientRect().top - terminal.clientHeight / 2 + mark.offsetHeight / 2;
}

function jump(direction) {
  if (!matches.length) return;
  const previous = current;
  current = (current + direction + matches.length) % matches.length;
  if (matches.length > MAX_MARKS) paint();
  else {
    const marks = els.jobLog.querySelectorAll("mark");
    marks[previous].classList.remove("log-current");
    marks[current].classList.add("log-current");
  }
  syncSearch(); revealCurrent();
}

function resetLog(rotated) {
  window.clearTimeout(highlightTimer);
  state.logText = "";
  state.logOffset = state.logTailOmittedBytes = state.logTrimmedChars = 0;
  state.logGeneration = null;
  highlighted = highlightDeferred = false;
  state.logRotated = rotated;
  if (!rotated) {
    els.logSearch.value = "";
    els.logFollow.checked = true;
    els.downloadLog.hidden = true;
  }
  els.jobLog.textContent = "";
  els.logNotice.hidden = true;
  els.copyLog.disabled = true;
  updateMatches();
}

export function resetLogState() { resetLog(false); }

export function appendLogChunk(chunk) {
  if (!chunk) return;
  if (chunk.reset) resetLog(true);
  if (Number(chunk.start) < state.logOffset) return;
  if (!state.logOffset && !state.logText) state.logTailOmittedBytes = Math.max(0, Number(chunk.start) || 0);
  state.logGeneration = chunk.generation ?? null;
  if (Number.isFinite(Number(chunk.offset))) state.logOffset = Number(chunk.offset);
  state.logText += chunk.text || "";
  const clipped = Math.max(0, state.logText.length - MAX_LOG_CHARS);
  if (clipped) {
    state.logTrimmedChars += clipped;
    state.logText = state.logText.slice(-MAX_LOG_CHARS);
  }
  if (chunk.text) {
    if (els.logSearch.value) updateMatches((matches[current] ?? -1) - clipped);
    highlighted = highlightDeferred = false;
    if (!clipped && !els.logSearch.value) {
      els.jobLog.append(chunk.text);
      if (els.logFollow.checked) els.jobLog.scrollTop = els.jobLog.scrollHeight;
    } else paint(clipped);
    els.copyLog.disabled = false;
    window.clearTimeout(highlightTimer);
    highlightTimer = window.setTimeout(highlightWhenIdle, 1500);
  }
  const truncated = state.logTailOmittedBytes > 0 || state.logTrimmedChars > 0;
  if (truncated || state.logRotated) {
    els.logNotice.hidden = false;
    const omitted = [state.logTailOmittedBytes ? `打开时省略 ${(state.logTailOmittedBytes / 1024).toFixed(1)} KB` : "", state.logTrimmedChars ? `后续裁剪 ${state.logTrimmedChars.toLocaleString()} 个字符` : ""].filter(Boolean).join("；");
    const rotation = state.logRotated ? "日志已轮转或重置，已重新读取当前保留的日志。" : "";
    const retained = state.logRotated ? "可下载已保留日志。" : "可下载已保留日志；服务端轮转可能省略更早内容。";
    updateText(els.logNotice, rotation + (truncated ? `仅显示末尾日志（${omitted}）。复制仅包含当前显示内容，${retained}` : ""));
  } else {
    els.logNotice.hidden = true;
  }
}

export function bindLogViewer() {
  function search() { updateMatches(); paint(); revealCurrent(); }
  function clear() { els.logSearch.value = ""; search(); els.logSearch.focus({ preventScroll: true }); }
  const terminal = els.jobLog;
  document.addEventListener("selectionchange", () => { if (highlightDeferred && !logSelection()) highlightWhenIdle(); });
  els.logSearch.addEventListener("input", search);
  els.logSearch.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Enter") { event.preventDefault(); jump(event.shiftKey ? -1 : 1); }
    if (event.key === "Escape" && els.logSearch.value) { event.preventDefault(); clear(); }
  });
  els.logClear.addEventListener("click", clear);
  els.logPrev.addEventListener("click", () => jump(-1));
  els.logNext.addEventListener("click", () => jump(1));
  function follow() { if (els.logFollow.checked) terminal.scrollTop = terminal.scrollHeight; }
  els.logFollow.addEventListener("change", follow);
  els.logBottom.addEventListener("click", () => { els.logFollow.checked = true; follow(); });
  terminal.addEventListener("scroll", () => {
    if (terminal.clientHeight && terminal.scrollTop + terminal.clientHeight < terminal.scrollHeight - 2) els.logFollow.checked = false;
  });
  new ResizeObserver(follow).observe(terminal);
  els.copyLog.addEventListener("click", async () => {
    const text = state.logText;
    if (!text) {
      toast("当前没有日志");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      toast(state.logTailOmittedBytes || state.logTrimmedChars ? "当前显示的末尾日志已复制；可下载已保留日志，服务端轮转可能省略更早内容。" : "日志已复制到剪贴板", "success");
    } catch {
      toast("复制失败，请检查浏览器权限", "error");
    }
  });
}
