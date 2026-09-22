/**
 * 浏览器侧导出工具。
 *
 * 三件事：
 *   1. CSV 文本生成（FR-B-1）：逗号分隔、对含逗号或换行的字段加双引号并把内部
 *      的双引号转义为两个。Excel 在中文 Windows 上不开 UTF-8 BOM 会乱码，所以
 *      这里在文件头加 BOM。
 *   2. 触发下载：用 Blob + a[download]。比 anchor.href=data:... 稳，转义过的
 *      内容不会因为体积过大被截断。Microsoft Edge 的 download 行为也一致。
 *   3. PNG 来自 echarts.getDataURL（FR-B-2），由 ChartPanel 自行调用，这里
 *      不做封装，避免 ChartPanel 里还要再绕一层。
 */

/** 单单元格值到 CSV 字符串的转换：null/undefined → 空串，其余按字面。 */
function cellToCsv(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') {
    // 千分位会让 Excel 当作文本，避免
    return Number.isFinite(value) ? String(value) : '';
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/** 把一行 cell 拼成 CSV 字符串。含 `,` `"` `\n` 的字段加双引号并把内部 `"` 转义。 */
function rowToCsv(cells: unknown[]): string {
  return cells
    .map((v) => {
      const s = cellToCsv(v);
      if (/[",\r\n]/.test(s)) {
        return `"${s.replace(/"/g, '""')}"`;
      }
      return s;
    })
    .join(',');
}

/**
 * 用列名和行集构建 CSV 文本。列名同样过转义规则。
 * Excel 友好：UTF-8 BOM + CRLF。
 *
 * FR-B-1 / FRD 第 8 节：结果被行数上限截断时，文件名与文件内首行都要标注。
 * 只在文件里留一句文案还不够——CSV 常被转发给第三方，脱离了界面后接收方
 * 无从判断手上的 1000 行是不是全量。因此这里把截断写成首行注释。
 */
export function buildCsv(
  columns: string[],
  rows: Record<string, unknown>[],
  options: { truncated?: boolean; totalRowCount?: number; limit?: number } = {},
): string {
  const header = rowToCsv(columns);
  const body = rows.map((r) => rowToCsv(columns.map((c) => r[c])));
  const lines: string[] = ['\ufeff'];
  if (options.truncated) {
    const total = options.totalRowCount ?? rows.length;
    const limit = options.limit ?? rows.length;
    lines.push(`# 已截断：本文件仅含前 ${rows.length} 行，实际命中 ${total} 行（上限 ${limit} 行）`);
  }
  lines.push(header, ...body);
  // 用 \r\n 而不是 \n 是为了 Excel 在不同地域下也能正确断行
  return lines.join('\r\n');
}

/** 截断时给文件名加标识，避免下载目录里两份同名文件分不清哪份是全量。 */
export function csvFilename(prefix: string, truncated: boolean): string {
  return `${prefix}${truncated ? '_已截断' : ''}_${localTimestamp()}.csv`;
}

/** 触发浏览器下载一个字符串内容。默认按文本处理；图片/PDF 用 downloadBlob。 */
export function downloadText(filename: string, content: string, mime = 'text/plain;charset=utf-8') {
  triggerDownload(filename, new Blob([content], { type: mime }));
}

/** 触发浏览器下载一个二进制内容。echarts 出来的 dataURL 经 atob 转回。 */
export function downloadBlob(filename: string, blob: Blob) {
  triggerDownload(filename, blob);
}

function triggerDownload(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  // 必须挂到 DOM 再 click，Firefox 不挂不下载
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 给浏览器一点时间把 URL 与 blob 关联起来再回收，否则极端时序下 Chrome 会报
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 把 dataURL（如 "data:image/png;base64,iVBOR..."）解码成 Blob。 */
export function dataUrlToBlob(dataUrl: string): Blob {
  const [meta, base64] = dataUrl.split(',', 2);
  const mimeMatch = /data:([^;]+)(?:;base64)?/.exec(meta);
  const mime = mimeMatch?.[1] ?? 'application/octet-stream';
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

/** 把问句转成文件系统安全的一段 slug，用于下载文件名。 */
export function slugifyQuestion(question: string, maxLen = 30): string {
  // 保留中英文与数字，剩下的用下划线兜底
  const cleaned = question
    .trim()
    .replace(/[\\/:*?"<>|\s]+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '');
  return cleaned.slice(0, maxLen) || 'query';
}

/** 生成形如 20260915_153020 的本地时间戳。 */
export function localTimestamp(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}
