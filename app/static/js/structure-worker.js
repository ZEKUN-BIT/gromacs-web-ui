import { prepareStructure, readStructureResponse } from "./structure-data.js?v=5b84b3e1eaca6ab9";

// The panel terminates this worker on replacement or timeout, including an in-flight download.
self.onmessage = async ({ data: { file, url, format } }) => {
  try {
    const text = file ? await file.text() : await readStructureResponse(await fetch(url, { credentials: "same-origin" }));
    self.postMessage({ prepared: prepareStructure(text, format) });
  } catch (error) {
    self.postMessage({ error: error.message || "结构预处理失败，请重试。" });
  }
};
