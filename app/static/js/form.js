// 表单模块：工作流指南、文件字段联动、提交前校验、健康检查、命令预览。

import {
  $,
  els,
  state,
  escapeHtml,
  updateText,
  debounce,
  toast,
  api,
  replaySwapAnimation,
} from "./core.js?v=58a36b0fc94d0be8";

import {
  hasFile, fileNameEndsWith, fileParamEndsWith, uploadedNames, selectedUploadFiles,
} from "./form-files.js?v=58a36b0fc94d0be8";
import {
  ligandCards, addLigandRow, ensureDefaultLigandRow, readLigandPayload, refreshRowMode, setLigandCardField,
  removeLigandRow as removeLigandEditorRow, detectLigandsFromPdb as detectPdbLigands,
} from "./form-ligands.js?v=58a36b0fc94d0be8";
import {
  setMdpPanelExpanded, updateMdpPanelState, refreshProtocolPreview, renderMdpPreview,
} from "./form-parameters.js?v=58a36b0fc94d0be8";
import {
  renderForceFieldSource, renderWaterModelOptions, refreshHealth, updateResourceAdvice,
  applyHardwareRecommendation as applyHardwareDefaults,
  renderReadiness,
} from "./form-environment.js?v=58a36b0fc94d0be8";

export {
  hasFile, fileNameEndsWith, fileParamEndsWith, uploadedNames, selectedUploadFiles,
  ligandCards, addLigandRow, ensureDefaultLigandRow, readLigandPayload, refreshRowMode,
  setMdpPanelExpanded, updateMdpPanelState,
  renderForceFieldSource, renderWaterModelOptions, refreshHealth, updateResourceAdvice,
};

export function removeLigandRow(index) {
  if (removeLigandEditorRow(index)) refreshPreview();
}

export async function detectLigandsFromPdb() {
  if (await detectPdbLigands()) refreshPreview();
}

export function applyHardwareRecommendation() {
  if (applyHardwareDefaults()) debouncedPreview();
}

