import { api, state, toast, showWorkspaceView, confirmDialog } from "./core.js?v=621bb939e2b97a80";
import { refreshJobs, selectJob } from "./jobs.js?v=621bb939e2b97a80";
import { applyProtocol } from "./library.js?v=621bb939e2b97a80";
import { renderMessage } from "./agent-content.js?v=621bb939e2b97a80";

const SESSION_KEY = "gromacs.agent-session";
const ACTION_STATUS = { pending: "等待确认", running: "执行中", succeeded: "已完成", failed: "执行失败", rejected: "已拒绝", expired: "预览已过期", uncertain: "结果待核实" };
const DRAFT_KEY = "new";
const jsonRequest = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function bindAgent() {
  const panel = document.querySelector("#agent-panel");
  if (!panel || panel.dataset.bound) return;
  panel.dataset.bound = "true";
  const find = id => document.getElementById(`agent-${id}`);
  const settingsDialog = find("settings");
  const composeView = document.getElementById("compose-view");
  let config = null, session = null, busy = false, uncertain = false, configLoadFailed = false;
  let pendingSend = null, contextJobId = null, pollTimer = null, pollFailures = 0;
  let savedSessionId = null;
  try { savedSessionId = sessionStorage.getItem(SESSION_KEY); } catch { /* Storage is optional. */ }
  const drafts = new Map();
  const modelLists = new Map();
  let draftKey = savedSessionId || DRAFT_KEY, renderedSessionId = null;
  let composing = false, unseenUpdates = false;
  const scroll = find("scroll");

  function updateComposer() {
    const input = find("message");
    input.style.height = "auto";
    const limit = Number.parseFloat(getComputedStyle(input).maxHeight) || 160;
    input.style.height = `${Math.min(Math.max(input.scrollHeight, 48), limit)}px`;
    input.style.overflowY = input.scrollHeight > input.clientHeight ? "auto" : "hidden";
    if (find("message-count")) find("message-count").textContent = `${input.value.length} / ${input.maxLength}`;
  }

  function saveDraft() { drafts.set(draftKey, find("message").value); }

  function useDraft(id, carry = false) {
    const key = id || DRAFT_KEY;
    if (draftKey === key) return;
    saveDraft();
    find("message").value = drafts.get(key) ?? (carry ? find("message").value : "");
    if (carry) drafts.delete(draftKey);
    draftKey = key;
    saveDraft();
    updateComposer();
  }

  function nearLatest() { return !scroll || scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop <= 64; }

  function updateLatest() {
    const button = find("scroll-latest");
    if (!button) return;
    const empty = !session?.messages?.length;
    if (empty || nearLatest()) unseenUpdates = false;
    button.hidden = empty || nearLatest();
    button.textContent = unseenUpdates ? "有新消息 · 回到最新 ↓" : "回到最新 ↓";
  }

  function scrollLatest() {
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
    unseenUpdates = false;
    updateLatest();
  }

  function updateConversationLabel() {
    const provider = session?.provider || config?.provider;
    const model = session?.model || config?.model;
    const label = config?.providers?.find(item => item.id === provider)?.label || provider;
    find("current-model").textContent = !model ? "请选择服务商与模型"
      : session?.provider === config?.provider && session?.model !== config?.model
        ? `上次：${label} · ${model}；下次：${config.model}`
        : `${session ? "本次对话" : "新对话将使用"}：${label} · ${model}`;
    const title = session?.messages?.find(message => message.role === "user")?.content || "新对话";
    if (find("conversation-title")) {
      find("conversation-title").textContent = title.length > 56 ? `${title.slice(0, 56)}…` : title;
      find("conversation-title").title = title;
    }
  }

  function errorMessage(message = "") {
    find("error").textContent = message;
    find("error").hidden = !message;
    if (find("settings-error")) {
      find("settings-error").textContent = message;
      find("settings-error").hidden = !message;
    }
  }

  function status(message) {
    if (find("status").textContent !== message) find("status").textContent = message;
    if (find("settings-status")) find("settings-status").textContent = message;
  }

  function rememberSession(id) {
    savedSessionId = id;
    find("history").value = id || "";
    try {
      if (id) sessionStorage.setItem(SESSION_KEY, id);
      else sessionStorage.removeItem(SESSION_KEY);
    } catch { /* A conversation still works without browser storage. */ }
  }

  function updateControls() {
    const running = busy || session?.status === "running";
    const blocked = running || uncertain;
    const pending = session?.actions.some(action => action.status === "pending");
    const providerChanged = session?.provider && session.provider !== config?.provider;
    find("config-fields").disabled = blocked || !config;
    find("send").disabled = blocked || pending || providerChanged || !config?.has_api_key || !find("message").value.trim();
    find("message").disabled = blocked;
    find("new-session").disabled = blocked || !config;
    find("delete-session").disabled = blocked || !session;
    find("history").disabled = blocked || find("history").options.length < 2;
    find("refresh").disabled = busy;
    find("include-context").disabled = blocked || !contextJobId;
    const profile = config?.profiles?.[find("provider").value];
    find("test").disabled = blocked || !config || !(profile?.has_api_key || find("api-key").value.trim());
    find("load-models").disabled = blocked || !profile?.has_api_key || Boolean(find("api-key").value.trim());
    find("load-models").title = find("api-key").value.trim() ? "请先保存新密钥，再获取可用模型。" : "使用此服务商已保存的密钥读取模型列表。";
    find("saved-profiles").querySelectorAll("button").forEach(button => { button.disabled = blocked; });
    find("clear-key").disabled = blocked || profile?.key_source !== "saved";
    find("send").textContent = running ? "处理中…" : uncertain ? "等待确认状态" : providerChanged ? "请新建对话" : pending ? "请先处理操作" : "发送";
    find("actions").querySelectorAll("button").forEach(button => { button.disabled = blocked; });
    find("messages").setAttribute("aria-busy", String(running));
    const progress = find("progress");
    if (progress) {
      progress.hidden = !blocked;
      progress.textContent = uncertain ? pollFailures >= 6 ? "自动同步已暂停，请点击“刷新状态”。" : "正在核实上次请求的结果…" : session?.status === "running" ? "助手正在处理请求…" : "正在同步对话…";
    }
    const configured = Boolean(config?.has_api_key);
    if (find("connection-status")) {
      find("connection-status").textContent = configured ? config.key_source === "environment" ? "已读取环境配置 · 自动沿用" : "设置已保存 · 自动沿用" : config ? "尚未配置 API" : configLoadFailed ? "配置读取失败" : "正在读取配置…";
      find("connection-status").dataset.configured = String(configured);
    }
    if (find("config-notice")) find("config-notice").hidden = !config || configured;
    updateConversationLabel();
  }

  function updateContext() {
    const previous = contextJobId;
    contextJobId = state.activeJobId || null;
    if (previous !== contextJobId) find("include-context").checked = false;
    const job = state.jobs?.find(item => item.id === contextJobId);
    find("context-label").textContent = contextJobId
      ? `附带当前任务：${job?.name || contextJobId}（${contextJobId}）`
      : "未选择任务；可在任务列表选择后附带摘要。";
    updateControls();
  }

  function renderSavedProfiles() {
    find("saved-profiles").replaceChildren(...config.providers.map(spec => {
      const profile = config.profiles[spec.id];
      const card = element("article", undefined, "agent-saved-profile");
      card.dataset.provider = spec.id;
      card.dataset.active = String(config.provider === spec.id);
      const head = element("div", undefined, "agent-profile-heading");
      head.append(element("h4", spec.label));
      if (config.provider === spec.id) head.append(element("span", "当前使用"));
      const details = element("dl");
      for (const [label, value] of [
        [profile.is_saved || profile.has_api_key ? "已配置模型" : "默认模型", profile.model],
        ["API Key", profile.key_source === "saved" ? "已保存 · 自动沿用" : profile.key_source === "environment" ? "来自环境变量" : "尚未配置"],
        ["服务地址", spec.endpoint],
      ]) details.append(element("dt", label), element("dd", value));
      const edit = element("button", "查看 / 修改", "tool-button");
      edit.type = "button";
      edit.setAttribute("aria-label", `查看或修改 ${spec.label}`);
      edit.addEventListener("click", () => {
        find("provider").value = spec.id;
        showProfile();
        find("model-choice").focus();
      });
      card.append(head, details, edit);
      return card;
    }));
  }

  function selectModel() {
    const choice = find("model-choice").value;
    const custom = choice === "__custom__";
    if (!custom) find("model").value = choice;
    find("custom-model-field").hidden = !custom;
    find("model").disabled = !custom;
  }

  function renderModelChoices(model) {
    const provider = find("provider").value;
    const spec = config.providers.find(item => item.id === provider);
    const options = modelLists.get(provider) || spec.models || [];
    const choices = new Map(options.map(item => [item.id, item.label === item.id ? item.id : `${item.label} · ${item.id}`]));
    if (model && !choices.has(model)) choices.set(model, `${model}（当前配置）`);
    find("model-choice").replaceChildren(...[...choices].map(([id, label]) => {
      const option = element("option", label);
      option.value = id;
      return option;
    }), Object.assign(element("option", "自定义模型 ID…"), { value: "__custom__" }));
    find("model-choice").value = model || "__custom__";
    find("model").value = model;
    selectModel();
    find("model-list-status").textContent = modelLists.has(provider)
      ? `已从服务商读取 ${options.length} 个模型。选择后点击“保存设置”生效。`
      : "使用已保存的密钥读取服务商模型列表，不发送对话。新密钥请先保存。";
  }

  function showProfile() {
    const provider = find("provider").value;
    const profile = config?.profiles?.[provider];
    const spec = config?.providers?.find(item => item.id === provider);
    renderModelChoices(profile?.model || spec?.default_model || "");
    find("api-key").value = "";
    find("key-status").textContent = profile?.key_source === "saved"
      ? "此服务商已保存密钥。密钥仅保存在服务端，页面不会读取或显示。"
      : profile?.key_source === "environment" ? "此服务商使用服务器环境变量中的密钥。移除保存的密钥不会删除环境变量。"
        : "此服务商尚未配置密钥。";
    find("endpoint").textContent = spec?.endpoint ? `服务地址：${spec.endpoint}` : "";
    updateControls();
  }

  function setConfig(value) {
    config = value;
    configLoadFailed = false;
    find("provider").replaceChildren(...config.providers.map(provider => {
      const option = element("option", provider.label);
      option.value = provider.id;
      return option;
    }));
    find("provider").value = config.provider;
    renderSavedProfiles();
    showProfile();
  }

  async function loadModels() {
    if (find("load-models").disabled) return;
    const provider = find("provider").value;
    const model = find("model").value;
    busy = true; updateControls(); errorMessage();
    find("model-list-status").textContent = "正在读取服务商的可用模型…";
    try {
      const result = await api(`/api/agent/models?provider=${encodeURIComponent(provider)}`);
      modelLists.set(provider, result.models);
      renderModelChoices(model);
      status("模型列表已更新；选择模型并保存后，下一轮对话使用新模型。");
    } catch (error) {
      errorMessage(error.message);
      find("model-list-status").textContent = "模型列表读取失败。可重试，或选择已有模型、自定义模型 ID。";
    } finally { busy = false; updateControls(); }
  }

  async function refreshHistory() {
    try {
      const result = await api("/api/agent/sessions");
      const placeholder = element("option", "选择已保存的对话…");
      placeholder.value = ""; placeholder.disabled = true;
      find("history").replaceChildren(placeholder, ...result.sessions.map(item => {
        const date = item.updated_at ? new Date(item.updated_at).toLocaleString() : "";
        const label = config?.providers.find(provider => provider.id === item.provider)?.label || item.provider;
        const option = element("option", `${item.title || "新对话"} · ${label} · ${date}`);
        option.value = item.id;
        return option;
      }));
      find("history").value = savedSessionId || "";
    } catch (error) { errorMessage(`对话列表加载失败：${error.message}`); }
    updateControls();
  }

  function actionCard(action) {
    const card = element("article", undefined, "agent-action");
    card.dataset.actionId = action.id;
    card.append(element("h3", action.title || action.tool), element("small", ACTION_STATUS[action.status] || action.status));
    const details = element("details", undefined, "agent-action-details");
    details.open = action.status === "pending";
    details.append(element("summary", "操作预览"), element("pre", JSON.stringify(action.preview ?? action.arguments ?? {}, null, 2)));
    card.append(details);
    if (action.expires_at && action.status === "pending") card.append(element("small", `预览有效期至：${new Date(action.expires_at).toLocaleString()}`));
    if (action.error) card.append(element("p", action.error, "product-error"));
    if (action.status === "uncertain") card.append(element("p", "请先在任务列表核实结果，再决定下一步，避免重复执行。"));
    const controls = element("div", undefined, "product-actions");
    if (action.status === "pending") {
      for (const [decision, label] of [["approve", "确认执行"], ["reject", "拒绝"]]) {
        const button = element("button", label, "tool-button");
        button.type = "button"; button.dataset.decision = decision;
        button.addEventListener("click", () => decide(action.id, decision));
        controls.append(button);
      }
    }
    if (action.status === "succeeded" && action.result?.kind === "simulation_draft") {
      card.append(element("p", "草案已准备。应用会替换当前表单参数并清空文件选择；请重新选择输入文件、核对参数后提交。"));
      const button = element("button", "应用草案到新任务表单", "tool-button");
      button.type = "button";
      button.addEventListener("click", () => {
        applyProtocol(action.result.params);
        document.getElementById("manual-compose-title")?.scrollIntoView({ block: "start", behavior: "smooth" });
        toast("AI 草案已应用，请选择输入文件并核对参数后提交。", "success");
      });
      controls.append(button);
    }
    const targetJobId = action.result?.job_id || action.result?.job?.id;
    if (targetJobId) {
      const button = element("button", "查看任务", "tool-button");
      button.type = "button";
      button.addEventListener("click", async () => {
        try { await selectJob(targetJobId); showWorkspaceView("detail"); }
        catch (error) { errorMessage(error.message); }
      });
      controls.append(button);
    }
    card.append(controls);
    return card;
  }

  // Keep unchanged nodes in place so polling preserves selections, focus and expanded previews.
  function reconcile(container, values, makeNode, keyFor, options = {}) {
    const existing = options.existing || new Map([...container.children].map(node => [node.dataset.renderKey, node]));
    let changed = false;
    values.forEach((value, index) => {
      const key = keyFor(value, index), signature = JSON.stringify(value);
      let node = existing.get(key);
      existing.delete(key);
      if (!node || node.dataset.signature !== signature) {
        const replacement = makeNode(value);
        replacement.dataset.renderKey = key;
        replacement.dataset.signature = signature;
        if (node) {
          const wasOpen = node.querySelector("details")?.open;
          const decision = node.contains(document.activeElement) ? document.activeElement.dataset.decision : null;
          if (wasOpen !== undefined && replacement.querySelector("details")) replacement.querySelector("details").open = wasOpen;
          node.replaceWith(replacement);
          if (decision) replacement.querySelector(`[data-decision="${decision}"]`)?.focus({ preventScroll: true });
        }
        node = replacement;
        changed = true;
      }
      if (container.children[index] !== node) {
        container.insertBefore(node, container.children[index] || null);
        changed = true;
      }
    });
    if (options.prune !== false) for (const node of existing.values()) { node.remove(); changed = true; }
    return changed;
  }

  function renderSession() {
    const switched = renderedSessionId !== (session?.id || savedSessionId);
    const follow = switched || nearLatest();
    if (switched) {
      find("messages").replaceChildren();
      find("active-actions").replaceChildren();
      find("past-actions").replaceChildren();
      find("action-history").open = false;
      renderedSessionId = session?.id || savedSessionId;
    }
    const messagesChanged = reconcile(find("messages"), session?.messages || [], renderMessage, (message, index) => String(message.id || index));
    const pastStatuses = new Set(["succeeded", "rejected", "expired"]);
    const activeActions = [], pastActions = [];
    for (const action of session?.actions || []) (pastStatuses.has(action.status) ? pastActions : activeActions).push(action);
    const existingActions = new Map([...find("actions").querySelectorAll(".agent-action")].map(node => [node.dataset.renderKey, node]));
    const activeChanged = reconcile(find("active-actions"), activeActions, actionCard, action => action.id, { existing: existingActions, prune: false });
    const pastChanged = reconcile(find("past-actions"), pastActions, actionCard, action => action.id, { existing: existingActions });
    const actionsChanged = activeChanged || pastChanged;
    find("action-history").hidden = !pastActions.length;
    find("action-history-count").textContent = String(pastActions.length);
    if (find("empty")) find("empty").hidden = Boolean(session?.messages?.length);
    updateControls();
    if (follow) scrollLatest();
    else {
      if (messagesChanged || actionsChanged) unseenUpdates = true;
      updateLatest();
    }
  }

  function acceptSession(value, { carryDraft = false } = {}) {
    useDraft(value.id, carryDraft);
    session = value;
    rememberSession(value.id);
    uncertain = false;
    pollFailures = 0;
    if (pendingSend?.session_id === value.id) {
      const added = (session.messages || []).slice(pendingSend.previousCount);
      if (added.some(message => message.role === "user" && message.content === pendingSend.message)) {
        if (find("message").value.trim() === pendingSend.message) {
          find("message").value = "";
          saveDraft();
          updateComposer();
        }
        pendingSend = null;
      }
    }
    renderSession();
    if (value.last_error) errorMessage(value.last_error);
    status(value.status === "running" ? "助手正在处理；切换页面后仍可稍后恢复对话。"
      : value.provider && value.provider !== config?.provider ? "此对话绑定的服务商与当前设置不同。请点击“新对话”使用当前服务商。"
        : value.actions.some(action => action.status === "pending") ? "请先逐项确认或拒绝待处理操作，再继续发送消息。"
          : value.model && value.model !== config?.model ? "模型设置已更新，下次发送将使用当前保存的模型。"
            : "对话已同步。可继续提问或确认操作。");
    schedulePoll();
  }

  function panelVisible() { return !document.hidden && !composeView?.hidden; }

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    if (panelVisible() && savedSessionId && (uncertain || session?.status === "running") && pollFailures < 6) {
      pollTimer = window.setTimeout(() => { if (panelVisible()) syncSession(); }, Math.min(1500 * (pollFailures + 1), 8000));
    }
  }

  async function syncSession(preserveError = false) {
    if (busy || !savedSessionId) { schedulePoll(); return; }
    const targetId = savedSessionId;
    busy = true; updateControls();
    try {
      const value = await api(`/api/agent/sessions/${encodeURIComponent(targetId)}`);
      if (targetId !== savedSessionId) return;
      const changedAction = value.actions.some(action => action.status === "succeeded"
        && !session?.actions.some(previous => previous.id === action.id && previous.status === "succeeded"));
      if (!preserveError) errorMessage();
      acceptSession(value);
      if (changedAction) await refreshJobs().catch(error => toast(`任务列表刷新失败：${error.message}`, "error"));
    } catch (error) {
      pollFailures++;
      if (error.status === 404) {
        useDraft(null); rememberSession(null); session = null; pendingSend = null; uncertain = false;
        renderSession(); status("原对话已不可用，可以新建对话。");
      } else {
        uncertain = true;
        status(pollFailures >= 6 ? "多次读取状态失败，自动同步已暂停。请点击“刷新状态”重试。" : "暂时无法核实对话状态；请刷新状态后继续，避免重复操作。");
      }
      errorMessage(error.message);
    } finally { busy = false; updateControls(); schedulePoll(); }
  }

  async function createSession({ carryDraft = false } = {}) {
    const value = await api("/api/agent/sessions", jsonRequest({}));
    pendingSend = null; acceptSession(value, { carryDraft });
    await refreshHistory();
    return value;
  }

  async function send(event) {
    event.preventDefault();
    const message = find("message").value.trim();
    if (find("send").disabled || !message) return;
    const context = find("include-context").checked ? contextJobId : undefined;
    find("action-history").open = false;
    scrollLatest();
    busy = true; updateControls(); errorMessage(); status("助手正在读取请求并规划操作…");
    try {
      if (!session) await createSession({ carryDraft: true });
      if (!pendingSend || pendingSend.message !== message || pendingSend.context_job_id !== context) {
        pendingSend = { message, context_job_id: context, request_id: requestId(), previousCount: session.messages.length, session_id: session.id };
      }
      const { previousCount: _previousCount, session_id: _sessionId, ...body } = pendingSend;
      acceptSession(await api(`/api/agent/sessions/${encodeURIComponent(session.id)}/messages`, jsonRequest(body)));
    } catch (error) {
      errorMessage(error.message);
      uncertain = Boolean(savedSessionId);
      status(uncertain ? "请求结果暂未确认，正在同步对话；请勿重复提交。" : "未能创建对话，请稍后重试。");
    } finally {
      busy = false; updateControls();
      if (uncertain) await syncSession(true); else schedulePoll();
    }
  }

  async function decide(id, decision) {
    if (busy || uncertain || session?.status === "running") return;
    find("action-history").open = true;
    busy = true; updateControls(); errorMessage(); status(decision === "approve" ? "正在执行已确认的操作…" : "正在拒绝操作…");
    try {
      acceptSession(await api(`/api/agent/sessions/${encodeURIComponent(session.id)}/actions/${encodeURIComponent(id)}`, jsonRequest({ decision })));
      await refreshJobs().catch(error => toast(`操作状态已更新；任务列表刷新失败：${error.message}`, "error"));
    } catch (error) {
      errorMessage(error.message); uncertain = true;
      status("操作结果暂未确认，正在同步状态；请勿重复执行。");
    } finally {
      busy = false; updateControls();
      if (uncertain) await syncSession(true); else schedulePoll();
    }
  }

  async function saveConfig(event, { clear = false, test = false } = {}) {
    event?.preventDefault();
    if (busy || uncertain || session?.status === "running") return;
    if (test && find("test").disabled) return;
    find("model").value = find("model").value.trim();
    if (!find("config-form").reportValidity()) return;
    const body = { provider: find("provider").value, model: find("model").value.trim() };
    const key = find("api-key").value.trim();
    if (clear) body.clear_api_key = true;
    else if (key) body.api_key = key;
    find("api-key").value = "";
    let saved = false;
    busy = true; updateControls(); errorMessage();
    try {
      const value = await api("/api/agent/config", jsonRequest(body, "PUT"));
      if (body.api_key || clear) modelLists.delete(body.provider);
      setConfig(value);
      saved = true;
      status(clear ? "已移除保存的密钥；若服务器配置了环境变量，将继续使用该密钥。"
        : session?.provider && session.provider !== config.provider ? "模型设置已保存。此对话仍绑定原服务商，请点击“新对话”使用新服务商。"
          : session?.model && session.model !== config.model ? "模型设置已保存，下次发送将使用新模型。上方保留上次回复使用的模型信息。"
            : "模型设置已保存，今后会自动沿用，可以直接开始对话。");
      if (test) {
        status("设置已保存，正在测试当前服务商与模型…");
        const result = await api("/api/agent/connection-test", jsonRequest({}));
        status(`${result.message}（${result.provider} · ${result.model}）`);
      } else if (!clear && config.has_api_key) settingsDialog.close();
    } catch (error) {
      errorMessage(error.message);
      status(saved ? "设置已保存，连接测试未通过。请根据下方错误排查后重试。" : "设置保存失败，请重新输入密钥后重试。");
    }
    finally {
      busy = false; updateControls();
      if (saved && !clear && !settingsDialog.open && panelVisible() && !find("message").disabled) find("message").focus({ preventScroll: true });
    }
  }

  function preparePrompt(prompt) {
    if (!prompt) return;
    const message = find("message");
    let notice = "";
    if (busy || uncertain || session?.status === "running") notice = "当前对话仍在处理或等待同步，请完成后再继续；你的输入已保留。";
    else if (session?.actions.some(action => action.status === "pending")) notice = "请先确认或拒绝当前操作，再继续；你的输入已保留。";
    else if (session?.provider && session.provider !== config?.provider) notice = "当前对话使用其他服务商，请先新建对话；你的输入已保留。";
    else if (message.value.trim() && message.value.trim() !== prompt) notice = "已有未发送的内容，已为你保留。请先发送或清空，再使用快捷问题。";
    if (notice) {
      status(notice);
      if (busy) toast(notice);
      return;
    }
    message.value = prompt;
    saveDraft(); updateComposer(); updateControls();
    status(config?.has_api_key ? "问题已填入，核对后点击“发送”。" : "问题已填入。请先在模型与 API 设置中配置密钥，再点击“发送”。");
  }

  async function initialize() {
    if (busy) return;
    configLoadFailed = false;
    busy = true; updateControls(); errorMessage(); status("正在读取已保存的模型设置…");
    let loaded = false;
    try {
      setConfig(await api("/api/agent/config"));
      loaded = true;
      await refreshHistory();
      status(config.has_api_key ? "已自动沿用保存的配置，可以直接发送消息。" : "首次使用请配置 API Key；保存后无需重复填写。");
    } catch (error) {
      configLoadFailed = true;
      errorMessage(error.message);
      status("模型配置读取失败，请点击“刷新状态”重试。");
    } finally { busy = false; updateControls(); }
    if (loaded && savedSessionId) await syncSession(true);
  }

  function openAgent() {
    document.body.classList.remove("sidebar-open");
    document.querySelector("#toggle-sidebar")?.setAttribute("aria-expanded", "false");
    showWorkspaceView("compose");
    updateComposer(); updateContext();
    panel.scrollIntoView({ block: "start", behavior: "smooth" });
    if (!find("message").disabled) find("message").focus({ preventScroll: true });
  }

  async function openSettings() {
    if (!settingsDialog.open) settingsDialog.showModal();
    find("api-key").value = "";
    if (busy || uncertain || session?.status === "running") return;
    busy = true; updateControls(); errorMessage(); status("正在读取已保存的模型设置…");
    try {
      setConfig(await api("/api/agent/config"));
      status(config.has_api_key ? "已保存的密钥会自动沿用；仅更换密钥时需要填写。" : "填写 API Key 并保存，以后可直接在首页对话。");
    } catch (error) { errorMessage(error.message); status("配置读取失败，请关闭设置后点击“刷新状态”重试。"); }
    finally { busy = false; updateControls(); }
    if (settingsDialog.open && !find("provider").disabled) {
      settingsDialog.scrollTop = 0;
      if (Object.values(config.profiles).some(profile => profile.is_saved || profile.has_api_key)) find("settings-title").focus({ preventScroll: true });
      else find("provider").focus();
    }
  }

  document.querySelector("#open-agent").addEventListener("click", openAgent);
  find("open-settings").addEventListener("click", openSettings);
  document.querySelectorAll("[data-agent-configure]").forEach(button => button.addEventListener("click", openSettings));
  find("settings-close").addEventListener("click", () => settingsDialog.close());
  settingsDialog.addEventListener("close", () => { find("api-key").value = ""; });
  scroll?.addEventListener("scroll", updateLatest, { passive: true });
  find("action-history").addEventListener("toggle", updateLatest);
  find("scroll-latest")?.addEventListener("click", scrollLatest);
  panel.querySelectorAll("[data-agent-starter]").forEach(button => {
    button.addEventListener("click", () => {
      preparePrompt(button.dataset.agentStarter);
      if (!find("message").disabled) find("message").focus({ preventScroll: true });
    });
  });
  function visibilityChanged() {
    window.clearTimeout(pollTimer);
    if (!panelVisible()) return;
    updateComposer(); updateLatest();
    if (uncertain || session?.status === "running") {
      pollFailures = 0;
      syncSession();
    }
  }
  document.addEventListener("visibilitychange", visibilityChanged);
  if (composeView) new MutationObserver(visibilityChanged).observe(composeView, { attributes: true, attributeFilter: ["hidden"] });
  find("provider").addEventListener("change", showProfile);
  find("model-choice").addEventListener("change", () => {
    selectModel();
    if (!find("custom-model-field").hidden) find("model").focus();
  });
  find("load-models").addEventListener("click", loadModels);
  find("api-key").addEventListener("input", updateControls);
  find("config-form").addEventListener("submit", saveConfig);
  find("clear-key").addEventListener("click", event => saveConfig(event, { clear: true }));
  find("test").addEventListener("click", event => saveConfig(event, { test: true }));
  find("message-form").addEventListener("submit", send);
  find("message").addEventListener("input", () => { saveDraft(); updateComposer(); updateControls(); });
  find("message").addEventListener("compositionstart", () => { composing = true; });
  find("message").addEventListener("compositionend", () => { composing = false; });
  find("message").addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing && !composing && event.keyCode !== 229) { event.preventDefault(); find("message-form").requestSubmit(); }
  });
  find("refresh").addEventListener("click", () => {
    pollFailures = 0;
    initialize();
  });
  find("new-session").addEventListener("click", async () => {
    if (busy || uncertain || session?.status === "running") return;
    busy = true; updateControls(); errorMessage();
    try { await createSession(); }
    catch (error) { errorMessage(error.message); }
    finally { busy = false; updateControls(); if (!find("message").disabled) find("message").focus({ preventScroll: true }); }
  });
  find("history").addEventListener("change", async () => {
    const id = find("history").value;
    if (busy || uncertain || session?.status === "running" || !id || id === savedSessionId) return;
    useDraft(id); session = null; pendingSend = null; rememberSession(id); renderSession();
    errorMessage(); status("正在恢复对话…");
    await syncSession();
  });
  find("delete-session").addEventListener("click", async () => {
    if (busy || uncertain || session?.status === "running" || !session) return;
    busy = true; updateControls(); errorMessage();
    try {
      if (!await confirmDialog({ title: "清除本次 AI 对话？", subtitle: "将删除本机保存的本次对话和操作记录", items: ["待确认的操作方案将一并清除。", "已经创建或修改的任务会保留。"], confirmText: "清除对话" })) return;
      await api(`/api/agent/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE" });
      const deletedId = session.id;
      useDraft(null); drafts.delete(deletedId);
      session = null; pendingSend = null; rememberSession(null);
      await refreshHistory();
      renderSession(); status("本次对话已清除。可以发送消息开启新对话。");
    } catch (error) { errorMessage(error.message); uncertain = true; }
    finally {
      busy = false; updateControls();
      if (uncertain) await syncSession(true);
    }
  });
  window.addEventListener("active-job-change", updateContext);
  window.addEventListener("jobs-updated", updateContext);
  window.addEventListener("resize", () => { if (panelVisible()) { updateComposer(); updateLatest(); } });
  updateComposer(); updateContext();
  initialize();
}
