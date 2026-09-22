import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts';
import { Alert, Button, Card, Empty, Space, Typography } from 'antd';
import type { ChartPayload } from '@/api/types';
import { dataUrlToBlob, downloadBlob, localTimestamp, slugifyQuestion } from '@/lib/exporters';

const { Text } = Typography;

const CHART_HEIGHT = 320;
const PNG_SCALE = 2; // FR-B-2：2x 设备像素比，避免下载后在小屏上糊

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
  /** 导出时的文件名 slug，默认从外部传入的 title 推断 */
  exportPrefix?: string;
  /** FR-M-2：本期未完整时在图表上方标注，避免使用者拿不完整周期与完整周期并列比较 */
  partialPeriod?: boolean;
}

export function ChartPanel({ chart, columns, rows, title, exportPrefix, partialPeriod }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);

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

  const handleExportPng = () => {
    const instance = instanceRef.current;
    if (!instance) return;
    const dataUrl = instance.getDataURL({
      type: 'png',
      pixelRatio: PNG_SCALE,
      backgroundColor: '#ffffff',
    });
    const blob = dataUrlToBlob(dataUrl);
    const slug = exportPrefix ?? slugifyQuestion(heading);
    downloadBlob(`${slug}_${localTimestamp()}.png`, blob);
  };

  const warnings = chart?.warnings ?? [];

  return (
    <Card
      size="small"
      title={heading}
      styles={{ body: { padding: 12 } }}
      extra={
        renderable ? (
          <Button size="small" onClick={handleExportPng}>
            导出 PNG
          </Button>
        ) : null
      }
    >
      {partialPeriod && (
        <Alert
          type="warning"
          showIcon
          message="本期未完整"
          description="当前周期尚未走完，图表中的数值不能直接与完整周期并列比较。"
          style={{ marginBottom: 8 }}
        />
      )}

      {renderable ? (
        <div ref={containerRef} style={{ height: CHART_HEIGHT, width: '100%' }} />
      ) : !rows.length ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="查询没有返回数据，建议放宽时间范围或减少筛选条件"
          style={{ height: CHART_HEIGHT, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}
        />
      ) : (
        // 退回表格时这里不重复渲染 ResultTable：调用方（AnswerCard / ResultPane）
        // 都已在图表下方挂了一份，再渲染一次会出现上下两张一模一样的表
        <div
          style={{
            height: 72,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            结果以表格形式呈现{check.reason ? `：${check.reason}` : ''}
          </Text>
        </div>
      )}

      {renderable && chart?.reason && (
        <Text type="secondary" style={{ display: 'block', fontSize: 12, marginTop: 8 }}>
          图型选择依据：{chart.reason}
        </Text>
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
      {renderable && rows.length > 0 && (
        <Space size={8} style={{ marginTop: 6 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            数据点 {rows.length} 个
          </Text>
        </Space>
      )}
    </Card>
  );
}