export const WORKFLOW_GUIDES = {
  protein_md: {
    title: "蛋白水溶液模拟",
    readyHint: "输入齐了。看一眼命令预览后就可以提交。",
    notes: ["EM 收敛后才进入平衡；质量门失败会停止流程。", "默认依次执行带位置限制与无限制的 C-rescale NPT。", "生产使用 Parrinello–Rahman NPT，并为各副本重新生成速度。"],
    steps: ["拓扑与盒子", "溶剂化与离子", "能量最小化", "限制 NPT", "无限制 NPT", "多副本生产 MD", "完成后分析与作图"],
    requirements: [
      {
        key: "structure",
        label: "蛋白 PDB/GRO",
        missing: "上传 .pdb/.gro 结构文件，或填写结构文件名。",
        test: (data) => hasFile(data, [".pdb", ".gro"]) || Boolean(data.structure_file),
      },
    ],
  },
  protein_ligand_md: {
    title: "蛋白-配体复合物模拟",
    readyHint: "输入齐了。提交前最好再看一遍配体拓扑、index 脚本和 mdp/complex。",
    notes: ["先确认配体质子化、电荷和拓扑，再组装复合物。", "EM 后依次通过限制 NPT、无限制 NPT 与稳定性检查。", "生产使用独立速度的多副本 NPT，并执行轨迹整理与分析。"],
    steps: ["蛋白拓扑", "配体参数化", "复合物与溶剂", "能量最小化", "分阶段 NPT", "多副本生产 MD", "完成后分析与作图"],
    requirements: [
      {
        key: "protein",
        label: "蛋白 PDB/GRO",
        missing: "上传蛋白 .pdb/.gro，或填写蛋白文件名。",
        test: (data) => hasFile(data, [".pdb", ".gro"]) || Boolean(data.protein_file),
      },
      {
        key: "ligand",
        label: "配体来源",
        missing: "每个配体都要有来源：上传 .gro/.itp、SDF/MOL2，输入 SMILES，或开启复合物 PDB 自动拆分。",
        test: (data) => {
          const ligands = data.ligands || [];
          if (!ligands.length) return false;
          const hasPdb = hasFile(data, [".pdb"]) || String(data.protein_file || "").toLowerCase().endsWith(".pdb");
          return ligands.every(
            (ligand) =>
              Boolean(ligand.gro_file && ligand.itp_file) ||
              Boolean(ligand.structure_file || ligand.chemistry_file || ligand.smiles) ||
              Boolean(data.auto_split_complex_pdb && hasPdb),
          );
        },
      },
    ],
  },
  em_only: {
    title: "单独能量最小化",
    readyHint: "输入齐了。确认 minim.mdp 和拓扑名后即可生成 EM 任务。",
    notes: ["拓扑名默认按 topol.top 处理。", "优先使用 mdp/protein/minim.mdp；上传 minim.mdp 会覆盖模板。", "EM 只检查结构和拓扑能否进入 grompp/mdrun。"],
    steps: ["读取结构", "读取拓扑", "grompp", "mdrun em"],
    requirements: [
      {
        key: "structure",
        label: "结构 PDB/GRO",
        missing: "上传 .pdb/.gro 结构文件。",
        test: (data) => hasFile(data, [".pdb", ".gro"]) || Boolean(data.structure_file),
      },
      { key: "topology", label: "topol.top", missing: "上传 .top 拓扑文件。", test: (data) => hasFile(data, [".top"]) },
    ],
  },
  run_tpr: {
    title: "直接运行 TPR",
    readyHint: "输入齐了。确认线程数后可直接运行 TPR。",
    notes: ["TPR 已包含模拟参数和拓扑信息。", "线程数只影响 mdrun 调用，不重写 TPR。", "输出文件会保留在该任务的工作目录中。"],
    steps: ["上传 TPR", "选择线程", "mdrun", "下载输出"],
    requirements: [
      { key: "tpr", label: "TPR 文件", missing: "上传 .tpr 文件，或填写 TPR 文件名。", test: (data) => hasFile(data, [".tpr"]) || Boolean(data.tpr_file) },
    ],
  },
  analysis_rmsd: {
    title: "RMSD 分析",
    readyHint: "输入齐了。确认拟合组与输出组后即可分析 RMSD。",
    notes: ["TPR 与轨迹必须来自兼容体系。", "拟合组名称要存在于默认或上传的 index 中。", "输出为 XVG，可用外部工具继续绘图。"],
    steps: ["上传 TPR", "上传轨迹", "选择拟合组", "输出 XVG"],
    requirements: [
      { key: "tpr", label: "TPR 文件", missing: "上传 .tpr 文件，或填写 TPR 文件名。", test: (data) => hasFile(data, [".tpr"]) || Boolean(data.tpr_file) },
      {
        key: "traj",
        label: "XTC/TRR 轨迹",
        missing: "上传 .xtc 或 .trr 轨迹文件，或填写轨迹文件名。",
        test: (data) => hasFile(data, [".xtc", ".trr"]) || Boolean(data.trajectory_file),
      },
    ],
  },
  postprocess: {
    title: "轨迹修正与拟合",
    readyHint: "输入齐了。按需要开启居中、拟合和首帧导出。",
    notes: ["PBC 居中适合先整理跨盒分子。", "拟合组通常使用 Backbone 或 C-alpha。", "首帧导出用于检查后处理后的空间位置。"],
    steps: ["PBC 修正", "居中", "旋转平移拟合", "提取首帧"],
    requirements: [
      { key: "tpr", label: "TPR 文件", missing: "上传 .tpr 文件，或填写 TPR 文件名。", test: (data) => hasFile(data, [".tpr"]) || Boolean(data.tpr_file) },
      {
        key: "traj",
        label: "XTC/TRR 轨迹",
        missing: "上传 .xtc 或 .trr 轨迹文件，或填写轨迹文件名。",
        test: (data) => hasFile(data, [".xtc", ".trr"]) || Boolean(data.trajectory_file),
      },
    ],
  },
  analysis_suite: {
    title: "批量轨迹分析",
    readyHint: "输入齐了。选择要跑的分析模块；缺少外部工具时日志会写清楚。",
    notes: ["DSSP 使用 GROMACS 自带的 dssp 子命令，请确认环境检测可用。", "PCA/SHAM 计算更重，建议先跑 RMSD/Rg 快速检查。", "能量项名称要与 EDR 中的实际项匹配。"],
    steps: ["能量 / RMSD", "Rg / 氢键", "DSSP", "PCA / SHAM"],
    requirements: [
      { key: "tpr", label: "TPR 文件", missing: "上传 .tpr 文件，或填写 TPR 文件名。", test: (data) => hasFile(data, [".tpr"]) || Boolean(data.tpr_file) },
      {
        key: "traj",
        label: "XTC/TRR 轨迹",
        missing: "上传 .xtc 或 .trr 轨迹文件，或填写轨迹文件名。",
        test: (data) => hasFile(data, [".xtc", ".trr"]) || Boolean(data.trajectory_file),
      },
    ],
  },
  custom: {
    title: "自定义 GROMACS 命令",
    readyHint: "命令已填写。确认子命令在允许范围内后提交。",
    notes: ["命令必须以当前 gmx 路径开头。", "这里只放开常用安全子命令。", "命令需要的输入文件也要一起上传。"],
    steps: ["填写命令", "检查预览", "提交", "查看日志"],
    requirements: [
      { key: "command", label: "gmx 命令", missing: "填写以 gmx 开头的命令。", test: (data) => Boolean(data.custom_command?.trim()) },
    ],
  },
};

