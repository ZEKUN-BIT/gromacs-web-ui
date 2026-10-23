import { els, state, FILE_FIELD_NAMES, api, toast, confirmDialog, showWorkspaceView, showDetailTab } from "./core.js?v=621bb939e2b97a80";
import { refreshJobs, selectJob } from "./jobs.js?v=621bb939e2b97a80";
import { product, loadProduct, bindLibrary, downloadJson } from "./library.js?v=621bb939e2b97a80";

let diagnostics = null;
let diagnosticsGeneration = 0;
const families = { simulation: ["protein_md", "protein_ligand_md", "em_only"], analysis: ["analysis_rmsd", "postprocess", "analysis_suite"], tpr: ["run_tpr"], all: [] };
const advanced = new Set(["solvent_template", "box_type", "solvent_group", "ignh", "clean_pdb", "pre_nvt", "maxwarn", "production_deffnm", "verbose", "ntmpi", "ntomp", "pin", "protein_group", "complex_group", "water_ions_group", "restraint_fc", "atom_type", "charge_method", "custom_index_script", "center_output", "fit_output", "first_frame_output", "stdin_overrides"]);

function setMode(mode) {
  const expert = mode === "expert";
  document.body.dataset.formMode = expert ? "expert" : "guided";
  document.querySelector("#guided-mode").setAttribute("aria-pressed", String(!expert));
  document.querySelector("#expert-mode").setAttribute("aria-pressed", String(expert));
  if (expert) document.querySelector("#workflow-intent").value = "all";
  try { localStorage.setItem("gromacs.form-mode", expert ? "expert" : "guided"); } catch { /* Browser storage is optional. */ }
  renderInputMapping(); filterWorkflows();
}

function filterWorkflows() {
  const intent = document.querySelector("#workflow-intent").value;
  const current = els.jobForm.querySelector("input[name='workflow']:checked")?.value;
  const family = families[intent] || [];
  for (const radio of els.jobForm.querySelectorAll("input[name='workflow']")) {
    radio.closest("label").hidden = family.length > 0 && !family.includes(radio.value) && radio.value !== current;
  }
}

export function renderInputMapping() {
  const container = document.querySelector("#input-mapping");
  if (!container) return;
  container.replaceChildren();
  const files = Array.from(els.files.files || []);
  if (!files.length) return;
  const heading = document.createElement("strong"); heading.textContent = `${files.length} 个文件已选择`; container.append(heading);
  const list = document.createElement("ul");
  const workflow = els.jobForm.querySelector("input[name='workflow']:checked")?.value;
  for (const file of files) {
    const field = [...els.jobForm.querySelectorAll("input[name], [data-ligand-field]")].find(input => input.value === file.name && (FILE_FIELD_NAMES.has(input.name) || input.dataset.ligandField?.endsWith("_file")));
    const role = field?.closest("label")?.childNodes[0]?.textContent?.trim()
      || (/\.(pdb|gro)$/i.test(file.name) && workflow === "protein_md" ? "蛋白结构" : /\.mdp$/i.test(file.name) ? "协议参数" : /\.(top|itp)$/i.test(file.name) ? "拓扑参数" : "输入文件");
    const item = document.createElement("li"); item.textContent = `${role}：${file.name}`; list.append(item);
  }
  container.append(list);
}

export function refreshComposerSummary() {
  const summary = document.querySelector("#submission-summary");
  const protocol = els.protocolSummary?.querySelector("strong")?.textContent;
  summary.textContent = protocol || els.guideTitle.textContent;
  document.querySelector("#submission-readiness").textContent = els.guideHint.textContent;
  const footer = document.querySelector("#submit-job-footer");
  footer.disabled = els.submitJob.disabled;
  footer.textContent = els.submitJob.querySelector(".button-label").textContent;
  renderInputMapping();
}

