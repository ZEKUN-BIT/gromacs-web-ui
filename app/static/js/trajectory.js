import { api, state } from "./core.js?v=5b84b3e1eaca6ab9";

// Prefer matching output in the same replica, but never hide the pairing from the user.
function suggestedReference(file, choices) {
  const stem = path => path.replace(/\.[^.]+$/, "").toLowerCase();
  const dir = path => path.slice(0, path.lastIndexOf("/") + 1);
  const rank = path => (dir(path) === dir(file) ? 0 : 10) + (stem(path) === stem(file) ? 0 : 4) + (/\.gro$/i.test(path) ? 0 : /\.tpr$/i.test(path) ? 1 : 2);
  return [...choices].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))[0] || "";
}

// Topology and connectivity arrive once; every coordinate batch uses this same atom order.
function trajectoryAtoms(meta, xyz) {
  const atoms = meta.elements.map((elem, i) => {
    const spec = { elem, x: xyz[3*i], y: xyz[3*i+1], z: xyz[3*i+2] };
    if (meta.molecular) {
      const [atom, resn, resi, chain, ss] = meta.molecular.atoms[i];
      Object.assign(spec, { atom, resn, resi, chain: String(chain), hetflag: !ss, ss: ss || "c", bonds: [] });
    }
    return spec;
  });
  for (const [a, b] of meta.molecular?.bonds || []) { atoms[a].bonds.push(b); atoms[b].bonds.push(a); }
  return atoms;
}

