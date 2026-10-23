// MDP 参数状态、协议摘要与参数覆盖预览。

import { els, state, escapeHtml, updateText, api } from "./core.js?v=621bb939e2b97a80";
import { selectedUploadFiles } from "./form-files.js?v=621bb939e2b97a80";

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

export async function refreshProtocolPreview(params, signal) {
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

export function renderMdpPreview(overrides = []) {
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