export function formObject() {
  const workflow = $("input[name='workflow']:checked")?.value || "protein_md";
  const payload = { workflow, gmx_bin: els.gmxBin.value.trim() || "gmx" };
  els.jobForm
    .querySelectorAll("input[name], select[name], textarea[name]")
    .forEach((field) => {
      if (field.type === "file" || field.type === "radio") return;
      if (field.type === "checkbox") {
        payload[field.name] = field.checked;
        return;
      }
      if (field.type === "number") {
        payload[field.name] = field.name.startsWith("mdp_") || field.name === "dump_time"
          ? field.value
          : field.value === "" ? "" : Number(field.value);
        return;
      }
      payload[field.name] = field.value;
    });
  payload.files = selectedUploadFiles().map((file) => file.name);
  payload.tags = String(payload.tags || "").split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
  if (!state.productAvailable) {
    for (const name of ["project", "tags", "notes"]) delete payload[name];
  }
  payload.ligands = readLigandPayload();
  return payload;
}

export function findUploadedName(suffixes, { exclude = [], prefer = [] } = {}) {
  const excluded = new Set(exclude.filter(Boolean));
  const names = uploadedNames().filter((name) => !excluded.has(name) && fileNameEndsWith(name, suffixes));
  for (const token of prefer) {
    const matched = names.find((name) => name.toLowerCase().includes(token.toLowerCase()));
    if (matched) return matched;
  }
  return names[0] || "";
}

function setAutoFileField(name, value, selectedNames, changed) {
  if (!value) return;
  const field = els.jobForm.querySelector(`[name='${name}']`);
  if (!field) return;
  const current = field.value.trim();
  const shouldReplace = !current || field.dataset.autoUpload === "true" || !selectedNames.has(current);
  if (!shouldReplace || current === value) return;
  field.value = value;
  field.dataset.autoUpload = "true";
  field.classList.add("auto-filled");
  window.setTimeout(() => field.classList.remove("auto-filled"), 900);
  changed.push(`${field.closest("label")?.childNodes[0]?.textContent?.trim() || name}: ${value}`);
}

function clearAutoFileField(name, changed) {
  const field = els.jobForm.querySelector(`[name='${name}']`);
  if (!field || field.dataset.autoUpload !== "true" || !field.value) return;
  field.value = "";
  changed.push(name);
}

export function syncFileFieldsFromUploads({ notify = false } = {}) {
  const names = uploadedNames();
  if (!names.length) return;
  const workflow = $("input[name='workflow']:checked")?.value || "protein_md";
  const selectedNames = new Set(names);
  const changed = [];

  if (workflow === "protein_md" || workflow === "em_only") {
    setAutoFileField("structure_file", findUploadedName([".pdb", ".gro"], { prefer: [".pdb"] }), selectedNames, changed);
  }

  if (workflow === "protein_ligand_md") {
    const protein = findUploadedName([".pdb", ".gro"], { prefer: [".pdb"] });
    setAutoFileField("protein_file", protein, selectedNames, changed);
    const groName = findUploadedName([".gro"], { exclude: [protein], prefer: ["_gmx", "lig"] });
    const itpName = findUploadedName([".itp"], { prefer: ["_gmx", "lig"] });
    const chemistryReference = findUploadedName([".sdf", ".mol2"], { prefer: ["lig"] });
    const splitComplex = Boolean(els.jobForm.querySelector("[name='auto_split_complex_pdb']")?.checked && protein?.toLowerCase().endsWith(".pdb"));
    ligandCards().forEach((card) => {
      const mode = card.querySelector("[data-ligand-field='mode']")?.value || "prepared";
      if (mode === "prepared") {
        setLigandCardField(card, "gro_file", groName, selectedNames, changed);
        setLigandCardField(card, "itp_file", itpName, selectedNames, changed);
      } else if (splitComplex && chemistryReference) {
        setLigandCardField(card, "chemistry_file", chemistryReference, selectedNames, changed);
      } else if (!splitComplex) {
        setLigandCardField(card, "structure_file", chemistryReference, selectedNames, changed);
      }
    });
  }

  if (workflow === "run_tpr" || workflow === "analysis_rmsd" || workflow === "postprocess" || workflow === "analysis_suite") {
    setAutoFileField("tpr_file", findUploadedName([".tpr"]), selectedNames, changed);
  }
  if (workflow === "analysis_rmsd" || workflow === "postprocess" || workflow === "analysis_suite") {
    setAutoFileField("trajectory_file", findUploadedName([".xtc", ".trr"]), selectedNames, changed);
    setAutoFileField("index_file", findUploadedName([".ndx"]), selectedNames, changed);
  }
  if (workflow === "analysis_suite") {
    setAutoFileField("edr_file", findUploadedName([".edr"]), selectedNames, changed);
  }

  if (notify && changed.length) {
    toast(`已根据新上传文件更新：${changed.slice(0, 3).join("；")}${changed.length > 3 ? "…" : ""}`, "success");
  }
}

