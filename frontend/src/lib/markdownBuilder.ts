/**
 * 整轮会话的 Markdown 导出（FR-H-4）。
 *
 * 输出结构（按 FRD 第 8 节要求）：
 *   - 文件头：会话标题 + 生成时间 + 数据截至
 *   - 每轮：问句（原话 + 改写）、结论、口径（指标 / 时间范围 / 口径说明）、SQL、结果表
 *
 * 与 PPTX 走同一份数据（已落库的轮次 + 后端已脱敏的 rows），
 * 不再重新请求原始数据，避免把未脱敏字段写进文件。
 *
 * Markdown 比 PPTX 轻：没有图表嵌入，结果表按 Markdown 表格语法输出，
 * 行数上限 50 行（PPTX 是表格占位，这里直接砍行数）。超过时在表后追加一行
 * 「已截断：仅显示前 50 行 / 共 N 行」的提示。
 */

import type { ChatTurn } from '@/hooks/useChatStream';
import { localTimestamp } from './exporters';

const MAX_TABLE_ROWS = 50;

/** 把单个 cell 转成 Markdown 表格单元格里能直接放的字面值。 */
function mdCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value);
  // 表格单元格里出现的 | 与换行会破坏表格结构，转义掉
  return String(value).replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

/** 把列名与行集渲染成 Markdown 表格。 */
function mdTable(columns: string[], rows: Record<string, unknown>[]): string {
  if (!columns.length) return '（结果集为空）';
  const header = `| ${columns.map(mdCell).join(' | ')} |`;
  const sep = `| ${columns.map(() => '---').join(' | ')} |`;
  const body = rows.map((r) => `| ${columns.map((c) => mdCell(r[c])).join(' | ')} |`);
  return [header, sep, ...body].join('\n');
}

/** 生成单个会话的 Markdown 文本。 */
export function buildSessionMarkdown(
  turns: ChatTurn[],
  options: { sessionTitle?: string; dataCutoff?: string | null },
): string {
  const now = localTimestamp();
  const lines: string[] = [
    `# ${options.sessionTitle ?? '会话纪要'}`,
    '',
    `> 数据截至：${options.dataCutoff ?? '未知'}　生成时间：${now}`,
    '',
  ];

  turns.forEach((turn, idx) => {
    const result = turn.result;
    const turnNum = idx + 1;
    lines.push(`## 第 ${turnNum} 轮　${turn.question}`, '');

    // 问句来源（FR-H-3）
    const originalQ = turn.originalQuestion ?? turn.question;
    const rewrittenQ = turn.rewrittenQuestion ?? '';
    if (rewrittenQ && rewrittenQ !== originalQ) {
      lines.push('- 原话：' + originalQ, '- 改写后：' + rewrittenQ, '');
    } else {
      lines.push('- 问句：' + originalQ, '');
    }

    if (!result) {
      lines.push('（无结果）', '');
      return;
    }
    if (result.status === 'error') {
      lines.push('**结论**：未能生成可执行查询。', '');
      if (result.sql_error) lines.push('错误：' + result.sql_error, '');
      return;
    }

    // 结论
    if (result.answer) {
      lines.push('**结论**：', '', result.answer, '');
    }
    if (result.highlights?.length) {
      lines.push('**要点**：', '', ...result.highlights.map((h) => `- ${h}`), '');
    }

    // 口径
    const metricUsed = result.metric_used?.length ? result.metric_used.join('、') : '';
    const caveats = result.caveats?.length ? result.caveats : [];
    const timeRange = result.time_range?.resolved || result.time_range?.label || '';
    const hasCaveats = caveats.length > 0;
    if (metricUsed || timeRange || hasCaveats) {
      lines.push('**口径**：');
      if (metricUsed) lines.push(`- 指标：${metricUsed}`);
      if (timeRange) lines.push(`- 时间范围：${timeRange}`);
      caveats.forEach((c) => lines.push(`- 说明：${c}`));
      lines.push('');
    }

    // 异常标记
    const findings = result.findings ?? [];
    if (findings.length) {
      lines.push('**异常标记**：');
      findings.forEach((f) => {
        const slice = f.slice != null ? `（${f.dimension ?? ''}=${f.slice}）` : '';
        lines.push(`- [${f.severity}] ${f.metric}${slice}：${f.reason}`);
      });
      lines.push('');
    }

    // SQL
    if (result.sql) {
      lines.push('**SQL**：', '', '```sql', result.sql, '```', '');
    }

    // 结果表
    if (result.columns?.length) {
      const allRows = result.rows ?? [];
      const truncated = allRows.length > MAX_TABLE_ROWS;
      const displayRows = truncated ? allRows.slice(0, MAX_TABLE_ROWS) : allRows;
      lines.push('**结果表**：', '', mdTable(result.columns, displayRows), '');
      if (truncated) {
        lines.push(
          `_已截断：本表仅显示前 ${MAX_TABLE_ROWS} 行，实际命中 ${allRows.length} 行_`,
          '',
        );
      }
    }

    // 守卫告警
    const notices = result.guard_warnings ?? [];
    if (notices.length) {
      lines.push('**安全沙箱告警**：');
      notices.forEach((n) => lines.push(`- ${n}`));
      lines.push('');
    }
  });

  return lines.join('\n');
}
