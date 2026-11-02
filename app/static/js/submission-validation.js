// Persistent, field-linked validation. Server messages are always plain text.
import { els, showWorkspaceView } from "./core.js?v=6cd8594e2b063155";

const markedFields = new Map();

function issueField(issue) {
  if (issue.field === "gmx_bin" && issue.ligandIndex == null) return els.gmxBin || null;
  const root = Number.isInteger(issue.ligandIndex)
    ? [...els.jobForm.querySelectorAll(".ligand-card")][issue.ligandIndex]
    : els.jobForm;
  if (!root || !issue.field) return null;
  // Do not interpolate server-provided locations into selectors.
  return [...root.querySelectorAll("input, select, textarea")].find(field =>
    Number.isInteger(issue.ligandIndex) ? field.dataset.ligandField === issue.field : field.name === issue.field,
  ) || (issue.field === "structure_file" && issue.ligandIndex == null ? els.files : null);
}

export function focusSubmissionIssue(issue) {
  const field = issueField(issue);
  if (!field) return;
  showWorkspaceView("compose");
  if (field.closest(".advanced-field, .advanced-section")) document.querySelector("#expert-mode")?.click();
  for (let parent = field.parentElement; parent && parent !== els.jobForm; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
    if (parent.id === "mdp-panel-body" && parent.hidden) document.querySelector("#mdp-panel-toggle")?.click();
  }
  // A stale file reference can belong to the other ligand input mode. Reveal
  // its field for correction without silently changing the chosen mode.
  const modeField = field.closest(".ligand-prepared-only, .ligand-acpype-only");
  if (modeField) modeField.classList.add("submission-field-revealed");
  field.scrollIntoView({ block: "center", behavior: "smooth" });
  field.focus({ preventScroll: true });
}

export function clearSubmissionIssues({ source } = {}) {
  const summary = document.querySelector("#submission-errors");
  if (source && summary?.dataset.source !== source) return;
  for (const [field, previous] of markedFields) {
    if (previous.invalid == null) field.removeAttribute("aria-invalid");
    else field.setAttribute("aria-invalid", previous.invalid);
    if (previous.describedBy == null) field.removeAttribute("aria-describedby");
    else field.setAttribute("aria-describedby", previous.describedBy);
    field.classList.remove("input-error");
  }
  markedFields.clear();
  document.querySelectorAll(".submission-field-revealed").forEach(field => {
    // Keep a revealed input available while the user is correcting it.
    if (!field.contains(document.activeElement)) field.classList.remove("submission-field-revealed");
  });
  if (summary) { summary.hidden = true; summary.replaceChildren(); }
}

export function showSubmissionIssues(issues, { focus = true, source = "submit" } = {}) {
  if (!issues.length) { clearSubmissionIssues(); return; }
  let summary = document.querySelector("#submission-errors");
  if (!summary) {
    summary = document.createElement("section");
    summary.id = "submission-errors";
    summary.className = "submission-errors";
    summary.setAttribute("role", "alert");
    summary.setAttribute("aria-labelledby", "submission-errors-title");
    summary.tabIndex = -1;
    els.jobForm.prepend(summary);
  }
  // Focusing can commit an input's native change event. Let that clear the old
  // feedback before installing the new issues, not after rendering them.
  if (focus) {
    showWorkspaceView("compose");
    summary.hidden = false;
    summary.focus();
  }
  clearSubmissionIssues();
  summary.hidden = false;
  summary.dataset.source = source;
  const title = document.createElement("strong");
  title.id = "submission-errors-title";
  title.textContent = "请检查以下问题";
  const list = document.createElement("ul");
  for (const [index, issue] of issues.entries()) {
    const item = document.createElement("li");
    item.id = `submission-error-${index}`;
    const field = issueField(issue);
    if (field) {
      const link = document.createElement("button");
      link.type = "button";
      link.className = "submission-error-link";
      link.textContent = issue.message;
      link.addEventListener("click", () => focusSubmissionIssue(issue));
      item.append(link);
      if (!markedFields.has(field)) markedFields.set(field, {
        invalid: field.getAttribute("aria-invalid"), describedBy: field.getAttribute("aria-describedby"),
      });
      field.setAttribute("aria-invalid", "true");
      field.classList.add("input-error");
      field.setAttribute("aria-describedby", [field.getAttribute("aria-describedby"), item.id].filter(Boolean).join(" "));
    } else item.textContent = issue.message;
    list.append(item);
  }
  summary.append(title, list);
}

export function apiSubmissionIssues(error, fallbackMessage) {
  const details = Array.isArray(error.detail) ? error.detail : [];
  if (!details.length) return [{ message: fallbackMessage || error.message || "参数校验失败" }];
  return details.map(item => {
    if (!item || typeof item !== "object") return { message: String(item) };
    const loc = Array.isArray(item.loc) ? item.loc.filter(part => !["body", "query", "path", "params"].includes(part)) : [];
    const ligand = loc.indexOf("ligands");
    const ligandIndex = ligand >= 0 && /^\d+$/.test(String(loc[ligand + 1])) ? Number(loc[ligand + 1]) : undefined;
    const field = String(ligandIndex != null ? loc[ligand + 2] || "" : loc[0] || "");
    const prefix = ligandIndex != null ? `配体 ${ligandIndex + 1} · ${field}：` : field ? `${field}：` : "";
    return { field, ligandIndex, message: prefix + String(item.msg || item.message || "参数校验失败") };
  });
}