function uploadedFileIssue(data, fieldName, label, allowGenerated = []) {
  const value = String(data[fieldName] || "").trim();
  if (!value || (data.files || []).includes(value) || allowGenerated.includes(value)) return "";
  return `${label} 填的是 ${value}，但本次上传里没有这个文件。请重新拖入文件，或清空字段后再选。`;
}

function uploadedFileIssueValue(value, data, label) {
  const text = String(value || "").trim();
  if (!text || (data.files || []).includes(text) || /_from_complex\.pdb$/.test(text)) return "";
  return `${label} 填的是 ${text}，但本次上传里没有这个文件。请重新拖入文件，或清空字段后再选。`;
}

function staleUploadedFileIssues(data) {
  const issues = [];
  if (data.workflow === "protein_md" || data.workflow === "em_only") {
    const issue = uploadedFileIssue(data, "structure_file", "结构文件");
    if (issue) issues.push(issue);
  }
  if (data.workflow === "protein_ligand_md") {
    const proteinIssue = uploadedFileIssue(data, "protein_file", "蛋白文件");
    if (proteinIssue) issues.push(proteinIssue);
    (data.ligands || []).forEach((ligand, index) => {
      const label = `配体 ${index + 1}${ligand.key ? `（${ligand.key}）` : ""}`;
      const mode = ligand.mode || "prepared";
      if (mode === "prepared" || ligand.gro_file || ligand.itp_file) {
        const groIssue = uploadedFileIssueValue(ligand.gro_file, data, `${label} .gro`);
        if (groIssue) issues.push(groIssue);
        const itpIssue = uploadedFileIssueValue(ligand.itp_file, data, `${label} .itp`);
        if (itpIssue) issues.push(itpIssue);
      }
      const structureIssue = uploadedFileIssueValue(ligand.structure_file, data, `${label} 结构文件`);
      if (structureIssue) issues.push(structureIssue);
      const chemistryIssue = uploadedFileIssueValue(ligand.chemistry_file, data, `${label} 化学文件`);
      if (chemistryIssue) issues.push(chemistryIssue);
    });
  }
  if (data.workflow === "run_tpr" || data.workflow === "analysis_rmsd" || data.workflow === "postprocess" || data.workflow === "analysis_suite") {
    const issue = uploadedFileIssue(data, "tpr_file", "TPR 文件");
    if (issue) issues.push(issue);
  }
  if (data.workflow === "analysis_rmsd" || data.workflow === "postprocess" || data.workflow === "analysis_suite") {
    ["trajectory_file", "index_file"].forEach((field) => {
      const labels = { trajectory_file: "轨迹文件", index_file: "index 文件" };
      const issue = uploadedFileIssue(data, field, labels[field]);
      if (issue) issues.push(issue);
    });
  }
  if (data.workflow === "analysis_suite" && data.do_energy) {
    const issue = uploadedFileIssue(data, "edr_file", "EDR 文件");
    if (issue) issues.push(issue);
  }
  return issues;
}

function hasPdbProtein(data) {
  return hasFile(data, [".pdb"]) || fileParamEndsWith(data.protein_file, [".pdb"]);
}

function ligandWillUseAcpype(data, ligand) {
  if (data.workflow !== "protein_ligand_md") return false;
  if ((ligand.mode || "prepared") === "acpype") return true;
  const split = Boolean(data.auto_split_complex_pdb && hasPdbProtein(data));
  const hasPrepared = Boolean(ligand.gro_file && ligand.itp_file);
  const hasChemistry = Boolean(ligand.chemistry_file || ligand.smiles);
  if (split && hasChemistry) return true;
  if (split && !hasPrepared && !(ligand.structure_file || ligand.chemistry_file || ligand.smiles)) return true;
  return false;
}

function willUseAcpype(data) {
  if (data.workflow !== "protein_ligand_md") return false;
  return (data.ligands || []).some((ligand) => ligandWillUseAcpype(data, ligand));
}

export function requiredSubmitIssues(data) {
  const issues = staleUploadedFileIssues(data);
  if (!data.dry_run && Number(data.maxwarn || 0) > 0) {
    issues.push("真实模拟要求 maxwarn = 0。请解决 grompp 警告，不要直接跳过。");
  }
  if (data.workflow === "protein_ligand_md") {
    const ligands = data.ligands || [];
    const keys = ligands.map((ligand) => String(ligand.key || "").trim()).filter(Boolean);
    if (new Set(keys).size !== keys.length) {
      issues.push("配体标识（key）重复；每个配体需要唯一的 key，否则工作文件、index 组和 POSRES 宏会冲突。");
    }
    if (ligands.length > 1 && data.auto_split_complex_pdb && hasPdbProtein(data)) {
      const missingResidues = ligands
        .map((ligand, index) => ({ ligand, index }))
        .filter(({ ligand }) => !String(ligand.residue || "").trim())
        .map(({ ligand, index }) => ligand.key || `配体 ${index + 1}`);
      if (missingResidues.length) {
        issues.push(`多配体自动拆分要求每个配体都有 PDB 残基名；缺少：${missingResidues.join("、")}。可点「从复合物 PDB 识别配体」自动填写。`);
      }
    }
    if (!data.dry_run) {
      const unconfirmed = ligands
        .map((ligand, index) => ({ ligand, index }))
        .filter(({ ligand }) => ligandWillUseAcpype(data, ligand) && !ligand.charge_confirmed);
      if (unconfirmed.length) {
        const detail = unconfirmed
          .map(({ ligand, index }) => `${ligand.key || `配体 ${index + 1}`}（当前电荷 ${ligand.charge ?? 0}）`)
          .join("、");
        issues.push(
          `以下配体会执行 ACPYPE/Antechamber，但还没有确认净电荷：${detail}。请逐行检查电荷并勾选「已确认总电荷」；电荷不对时常见报错是电子数奇偶不匹配，并在 sqm/antechamber 阶段失败。`,
        );
      }
    }
  }
  return issues;
}

