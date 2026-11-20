// 上传文件选择与文件类型判断。

import { els } from "./core.js?v=5b84b3e1eaca6ab9";

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
  // Metadata alone cannot distinguish different files selected from different directories.
  return [...new Set(files)];
}
