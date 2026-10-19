// 环境诊断、力场水模型与硬件配置建议。

import { $, els, state, escapeHtml, updateText, toast, api } from "./core.js?v=58a36b0fc94d0be8";

function setHealthCard(dotClass, title, detail) {
  const dot = els.healthCard.querySelector(".dot");
  dot.className = `dot ${dotClass}`;
  updateText(els.healthCard.querySelector("strong"), title);
  updateText(els.healthCard.querySelector("small"), detail);
}

export function renderReadiness() {
  const payload = state.healthPayload;
  if (!payload) return;
  const diagnostics = payload.diagnostics || {};
  const workflow = $("input[name='workflow']:checked")?.value;
  const missing = [];
  if (!payload.gromacs?.available) missing.push("GROMACS");
  const parameterizing = workflow === "protein_ligand_md"
    ? [...document.querySelectorAll('.ligand-card')].filter(card => card.querySelector('[data-ligand-field="mode"]')?.value === "acpype") : [];
  if (parameterizing.length) {
    if (!diagnostics.acpype?.available) missing.push("ACPYPE");
    if (!diagnostics.ambertools?.available) missing.push("AmberTools");
    const needsBabel = $("input[name='use_obabel']")?.checked || parameterizing.some(card =>
      card.querySelector('[data-ligand-field="chemistry_file"]')?.value.trim() || card.querySelector('[data-ligand-field="smiles"]')?.value.trim());
    if (needsBabel && !diagnostics.open_babel?.available) missing.push("Open Babel");
  }
  if (workflow === "analysis_suite" && $("input[name='do_dssp']")?.checked && diagnostics.gromacs_dssp?.available === false) missing.push("GROMACS DSSP 子命令");
  const workerNote = payload.worker?.available === false ? "；执行服务未启动，提交后将等待运行" : "";
  const text = missing.length ? `请配置 ${missing.join("、")}` : `当前流程所需工具可用${workerNote}`;
  setHealthCard(missing.length ? "bad" : "ok", missing.length ? "运行环境待完善" : "运行环境已就绪", text);
  const onboarding = document.querySelector("#onboarding-environment");
  if (onboarding) onboarding.textContent = payload.gromacs?.available ? `GROMACS 可用${workerNote}，可运行教学示例。` : "尚未检测到 GROMACS。可先查看离线示例，或打开帮助配置环境。";
}

function renderForceFieldOptions(forceFields) {
  if (!els.forceFieldOptions) return;
  const common = ["amber19sb", "amber99sb-ildn", "charmm36-jul2022", "oplsaa"];
  const localNames = forceFields.map((item) => item.name).filter(Boolean);
  const html = [
    ...common.map((name) => `<option value="${escapeHtml(name)}" label="GROMACS 官方自带（优先）"></option>`),
    ...localNames
      .filter((name) => !common.some((systemName) => normalizeForceFieldName(systemName).toLowerCase() === normalizeForceFieldName(name).toLowerCase()))
      .map((name) => `<option value="${escapeHtml(name)}" label="项目附加力场"></option>`),
  ].join("");
  if (els.forceFieldOptions.dataset.signature !== html) {
    els.forceFieldOptions.dataset.signature = html;
    els.forceFieldOptions.innerHTML = html;
  }

  renderForceFieldSource();
}

function normalizeForceFieldName(value) {
  return String(value || "")
    .replace(/\\/g, "/")
    .split("/")
    .pop()
    .replace(/\.ff$/i, "")
    .trim();
}

export function renderForceFieldSource() {
  if (!els.forceFieldSource) return;
  const selected = normalizeForceFieldName(els.jobForm?.querySelector("input[name='force_field']")?.value).toLowerCase();
  const isLocal = state.localForceFields.some((item) => normalizeForceFieldName(item.name).toLowerCase() === selected);
  updateText(els.forceFieldSource, isLocal ? "项目附加力场" : "GROMACS 官方自带");
}

export function renderWaterModelOptions() {
  if (!els.waterModel) return;
  const current = els.waterModel.value || "opc";
  const selectedForceField = normalizeForceFieldName($("input[name='force_field']")?.value || "amber19sb").toLowerCase();
  const localForceField = state.localForceFields.find((item) => normalizeForceFieldName(item.name).toLowerCase() === selectedForceField);
  const localModels = (localForceField?.water_models || []).map((model) => model.name).filter(Boolean);
  const common = ["opc", "opc3", "tip3p", "spce", "tip4p", "tip5p", "tips3p", "none"];
  const unique = [...new Set([current, ...localModels, ...common])];
  const html = unique.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  if (els.waterModel.dataset.signature !== html) {
    els.waterModel.dataset.signature = html;
    els.waterModel.innerHTML = html;
    els.waterModel.value = current;
  }
}

function applyRuntimeDefaults(settings) {
  [
    ["ntmpi", "default_ntmpi"],
    ["ntomp", "default_ntomp"],
  ].forEach(([name, key]) => {
    const field = els.jobForm?.querySelector(`[name='${name}']`);
    const value = settings?.[key];
    if (!field || !value) return;
    const current = String(field.value || "").trim();
    const shouldApply =
      !field.dataset.userEdited &&
      (current === "" || current === field.defaultValue || field.dataset.runtimeDefaultApplied === "true");
    if (!shouldApply) return;
    field.value = String(value);
    field.dataset.runtimeDefaultApplied = "true";
  });
}