export function submitWarnings(data) {
  const warnings = [];
  if (!data.dry_run) {
    warnings.push("这不是 Dry run。确认后会真正执行 GROMACS、ACPYPE 或 Open Babel，长任务会持续占用本机 CPU/GPU。");
  }
  const hasPdbInput = hasFile(data, [".pdb"]) || fileParamEndsWith(data.structure_file, [".pdb"]) || fileParamEndsWith(data.protein_file, [".pdb"]);
  if (data.workflow === "protein_md" && data.clean_pdb && hasPdbInput) {
    warnings.push("Protein MD 会先生成 protein_pdb2gmx_input.pdb，并移除结晶水、常见离子和 HETATM 配体；原始 PDB 会留在任务目录。");
  }
  if (data.workflow === "protein_ligand_md" && data.auto_split_complex_pdb && hasPdbProtein(data)) {
    const ligands = data.ligands || [];
    warnings.push("Protein-Ligand MD 会把复合物 PDB 拆成蛋白输入和每个配体的 *_from_complex.pdb，避免 pdb2gmx 把配体当作蛋白残基处理。");
    if (ligands.length > 1) {
      warnings.push(`检测到 ${ligands.length} 个配体：任务会自动合并各配体 itp 的 [ atomtypes ] 段（按类型名去重，冲突会中止），并为每个配体生成独立 index 组与 POSRES 宏。`);
    }
    const autoAcpype = ligands.filter((ligand) => ligandWillUseAcpype(data, ligand) && !(ligand.gro_file && ligand.itp_file));
    if (autoAcpype.length) {
      warnings.push(
        `以下配体没有单独的 itp/gro，将用拆分出的坐标进入 ACPYPE 参数化：${autoAcpype.map((ligand) => ligand.key || "未命名").join("、")}。请确认每个配体的电荷、残基名和质子化状态。`,
      );
    }
    const mapped = ligands.filter((ligand) => ligand.chemistry_file || ligand.smiles);
    if (mapped.length) {
      warnings.push(`将把 ${mapped.map((ligand) => ligand.key || "未命名").join("、")} 的 SDF/SMILES 键级、芳香性和形式电荷映射到共折叠配体坐标；重原子数、电荷或原子映射不一致时任务会停止。`);
    }
    if (ligands.length === 1 && !String(ligands[0].residue || "").trim()) {
      warnings.push("配体残基名为空时，会把非水、非离子的 HETATM 记录作为该配体；如果 PDB 中有多个辅因子或小分子，请点「从复合物 PDB 识别配体」逐行填写残基名。");
    }
  }
  if (willUseAcpype(data)) {
    const acpypeKeys = (data.ligands || [])
      .filter((ligand) => ligandWillUseAcpype(data, ligand))
      .map((ligand) => `${ligand.key || "未命名"}[${ligand.charge ?? 0}]`);
    warnings.push(`ACPYPE 将参数化配体：${acpypeKeys.join("、")}；电荷方法 ${data.charge_method || "bcc"}，原子类型 ${data.atom_type || "gaff2"}。这些参数会直接影响 antechamber 是否能成功。`);
    if (data.use_obabel) {
      warnings.push("Open Babel 会先把配体转为 SDF 并按目标 pH 自动估计质子化和显式氢；这不是化学正确性的保证，请检查芳香性、键级、互变异构体和形式电荷。");
    } else if (data.auto_split_complex_pdb && hasPdbProtein(data)) {
      warnings.push("当前会把复合物中拆出的 PDB 配体直接交给 ACPYPE。PDB 缺少可靠键级与形式电荷；除非你已验证该路径，否则建议开启“Open Babel 化学结构准备”或单独上传可靠的 SDF/MOL2。");
    }
  }
  const salt = String(data.ion_concentration ?? "").trim() === "" ? null : Number(data.ion_concentration);
  if (Number.isFinite(salt) && salt > 0.3) {
    warnings.push(`盐浓度设置为 ${salt} mol/L，属于较高盐条件；请确认这来自实验设计，而不是把 0.15 mol/L 误填成其他数值。`);
  }
  if (Number(data.replicas || 1) < 3 && ["protein_md", "protein_ligand_md"].includes(data.workflow)) {
    warnings.push("生产副本少于 3 条，只能提供较弱的副本间一致性证据；探索性运行可以使用，正式结论建议至少 3 条独立轨迹。 ");
  }
  if (!data.release_restraints && ["protein_md", "protein_ligand_md"].includes(data.workflow)) {
    warnings.push("已关闭解除位置限制后的 NPT 再平衡，生产阶段会从带限制的结构直接开始；仅在你有明确方案时使用。 ");
  }
  if (data.mdp_override_enabled) {
    warnings.push("已启用 MDP 参数覆盖；只会修改本次任务目录里的 MDP 副本，不会动 mdp/protein 或 mdp/complex 模板。提交前请看一眼覆盖摘要。");
    if (data.mdp_tc_grps) {
      warnings.push(`你覆盖了 tc-grps = ${data.mdp_tc_grps}。这些组名必须存在于默认组或 index.ndx 中，否则 grompp 会失败。`);
    }
    if (data.mdp_define_equil) {
      warnings.push("你覆盖了 NVT/NPT 的 define。蛋白-only 通常不要使用 POSRES_LIG；复合物使用 POSRES_LIG 时，拓扑中必须包含对应 posre_lig.itp 条件 include。");
    }
  }
  if (data.pre_nvt) {
    warnings.push("已启用可选 NVT 预平衡。它适合直接 NPT 出现盒子剧烈振荡或结构不稳定时补救，不应无理由作为固定步骤。");
  }
  if (data.custom_stdin) {
    warnings.push("已启用自定义交互 stdin；请确认每个 stdin 块都是要发给 gmx 的内容。");
  }
  if (data.workflow === "analysis_suite" && data.do_sham && !data.do_pca) {
    warnings.push("已选择 SHAM 但未选择 PCA；任务会自动先生成 PC1/PC2 投影，再用 pc12_sham_1.xvg 构建自由能形貌图。");
  }
  return warnings;
}

