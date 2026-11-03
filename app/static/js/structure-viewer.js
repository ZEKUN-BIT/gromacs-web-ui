import { api, els, state, ACTIVE_STATES } from "./core.js?v=a501fb3828ebf5d3";
import { selectedUploadFiles } from "./form-files.js?v=a501fb3828ebf5d3";
import { checkStructureSize, prepareStructure, readStructureResponse, structureFiles, structureFormat } from "./structure-data.js?v=a501fb3828ebf5d3";

const CHAIN_COLORS = ["#43a6a3", "#8b87cd", "#d1a453", "#de8595", "#659ac9", "#85ad64"];
const POLYMER = new Set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL HID HIE HIP HSD HSE HSP CYX CYM ASH GLH LYN MSE ACE NME DA DC DG DT DU A C G U T".split(" "));
let rendererModule;

function loadRenderer() {
  rendererModule ||= import("../vendor/3dmol/3Dmol.es6-min.js?v=a501fb3828ebf5d3").catch(error => { rendererModule = null; throw error; });
  return rendererModule;
}

class StructurePanel {
  constructor(root) {
    this.root = root;
    this.canvas = root.querySelector("[data-structure-canvas]");
    this.select = root.querySelector("[data-structure-file]");
    this.style = root.querySelector("[data-structure-style]");
    this.message = root.querySelector("[data-structure-message]");
    this.overlay = root.querySelector("[data-structure-overlay]");
    this.summary = root.querySelector("[data-structure-summary]");
    this.legend = root.querySelector("[data-structure-chains]");
    this.sources = new Map();
    this.token = 0;
    this.loadedKey = this.loadingKey = "";
    this.select.addEventListener("change", () => this.load());
    this.style.addEventListener("change", () => this.renderStyle());
    root.querySelector("[data-structure-retry]").addEventListener("click", () => this.load(true));
    root.querySelectorAll("[data-structure-action]").forEach(button => button.addEventListener("click", () => this.move(button.dataset.structureAction)));
    this.canvas.addEventListener("keydown", event => {
      const action = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down", "+": "in", "=": "in", "-": "out", Home: "fit" }[event.key];
      if (action && this.root.dataset.state === "ready") { event.preventDefault(); this.move(action); }
    });
    this.resizeObserver = new ResizeObserver(() => {
      if (!root.getClientRects().length || document.hidden) return;
      this.viewer?.resize();
      if (this.viewer && this.root.dataset.state === "ready") {
        const scale = this.viewportScale();
        if (this.fitScale && Math.abs(scale - this.fitScale) > 0.01) {
          const current = this.viewer.getView(), ratio = scale / this.fitScale;
          this.viewer.setView(this.initialView);
          this.viewer.zoom(ratio);
          this.initialView = this.viewer.getView();
          this.viewer.setView(current);
          this.viewer.zoom(ratio);
          this.viewer.render();
          this.fitScale = scale;
        }
      }
      void this.load();
    });
    this.resizeObserver.observe(this.canvas);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (this.viewer) { this.viewer.setBackgroundColor(this.background(), 1); this.viewer.render(); }
    });
  }

  background() { return getComputedStyle(this.root).getPropertyValue("--viewer-background").trim(); }

  viewportScale() { return Math.min(1, this.canvas.clientWidth / Math.max(1, this.canvas.clientHeight)); }

  showMessage(message, status = "empty") {
    this.root.dataset.state = status;
    this.root.setAttribute("aria-busy", String(status === "loading"));
    this.message.textContent = message;
    this.overlay.hidden = status === "ready";
    this.root.querySelector("[data-structure-retry]").hidden = status !== "error";
    this.root.querySelectorAll("[data-structure-action], [data-structure-style]").forEach(control => { control.disabled = status !== "ready"; });
    this.canvas.tabIndex = status === "ready" ? 0 : -1;
  }

  clear(message = "选择任务后查看结构") {
    this.token += 1;
    this.controller?.abort();
    this.loadedKey = this.loadingKey = "";
    this.viewer?.removeAllModels();
    if (this.root.getClientRects().length) this.viewer?.render();
    this.legend.replaceChildren();
    this.summary.textContent = "PDB / GRO";
    delete this.root.dataset.atomCount;
    this.showMessage(message);
  }

  setSources(sources, emptyMessage, truncated = false) {
    const previous = this.select.value;
    this.sources = new Map(sources.map(source => [source.key, source]));
    this.select.replaceChildren(...sources.map(source => new Option(source.name, source.key)));
    this.select.disabled = !sources.length;
    if (!sources.length) {
      this.select.add(new Option("暂无结构文件", ""));
      this.clear(emptyMessage);
      return;
    }
    this.select.value = this.sources.has(previous) ? previous : sources[0].key;
    this.select.title = truncated ? "结构较多，显示每种格式的前 100 个文件" : "选择要显示的结构文件";
    void this.load();
  }

  async load(force = false) {
    const source = this.sources.get(this.select.value);
    if (!source || !this.root.getClientRects().length || document.hidden) return;
    const key = `${source.key}:${source.revision}`;
    if (!force && (this.loadedKey === key || this.loadingKey === key)) return;
    this.clear("正在加载结构…");
    const token = this.token;
    this.loadingKey = key;
    this.showMessage("正在加载结构…", "loading");
    this.controller = new AbortController();
    const controller = this.controller;
    const timeout = setTimeout(() => controller.abort(), 25_000);
    try {
      checkStructureSize(source.size);
      const text = source.file ? await source.file.text() : await readStructureResponse(await fetch(source.url, { signal: controller.signal, credentials: "same-origin" }));
      if (token !== this.token) return;
      const prepared = prepareStructure(text, source.format);
      const renderer = await loadRenderer();
      if (token !== this.token) return;
      if (controller.signal.aborted) throw new Error("结构加载超时，请重试。");
      if (!this.root.getClientRects().length || document.hidden) return;
      if (!this.viewer) {
        try {
          this.viewer = renderer.createViewer(this.canvas, { backgroundColor: this.background(), antialias: true, id: `${this.root.id}-webgl` });
        } catch {
          this.canvas.replaceChildren();
          throw new Error("无法启用三维显示。请开启浏览器硬件加速后重试。");
        }
      }
      const model = this.viewer.addModel(prepared.text, source.format, { keepH: false });
      const atoms = model.selectedAtoms({});
      if (!atoms.length || atoms.some(atom => ![atom.x, atom.y, atom.z].every(Number.isFinite))) throw new Error("未能解析有效的原子坐标，请检查结构文件。");
      this.hasBackbone = atoms.filter(atom => atom.atom === "CA" && atom.elem === "C").length >= 3;
      const chains = [...new Set(atoms.map(atom => atom.chain || ""))];
      this.chainColors = new Map(chains.map((chain, index) => [chain, CHAIN_COLORS[index % CHAIN_COLORS.length]]));
      this.legend.replaceChildren(...chains.slice(0, 8).map(chain => {
        const item = document.createElement("span"), swatch = document.createElement("i");
        swatch.style.backgroundColor = this.chainColors.get(chain);
        item.append(swatch, document.createTextNode(chain ? `链 ${chain}` : "未标注链"));
        return item;
      }));
      const notes = [prepared.waterAtoms ? "水分子已隐藏" : "", prepared.firstModelOnly ? "显示第一模型" : ""].filter(Boolean);
      this.baseSummary = `${atoms.length.toLocaleString()} 个原子${notes.length ? ` · ${notes.join(" · ")}` : ""}`;
      this.root.dataset.atomCount = String(atoms.length);
      this.loadedKey = key;
      this.showMessage("", "ready");
      this.renderStyle();
      this.viewer.zoomTo();
      this.fitScale = this.viewportScale();
      this.viewer.zoom(0.9 * this.fitScale);
      this.initialView = this.viewer.getView();
      this.viewer.render();
      this.canvas.setAttribute("aria-label", `${source.name} 的三维结构。方向键旋转，加减键缩放，Home 键重置。`);
    } catch (error) {
      if (token !== this.token) return;
      this.viewer?.removeAllModels();
      this.viewer?.render();
      this.showMessage(controller.signal.aborted ? "结构加载超时，请重试。" : error.message || "结构预览加载失败，请重试。", "error");
      this.summary.textContent = "预览失败不影响模拟提交";
    } finally {
      clearTimeout(timeout);
      if (token === this.token) this.loadingKey = "";
    }
  }

  renderStyle() {
    if (!this.viewer || this.root.dataset.state !== "ready") return;
    const style = this.style.value;
    const colorfunc = atom => this.chainColors.get(atom.chain || "") || CHAIN_COLORS[0];
    this.viewer.setStyle({}, {});
    if (style === "cartoon" && this.hasBackbone) {
      this.viewer.setStyle({}, { cartoon: { colorfunc } });
      this.viewer.setStyle({ predicate: atom => !POLYMER.has(atom.resn?.toUpperCase()) }, { stick: { radius: 0.16, colorscheme: "Jmol" }, sphere: { scale: 0.25, colorscheme: "Jmol" } });
    } else if (style === "line") {
      this.viewer.setStyle({}, { line: { colorscheme: "Jmol" } });
    } else {
      this.viewer.setStyle({}, { stick: { radius: 0.15, colorscheme: "Jmol" }, sphere: { scale: 0.24, colorscheme: "Jmol" } });
    }
    this.summary.textContent = this.baseSummary + (style === "cartoon" && !this.hasBackbone ? " · 无蛋白主链，使用球棍" : "");
    this.viewer.render();
  }

  move(action) {
    if (!this.viewer || this.root.dataset.state !== "ready") return;
    if (action === "fit") this.viewer.setView(this.initialView);
    else if (action === "in" || action === "out") this.viewer.zoom(action === "in" ? 1.2 : 1 / 1.2);
    else this.viewer.rotate(["left", "up"].includes(action) ? -15 : 15, ["up", "down"].includes(action) ? "x" : "y");
    this.viewer.render();
  }
}