export function bindTrajectory() {
  const el = Object.fromEntries("file ref selection refresh load status player canvas prev play next speed seek number stamp".split(" ").map(key => [key, document.getElementById(`traj-${key}`)]));
  let job = null, controller, frameController, seekTimer, playTimer, playing = false;
  let meta, viewer, model, current = -1, batch = [];
  const active = () => state.workspaceView === "detail" && state.detailTab === "trajectory" ? state.activeJobId : null;
  const endpoint = () => `/api/jobs/${encodeURIComponent(job)}/trajectory/`;
  const params = () => new URLSearchParams({ trajectory: el.file.value, topology: el.ref.value, selection: el.selection.value });
  const note = text => { if (el.status.textContent !== text) el.status.textContent = text; };
  const timeFormat = new Intl.NumberFormat("zh-CN", { maximumSignificantDigits: 8, useGrouping: false });
  function pause() {
    playing = false;
    clearTimeout(playTimer);
    el.play.textContent = "播放";
  }
  function clearPlayer() {
    pause(); clearTimeout(seekTimer);
    frameController?.abort(); controller?.abort();
    meta = model = null; batch = []; current = -1;
    viewer?.removeAllModels();
    el.player.hidden = true; el.stamp.textContent = "";
    el.load.disabled = !(job && el.file.value && el.ref.value);
  }
  function choose() {
    clearPlayer();
    note(el.load.disabled ? "需要同一体系的轨迹和拓扑文件。" : "确认拓扑与轨迹的原子顺序一致，然后加载动画。");
  }
  async function list() {
    clearPlayer(); el.file.replaceChildren(); el.ref.replaceChildren();
    el.load.disabled = el.file.disabled = el.ref.disabled = el.selection.disabled = true;
    if (!job) { note("选择任务后查看可用轨迹。"); return; }
    const req = controller = new AbortController();
    note("正在查找轨迹和拓扑…");
    try {
      const data = await api(endpoint() + "files", { signal: req.signal });
      if (req.signal.aborted) return;
      for (const [select, paths] of [[el.file, data.trajectories], [el.ref, data.topologies]]) {
        select.replaceChildren(...paths.map(path => new Option(path, path)));
        if (!paths.length) select.add(new Option("没有可用文件", ""));
        select.disabled = !paths.length;
      }
      el.ref.value = suggestedReference(el.file.value, data.topologies);
      el.selection.disabled = el.load.disabled = !(el.file.value && el.ref.value);
      note(!data.trajectories.length ? "此任务尚无 XTC / TRR 轨迹。生成轨迹后可刷新文件；单个静态结构不能播放。" : !data.topologies.length ? "缺少配套拓扑，请使用包含同一体系 TPR、GRO 或 PDB 的任务。" : `请选择配套文件，然后加载动画。${data.truncated ? "每类最多列出 200 个文件。" : ""}`);
    } catch (error) {
      if (!req.signal.aborted) note(error.status === 404 && error.detail === "Not Found"
        ? "当前 Web 服务还没有轨迹接口。仅刷新页面不会更新后端；请在运行任务结束后重启控制台再试。" : error.message);
    }
  }
  function contextChanged() {
    const next = active();
    if (next === job) return;
    job = next;
    void list();
  }
  async function load() {
    clearPlayer();
    if (el.load.disabled) return;
    const req = controller = new AbortController();
    el.load.disabled = true; note("正在索引保存帧并读取拓扑…");
    try {
      const [data, renderer] = await Promise.all([api(endpoint() + "preview?" + params(), { signal: req.signal, timeoutMs: 60000 }), import("../vendor/3dmol/3Dmol.es6-min.js?v=5b84b3e1eaca6ab9")]);
      if (req.signal.aborted) return;
      meta = data; el.player.hidden = false;
      viewer ||= renderer.createViewer(el.canvas, { backgroundColor: getComputedStyle(el.canvas).getPropertyValue("--viewer-background").trim() || "#fbfcfc", antialias: true, id: "trajectory-webgl" });
      viewer.resize();
      el.number.max = el.seek.max = String(meta.frames);
      el.number.value = el.seek.value = "1";
      el.prev.disabled = el.play.disabled = el.next.disabled = true;
      if (!document.hidden) await seek(0);
    } catch (error) {
      if (!req.signal.aborted) { clearPlayer(); note(error.message || "无法启用三维轨迹预览。"); }
    } finally { if (!req.signal.aborted) el.load.disabled = false; }
  }
  function draw(frame) {
    const first = !model;
    if (first) {
      model = viewer.addModel();
      model.addAtoms(trajectoryAtoms(meta, frame.xyz));
    } else {
      model.selectedAtoms({}).forEach((atom, i) => { atom.x = frame.xyz[3*i]; atom.y = frame.xyz[3*i+1]; atom.z = frame.xyz[3*i+2]; });
    }
    // Reapply styles to invalidate 3Dmol's geometry without resetting the camera.
    if (meta.molecular) {
      model.setStyle({ hetflag: false }, { cartoon: { color: "#328aaa" } });
      model.setStyle({ hetflag: true }, { stick: { radius: 0.16, colorscheme: "orangeCarbon" }, sphere: { scale: 0.26, colorscheme: "orangeCarbon" } });
    } else {
      model.setStyle({}, { sphere: { scale: 0.55, colorscheme: "Jmol" } });
    }
    if (first) {
      const focus = { or: [{ hetflag: false }, { resn: "PET" }] };
      viewer.zoomTo(meta.molecular && model.selectedAtoms(focus).length ? focus : {});
    }
    viewer.render();
    current = frame.index;
    el.seek.value = el.number.value = String(current + 1);
    el.prev.disabled = current === 0; el.next.disabled = current === meta.frames - 1; el.play.disabled = meta.frames < 2;
    el.seek.setAttribute("aria-valuetext", `第 ${current + 1} 帧，共 ${meta.frames} 帧`);
    el.stamp.textContent = `第 ${current + 1} / ${meta.frames} 帧 · ${timeFormat.format(frame.time_ps)} ps`;
    note(`${meta.display_note || ""}显示 ${meta.elements.length} / ${meta.source_atoms} 个原子 · ${meta.molecular ? "蛋白丝带 / 配体球棍 / 水隐藏" : "按元素着色"} · 拖动旋转，滚轮缩放。`);
  }
  async function seek(index) {
    if (!meta || !Number.isInteger(index) || index < 0 || index >= meta.frames) return;
    frameController?.abort();
    const req = frameController = new AbortController();
    try {
      let frame = batch.find(item => item.index === index);
      if (!frame) {
        const query = params(); query.set("revision", meta.revision); query.set("start", String(index));
        const data = await api(endpoint() + "preview?" + query, { signal: req.signal });
        if (req.signal.aborted) return;
        batch = data.frames; frame = batch[0];
      }
      draw(frame);
      if (playing && current + 1 < meta.frames) playTimer = setTimeout(() => void seek(current + 1), 1000 / Number(el.speed.value));
      else pause();
    } catch (error) {
      if (req.signal.aborted) return;
      pause();
      if (error.status === 409) clearPlayer();
      else if (current >= 0) el.number.value = el.seek.value = String(current + 1);
      note(error.message || "帧读取失败，请重新加载动画。");
    }
  }
  function manual(index, delay = 0) {
    pause(); frameController?.abort(); clearTimeout(seekTimer);
    seekTimer = setTimeout(() => void seek(index), delay);
  }
  el.file.addEventListener("change", () => { el.ref.value = suggestedReference(el.file.value, [...el.ref.options].map(o => o.value)); choose(); });
  el.ref.addEventListener("change", choose); el.selection.addEventListener("change", choose);
  el.refresh.addEventListener("click", list); el.load.addEventListener("click", load);
  el.prev.addEventListener("click", () => manual(current - 1)); el.next.addEventListener("click", () => manual(current + 1));
  el.seek.addEventListener("input", () => manual(Number(el.seek.value) - 1, 65));
  el.number.addEventListener("change", () => {
    if (!el.number.value || !el.number.validity.valid) { el.number.reportValidity(); return; }
    manual(el.number.valueAsNumber - 1);
  });
  el.play.addEventListener("click", () => {
    clearTimeout(seekTimer); frameController?.abort();
    if (playing) { pause(); return; }
    playing = true; el.play.textContent = "暂停";
    void seek(current + 1 < meta.frames ? current + 1 : 0);
  });
  for (const event of ["workspace-view-change", "detail-tab-change", "active-job-change", "job-rendered"]) window.addEventListener(event, contextChanged);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      pause(); clearTimeout(seekTimer); frameController?.abort();
      if (current >= 0) el.number.value = el.seek.value = String(current + 1);
    } else {
      contextChanged();
      if (meta && current < 0) void seek(0);
    }
  });
  contextChanged();
}
