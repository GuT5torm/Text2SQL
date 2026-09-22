/**
 * 数值展示格式。
 *
 * 基准值与异常标记共用一套格式：界面上同一个数字在两处显示成两种样子，
 * 会被读成两个不同的数。百分比一律保留一位小数，金额按整数与否决定是否补小数。
 */

/** 金额与计数。整数不加小数位，非整数固定两位，避免 2.5 与 2.50 混排。 */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (Number.isInteger(value)) return value.toLocaleString('zh-CN');
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 相对偏离。带正负号，保留一位小数。
 * null 表示不可比（不完整周期或基数为零），返回占位符而不是 0% —— 0% 会被
 * 读成「与上期持平」,而真实情况是没有可比的上期。
 */
export function formatDeviation(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '不可比';
  const sign = value > 0 ? '+' : '';
  return `${sign}${(value * 100).toFixed(1)}%`;
}

/** 比率型指标的值。0.0377 → 3.8%。 */
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}
