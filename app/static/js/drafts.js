import { els, state, FILE_FIELD_NAMES } from "./core.js?v=a3c62023f9227a95";
import { formObject, addLigandRow } from "./form.js?v=a3c62023f9227a95";

const DRAFT_KEY = "gromacs.simulation-draft.v1";
const writer = crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`;
let observed = null, conflict = false;

function conflictNotice() {
  conflict = true;
  els.draftStatus.textContent = "另一标签页更新了草稿，当前参数尚未覆盖保存。";
  let notice = document.querySelector("#draft-conflict");
  if (!notice) {
    notice = document.createElement("div");
    notice.id = "draft-conflict";
    notice.className = "draft-conflict";
    notice.setAttribute("role", "status");
    const message = document.createElement("p");
    message.textContent = "检测到另一标签页更新或删除了共享草稿。请先选择要保留的版本。";
    notice.append(message);
    for (const [label, action] of [["保存当前参数", () => {
      observed = localStorage.getItem(DRAFT_KEY); conflict = false; notice.hidden = true; saveDraft();
    }], ["载入另一标签页草稿", () => {
      conflict = false; notice.hidden = true; restoreDraft();
      els.jobForm.dispatchEvent(new Event("change", { bubbles: true }));
    }]]) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "tool-button"; button.textContent = label;
      button.addEventListener("click", () => {
        try { action(); } catch { els.draftStatus.textContent = "浏览器无法读取草稿，当前参数已保留"; }
      });
      notice.append(button);
    }
    els.jobForm.before(notice);
  }
  notice.hidden = false;
}

export function restoreDraft() {
  try {
    observed = localStorage.getItem(DRAFT_KEY);
    const saved = JSON.parse(observed || "null");
    if (!saved || saved.version !== 1 || !saved.params || typeof saved.params !== "object") return;
    const params = saved.params;
    els.jobForm.querySelectorAll("input[name],select[name],textarea[name]").forEach((field) => {
      if (!Object.hasOwn(params, field.name) || field.type === "file" || FILE_FIELD_NAMES.has(field.name)) return;
      if (field.type === "radio") field.checked = field.value === params[field.name];
      else if (field.type === "checkbox") field.checked = params[field.name] === true;
      else if (field.name === "tags" && Array.isArray(params.tags)) field.value = params.tags.join(", ");
      else if (["string", "number"].includes(typeof params[field.name])) field.value = String(params[field.name]);
      field.dataset.userEdited = "true";
    });
    if (Array.isArray(params.ligands) && params.ligands.length) {
      els.ligandList.replaceChildren();
      params.ligands.slice(0, 20).forEach((ligand) => addLigandRow({ ...ligand, charge_confirmed: false }));
    }
    state.viewChosen = true;
    els.draftStatus.textContent = "草稿已恢复，输入文件需重新选择";
  } catch {
    els.draftStatus.textContent = "草稿无法恢复";
  }
}

function saveDraft() {
  try {
    if (conflict) return;
    const current = localStorage.getItem(DRAFT_KEY);
    if (current !== observed) { conflictNotice(); return; }
    const params = formObject({ forDraft: true });
    delete params.files;
    delete params.gmx_bin;
    FILE_FIELD_NAMES.forEach((key) => delete params[key]);
    params.ligands = params.ligands.map((ligand) => Object.fromEntries(
      Object.entries(ligand).filter(([key]) => !key.endsWith("_file") && key !== "charge_confirmed"),
    ));
    const previous = JSON.parse(current || "null");
    if (JSON.stringify(previous?.params) === JSON.stringify(params)) {
      els.draftStatus.textContent = "参数草稿已保存";
      return;
    }
    const revision = (previous?.revision || 0) + 1;
    observed = JSON.stringify({ version: 1, revision, writer, saved_at: new Date().toISOString(), params });
    localStorage.setItem(DRAFT_KEY, observed);
    els.draftStatus.textContent = "参数草稿已保存";
  } catch {
    els.draftStatus.textContent = "浏览器无法保存草稿";
  }
}

export function bindDraftSaving() {
  let timer;
  const saveSoon = () => {
    clearTimeout(timer);
    timer = setTimeout(saveDraft, 350);
  };
  els.jobForm.addEventListener("input", saveSoon);
  els.jobForm.addEventListener("change", () => { clearTimeout(timer); saveDraft(); });
  els.ligandList.addEventListener("click", saveSoon);
  els.addLigand.addEventListener("click", saveSoon);
  window.addEventListener("storage", event => {
    if ((event.key === DRAFT_KEY || event.key === null) && event.newValue !== observed) conflictNotice();
  });
  els.clearDraft.addEventListener("click", () => {
    clearTimeout(timer);
    try {
      if (localStorage.getItem(DRAFT_KEY) !== observed || conflict) { conflictNotice(); return; }
      localStorage.removeItem(DRAFT_KEY);
      observed = null;
      els.draftStatus.textContent = "已删除保存的草稿";
    } catch {
      els.draftStatus.textContent = "无法删除草稿";
    }
  });
}
