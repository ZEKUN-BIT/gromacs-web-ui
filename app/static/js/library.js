import { els, state, api, toast, confirmDialog, showWorkspaceView, FILE_FIELD_NAMES } from "./core.js?v=621bb939e2b97a80";
import { formObject, toggleSections, refreshPreview, renderWorkflowGuide, addLigandRow, renderWaterModelOptions } from "./form.js?v=621bb939e2b97a80";

export const product = { templates: [], projects: [], defaults: {}, available: false };
let productGeneration = 0;
export function downloadJson(name, value) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function loadProduct(selectedTemplate) {
  const generation = ++productGeneration;
  const payload = await api("/api/product");
  if (generation !== productGeneration) return;
  Object.assign(product, payload);
  product.available = true;
  state.productAvailable = true;
  const filter = document.querySelector("#project-filter");
  const selected = filter.value;
  filter.replaceChildren(new Option("全部项目", ""));
  const options = document.querySelector("#project-options");
  options.replaceChildren();
  for (const project of product.projects) {
    if (!project.name) continue;
    filter.add(new Option(`${project.name} (${project.count})`, project.name));
    options.append(new Option(project.name, project.name));
  }
  if (selected && !product.projects.some(item => item.name === selected)) filter.add(new Option(selected, selected));
  filter.value = selected;
  renderTemplates(selectedTemplate);
}

function renderTemplates(selectedTemplate) {
  const select = document.querySelector("#template-select");
  const selected = selectedTemplate || select.value;
  select.replaceChildren();
  for (const item of product.templates) select.add(new Option(`${item.name} · v${item.revision}`, item.id));
  if (!select.options.length) select.add(new Option("暂无模板，保存当前协议或导入 JSON", ""));
  if (product.templates.some(item => item.id === selected)) select.value = selected;
  showTemplate();
}

function chosenTemplate() { return product.templates.find(item => item.id === document.querySelector("#template-select").value); }
function showTemplate() {
  const item = chosenTemplate();
  document.querySelector("#template-details").textContent = item
    ? [item.description, `适用范围：${item.applicability || "未填写，使用前请确认"}`, `来源：${item.source || "未填写"}`, `工作流：${item.params.workflow}`].filter(Boolean).join("\n")
    : "模板保存在本机。相同名称再次保存会创建一个新版本。";
  for (const id of ["apply-template", "export-template", "delete-template"]) document.getElementById(id).disabled = !item;
}

export function applyProtocol(params) {
  const preserved = Object.fromEntries(["name", "project", "tags", "notes"].map(name => [name, els.jobForm.elements.namedItem(name)?.value || ""]));
  els.jobForm.reset();
  els.files.value = "";
  const values = { ...product.defaults, ...params };
  els.jobForm.querySelectorAll("input[name],select[name],textarea[name]").forEach(field => {
    if (field.type === "file") return;
    if (FILE_FIELD_NAMES.has(field.name)) { field.value = ""; return; }
    if (!Object.hasOwn(values, field.name)) return;
    const value = values[field.name];
    if (field.type === "radio") field.checked = field.value === value;
    else if (field.type === "checkbox") field.checked = value === true;
    else if (["number", "string"].includes(typeof value)) {
      // A template may reference a force field's water model that is not in the
      // current machine's initial options. Keep it visible for explicit review.
      if (field.name === "water_model" && ![...field.options].some(option => option.value === String(value))) {
        field.add(new Option(String(value), String(value)));
      }
      field.value = String(value);
    }
    if (["force_field", "ntmpi", "ntomp", "pin", "gpu"].includes(field.name)) field.dataset.userEdited = "true";
  });
  for (const [name, value] of Object.entries(preserved)) els.jobForm.elements.namedItem(name).value = value;
  els.ligandList.replaceChildren();
  (params.ligands?.length ? params.ligands : [{}]).slice(0, 20).forEach(ligand => addLigandRow({ ...ligand, charge_confirmed: false }));
  renderWaterModelOptions();
  els.fileSummary.textContent = "模板已应用，请重新选择输入文件";
  toggleSections(); renderWorkflowGuide();
  els.jobForm.dispatchEvent(new Event("change", { bubbles: true }));
  showWorkspaceView("compose");
  refreshPreview();
}

export function bindLibrary() {
  const dialog = document.querySelector("#library-dialog");
  const error = document.querySelector("#template-error");
  let saving = false;
  const setSaving = value => {
    saving = value;
    document.querySelector('#template-form button[type="submit"]').disabled = value;
    document.querySelector("#import-template").disabled = value;
  };
  document.querySelector("#open-library").addEventListener("click", async () => {
    error.textContent = ""; dialog.showModal();
    try { await loadProduct(); } catch (failure) { error.textContent = failure.message; }
  });
  document.querySelector("#template-select").addEventListener("change", showTemplate);
  document.querySelector("#apply-template").addEventListener("click", async () => {
    const item = chosenTemplate(); if (!item) return;
    dialog.close();
    if (!await confirmDialog({ title: "应用协议模板？", subtitle: "任务名和项目保留；输入文件需要重新选择", items: [`${item.name} · v${item.revision}`, item.applicability || "此模板未注明适用范围，请核对参数。"], confirmText: "替换当前参数" })) {
      dialog.showModal(); document.querySelector("#apply-template").focus(); return;
    }
    applyProtocol(item.params); toast("模板已应用，请选择输入并核对协议。", "success");
  });
  document.querySelector("#export-template").addEventListener("click", () => {
    const item = chosenTemplate(); if (!item) return;
    const { id, revision, created_at, ...payload } = item;
    downloadJson("gromacs-protocol.json", payload);
  });
  document.querySelector("#delete-template").addEventListener("click", async () => {
    const item = chosenTemplate(); if (!item) return;
    dialog.close();
    if (!await confirmDialog({ title: "删除此模板版本？", subtitle: "已创建的任务和其他模板版本保留", items: [`${item.name} · v${item.revision}`], confirmText: "删除版本" })) {
      dialog.showModal(); document.querySelector("#delete-template").focus(); return;
    }
    dialog.showModal();
    try { await api(`/api/templates/${encodeURIComponent(item.id)}`, { method: "DELETE" }); await loadProduct(); toast("模板版本已删除"); }
    catch (failure) { error.textContent = failure.message; }
  });
  document.querySelector("#template-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (saving) return;
    setSaving(true); error.textContent = "";
    try {
      const saved = await api("/api/templates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        name: document.querySelector("#template-name").value,
        applicability: document.querySelector("#template-applicability").value,
        source: document.querySelector("#template-source").value,
        description: document.querySelector("#template-description").value, params: formObject(),
      }) });
      await loadProduct(saved.id); toast("协议已保存为新版本", "success");
    } catch (failure) { error.textContent = failure.message; }
    finally { setSaving(false); }
  });
  document.querySelector("#import-template").addEventListener("change", async event => {
    error.textContent = "";
    const file = event.target.files[0]; if (!file) return;
    if (saving) return;
    setSaving(true);
    try {
      if (file.size > 128 * 1024) throw new Error("模板文件不能超过 128 KiB。");
      let payload;
      try { payload = JSON.parse(await file.text()); }
      catch { throw new Error("无法读取模板，请选择导出的 JSON 模板文件。"); }
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("模板内容必须是一个 JSON 对象。");
      if (payload.schema_version !== 1) throw new Error("不支持的模板格式版本。");
      const saved = await api("/api/templates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      await loadProduct(saved.id); toast("模板已导入；应用前可先查看适用范围。", "success");
    } catch (failure) { error.textContent = failure.message; }
    finally { event.target.value = ""; setSaving(false); }
  });
}
