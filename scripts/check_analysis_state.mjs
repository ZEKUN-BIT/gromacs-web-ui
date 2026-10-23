import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/static/js/existing-analysis.js", import.meta.url), "utf8");
const parser = source.slice(source.indexOf("export async function readAnalysisPreparation"), source.indexOf("export function bindExistingAnalysis"))
  .replace("export async function", "async function");
const context = vm.createContext({ TextDecoder, Error });
vm.runInContext(parser + "\nglobalThis.parse = readAnalysisPreparation;", context);
const encode = value => new TextEncoder().encode(JSON.stringify(value) + "\n");
const events = [
  { type: "progress", phase: "preflight", message: "正在预检轨迹…" },
  { type: "heartbeat" },
  { type: "progress", phase: "copy", copied_bytes: 512, total_bytes: 1024 },
  { type: "result", job: { id: "analysis-1", name: "研究分析", status: "queued" } },
];
const bytes = new TextEncoder().encode(events.map(event => JSON.stringify(event) + "\n").join(""));
const chunks = new ReadableStream({ start(controller) {
  for (let offset = 0; offset < bytes.length; offset += 3) controller.enqueue(bytes.slice(offset, offset + 3));
  controller.close();
} });
const progress = [];
const result = await context.parse(new Response(chunks), event => progress.push(event));
assert.equal(result.id, "analysis-1");
assert.equal(progress.length, 2);
assert.equal(progress[0].message, "正在预检轨迹…");

const serverError = new Response(encode({ type: "error", message: "请缩短时间范围" }));
await assert.rejects(context.parse(serverError, () => {}), /请缩短时间范围/);
await assert.rejects(context.parse(new Response(encode(events[0])), () => {}), /先查看任务列表.*避免重复提交/);
await assert.rejects(context.parse(new Response('{"detail":"文件不可用"}', { status: 409 }), () => {}), /文件不可用/);

let reads = 0, cancelled = false;
function connection(firstEvent) {
  reads = 0; cancelled = false;
  return { ok: true, body: { getReader: () => ({
    read: async () => { if (reads++ === 0) return { value: encode(firstEvent), done: false }; throw new Error("network lost"); },
    cancel: async () => { cancelled = true; }, releaseLock: () => {},
  }) } };
}
assert.equal((await context.parse(connection(events[3]), () => {})).id, "analysis-1");
assert.equal(reads, 1, "A confirmed result must not be turned into a failure by a later connection error");
assert.equal(cancelled, true);
await assert.rejects(context.parse(connection(events[0]), () => {}), /先查看任务列表.*避免重复提交/);
assert.equal(cancelled, true);
console.log("Analysis progress handles fragmented UTF-8, server errors, partial disconnects, and confirmed success without requiring EOF.");
