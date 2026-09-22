import { Alert, Card, Empty, Space, Typography } from 'antd';
import type { ChatTurn } from '@/hooks/useChatStream';
import type { DrilldownRequest, GuardNotice } from '@/api/types';
import { normalizeGuardNotices } from '@/lib/streamState';
import { ChartPanel } from './ChartPanel';
import { ResultTable } from './ResultTable';
import { SqlPanel } from './SqlPanel';

const { Text, Title } = Typography;

interface Props {
  turn: ChatTurn | null;
  /** 下钻入口（FR-L-2）。结果表单元格点击后由页面换成问句重新提交 */
  onDrilldown?: (slots: DrilldownRequest) => void;
}

/**
 * 右侧结果区。展示当前聚焦那一轮的图表、SQL 与明细。
 * 单独成区是因为图表在窄栏里会被压扁，而结论文本留在对话流中保持上下文连贯。
 */
export function ResultPane({ turn, onDrilldown }: Props) {
  if (!turn || !turn.result) {
    return (
      <Card style={{ borderRadius: 8, height: '100%' }}>
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="发起一次提问后，图表与查询明细会显示在这里"
          style={{ marginTop: 120 }}
        />
      </Card>
    );
  }

  const result = turn.result;
  const hasRows = result.rows.length > 0;

  // FR-I-2：结构化通道优先，老数据退回 guard_warnings 纯文案（按 rejected 渲染）
  const guardNotices: GuardNotice[] = normalizeGuardNotices(result.guard_notices).length
    ? normalizeGuardNotices(result.guard_notices)
    : (result.guard_warnings ?? []).map((message) => ({
        kind: 'rejected' as const,
        code: '',
        message,
      }));

  // 单元格下钻：维度取点击列的列名，主指标取基准里带的那个（与卡片侧保持一致），
  // 时间限定沿用本轮说法。缺少主指标时仍可提交——服务端对未知指标有兜底。
  const drillMetric = result.baselines?.metric ?? result.metric_used?.[0] ?? null;
  const handleCellDrill = ({ dimension, value }: { dimension: string; value: string }) => {
    onDrilldown?.({
      source: 'cell',
      metric: drillMetric,
      dimension,
      slice: value,
      time_label: result.time_range?.label ?? null,
    });
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card size="small" style={{ borderRadius: 8 }} styles={{ body: { padding: '10px 12px' } }}>
        <Title level={5} style={{ margin: 0, fontSize: 14, lineHeight: 1.6 }}>
          {turn.question}
        </Title>
      </Card>

      {guardNotices.map((notice, i) =>
        notice.kind === 'rewritten' ? (
          <Alert
            key={`${notice.code || 'x'}-${i}`}
            type="info"
            showIcon
            message={
              <span>
                {notice.code && <Text strong style={{ marginRight: 6 }}>{notice.code}</Text>}
                SQL 被改写后放行：{notice.message}
              </span>
            }
          />
        ) : (
          <Alert
            key={`${notice.code || 'x'}-${i}`}
            type="warning"
            showIcon
            message="SQL 被安全沙箱拦截"
            description={
              <span>
                {notice.code && <Text strong style={{ marginRight: 6 }}>{notice.code}</Text>}
                {notice.message}
              </span>
            }
          />
        ),
      )}

      {/* 报错正文由下方的 SqlPanel 展开项承载，这里只提示状态，避免同一段文字出现两次 */}
      {result.status === 'error' && (
        <Alert type="error" showIcon message="这一轮没有产出可执行查询" />
      )}

      {hasRows && (
        <ChartPanel
          chart={result.chart}
          columns={result.columns}
          rows={result.rows}
          title={turn.question.slice(0, 40)}
          partialPeriod={result.partial_period}
        />
      )}

      {result.sql && (
        <SqlPanel
          sql={result.sql}
          originalSql={result.original_sql}
          error={result.sql_error}
          retryCount={result.retry_count}
          sqlReasoning={result.sql_reasoning}
          fixReasoning={result.fix_reasoning}
        />
      )}

      {hasRows && (
        <Card size="small" title="查询明细" style={{ borderRadius: 8 }}>
          <Space size={12} style={{ marginBottom: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              共 {result.row_count} 行
            </Text>
            {result.truncated && (
              <Text type="warning" style={{ fontSize: 12 }}>
                结果已被行数上限截断
              </Text>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              查询耗时 {result.query_elapsed_ms.toFixed(0)} ms
            </Text>
          </Space>
          <ResultTable
            columns={result.columns}
            rows={result.rows}
            onCellDrill={onDrilldown ? handleCellDrill : undefined}
            truncated={result.truncated}
            totalRowCount={result.row_count}
          />
        </Card>
      )}
    </div>
  );
}
