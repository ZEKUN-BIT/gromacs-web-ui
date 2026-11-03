import { els, state, FILE_FIELD_NAMES, api, toast, confirmDialog, showWorkspaceView, showDetailTab } from "./core.js?v=b71b8f0b4a932c44";
import { refreshJobs, selectJob } from "./jobs.js?v=b71b8f0b4a932c44";
import { product, loadProduct, bindLibrary, downloadJson } from "./library.js?v=b71b8f0b4a932c44";

let diagnostics = null;
let diagnosticsGeneration = 0;
let workspacePrepared = false, productLoading = false;
const composeSteps = ["inputs", "parameters", "resources"];
let composeStep = "inputs";

function showComposeStep(step, { focus = false } = {}) {
  composeStep = composeSteps.includes(step) ? step : "inputs";
  const expert = document.body.dataset.formMode === "expert";
  const index = composeSteps.indexOf(composeStep);
  document.querySelectorAll("[data-compose-panel]").forEach(panel => {
    panel.classList.toggle("compose-inactive", !expert && panel.dataset.composePanel !== composeStep);
  });
  document.querySelectorAll("[data-compose-anchor]").forEach(button => {
    if (!expert && button.dataset.composeAnchor === composeStep) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
  document.querySelector("#compose-previous").hidden = expert || index === 0;
  document.querySelector("#compose-next").hidden = expert || index === composeSteps.length - 1;
  document.querySelector("#compose-next").textContent = index === 0 ? "下一步：模拟设置" : "下一步：检查与运行";
  els.submitJob.hidden = !expert && composeStep !== "resources";
  document.querySelector("#compose-review").hidden = expert || composeStep !== "resources";
  document.querySelector("#manual-compose").dataset.composeStep = composeStep;
  const titles = { inputs: ["任务与体系", "选择输入文件，确认本次模拟的体系条件。"], parameters: ["模拟设置", "使用模板协议，或按需要调整模拟与分析选项。"], resources: ["检查与运行", "核对任务信息，分配本次计算使用的资源。"] };
  document.querySelector("#compose-stage-title").textContent = expert ? "协议与运行" : titles[composeStep][0];
  document.querySelector("#compose-stage-description").textContent = expert ? "完整的体系、协议与计算资源设置。" : titles[composeStep][1];
  const panels = [...document.querySelectorAll(".parameter-column > [data-compose-panel]")];
  document.querySelector("#compose-stage-empty").hidden = expert || panels.some(panel => !panel.classList.contains("compose-inactive") && !panel.hidden && [...panel.querySelectorAll("input,select,textarea")].some(field => field.getClientRects().length));
  if (focus) {
    const heading = document.querySelector("#compose-stage-title");
    heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
    document.querySelector(".parameter-column").scrollTop = 0;
    document.querySelector(".composer-toolbar").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function revealComposeField(field) {
  const panel = field.closest("[data-compose-panel]");
  if (panel) showComposeStep(panel.dataset.composePanel);
  for (let parent = field.parentElement; parent && parent !== els.jobForm; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
}
const families = { simulation: ["protein_md", "protein_ligand_md", "em_only"], analysis: ["analysis_rmsd", "postprocess", "analysis_suite"], tpr: ["run_tpr"], all: [] };
const advanced = new Set(["solvent_template", "box_type", "solvent_group", "ignh", "clean_pdb", "pre_nvt", "maxwarn", "production_deffnm", "verbose", "ntmpi", "pin", "protein_group", "complex_group", "water_ions_group", "restraint_fc", "atom_type", "charge_method", "custom_index_script", "center_output", "fit_output", "first_frame_output", "stdin_overrides"]);

function setMode(mode) {
  const expert = mode === "expert";
  document.body.dataset.formMode = expert ? "expert" : "guided";
  document.querySelector("#guided-mode").setAttribute("aria-pressed", String(!expert));
  document.querySelector("#expert-mode").setAttribute("aria-pressed", String(expert));
  document.querySelectorAll(".form-disclosure").forEach(section => { section.open = expert; });
  if (expert) document.querySelector("#workflow-intent").value = "all";
  try { localStorage.setItem("gromacs.form-mode", expert ? "expert" : "guided"); } catch { /* Browser storage is optional. */ }
  renderInputMapping(); filterWorkflows();
  showComposeStep(composeStep);
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
  const details = document.createElement("details");
  const heading = document.createElement("summary"); heading.textContent = `${files.length} 个文件已识别 · 查看输入映射`; details.append(heading);
  const list = document.createElement("ul");
  const workflow = els.jobForm.querySelector("input[name='workflow']:checked")?.value;
  for (const file of files) {
    const field = [...els.jobForm.querySelectorAll("input[name], [data-ligand-field]")].find(input => input.value === file.name && (FILE_FIELD_NAMES.has(input.name) || input.dataset.ligandField?.endsWith("_file")));
    const role = field?.closest("label")?.childNodes[0]?.textContent?.trim()
      || (/\.(pdb|gro)$/i.test(file.name) && workflow === "protein_md" ? "蛋白结构" : /\.mdp$/i.test(file.name) ? "协议参数" : /\.(top|itp)$/i.test(file.name) ? "拓扑参数" : "输入文件");
    const item = document.createElement("li"); item.textContent = `${role}：${file.name}`; list.append(item);
  }
  details.append(list); container.append(details);
}

export function refreshComposerSummary() {
  const summary = document.querySelector("#submission-summary");
  const protocol = els.protocolSummary?.querySelector("strong")?.textContent;
  summary.textContent = protocol || els.guideTitle.textContent;
  document.querySelector("#submission-readiness").textContent = els.guideHint.textContent;
  const review = document.querySelector("#compose-review-content");
  const form = els.jobForm;
  const distance = form.elements.namedItem("box_distance").value;
  const salt = form.elements.namedItem("ion_concentration").value;
  document.querySelector("#system-settings-summary").textContent = `${distance || "未设置"} nm 盒壁距离 · ${salt ? `${salt} mol/L 盐浓度` : "仅中和电荷"}`;
  const rows = [
    ["任务名称", form.elements.namedItem("name").value || "尚未命名"],
    ["工作流", els.guideTitle.textContent],
    ["输入文件", Array.from(els.files.files || []).map(file => file.name).join("、") || "尚未选择文件"],
    ["运行方式", form.elements.namedItem("dry_run").checked ? "只检查命令（Dry run）" : "执行计算"],
  ];
  review.replaceChildren(...rows.map(([label, value]) => {
    const row = document.createElement("div");
    const term = document.createElement("dt"); term.textContent = label;
    const definition = document.createElement("dd"); definition.textContent = value;
    row.append(term, definition); return row;
  }));
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
  if (productLoading) return;
  productLoading = true;
  if (!workspacePrepared) {
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
  workspacePrepared = true;
  }
  const initial = new Map([...els.jobForm.elements].map(field => [field, field.type === "checkbox" ? field.checked : field.value]));
  try {
    await loadProduct();
    document.querySelector("#product-availability")?.remove();
    for (const id of ["open-library", "project-filter", "open-help", "start-example"]) {
      const control = document.getElementById(id); control.disabled = false; control.removeAttribute("title");
    }
    document.querySelector(".new-job-organization").hidden = false;
    document.querySelector(".organization-panel").hidden = false;
    document.querySelector("#template-error").textContent = "";
  }
  catch (error) {
    const message = error.status === 404
      ? "当前服务仍在使用旧版本。新功能将在现有任务结束、重启服务后启用；任务监控与原有工作流可继续使用。"
      : "新增功能暂时无法连接服务。可重试连接，已填写参数和已选文件会保留。";
    document.querySelector("#product-availability")?.remove();
    const notice = document.createElement("p"); notice.className = "product-availability"; notice.id = "product-availability"; notice.setAttribute("role", "status"); notice.textContent = message;
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "tool-button"; retry.id = "retry-product-initialization"; retry.textContent = "重试连接";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      await prepareWorkspace();
      els.jobForm.dispatchEvent(new Event("change", { bubbles: true }));
    });
    notice.append(retry);
    document.querySelector(".workspace").prepend(notice);
    for (const id of ["open-library", "project-filter", "open-help", "start-example"]) {
      const control = document.getElementById(id); control.disabled = true; control.title = message;
    }
    if (error.status === 404) {
      document.querySelector(".new-job-organization").hidden = true;
      document.querySelector(".organization-panel").hidden = true;
    }
    document.querySelector("#template-error").textContent = message;
  }
  finally { productLoading = false; }
  // Apply late defaults only to untouched fields; restored drafts and edits win.
  for (const name of ["force_field", "water_model", "solvent_template", "box_type", "box_distance", "replicas", "verbose", "ignh", "clean_pdb", "pre_nvt", "release_restraints", "pin", "gpu"]) {
    const field = els.jobForm.elements.namedItem(name);
    const value = product.defaults[name];
    if (!field || value == null) continue;
    const unchanged = !field.dataset.userEdited && initial.get(field) === (field.type === "checkbox" ? field.checked : field.value);
    if (field.type === "checkbox") {
      const current = field.checked;
      field.defaultChecked = value;
      field.checked = unchanged ? value : current;
    } else {
      const current = field.value;
      if (field.tagName === "INPUT") field.defaultValue = String(value);
      if (unchanged && field.tagName === "SELECT" && ![...field.options].some(option => option.value === String(value))) field.add(new Option(String(value), String(value)));
      field.value = unchanged ? String(value) : current;
    }
  }
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
  els.jobForm.addEventListener("change", () => { filterWorkflows(); refreshComposerSummary(); showComposeStep(composeStep); });
  document.querySelector("#compose-previous").addEventListener("click", () => showComposeStep(composeSteps[composeSteps.indexOf(composeStep) - 1], { focus: true }));
  document.querySelector("#compose-next").addEventListener("click", () => showComposeStep(composeSteps[composeSteps.indexOf(composeStep) + 1], { focus: true }));
  els.jobForm.addEventListener("compose-reveal-field", event => revealComposeField(event.detail));
  els.jobForm.addEventListener("invalid", event => revealComposeField(event.target), true);
  new MutationObserver(refreshComposerSummary).observe(els.submitJob, { attributes: true, childList: true, subtree: true });
  new MutationObserver(refreshComposerSummary).observe(els.protocolSummary, { childList: true, subtree: true });
  document.querySelector("#start-own-data").addEventListener("click", () => { document.querySelector("#start-guide").open = false; showComposeStep("inputs"); els.files.focus(); els.dropzone.scrollIntoView({ block: "center", behavior: "smooth" }); });
  document.querySelectorAll("[data-compose-anchor]").forEach(button => button.addEventListener("click", () => {
    if (document.body.dataset.formMode !== "expert") showComposeStep(button.dataset.composeAnchor, { focus: true });
    else {
      const target = { inputs: ".form-column", parameters: ".mdp-section:not([hidden]), .parameter-column", resources: ".execution-section:not([hidden]), .submission-bar" }[button.dataset.composeAnchor];
      document.querySelector(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
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
