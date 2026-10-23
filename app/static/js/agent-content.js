// A deliberately small Markdown subset. Model output is always text, never HTML.
const ROLES = { user: "你", assistant: "助手", system: "系统" };
const FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const HEADING = /^ {0,3}(#{1,6})\s+(.+)$/;
const LIST = /^ {0,3}(?:([-+*])|(\d{1,9})[.)])\s+(.+)$/;
const QUOTE = /^ {0,3}> ?(.*)$/;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function copyControl(value, label) {
  const control = element("span", undefined, "agent-copy-control");
  const button = element("button", "复制", "agent-copy-button");
  const status = element("span", "", "agent-copy-status");
  button.type = "button";
  button.setAttribute("aria-label", label);
  status.setAttribute("role", "status");
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "";
    try {
      await navigator.clipboard.writeText(value);
      status.textContent = "已复制";
    } catch {
      status.textContent = "复制失败，请选中文字手动复制。";
    } finally {
      button.disabled = false;
    }
  });
  control.append(button, status);
  return control;
}

function inline(parent, text) {
  // Bounded tokens avoid expensive scans of malformed model output. Nesting and
  // image syntax are intentionally unsupported; they remain readable as text.
  const tokens = /`([^`\n]{1,4096})`|\*\*([^*\n]{1,4096})\*\*|(?<!!)\[([^\]\n]{1,512})\]\(([^)\s]{1,2048})\)/g;
  let cursor = 0;
  for (const match of text.matchAll(tokens)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[1]) parent.append(element("code", match[1]));
    else if (match[2]) parent.append(element("strong", match[2]));
    else {
      let url;
      try {
        if (/^https?:\/\//i.test(match[4])) url = new URL(match[4]);
      } catch { /* Unsupported links remain plain text. */ }
      if (url && ["http:", "https:"].includes(url.protocol)) {
        const link = element("a", match[3]);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } else parent.append(document.createTextNode(match[0]));
    }
    cursor = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
}

function tableHeader(lines, index) {
  if (!lines[index]?.includes("|") || !lines[index + 1]?.includes("|")) return false;
  const headers = cells(lines[index]), separators = cells(lines[index + 1]);
  return headers.length > 1 && headers.length === separators.length && separators.every(cell => /^:?-{3,}:?$/.test(cell));
}

function startsBlock(lines, index) {
  const line = lines[index];
  return !line.trim() || FENCE.test(line) || HEADING.test(line) || LIST.test(line) || QUOTE.test(line) || tableHeader(lines, index);
}

function markdown(parent, content) {
  const lines = content.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }
    const fence = line.match(FENCE);
    if (fence) {
      const code = [], marker = fence[1];
      const closingFence = new RegExp(`^ {0,3}${marker[0]}{${marker.length},}\\s*$`);
      index++;
      while (index < lines.length && !closingFence.test(lines[index])) {
        code.push(lines[index++]);
      }
      if (index < lines.length) index++;
      const block = element("div", undefined, "agent-code-block");
      const head = element("div", undefined, "agent-code-head");
      head.append(element("span", fence[2].trim().split(/\s/)[0].slice(0, 32) || "代码", "agent-code-language"));
      head.append(copyControl(code.join("\n"), "复制代码"));
      const pre = element("pre");
      pre.append(element("code", code.join("\n")));
      block.append(head, pre);
      parent.append(block);
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      const node = element(`h${Math.min(heading[1].length + 2, 6)}`);
      inline(node, heading[2]); parent.append(node); index++; continue;
    }
    if (tableHeader(lines, index)) {
      const wrapper = element("div", undefined, "agent-table-wrap");
      const table = element("table"), head = element("thead"), row = element("tr"), body = element("tbody");
      const headers = cells(line);
      headers.forEach(value => { const cell = element("th"); cell.scope = "col"; inline(cell, value); row.append(cell); });
      head.append(row); index += 2;
      while (index < lines.length && lines[index].includes("|") && cells(lines[index]).length === headers.length) {
        const row = element("tr");
        cells(lines[index++]).forEach(value => { const cell = element("td"); inline(cell, value); row.append(cell); });
        body.append(row);
      }
      table.append(head, body); wrapper.append(table); parent.append(wrapper); continue;
    }
    const list = line.match(LIST);
    if (list) {
      const ordered = Boolean(list[2]), node = element(ordered ? "ol" : "ul");
      if (ordered) node.start = Number(list[2]);
      while (index < lines.length) {
        const item = lines[index].match(LIST);
        if (!item || Boolean(item[2]) !== ordered) break;
        const child = element("li"); inline(child, item[3]); node.append(child); index++;
      }
      parent.append(node); continue;
    }
    if (QUOTE.test(line)) {
      const quotes = [], node = element("blockquote");
      while (index < lines.length && QUOTE.test(lines[index])) quotes.push(lines[index++].replace(QUOTE, "$1"));
      inline(node, quotes.join("\n")); parent.append(node); continue;
    }
    const paragraph = [line], node = element("p");
    index++;
    while (index < lines.length && !startsBlock(lines, index)) paragraph.push(lines[index++]);
    inline(node, paragraph.join("\n")); parent.append(node);
  }
}

export function renderMessage(message) {
  const content = typeof message.content === "string" ? message.content : "";
  const role = Object.hasOwn(ROLES, message.role) ? message.role : "system";
  const article = element("article", undefined, "agent-message");
  article.dataset.role = role;
  const head = element("div", undefined, "agent-message-head");
  head.append(element("strong", ROLES[role]));
  if (typeof message.created_at === "string" && /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(message.created_at)) {
    const date = new Date(message.created_at);
    const [year, month, day] = message.created_at.slice(0, 10).split("-").map(Number);
    const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
    if (month >= 1 && month <= 12 && day >= 1 && day <= lastDay && !Number.isNaN(date.getTime())) {
      const time = element("time", date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      time.dateTime = date.toISOString(); time.title = date.toLocaleString(); head.append(time);
    }
  }
  head.append(copyControl(content, "复制消息"));
  const body = element("div", undefined, "agent-message-body");
  if (role === "assistant") markdown(body, content);
  else body.append(element("p", content, "agent-message-plain"));
  article.append(head, body);
  return article;
}