function addFieldHelp() {
  els.jobForm.querySelectorAll("label[title]").forEach(label => {
    const help = label.title;
    if (!help || label.closest(".workflow-switch") || label.querySelector(":scope > .field-help")) return;
    const button = document.createElement("button");
    button.type = "button"; button.className = "field-help"; button.textContent = "?";
    const title = label.querySelector(".field-label")?.textContent?.trim()
      || [...label.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim())?.textContent.trim();
    button.setAttribute("aria-label", `说明：${title || "此参数"}`);
    button.addEventListener("click", event => {
      event.preventDefault(); event.stopPropagation();
      document.querySelector("#field-help-content").textContent = help;
      document.querySelector("#field-help-dialog").showModal();
    });
    label.append(button);
  });
}

export async function prepareWorkspace() {
  try { await loadProduct(); }
  catch (error) {
    const message = error.status === 404
      ? "当前服务仍在使用旧版本。新功能将在现有任务结束、重启服务后启用；任务监控与原有工作流可继续使用。"
      : "新增功能暂时无法连接服务。任务监控与原有工作流可继续使用，请稍后刷新页面。";
    const notice = document.createElement("p"); notice.className = "product-availability"; notice.id = "product-availability"; notice.setAttribute("role", "status"); notice.textContent = message;
    document.querySelector(".workspace-bar").after(notice);
    for (const id of ["open-library", "project-filter", "open-help", "start-example"]) {
      const control = document.getElementById(id); control.disabled = true; control.title = message;
    }
    document.querySelector(".new-job-organization").hidden = true;
    document.querySelector(".organization-panel").hidden = true;
    document.querySelector("#template-error").textContent = message;
  }
  // Match fresh forms to backend defaults; saved drafts are restored afterwards.
  for (const name of ["force_field", "water_model", "solvent_template", "box_type", "box_distance", "replicas", "verbose", "ignh", "clean_pdb", "pre_nvt", "release_restraints", "pin", "gpu"]) {
    const field = els.jobForm.elements.namedItem(name);
    const value = product.defaults[name];
    if (!field || value == null) continue;
    if (field.type === "checkbox") { field.checked = value; field.defaultChecked = value; }
    else { field.value = String(value); if (field.tagName === "INPUT") field.defaultValue = String(value); }
  }
  els.jobForm.querySelectorAll("input[name],select[name],textarea[name]").forEach(field => {
    if (advanced.has(field.name)) field.closest("label")?.classList.add("advanced-field");
  });
  const parameters = document.querySelector(".parameter-column");
  parameters.querySelector('[data-section="ligand"]').classList.add("input-section");
  parameters.querySelector('[data-section="post"]').classList.add("trajectory-section");
  parameters.querySelector('[data-section="interaction"]').classList.add("advanced-section");
  let mode = "guided";
  try { mode = localStorage.getItem("gromacs.form-mode") || mode; } catch { /* Optional storage. */ }
  setMode(mode); addFieldHelp();
  new MutationObserver(addFieldHelp).observe(els.ligandList, { childList: true, subtree: true });
}

