// 上传文件选择与文件类型判断。

import { els } from "./core.js?v=58a36b0fc94d0be8";

export function hasFile(data, suffixes) {
  return (data.files || []).some((name) => suffixes.some((suffix) => name.toLowerCase().endsWith(suffix)));
}

export function fileNameEndsWith(name, suffixes) {
  const lower = String(name || "").toLowerCase();
  return suffixes.some((suffix) => lower.endsWith(suffix));
}

export function fileParamEndsWith(value, suffixes) {
  const text = String(value || "").toLowerCase();
  return suffixes.some((suffix) => text.endsWith(suffix));
}

export function uploadedNames() {
  return selectedUploadFiles().map((file) => file.name);
}

export function selectedUploadFiles() {
  const files = [
    ...Array.from(els.files?.files || []),
    ...Array.from(els.ligandReferenceFile?.files || []),
  ];
  const seen = new Set();
  return files.filter((file) => {
    const key = `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
