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
} from "./core.js?v=20261006-026";

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
    notes: ["DSSP 依赖 mkdssp，可先关闭避免任务失败。", "PCA/SHAM 计算更重，建议先跑 RMSD/Rg 快速检查。", "能量项名称要与 EDR 中的实际项匹配。"],
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
  payload.ligands = readLigandPayload();
  return payload;
}

export function hasFile(data, suffixes) {
  return (data.files || []).some((name) => suffixes.some((suffix) => name.toLowerCase().endsWith(suffix)));
}

export function fileNameEndsWith(name, suffixes) {
  const lower = String(name || "").toLowerCase();
  return suffixes.some((suffix) => lower.endsWith(suffix));
}

export function fileParamEndsWith(value, suffixes) {
  const text = String(value || "").toLowerCase();
  return suffixes.some((suffix) => text.endsWith(suffix));
}

export function uploadedNames() {
  return selectedUploadFiles().map((file) => file.name);
}

export function selectedUploadFiles() {
  const files = [
    ...Array.from(els.files?.files || []),
    ...Array.from(els.ligandReferenceFile?.files || []),
  ];
  const seen = new Set();
  return files.filter((file) => {
    const key = `${file.name} ${file.size} ${file.lastModified}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}


/* ---------- 多配体行 ---------- */

const PDB_WATER_RESIDUES = new Set(["HOH", "WAT", "SOL", "H2O", "TIP3", "TIP3P", "TIP4P", "OPC", "OPC3"]);
const PDB_ION_RESIDUES = new Set([
  "AG", "AL", "BA", "BR", "CA", "CAL", "CD", "CL", "CLA", "CO", "CS", "CU",
  "F", "FE", "FE2", "FE3", "HG", "IOD", "K", "LI", "MG", "MG2", "MN", "NA",
  "NI", "PB", "POT", "RB", "SOD", "SR", "ZN",
]);

export function ligandCards() {
  return Array.from(els.jobForm.querySelectorAll(".ligand-card"));
}

function suggestedLigandKey(index) {
  return index === 0 ? "lig" : `lig${index + 1}`;
}

function ligandCardHtml(index, initial = {}) {
  const key = escapeHtml(initial.key ?? suggestedLigandKey(index));
  const name = escapeHtml(initial.name ?? "");
  const residue = escapeHtml(initial.residue ?? "");
  const count = initial.count ?? 1;
  const mode = initial.mode || "prepared";
  const gro = escapeHtml(initial.gro_file ?? "");
  const itp = escapeHtml(initial.itp_file ?? "");
  const structure = escapeHtml(initial.structure_file ?? "");
  const chemistry = escapeHtml(initial.chemistry_file ?? "");
  const smiles = escapeHtml(initial.smiles ?? "");
  const charge = initial.charge ?? 0;
  const ph = initial.ph ?? 7.4;
  const protonation = initial.protonation_mode || "preserve";
  const chargeConfirmed = Boolean(initial.charge_confirmed);
  const posres = initial.posres !== false;
  const modeSelected = (value) => (value === mode ? "selected" : "");
  const protonationSelected = (value) => (value === protonation ? "selected" : "");
  return `
  <div class="ligand-card" data-ligand-index="${index}" data-mode="${escapeHtml(mode)}">
    <div class="ligand-card-head">
      <strong>配体 ${index + 1}</strong>
      <button class="ligand-remove" type="button" data-remove-ligand="${index}">移除</button>
    </div>
    <div class="dense-grid">
      <label title="配体标识；决定工作文件名（如 ligA_GMX.itp）、index 组名 Ligand_ligA 和 POSRES_LIGA 宏">
        标识 <small>key</small>
        <input data-ligand-field="key" value="${key}" spellcheck="false" />
      </label>
      <label title="配体分子名，需与 itp 中的 moleculetype 一致（prepared 模式会自动读取 itp 的真实名称）">
        配体名 <small>moleculetype</small>
        <input data-ligand-field="name" value="${name}" spellcheck="false" />
      </label>
      <label title="复合物 PDB 中该配体的残基名；自动拆分时按残基名提取">
        PDB 残基名 <small>residue</small>
        <input data-ligand-field="residue" value="${residue}" placeholder="如 BTA" spellcheck="false" />
      </label>
      <label title="topol.top 中写入的该配体分子数">
        数量 <small>count</small>
        <input data-ligand-field="count" type="number" min="1" value="${count}" />
      </label>
      <label title="prepared：上传现成的 itp/gro；acpype：由 obabel/acpype 现场参数化">
        模式 <small>mode</small>
        <select data-ligand-field="mode">
          <option value="prepared" ${modeSelected("prepared")}>prepared itp/gro</option>
          <option value="acpype" ${modeSelected("acpype")}>obabel/acpype</option>
        </select>
      </label>
      <label class="check-row" title="NVT/NPT 阶段对该配体施加位置限制">
        <input data-ligand-field="posres" type="checkbox" ${posres ? "checked" : ""} />
        <span>位置限制</span>
      </label>
      <label class="ligand-prepared-only" title="prepared 模式：配体坐标文件">
        配体 .gro <small>coordinates</small>
        <input data-ligand-field="gro_file" value="${gro}" placeholder="lig_GMX.gro" spellcheck="false" />
      </label>
      <label class="ligand-prepared-only" title="prepared 模式：配体拓扑文件">
        配体 .itp <small>topology</small>
        <input data-ligand-field="itp_file" value="${itp}" placeholder="lig_GMX.itp" spellcheck="false" />
      </label>
      <label class="ligand-acpype-only" title="acpype 模式：配体结构文件（SDF/MOL2；留空则用自动拆分出的 PDB）">
        结构文件 <small>SDF/MOL2</small>
        <input data-ligand-field="structure_file" value="${structure}" placeholder="留空用复合物拆出的 PDB" spellcheck="false" />
      </label>
      <label class="ligand-acpype-only" title="可选：只提供键级、芳香性、形式电荷和质子化状态；配体三维坐标仍取自复合物 PDB">
        化学参考 <small>SDF/MOL2</small>
        <input data-ligand-field="chemistry_file" value="${chemistry}" placeholder="可选" spellcheck="false" />
      </label>
      <label class="ligand-acpype-only" title="与化学文件二选一；SMILES 负责键级、芳香性和形式电荷，共折叠 PDB 负责三维坐标">
        SMILES <small>chemistry</small>
        <input data-ligand-field="smiles" value="${smiles}" placeholder="例如 CC(=O)[O-]" spellcheck="false" maxlength="2000" />
      </label>
      <label class="ligand-acpype-only" title="配体净电荷，传给 antechamber -nc">
        净电荷 <small>charge</small>
        <input data-ligand-field="charge" type="number" value="${charge}" />
      </label>
      <label class="ligand-acpype-only" title="保留输入状态：按 SDF/SMILES 的形式电荷加氢；按目标 pH：允许 Open Babel 重新估计质子化">
        质子化 <small>protonation</small>
        <select data-ligand-field="protonation_mode">
          <option value="preserve" ${protonationSelected("preserve")}>保留 SDF/SMILES 状态</option>
          <option value="ph" ${protonationSelected("ph")}>按目标 pH 估计</option>
        </select>
      </label>
      <label class="ligand-acpype-only" title="Open Babel -p 使用的目标 pH；仍需人工确认质子化与互变异构体">
        目标 pH <small>protonation</small>
        <input data-ligand-field="ph" type="number" min="0" max="14" step="0.1" value="${ph}" />
      </label>
      <label class="ligand-acpype-only check-row" title="ACPYPE/Antechamber 参数化前请先确认配体净电荷；默认 0 不一定正确">
        <input data-ligand-field="charge_confirmed" type="checkbox" ${chargeConfirmed ? "checked" : ""} />
        <span>已确认总电荷</span>
      </label>
    </div>
  </div>`;
}

function refreshLigandRemoveState() {
  const cards = ligandCards();
  cards.forEach((card) => {
    const button = card.querySelector("[data-remove-ligand]");
    if (button) button.disabled = cards.length <= 1;
  });
}

export function refreshRowMode(card) {
  const mode = card.querySelector("[data-ligand-field='mode']")?.value || "prepared";
  card.dataset.mode = mode;
}

export function addLigandRow(initial = {}) {
  const index = ligandCards().length;
  const holder = document.createElement("div");
  holder.innerHTML = ligandCardHtml(index, initial);
  els.ligandList.appendChild(holder.firstElementChild);
  refreshLigandRemoveState();
  return index;
}

export function ensureDefaultLigandRow() {
  if (!ligandCards().length) addLigandRow();
}

export function removeLigandRow(index) {
  const cards = ligandCards();
  const card = cards.find((item) => Number(item.dataset.ligandIndex) === index);
  if (!card) return;
  if (cards.length <= 1) {
    toast("至少保留一个配体", "info");
    return;
  }
  card.remove();
  ligandCards().forEach((item, position) => {
    item.dataset.ligandIndex = String(position);
    const removeButton = item.querySelector("[data-remove-ligand]");
    if (removeButton) removeButton.dataset.removeLigand = String(position);
    const title = item.querySelector(".ligand-card-head strong");
    if (title) title.textContent = `配体 ${position + 1}`;
  });
  refreshLigandRemoveState();
  refreshPreview();
}

export function readLigandPayload() {
  const rows = new Map();
  els.jobForm.querySelectorAll("[data-ligand-field]").forEach((field) => {
    const card = field.closest(".ligand-card");
    if (!card) return;
    const index = Number(card.dataset.ligandIndex);
    if (Number.isNaN(index)) return;
    if (!rows.has(index)) rows.set(index, {});
    const entry = rows.get(index);
    let value;
    if (field.type === "checkbox") value = field.checked;
    else if (field.type === "number") value = field.value === "" ? "" : Number(field.value);
    else value = field.value;
    entry[field.dataset.ligandField] = value;
  });
  return [...rows.entries()].sort(([left], [right]) => left - right).map(([, entry]) => entry);
}

function setLigandCardField(card, fieldName, value, selectedNames, changed) {
  if (!value) return;
  const field = card.querySelector(`[data-ligand-field='${fieldName}']`);
  if (!field) return;
  const current = String(field.value || "").trim();
  const shouldReplace = !current || field.dataset.autoUpload === "true" || !selectedNames.has(current);
  if (!shouldReplace || current === value) return;
  field.value = value;
  field.dataset.autoUpload = "true";
  field.classList.add("auto-filled");
  window.setTimeout(() => field.classList.remove("auto-filled"), 900);
  changed.push(`配体 ${Number(card.dataset.ligandIndex) + 1} ${fieldName}: ${value}`);
}

export async function detectLigandsFromPdb() {
  const protein = String(els.jobForm.querySelector("[name='protein_file']")?.value || "").trim();
  const file = selectedUploadFiles().find((item) => item.name === protein && item.name.toLowerCase().endsWith(".pdb"));
  if (!file) {
    toast("先在右侧上传复合物 PDB，并让「蛋白文件」指向它", "error");
    return;
  }
  let text = "";
  try {
    text = await file.text();
  } catch {
    toast("无法读取 PDB 文件内容", "error");
    return;
  }
  const residues = [];
  const seen = new Set();
  for (const line of text.split("\n").slice(0, 200000)) {
    const record = line.slice(0, 6).trim().toUpperCase();
    if (record !== "HETATM") continue;
    const residue = line.slice(17, 20).trim().toUpperCase();
    if (!residue || seen.has(residue)) continue;
    if (PDB_WATER_RESIDUES.has(residue) || PDB_ION_RESIDUES.has(residue)) continue;
    seen.add(residue);
    residues.push(residue);
  }
  if (!residues.length) {
    toast("PDB 里没有找到非水/非离子的 HETATM 残基", "info");
    return;
  }
  let cards = ligandCards();
  residues.forEach((residue, position) => {
    if (position < cards.length) {
      const field = cards[position].querySelector("[data-ligand-field='residue']");
      if (field && !String(field.value || "").trim()) field.value = residue;
    } else {
      addLigandRow({ key: suggestedLigandKey(position), residue, mode: "acpype" });
    }
  });
  cards = ligandCards();
  refreshLigandRemoveState();
  toast(`识别到 ${residues.length} 个 HETATM 残基：${residues.join(", ")}；已填入配体行`, "success");
  refreshPreview();
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

export function setMdpPanelExpanded(expanded) {
  if (!els.mdpPanelToggle || !els.mdpPanelBody) return;
  els.mdpPanelBody.hidden = !expanded;
  els.mdpPanelToggle.setAttribute("aria-expanded", String(expanded));
  updateText(els.mdpPanelToggle, expanded ? "收起高级参数" : "高级参数");
}

export function updateMdpPanelState() {
  if (!els.mdpPanelState || !els.jobForm) return;
  const enabled = Boolean(els.jobForm.querySelector("input[name='mdp_override_enabled']")?.checked);
  if (!enabled) {
    updateText(els.mdpPanelState, "使用原始模板");
    return;
  }
  const filled = Array.from(els.jobForm.querySelectorAll("[name^='mdp_']"))
    .filter((field) => field.name !== "mdp_override_enabled" && String(field.value || "").trim())
    .length;
  updateText(els.mdpPanelState, filled ? `已启用覆盖，${filled} 项已填写` : "已启用覆盖，等待填写参数");
}

/* ---------- 健康检查 ---------- */

function setHealthCard(dotClass, title, detail) {
  const dot = els.healthCard.querySelector(".dot");
  dot.className = `dot ${dotClass}`;
  updateText(els.healthCard.querySelector("strong"), title);
  updateText(els.healthCard.querySelector("small"), detail);
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
  updateText(els.resourceAdvice.querySelector("span"), `${warnings.length ? `⚠ ${warnings.join("；")}` : prefix}${trajectoryNote}`);
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
  debouncedPreview();
  toast("已应用本机推荐配置，请用实际 ns/day 验证。", "success");
}

export async function refreshHealth() {
  try {
    const payload = await api("/api/health");
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
        ["DSSP", diagnostics.dssp],
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
    const localHint = state.localForceFields.length
      ? `本地力场：${state.localForceFields.map((item) => item.name).join(", ")}`
      : "未发现本地 *.ff 力场";
    if (payload.gromacs.available) {
      setHealthCard("ok", "GROMACS 已就绪", `${payload.gromacs.message || payload.gromacs.binary}；${localHint}`);
    } else {
      setHealthCard("bad", "GROMACS 未检测到", `${payload.gromacs.message || payload.gromacs.binary}；${localHint}`);
    }
  } catch (error) {
    setHealthCard("bad", "健康检查失败", error.message);
    throw error;
  }
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

async function refreshProtocolPreview(params, signal) {
  if (!["protein_md", "protein_ligand_md"].includes(params.workflow)) {
    els.protocolSummary.textContent = "";
    return;
  }
  try {
    const uploaded_mdp = {};
    for (const file of selectedUploadFiles()) {
      if (!["ions.mdp", "minim.mdp", "nvt.mdp", "npt.mdp", "md.mdp"].includes(file.name)) continue;
      if (file.size > 262144) throw new Error("MDP 超过 256 KiB，无法预览协议");
      uploaded_mdp[file.name] = await file.text();
    }
    if (signal.aborted) return;
    const result = await api("/api/protocol-preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...params, uploaded_mdp }), signal,
    });
    if (signal.aborted) return;
    const names = { NVT: "NVT", NPT: "限制 NPT", "NPT unrestrained": "无限制 NPT", Production: "生产 MD" };
    const rows = result.stages.map((stage) => {
      const source = stage.source === "uploaded" ? "上传文件" : stage.source === "built-in" ? "内置模板" : stage.source.split("/").slice(-3).join("/");
      const duration = stage.duration_ns == null ? "未确定" : `${Number(stage.duration_ns.toPrecision(6))} ns`;
      return `<tr><th scope="row">${escapeHtml(names[stage.stage] || stage.stage)}${stage.replicas > 1 ? ` × ${stage.replicas}` : ""}<small title="${escapeHtml(stage.source)}">${escapeHtml(source)}${stage.overrides.length ? " · 已覆盖" : ""}</small></th><td>${escapeHtml(duration)}</td><td>${escapeHtml(stage.temperature_k || "未设置")} K</td><td>${escapeHtml(stage.pcoupl)}</td></tr>`;
    }).join("");
    const production = result.stages.find((stage) => stage.stage === "Production");
    const npt = result.stages.find((stage) => stage.stage === "NPT");
    const nvt = result.stages.find((stage) => stage.stage === "NVT");
    const placeholders = { mdp_dt_ps: production?.dt_ps, mdp_md_ns: production?.duration_ns,
      mdp_npt_ns: npt?.duration_ns, mdp_nvt_ns: nvt?.duration_ns,
      mdp_temperature_k: production?.temperature_k?.split(/\s+/)[0], mdp_pressure_bar: production?.pressure_bar };
    Object.entries(placeholders).forEach(([name, value]) => {
      const input = els.jobForm.querySelector(`[name='${name}']`);
      if (input && value != null) input.placeholder = String(value);
    });
    const total = production?.duration_ns != null ? `${Number((production.duration_ns * production.replicas).toPrecision(6))} ns` : "未确定";
    els.protocolSummary.innerHTML = `<strong>本次协议 · 总生产采样 ${escapeHtml(total)}</strong><div class="report-table-scroll"><table><thead><tr><th>阶段与来源</th><th>每段时长</th><th>温度</th><th>压控</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  } catch (error) {
    if (!signal.aborted) els.protocolSummary.textContent = error.message;
  }
}

function renderMdpPreview(overrides = []) {
  if (!els.mdpPreview) return;
  if (!overrides.length) {
    els.mdpPreview.hidden = true;
    state.mdpSignature = "";
    return;
  }
  const rows = overrides
    .map(
      (item) => `
        <div class="mdp-preview-row">
          <span>${escapeHtml(item.file)}</span>
          <code>${escapeHtml(item.key)} = ${escapeHtml(item.value)} · ${escapeHtml(item.label)}</code>
        </div>
      `,
    )
    .join("");
  const html = `<strong>本次会覆盖 ${overrides.length} 个 MDP 参数</strong><div class="mdp-preview-list">${rows}</div>`;
  if (state.mdpSignature !== html) {
    state.mdpSignature = html;
    els.mdpPreview.innerHTML = html;
  }
  els.mdpPreview.hidden = false;
}

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
