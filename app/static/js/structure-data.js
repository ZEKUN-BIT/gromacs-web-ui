// Limits apply to previews only; the submitted simulation files are unchanged.
export const MAX_STRUCTURE_BYTES = 20 * 1024 * 1024;
export const MAX_STRUCTURE_ATOMS = 50_000;
const WATER = new Set(["HOH", "WAT", "SOL", "TIP3", "TIP3P", "TIP4", "TIP4P", "TIP5", "TIP5P", "SPC", "SPCE", "OPC", "OPC3"]);

export function structureFormat(name) {
  return /\.(pdb|gro)$/i.exec(String(name || ""))?.[1].toLowerCase() || "";
}

export function checkStructureSize(size) {
  if (size > MAX_STRUCTURE_BYTES) throw new Error("结构预览支持 20 MB 以内的文件。可选择去除水分子的结构；模拟提交不受此限制。");
}

function coordinates(line, offset, width) {
  return [0, 1, 2].every(index => {
    const value = line.slice(offset + index * width, offset + (index + 1) * width).trim();
    return value !== "" && Number.isFinite(Number(value));
  });
}

export function prepareStructure(text, format) {
  checkStructureSize(new TextEncoder().encode(text).byteLength);
  const lines = text.split(/\r\n|\n|\r/);
  const kept = [];
  let atomCount = 0, waterAtoms = 0, firstModelOnly = false;
  const addAtom = (line, residue, offset) => {
    if (WATER.has(residue.toUpperCase())) { waterAtoms += 1; return; }
    if (!coordinates(line, offset, 8)) throw new Error("结构中存在无效坐标，暂时无法预览。请检查 PDB/GRO 文件。");
    atomCount += 1;
    if (atomCount > MAX_STRUCTURE_ATOMS) throw new Error("去除水分子后超过 50,000 个原子，请选择较小的结构进行预览。");
    kept.push(line);
  };
  if (format === "pdb") {
    let models = 0;
    for (const line of lines) {
      const record = line.slice(0, 6).trim();
      if (record === "MODEL") { models += 1; if (models > 1) { firstModelOnly = true; break; } }
      if (record === "ENDMDL") { firstModelOnly = true; break; }
      if (record === "ATOM" || record === "HETATM") addAtom(line, line.slice(17, 20).trim(), 30);
      else if (["HELIX", "SHEET", "TER", "CONECT", "CRYST1"].includes(record)) kept.push(line);
    }
  } else if (format === "gro") {
    const countText = (lines[1] || "").trim();
    const count = /^\d+$/.test(countText) ? Number(countText) : 0;
    if (!count || !Number.isSafeInteger(count) || lines.length < count + 3) throw new Error("GRO 的原子数量与文件内容不一致，暂时无法预览。");
    for (const line of lines.slice(2, count + 2)) addAtom(line, line.slice(5, 10).trim(), 20);
    firstModelOnly = lines.slice(count + 3).some(line => line.trim());
    kept.unshift(lines[0], String(atomCount));
    kept.push(lines[count + 2]);
  } else {
    throw new Error("请选择 PDB 或 GRO 结构文件。");
  }
  if (!atomCount) throw new Error(waterAtoms ? "此文件只有水分子，未找到可预览的蛋白或配体。" : "未找到有效原子，请检查结构文件。");
  return { text: kept.join("\n") + "\n", atomCount, waterAtoms, firstModelOnly };
}

export async function readStructureResponse(response) {
  try {
    if (!response.ok) throw new Error(`结构文件读取失败（HTTP ${response.status}），请刷新后重试。`);
    checkStructureSize(Number(response.headers.get("Content-Length")));
  } catch (error) {
    if (response.body) await response.body.cancel().catch(() => {});
    throw error;
  }
  if (!response.body) throw new Error("结构文件为空，暂时无法预览。");
  const reader = response.body.getReader(), decoder = new TextDecoder();
  const parts = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      checkStructureSize(size);
      parts.push(decoder.decode(value, { stream: true }));
    }
    parts.push(decoder.decode());
    return parts.join("");
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export function structureFiles(files) {
  const priority = path => /(?:^|\/)(?:confout|final|md(?:_[^/]*)?)\.(?:gro|pdb)$/i.test(path) ? 0 : 1;
  const seen = new Set();
  return files.filter(file => {
    if (!structureFormat(file.path) || seen.has(file.path)) return false;
    seen.add(file.path);
    return true;
  }).sort((a, b) => priority(a.path) - priority(b.path) || a.path.localeCompare(b.path));
}
