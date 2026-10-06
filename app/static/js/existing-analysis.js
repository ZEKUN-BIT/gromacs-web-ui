import { api, state, toast } from "./core.js?v=20261006-026";
import { refreshJobs, selectJob } from "./jobs.js?v=20261006-026";

export function bindExistingAnalysis() {
  const dialog = document.querySelector("#existing-analysis-dialog");
  const form = document.querySelector("#existing-analysis-form");
  const fields = document.querySelector("#existing-analysis-fields");
  const submit = document.querySelector("#existing-analysis-submit");
  const cancel = document.querySelector("#existing-analysis-cancel");
  const error = document.querySelector("#existing-analysis-error");
  const status = document.querySelector("#existing-analysis-status");
  const fileKeys = ["trajectory_file", "tpr_file", "index_file", "edr_file"];
  const metrics = ["do_rmsd", "do_rg", "do_energy", "do_dssp", "do_hbond", "do_pca", "do_research"];
  let sourceId = null;
  let inputs = {};
  let busy = false;
  let indexKey = "", indexLoading = false;

  async function updateIndexGroups() {
    const select = form.elements.research_ligand_group;
    const filename = form.elements.index_file.value;
    const key = `${sourceId}:${filename}`;
    if (!form.elements.do_research.checked || indexKey === key) return;
    indexKey = key;
    select.replaceChildren(new Option("仅分析蛋白", ""));
    if (!filename) { indexLoading = false; select.disabled = false; return; }
    indexLoading = true; select.disabled = true;
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(sourceId)}/analysis-index-groups?file=${encodeURIComponent(filename)}`);
      if (indexKey !== key) return;
      result.groups.filter((group) => group.atoms > 0).forEach((group) => select.add(new Option(`${group.name} (${group.atoms} 原子)`, group.name)));
    } catch (failure) {
      if (indexKey === key) { showError(failure.message); indexKey = ""; }
    } finally {
      if (indexKey === key || indexKey === "") { indexLoading = false; select.disabled = false; }
    }
  }

  function setBusy(value, message = "") {
    busy = value;
    fields.disabled = value;
    submit.disabled = value;
    cancel.disabled = value;
    status.hidden = !message;
    status.textContent = message;
    form.setAttribute("aria-busy", String(value));
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = !message;
  }

  function matchTrajectory() {
    const stem = form.elements.trajectory_file.value.replace(/\.(xtc|trr)$/i, "");
    for (const key of ["tpr_file", "edr_file"]) {
      const match = (inputs[key] || []).find((file) => file.path.replace(/\.[^.]+$/, "") === stem);
      // Require an explicit selection when a processed trajectory has no matching TPR.
      form.elements[key].value = match?.path || "";
    }
    form.elements.do_energy.checked = Boolean(form.elements.edr_file.value);
    updateSelection();
  }

  function updateSelection() {
    document.querySelector("#research-settings").hidden = !form.elements.do_research.checked;
    updateIndexGroups();
    const energy = form.elements.do_energy;
    energy.disabled = !inputs.edr_file?.length;
    if (energy.disabled) energy.checked = false;
    form.elements.edr_file.required = energy.checked;
    let bytes = 0;
    for (const key of fileKeys) {
      if (key === "edr_file" && !energy.checked) continue;
      bytes += inputs[key]?.find((file) => file.path === form.elements[key].value)?.size || 0;
    }
    const size = bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    document.querySelector("#existing-analysis-size").textContent = `输入副本占用 ${size}，分析输出另计。`;
  }

  document.querySelector("#analyze-job").addEventListener("click", async () => {
    sourceId = state.activeJobId;
    if (!sourceId || dialog.open) return;
    const production = state.activeJob?.params?.production_deffnm || "md";
    indexKey = ""; indexLoading = false;
    form.elements.research_ligand_group.replaceChildren(new Option("仅分析蛋白", ""));
    form.reset();
    updateSelection();
    inputs = {};
    showError("");
    document.querySelector("#existing-analysis-source").textContent = "";
    document.querySelector("#existing-analysis-size").textContent = "";
    fileKeys.forEach((key) => form.elements[key].replaceChildren());
    dialog.showModal();
    setBusy(true, "正在读取可用文件…");
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(sourceId)}/analysis-inputs`);
      inputs = result.files;
      document.querySelector("#existing-analysis-source").textContent = `来源：${result.source_name}`;
      form.elements.name.value = `${result.source_name} · 追加分析`.slice(0, 160);
      for (const key of fileKeys) {
        const select = form.elements[key];
        select.add(new Option(key === "index_file" || key === "edr_file" ? "不使用" : "请选择", ""));
        (inputs[key] || []).forEach((file) => select.add(new Option(file.path, file.path)));
      }
      const trajectories = inputs.trajectory_file || [];
      const paired = trajectories.filter((file) => inputs.tpr_file.some((tpr) => tpr.path.replace(/\.tpr$/i, "") === file.path.replace(/\.(xtc|trr)$/i, "")));
      const preferred = paired.find((file) => {
        const stem = file.path.replace(/\.(xtc|trr)$/i, "");
        return stem === production || stem.startsWith(`${production}_r`) || /^md_r\d+$/.test(stem);
      }) || paired.find((file) => /\.xtc$/i.test(file.path)) || paired[0];
      form.elements.trajectory_file.value = preferred?.path || trajectories[0]?.path || "";
      const mainIndex = inputs.index_file?.find((file) => file.path === "index.ndx");
      if (mainIndex || inputs.index_file?.length === 1) form.elements.index_file.value = mainIndex?.path || inputs.index_file[0].path;
      matchTrajectory();
      setBusy(false);
      if (!trajectories.length || !inputs.tpr_file?.length) {
        showError("当前任务缺少可用的轨迹或 TPR 文件，无法创建分析。");
        submit.disabled = true;
      } else if (result.truncated) {
        showError("任务文件列表过长，当前候选文件可能不完整。");
      }
      form.elements.name.focus();
    } catch (failure) {
      setBusy(false);
      showError(failure.message);
      submit.disabled = true;
    }
  });

  form.elements.trajectory_file.addEventListener("change", matchTrajectory);
  form.addEventListener("change", updateSelection);
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    if (form.elements.do_research.checked && indexLoading) { showError("正在读取索引组，请稍候。"); return; }
    if (!metrics.some((key) => form.elements[key].checked)) {
      showError("请至少选择一项分析指标。");
      return;
    }
    const request = Object.fromEntries(new FormData(form));
    [...metrics, "dry_run"].forEach((key) => { request[key] = form.elements[key].checked; });
    for (const key of ["research_stride", "contact_cutoff_nm", "begin_ns"]) request[key] = Number(request[key]);
    request.end_ns = request.end_ns === "" ? null : Number(request.end_ns);
    if (request.end_ns !== null && request.end_ns <= request.begin_ns) {
      showError("分析结束时间必须大于开始时间。");
      return;
    }
    if (!request.do_research) request.research_ligand_group = "";
    showError("");
    setBusy(true, "正在复制输入并计算校验值，大轨迹可能需要较长时间…");
    let created;
    try {
      created = await api(`/api/jobs/${encodeURIComponent(sourceId)}/analysis`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
      });
    } catch (failure) {
      showError(failure.message);
      return;
    } finally {
      setBusy(false);
    }
    dialog.close();
    toast(`已创建分析任务：${created.name}`, "success");
    try {
      await refreshJobs();
      await selectJob(created.id);
    } catch (failure) {
      toast(`任务已创建，刷新列表失败：${failure.message}`, "error");
    }
  });

  document.querySelector("#analysis-source-job").addEventListener("click", async (event) => {
    const jobId = event.currentTarget.dataset.jobId;
    if (!jobId) return;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      await selectJob(jobId);
    } catch {
      toast("来源任务已删除或暂时无法读取。", "error");
    }
  });
}
