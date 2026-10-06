import { els, state, FILE_FIELD_NAMES } from "./core.js?v=20261006-026";
import { formObject, addLigandRow } from "./form.js?v=20261006-026";

const DRAFT_KEY = "gromacs.simulation-draft.v1";

export function restoreDraft() {
  try {
    const saved = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    if (!saved || saved.version !== 1 || !saved.params || typeof saved.params !== "object") return;
    const params = saved.params;
    els.jobForm.querySelectorAll("input[name],select[name],textarea[name]").forEach((field) => {
      if (!Object.hasOwn(params, field.name) || field.type === "file" || FILE_FIELD_NAMES.has(field.name)) return;
      if (field.type === "radio") field.checked = field.value === params[field.name];
      else if (field.type === "checkbox") field.checked = params[field.name] === true;
      else if (["string", "number"].includes(typeof params[field.name])) field.value = String(params[field.name]);
      if (["force_field", "ntmpi", "ntomp", "pin", "gpu"].includes(field.name)) field.dataset.userEdited = "true";
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
    const params = formObject();
    delete params.files;
    delete params.gmx_bin;
    FILE_FIELD_NAMES.forEach((key) => delete params[key]);
    params.ligands = params.ligands.map((ligand) => Object.fromEntries(
      Object.entries(ligand).filter(([key]) => !key.endsWith("_file") && key !== "charge_confirmed"),
    ));
    localStorage.setItem(DRAFT_KEY, JSON.stringify({ version: 1, params }));
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
  els.jobForm.addEventListener("change", saveDraft);
  els.ligandList.addEventListener("click", saveSoon);
  els.addLigand.addEventListener("click", saveSoon);
  els.clearDraft.addEventListener("click", () => {
    clearTimeout(timer);
    try {
      localStorage.removeItem(DRAFT_KEY);
      els.draftStatus.textContent = "已删除保存的草稿";
    } catch {
      els.draftStatus.textContent = "无法删除草稿";
    }
  });
}
