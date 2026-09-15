import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts';
import { Alert, Card, Empty, Typography } from 'antd';
import type { ChartPayload } from '@/api/types';
import { ResultTable } from './ResultTable';

const { Text, Paragraph } = Typography;

const CHART_HEIGHT = 320;

/**
 * 渲染前的字段校验。后端已在 chart/builder.py 校验过一次，这里再兜一层：
 * option 是后端生成的结构化配置，前端只核对它引用的列确实存在于结果集中，
 * 不满足就退回表格，绝不留一个空白图表区。
 */
function validateOption(
  option: Record<string, unknown> | null,
  columns: string[],
): { ok: boolean; reason: string } {
  if (!option) return { ok: false, reason: '后端未返回图表配置' };

  const series = option.series as unknown;
  if (!Array.isArray(series) || series.length === 0) {
    return { ok: false, reason: '图表配置中没有可用的数据系列' };
  }

  const present = new Set(columns);
  if (present.size === 0) return { ok: false, reason: '结果集没有列信息' };

  const xAxis = option.xAxis as { name?: string } | undefined;
  const xName = xAxis?.name;
  // 饼图没有 xAxis，数据点自带 name，不做列名校验
  if (xName && !present.has(xName)) {
    return { ok: false, reason: `图表横轴引用的字段「${xName}」不在结果集列中` };
  }

  return { ok: true, reason: '' };
}

interface Props {
  chart: ChartPayload | null;
  columns: string[];
  rows: Record<string, unknown>[];
  title?: string;
}

export function ChartPanel({ chart, columns, rows, title }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

  const fallbackRows = chart?.table_rows?.length ? chart.table_rows : rows;
  const fallbackColumns = chart?.table_columns?.length ? chart.table_columns : columns;

  const { option, heading } = useMemo(() => {
    if (!chart || !chart.option) return { option: null, heading: title ?? '查询结果' };
    // 后端把标题写进了 option，按视觉规范改为卡片标题呈现，图表内不再重复
    const next = { ...chart.option };
    const embedded = (next.title as { text?: string } | undefined)?.text;
    delete next.title;
    return { option: next, heading: title || embedded || '查询结果' };
  }, [chart, title]);

  const check = useMemo(() => validateOption(option, columns), [option, columns]);
  const renderable = Boolean(chart && chart.chart_type !== 'table' && option && check.ok);

  useEffect(() => {
    if (!renderable || !containerRef.current) return;
    const instance = echarts.init(containerRef.current);
    instanceRef.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      instanceRef.current = null;
    };
  }, [renderable]);

  useEffect(() => {
    if (renderable && option) instanceRef.current?.setOption(option, true);
  }, [option, renderable]);

  const warnings = chart?.warnings ?? [];

  return (
    <Card size="small" title={heading} styles={{ body: { padding: 12 } }}>
      {renderable ? (
        <div ref={containerRef} style={{ height: CHART_HEIGHT, width: '100%' }} />
      ) : !rows.length ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="查询没有返回数据，建议放宽时间范围或减少筛选条件"
          style={{ height: CHART_HEIGHT, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}
        />
      ) : (
        <ResultTable columns={fallbackColumns} rows={fallbackRows} />
      )}

      {renderable && chart?.reason && (
        <Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
          图型选择依据：{chart.reason}
        </Paragraph>
      )}
      {!renderable && rows.length > 0 && (
        <Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
          已退回表格呈现{check.reason ? `：${check.reason}` : chart?.reason ? `：${chart.reason}` : ''}
        </Paragraph>
      )}
      {warnings.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {warnings.map((w) => (
            <Alert key={w} type="warning" showIcon message={w} style={{ marginBottom: 4 }} />
          ))}
        </div>
      )}
      {chart?.chart_type === 'table' && chart.reason && rows.length > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {chart.reason}
        </Text>
      )}
    </Card>
  );
}