export function selectedFileSummary() {
  const files = selectedUploadFiles();
  if (!files.length) return "可上传结构、拓扑、MDP 和轨迹文件";
  if (files.length === 1) return files[0].name;
  const head = files.slice(0, 3).map((file) => file.name).join(", ");
  return `已选 ${files.length} 个文件：${head}${files.length > 3 ? " …" : ""}`;
}

export function renderWorkflowGuide() {
  const data = formObject();
  const guide = WORKFLOW_GUIDES[data.workflow] || WORKFLOW_GUIDES.protein_md;
  const requirements = guide.requirements.map((item) => ({ ...item, met: item.test(data) }));
  const readyCount = requirements.filter((item) => item.met).length;
  const totalCount = requirements.length || 1;
  const readyPercent = Math.round((readyCount / totalCount) * 100);
  const nextMissing = requirements.find((item) => !item.met);

  els.workflowGuide.dataset.ready = String(readyCount === totalCount);
  updateText(els.guideTitle, guide.title);
  updateText(els.guideReady, `输入 ${readyCount}/${totalCount}`);
  updateText(els.guideHint, nextMissing ? `下一步：${nextMissing.missing}` : guide.readyHint);
  els.guideBarFill.style.transform = `scaleX(${readyPercent / 100})`;

  const phases = data.workflow === "protein_md"
    ? { 0: "体系准备", 2: "最小化与平衡", 5: "生产与分析" }
    : data.workflow === "protein_ligand_md"
      ? { 0: "体系准备", 3: "最小化与平衡", 5: "生产与分析" }
      : {};
  els.guideSteps.classList.toggle("phased-guide", Object.keys(phases).length > 0);
  const stepsHtml = guide.steps.map((step, index) => `<li${phases[index] ? ' class="phase-start"' : ""}>${phases[index] ? `<small class="guide-phase-label">${escapeHtml(phases[index])}</small>` : ""}<span>${escapeHtml(step)}</span></li>`).join("");
  if (els.guideSteps.dataset.signature !== stepsHtml) {
    els.guideSteps.dataset.signature = stepsHtml;
    els.guideSteps.innerHTML = stepsHtml;
  }
  const reqHtml = requirements
    .map((item) => {
      return `
        <span class="requirement-chip ${item.met ? "met" : ""}">
          <strong>${escapeHtml(item.label)}</strong>
          <small>${item.met ? "已就绪" : "待补充"}</small>
        </span>
      `;
    })
    .join("");
  if (els.guideRequirements.dataset.signature !== reqHtml) {
    els.guideRequirements.dataset.signature = reqHtml;
    els.guideRequirements.innerHTML = reqHtml;
  }

  updateText(els.guideNoteTitle, guide.title);
  const notesHtml = (guide.notes || []).map((note) => `<li>${escapeHtml(note)}</li>`).join("");
  if (els.guideNotes.dataset.signature !== notesHtml) {
    els.guideNotes.dataset.signature = notesHtml;
    els.guideNotes.innerHTML = notesHtml;
  }
}

