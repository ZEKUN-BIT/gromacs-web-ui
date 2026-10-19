// 多配体编辑与 PDB 配体识别。

import { els, escapeHtml, toast } from "./core.js?v=58a36b0fc94d0be8";
import { selectedUploadFiles } from "./form-files.js?v=58a36b0fc94d0be8";

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
  const count = escapeHtml(initial.count ?? 1);
  const mode = initial.mode || "prepared";
  const gro = escapeHtml(initial.gro_file ?? "");
  const itp = escapeHtml(initial.itp_file ?? "");
  const structure = escapeHtml(initial.structure_file ?? "");
  const chemistry = escapeHtml(initial.chemistry_file ?? "");
  const smiles = escapeHtml(initial.smiles ?? "");
  const charge = escapeHtml(initial.charge ?? 0);
  const ph = escapeHtml(initial.ph ?? 7.4);
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
  return true;
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

export function setLigandCardField(card, fieldName, value, selectedNames, changed) {
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
  return true;
}
