// Preview parsing and transfer limits run without a browser or live service.
import assert from "node:assert/strict";
import {
  MAX_STRUCTURE_ATOMS, MAX_STRUCTURE_BYTES, prepareStructure, readStructureResponse, structureFiles, structureFormat,
} from "../app/static/js/structure-data.js";

const pdb = "ATOM      1  CA  ALA A   1      11.000  -2.000   3.500  1.00  0.00           C";
const water = pdb.replace("ALA", "HOH");
const first = prepareStructure(`MODEL        1\n${pdb}\n${water}\nENDMDL\nMODEL        2\n${pdb.replace("11.000", "99.000")}\nENDMDL`, "pdb");
assert.equal(first.atomCount, 1);
assert.equal(first.waterAtoms, 1);
assert.equal(first.firstModelOnly, true);
assert(!first.text.includes("HOH") && !first.text.includes("99.000"));
assert(first.text.includes("11.000  -2.000   3.500"));
assert.throws(() => prepareStructure(pdb.replace("11.000", "   NaN"), "pdb"), /无效坐标/);
assert.throws(() => prepareStructure(water, "pdb"), /只有水分子/);
assert.throws(() => prepareStructure("<script>alert(1)</script>", "pdb"), /未找到有效原子/);
assert.throws(() => prepareStructure((pdb + "\n").repeat(MAX_STRUCTURE_ATOMS + 1), "pdb"), /50,000/);
assert.throws(() => prepareStructure("x".repeat(MAX_STRUCTURE_BYTES + 1), "pdb"), /20 MB/);

const groAtom = "    1ALA     CA    1   1.100  -0.200   0.350";
const groWater = "    2SOL     OW    2   0.000   0.000   0.000";
const gro = prepareStructure(`Preview\n2\n${groAtom}\n${groWater}\n1.0 1.0 1.0\n`, "gro");
assert.equal(gro.atomCount, 1);
assert.equal(gro.waterAtoms, 1);
assert.equal(gro.text, `Preview\n1\n${groAtom}\n1.0 1.0 1.0\n`);
assert.equal(gro.firstModelOnly, false);
assert.throws(() => prepareStructure(`Preview\n3\n${groAtom}\n1.0 1.0 1.0`, "gro"), /原子数量/);
assert.throws(() => prepareStructure(`Preview\n1\n${groAtom.replace("1.100", "  NaN")}\n1.0 1.0 1.0`, "gro"), /无效坐标/);
assert.equal(structureFormat("protein.PDB"), "pdb");
assert.equal(structureFormat("protein.pdb.gz"), "");
assert.deepEqual(structureFiles([{ path: "input.pdb" }, { path: "#input.pdb#" }, { path: "md_0_100.gro" }, { path: "input.pdb" }, { path: "trajectory.xtc" }]).map(file => file.path), ["md_0_100.gro", "input.pdb"]);

const bytes = new TextEncoder().encode("蛋白\n" + pdb);
assert.equal(await readStructureResponse(new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes.slice(0, 1)); controller.enqueue(bytes.slice(1)); controller.close(); } }))), "蛋白\n" + pdb);
let headerCancelled = false;
await assert.rejects(readStructureResponse(new Response(new ReadableStream({ cancel() { headerCancelled = true; } }), { headers: { "Content-Length": String(MAX_STRUCTURE_BYTES + 1) } })), /20 MB/);
assert(headerCancelled, "Oversized responses must be cancelled before their body is consumed");
let streamedCancelled = false;
await assert.rejects(readStructureResponse(new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(MAX_STRUCTURE_BYTES + 1)); }, cancel() { streamedCancelled = true; } }))), /20 MB/);
assert(streamedCancelled, "Missing or inaccurate Content-Length must not bypass the byte limit");
await assert.rejects(readStructureResponse(new Response("missing", { status: 404 })), /HTTP 404/);
console.log("PASS structure preview: first-model selection, water filtering, PDB/GRO coordinate validation, atom/byte limits, bounded streaming and file ordering.");