export function setSubmitReady(isReady) {
  if (!els.submitJob || els.submitJob.dataset.busy === "true") return;
  els.submitJob.disabled = !isReady;
  updateText(els.submitJob.querySelector(".button-label"), isReady ? "启动模拟" : "补全输入");
  els.submitJob.title = isReady ? "提交这个流程" : "先补齐这个流程需要的文件或参数";
}

export function friendlyPreviewError(message) {
  const rules = [
    [/\.pdb or \.gro structure/i, "请上传 .pdb 或 .gro 结构文件，或填写对应文件名。"],
    [/protein \.pdb\/\.gro/i, "请上传蛋白 .pdb/.gro 文件，或填写蛋白文件名。"],
    [/\.tpr file for Run TPR/i, "运行 TPR 需要 .tpr 文件。"],
    [/one \.tpr and one \.xtc\/\.trr/i, "该流程需要 1 个 .tpr 文件和 1 个 .xtc/.trr 轨迹文件。"],
    [/ACPYPE mode requires/i, "ACPYPE 模式需要上传配体 .sdf/.mol2/.pdb，或开启复合物 PDB 自动拆分。"],
    [/requires confirming ligand_charge/i, "真实运行 ACPYPE 前必须先确认配体总电荷；请填写 ligand_charge 并勾选“已确认配体总电荷”。"],
    [/references '.+'[, ]+but that file is not in the current upload list/i, "右侧文件字段还指着旧文件。请清空该字段，或重新拖入对应文件。"],
    [/requires setting MDP dt/i, "设置 MDP 时长或输出间隔时，必须同时填写时间步 dt。"],
    [/MDP .+ must be/i, "MDP 覆盖参数格式不正确，请检查数值是否为空、为正数或符合单位。"],
    [/must be a file name only/i, "输出文件名只能填写文件名本身，不能包含绝对路径、..、/ 或 \\\\。"],
    [/Failed to apply MDP overrides/i, "应用 MDP 覆盖失败，请检查任务目录中是否存在对应的 .mdp 文件。"],
    [/Prepared ligand mode requires/i, "prepared 模式需要配体 .gro 和 .itp 文件。"],
    [/Multi-ligand complexes need a PDB residue name/i, "多配体自动拆分需要每个配体填写 PDB 残基名；可点「从复合物 PDB 识别配体」自动填写。"],
    [/Unmapped HETATM residues/i, "复合物 PDB 中存在没有对应配体的 HETATM 残基；请为每个残基添加配体行并填写残基名，或从 PDB 中删除它们。"],
    [/duplicate ligand key/i, "配体标识（key）重复；每个配体需要唯一标识。"],
    [/Conflicting (atomtypes|nonbond_params|pairtypes) definition/i, "不同配体 itp 的 [ atomtypes ] 等定义冲突（同一类型名但参数不同）；请检查各配体是否使用同一力场版本。"],
    [/No ligand atoms found/i, "没有在复合物 PDB 中找到配体原子。请填写配体残基名，或单独上传配体文件。"],
    [/Custom command is empty/i, "请填写自定义 GROMACS 命令。"],
    [/must start with the configured gmx binary/i, "自定义命令必须以当前配置的 gmx 路径开头。"],
    [/Unsupported gmx subcommand/i, "该 gmx 子命令未在安全白名单内。"],
  ];
  return rules.find(([pattern]) => pattern.test(message))?.[1] || message || "参数还没填完整，请检查必填项。";
}

export function friendlyJobActionError(message) {
  const rules = [
    [/still stopping/i, "任务进程还在停止中，请稍后再删除。"],
    [/Cancel the job before deleting/i, "运行中或排队中的任务需要先取消，再删除。"],
    [/Job not found/i, "任务不存在，可能已经被删除。"],
  ];
  return rules.find(([pattern]) => pattern.test(message))?.[1] || message || "任务操作失败。";
}