export function bindStructureViewers() {
  const compose = new StructurePanel(document.querySelector("#compose-structure"));
  const detail = new StructurePanel(document.querySelector("#detail-structure"));
  let uploads = [], uploadRevision = 0, job = null, listedKey = "", requestedAt = 0, pendingKey = "", generation = 0, listingController;

  function updateUploads() {
    const next = selectedUploadFiles().filter(file => structureFormat(file.name));
    if (next.length === uploads.length && next.every((file, index) => file === uploads[index])) return;
    uploads = next;
    uploadRevision += 1;
    compose.setSources(next.map((file, index) => ({ key: `${index}:${file.name}`, name: file.name, size: file.size, revision: uploadRevision, format: structureFormat(file.name), file })), "添加 PDB 或 GRO 文件，预览蛋白结构");
  }

  function resetJob() {
    generation += 1;
    listingController?.abort();
    listedKey = pendingKey = "";
    detail.setSources([], state.activeJobId ? "正在查找结构文件…" : "选择任务后查看结构");
  }

  async function refreshStructures(force = false) {
    if (document.hidden || state.workspaceView !== "detail" || !job || job.id !== state.activeJobId) return;
    const key = `${job.id}:${job.status}:${job.step_index}:${job.storage_revision || 0}`;
    if (!force && (pendingKey === key || (listedKey === key && (!ACTIVE_STATES.has(job.status) || Date.now() - requestedAt < 5000)))) return;
    const id = job.id, requestGeneration = ++generation;
    listingController?.abort();
    listingController = new AbortController();
    pendingKey = key;
    requestedAt = Date.now();
    try {
      const pages = await Promise.all([".pdb", ".gro"].map(extension => api(`/api/jobs/${encodeURIComponent(id)}/files?${new URLSearchParams({ search: extension, limit: "100" })}`, { signal: listingController.signal })));
      if (requestGeneration !== generation || state.activeJobId !== id) return;
      const files = structureFiles(pages.flatMap(page => page.files || []));
      detail.setSources(files.map(file => ({ key: `${id}:${file.path}`, name: file.path, size: file.size, revision: `${file.size}:${file.modified}`, format: structureFormat(file.path), url: `/api/jobs/${encodeURIComponent(id)}/download?path=${encodeURIComponent(file.path)}` })), "此任务尚无 PDB/GRO 结构。结构文件生成后会在这里显示。", pages.some(page => page.has_more || page.truncated));
      listedKey = key;
    } catch (error) {
      if (requestGeneration !== generation) return;
      detail.clear("结构列表读取失败，请点击右上角刷新。");
      detail.summary.textContent = error.message || "连接失败";
      // A failing endpoint must not be retried on every progress update.
      listedKey = key;
    } finally {
      if (requestGeneration === generation) pendingKey = "";
    }
  }

  els.jobForm.addEventListener("change", updateUploads);
  els.jobForm.addEventListener("reset", () => queueMicrotask(updateUploads));
  document.querySelectorAll("[data-structure-add]").forEach(button => button.addEventListener("click", () => els.files.click()));
  document.querySelector("[data-structure-refresh]").addEventListener("click", () => { void refreshStructures(true); void detail.load(true); });
  window.addEventListener("active-job-change", () => { resetJob(); job = state.activeJob?.id === state.activeJobId ? state.activeJob : state.jobs.find(item => item.id === state.activeJobId); void refreshStructures(); });
  window.addEventListener("job-rendered", event => {
    const next = event.detail;
    if (!next || (job && next.id !== job.id)) resetJob();
    job = next;
    void refreshStructures();
  });
  window.addEventListener("workspace-view-change", () => {
    if (state.workspaceView !== "detail") { generation += 1; listingController?.abort(); pendingKey = ""; }
    updateUploads();
    void compose.load();
    void detail.load();
    void refreshStructures();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { void compose.load(); void detail.load(); void refreshStructures(); } });
}