export function updateResourceAdvice() {
  if (!els.resourceAdvice) return;
  const recommendation = state.performanceRecommendation;
  if (!recommendation) {
    updateText(els.resourceAdvice.querySelector("span"), "正在检测本机算力…");
    return;
  }
  const ntmpi = Math.max(1, Number(els.jobForm?.querySelector("[name='ntmpi']")?.value) || 1);
  const ntomp = Math.max(1, Number(els.jobForm?.querySelector("[name='ntomp']")?.value) || 1);
  const parallel = Math.max(1, Number(els.maxParallel?.value) || 1);
  const gpu = Boolean(els.jobForm?.querySelector("[name='gpu']")?.checked);
  const logicalCpus = Math.max(recommendation.ntomp, Number(recommendation.logical_cpus) || recommendation.ntomp);
  const cpuDemand = ntmpi * ntomp * parallel;
  const warnings = [];
  if (cpuDemand > logicalCpus) warnings.push(`CPU 线程需求 ${cpuDemand} 超过 ${logicalCpus} 个逻辑 CPU`);
  if (gpu && !recommendation.gpu) warnings.push("当前环境尚未验证 GPU 加速可用");
  if (gpu && parallel > Math.max(1, recommendation.gpu_count || 0)) warnings.push("多个 GPU 任务可能争用同一张 GPU");

  const workflow = $("input[name='workflow']:checked")?.value;
  let trajectoryNote = "";
  if (["protein_md", "protein_ligand_md"].includes(workflow)) {
    const mdNs = Number(els.jobForm?.querySelector("[name='mdp_md_ns']")?.value) || 100;
    const intervalPs = Number(els.jobForm?.querySelector("[name='mdp_output_interval_ps']")?.value) || 10;
    const replicas = Math.max(1, Number(els.jobForm?.querySelector("[name='replicas']")?.value) || 1);
    const frames = Math.ceil((mdNs * 1000) / intervalPs + 1) * replicas;
    if (frames >= 10000) trajectoryNote = `；预计约 ${frames.toLocaleString()} 个全体系轨迹帧，注意磁盘占用`;
  }
  const prefix = `推荐 ${recommendation.ntmpi}×${recommendation.ntomp}、pin ${recommendation.pin}${recommendation.gpu ? "、GPU" : "、CPU"}`;
  const applied = ntmpi === recommendation.ntmpi && ntomp === recommendation.ntomp && gpu === Boolean(recommendation.gpu)
    && els.jobForm?.querySelector("[name='pin']")?.value === recommendation.pin;
  const current = `当前 ${ntmpi}×${ntomp}、${gpu ? "GPU" : "CPU"}`;
  updateText(els.resourceAdvice.querySelector("span"), `${current}；${warnings.length ? `⚠ ${warnings.join("；")}` : applied ? "已应用推荐配置" : prefix}${trajectoryNote}`);
  els.resourceAdvice.dataset.state = warnings.length ? "warning" : "ok";
}

export function applyHardwareRecommendation() {
  const recommendation = state.performanceRecommendation;
  if (!recommendation) return;
  const values = { ntmpi: recommendation.ntmpi, ntomp: recommendation.ntomp, pin: recommendation.pin };
  Object.entries(values).forEach(([name, value]) => {
    const field = els.jobForm?.querySelector(`[name='${name}']`);
    if (!field) return;
    field.value = String(value);
    field.dataset.userEdited = "true";
  });
  const gpu = els.jobForm?.querySelector("[name='gpu']");
  if (gpu) {
    gpu.checked = Boolean(recommendation.gpu);
    gpu.dataset.userEdited = "true";
  }
  updateResourceAdvice();
  toast("已应用本机推荐配置，请用实际 ns/day 验证。", "success");
  return true;
}

export async function refreshHealth() {
  try {
    const payload = await api("/api/health");
    state.healthPayload = payload;
    els.gmxBin.value = payload.settings.gmx_bin || "gmx";
    if (els.maxParallel) els.maxParallel.value = String(payload.settings.max_parallel || 1);
    applyRuntimeDefaults(payload.settings || {});
    updateText(els.runtimePath, payload.settings.runtime_root || "runtime");
    state.localForceFields = payload.local_force_fields || [];
    renderForceFieldOptions(state.localForceFields);
    renderWaterModelOptions();
    const diagnostics = payload.diagnostics || {};
    const recommendation = diagnostics.performance_recommendation || null;
    state.performanceRecommendation = recommendation
      ? { ...recommendation, logical_cpus: diagnostics.host?.cpu_count }
      : null;
    updateResourceAdvice();
    if (els.environmentDiagnostics) {
      const rows = [
        ["GROMACS", diagnostics.gromacs],
        ["ACPYPE", diagnostics.acpype],
        ["Open Babel", diagnostics.open_babel],
        ["AmberTools", diagnostics.ambertools],
        ["GROMACS DSSP", diagnostics.gromacs_dssp],
        ["外部 mkdssp（可选）", diagnostics.dssp],
        ["NVIDIA GPU", diagnostics.gpu],
        ["GPU 加速", { available: Boolean(recommendation?.gpu), reason: diagnostics.gpu?.reason }],
        ["力场", diagnostics.force_fields],
      ];
      const waterReady = Object.entries(diagnostics.water_models || {})
        .filter(([, item]) => item.available)
        .map(([name]) => name)
        .join(", ");
      els.environmentDiagnostics.innerHTML = `${rows
        .map(
          ([label, item]) =>
            `<div title="${escapeHtml(item?.reason || "")}"><span>${escapeHtml(label)}</span><strong data-state="${item?.available ? "ok" : "bad"}">${item?.available ? "可用" : "不可用"}</strong></div>`,
        )
        .join("")}<div><span>水模型</span><strong data-state="${waterReady ? "ok" : "bad"}">${escapeHtml(waterReady || "未识别")}</strong></div>`;
    }
    renderReadiness();
  } catch (error) {
    setHealthCard("bad", "健康检查失败", error.message);
    throw error;
  }
}