export async function saveSettings() {
  const button = els.saveSettings;
  button.disabled = true;
  try {
    const gmxBin = els.gmxBin.value.trim() || "gmx";
    const maxParallel = els.maxParallel ? Math.max(1, Math.min(16, parseInt(els.maxParallel.value, 10) || 1)) : undefined;
    const settings = await api("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gmx_bin: gmxBin, ...(maxParallel !== undefined ? { max_parallel: maxParallel } : {}) }),
    });
    toast(`gmx 路径已保存：${settings.gmx_bin}`, "success");
    await refreshHealth();
    await refreshPreview();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

/* ---------- 命令预览（防抖 + 取消过期请求） ---------- */

let previewAbort = null;

export async function refreshPreview() {
  if (previewAbort) previewAbort.abort();
  const controller = new AbortController();
  previewAbort = controller;
  const params = formObject();
  void refreshProtocolPreview(params, controller.signal);
  try {
    const payload = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
      signal: controller.signal,
    });
    state.previewCommands = payload.commands || [];
    renderMdpPreview(payload.mdp_overrides || []);
    updateText(els.previewCount, `${payload.commands.length} 步`);
    const html = payload.commands
      .map(
        (step) => `
          <li>
            <div class="command-preview-head"><strong>${escapeHtml(step.title)}${step.kind === "internal" ? " · 内部处理" : ""}${step.stdin ? " · stdin" : ""}</strong><button class="copy-command" type="button" title="复制这一步命令">复制</button></div>
            <code>${escapeHtml(step.args.join(" "))}</code>
            ${step.stdin_text ? `<pre class="stdin-preview">${escapeHtml(step.stdin_text)}</pre>` : ""}
          </li>
        `,
      )
      .join("");
    if (state.previewSignature !== html) {
      state.previewSignature = html;
      els.commandPreview.innerHTML = html;
    }
    setSubmitReady(true);
  } catch (error) {
    if (error.name === "AbortError") return;
    state.previewCommands = [];
    renderMdpPreview([]);
    updateText(els.previewCount, "待完善");
    const html = `<li class="preview-blocked"><strong>还缺输入</strong><code>${escapeHtml(friendlyPreviewError(error.message))}</code></li>`;
    if (state.previewSignature !== html) {
      state.previewSignature = html;
      els.commandPreview.innerHTML = html;
    }
    setSubmitReady(false);
  }
}

function buildStdinOverrideTemplate() {
  return (state.previewCommands || [])
    .filter((step) => step.stdin_text)
    .map((step) => `[${step.title}]\n${String(step.stdin_text).trimEnd()}`)
    .join("\n\n");
}

export function fillStdinOverridesFromPreview() {
  const template = buildStdinOverrideTemplate();
  if (!template) {
    toast("当前预览中没有需要 stdin 的交互步骤。", "info");
    return;
  }
  els.customStdin.checked = true;
  els.stdinOverrides.value = template;
  toast("已根据预览生成 stdin 覆盖模板。", "success");
  debouncedPreview();
}

export const debouncedPreview = debounce(refreshPreview, 280);

/* ---------- 工作流分区显示 ---------- */

let lastSectionWorkflow = null;

export function toggleSections() {
  const workflow = $("input[name='workflow']:checked")?.value || "protein_md";
  document.body.dataset.workflow = workflow;
  renderReadiness();
  const centerField = els.jobForm.elements.namedItem("center_group");
  const centerDefault = ["protein_ligand_md", "postprocess"].includes(workflow) ? "Protein_Lig" : "Protein";
  if (centerField) {
    const previousDefault = centerField.dataset.workflowDefault ?? centerField.defaultValue;
    if (!centerField.value.trim() || centerField.value === previousDefault) centerField.value = centerDefault;
    centerField.dataset.workflowDefault = centerDefault;
  }
  const visible = new Set();
  if (["protein_md", "protein_ligand_md", "em_only", "run_tpr"].includes(workflow)) visible.add("base");
  if (workflow === "protein_md" || workflow === "protein_ligand_md") visible.add("mdp");
  if (workflow === "protein_ligand_md") visible.add("ligand");
  if (workflow === "protein_ligand_md" || workflow === "postprocess") visible.add("post");
  if (workflow === "analysis_rmsd" || workflow === "analysis_suite") visible.add("post");
  if (workflow === "analysis_suite") visible.add("analysis");
  if (["protein_ligand_md", "postprocess", "analysis_rmsd", "analysis_suite"].includes(workflow)) visible.add("interaction");
  if (workflow === "custom") visible.add("custom");
  document.querySelectorAll("[data-section]").forEach((section) => {
    section.hidden = !visible.has(section.dataset.section);
  });

  const compactBaseFields = new Set(["ntmpi", "ntomp", "gpu", "verbose", "dry_run", "maxwarn"]);
  document.querySelectorAll('[data-section="base"] .dense-grid > label').forEach((label) => {
    const field = label.querySelector("input, select")?.name;
    label.hidden = ["em_only", "run_tpr"].includes(workflow) && !compactBaseFields.has(field);
  });

  if (els.stdinOverrides) {
    els.stdinOverrides.placeholder = workflow === "protein_ligand_md"
      ? "[Create ligand heavy-atom index]\n0 & ! a H*\nq\n\n[Center trajectory]\nProtein_Lig\nSystem"
      : "[Center trajectory]\nProtein\nSystem\n\n[Fit trajectory]\nBackbone\nSystem";
  }

  // 仅在真正切换工作流时重播指南过渡，避免输入框 change 连带触发
  if (workflow !== lastSectionWorkflow) {
    lastSectionWorkflow = workflow;
    replaySwapAnimation("#workflow-guide");
  }
}