export function bindWorkspace() {
  bindLibrary();
  document.querySelectorAll("[data-close-dialog]").forEach(button => button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close()));
  document.querySelector("#guided-mode").addEventListener("click", () => setMode("guided"));
  document.querySelector("#expert-mode").addEventListener("click", () => setMode("expert"));
  document.querySelector("#workflow-intent").addEventListener("change", event => {
    const family = families[event.target.value];
    const selected = els.jobForm.querySelector("input[name='workflow']:checked");
    if (family.length && !family.includes(selected.value)) {
      const next = els.jobForm.querySelector(`input[name='workflow'][value='${family[0]}']`);
      next.checked = true; next.dispatchEvent(new Event("change", { bubbles: true }));
    }
    filterWorkflows();
  });
  els.jobForm.addEventListener("change", () => { filterWorkflows(); refreshComposerSummary(); });
  document.querySelector("#submit-job-footer").addEventListener("click", () => els.submitJob.click());
  new MutationObserver(refreshComposerSummary).observe(els.submitJob, { attributes: true, childList: true, subtree: true });
  new MutationObserver(refreshComposerSummary).observe(els.protocolSummary, { childList: true, subtree: true });
  document.querySelector("#start-own-data").addEventListener("click", () => { document.querySelector("#start-guide").open = false; els.files.focus(); els.dropzone.scrollIntoView({ block: "center", behavior: "smooth" }); });
  document.querySelectorAll("[data-compose-anchor]").forEach(button => button.addEventListener("click", () => {
    const target = { inputs: ".form-column", parameters: ".mdp-section:not([hidden]), .parameter-column", resources: ".execution-section:not([hidden]), .submission-bar" }[button.dataset.composeAnchor];
    document.querySelector(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  document.querySelector("#project-filter").addEventListener("change", async event => {
    state.jobProject = event.target.value; state.jobsOffset = 0;
    await refreshJobs().catch(error => toast(error.message, "error"));
    window.dispatchEvent(new CustomEvent("jobs-page-change"));
  });
  document.querySelector("#start-example").addEventListener("click", async event => {
    if (!await confirmDialog({ title: "运行教学示例？", subtitle: "会创建独立任务，使用 1 个 CPU 线程运行 1 ps 氩体系", items: ["用于体验提交、监控、图表与导出。", "这是教学体系，不是蛋白模拟的推荐协议。"], confirmText: "创建示例任务" })) return;
    const button = event.target; button.disabled = true;
    try {
      const job = await api("/api/examples/start", { method: "POST" });
      state.jobFilter = ""; state.jobProject = ""; state.jobsOffset = 0; els.jobFilter.value = ""; document.querySelector("#project-filter").value = "";
      await refreshJobs(); await selectJob(job.id); showDetailTab("overview"); showWorkspaceView("detail");
      // An empty history auto-selects its first job during refreshJobs(), so
      // selectJob() may return without reconnecting the task's event stream.
      window.dispatchEvent(new CustomEvent("active-job-change"));
      await loadProduct();
    } catch (error) { toast(error.message, "error"); } finally { button.disabled = false; }
  });
  const help = document.querySelector("#help-dialog");
  let helpGeneration = 0;
  const loadHelp = async () => {
    const generation = ++helpGeneration;
    const target = document.querySelector("#help-content"); target.textContent = "正在读取文档…";
    try {
      const text = await api(`/api/help/document?name=${encodeURIComponent(document.querySelector("#help-document").value)}`);
      if (generation === helpGeneration) target.textContent = text;
    } catch (error) { if (generation === helpGeneration) target.textContent = error.message; }
  };
  document.querySelector("#open-help").addEventListener("click", () => { help.showModal(); loadHelp(); });
  document.querySelector("#help-document").addEventListener("change", loadHelp);
  document.querySelector("#preview-diagnostics").addEventListener("click", async event => {
    const generation = ++diagnosticsGeneration;
    event.target.disabled = true;
    document.querySelector("#diagnostic-preview").textContent = "正在读取环境诊断…";
    document.querySelector("#download-diagnostics").disabled = true;
    try {
      const payload = await api("/api/support/diagnostics");
      if (generation !== diagnosticsGeneration) return;
      diagnostics = payload;
      document.querySelector("#diagnostic-preview").textContent = JSON.stringify(diagnostics, null, 2);
      document.querySelector("#download-diagnostics").disabled = false;
    }
    catch (error) { if (generation === diagnosticsGeneration) document.querySelector("#diagnostic-preview").textContent = error.message; }
    finally { event.target.disabled = false; }
  });
  document.querySelector("#download-diagnostics").addEventListener("click", () => { if (diagnostics) downloadJson("gromacs-diagnostics.json", diagnostics); });
  document.querySelector("#show-local-usage").addEventListener("click", async () => {
    const generation = ++diagnosticsGeneration;
    document.querySelector("#diagnostic-preview").textContent = "正在读取本机使用记录…";
    document.querySelector("#download-diagnostics").disabled = true;
    try {
      const usage = await api("/api/support/usage");
      if (generation === diagnosticsGeneration) document.querySelector("#diagnostic-preview").textContent = JSON.stringify(usage, null, 2);
    }
    catch (error) { if (generation === diagnosticsGeneration) document.querySelector("#diagnostic-preview").textContent = error.message; }
  });
}
