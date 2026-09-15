import { Alert, Card, Empty, Space, Typography } from 'antd';
import type { ChatTurn } from '@/hooks/useChatStream';
import { ChartPanel } from './ChartPanel';
import { ResultTable } from './ResultTable';
import { SqlPanel } from './SqlPanel';

const { Text, Title } = Typography;

interface Props {
  turn: ChatTurn | null;
}

/**
 * 右侧结果区。展示当前聚焦那一轮的图表、SQL 与明细。
 * 单独成区是因为图表在窄栏里会被压扁，而结论文本留在对话流中保持上下文连贯。
 */
export function ResultPane({ turn }: Props) {
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card size="small" style={{ borderRadius: 8 }} styles={{ body: { padding: '10px 12px' } }}>
        <Title level={5} style={{ margin: 0, fontSize: 14, lineHeight: 1.6 }}>
          {turn.question}
        </Title>
      </Card>

      {result.guard_warnings.length > 0 &&
        result.guard_warnings.map((w) => (
          <Alert key={w} type="warning" showIcon message="SQL 被安全沙箱拦截" description={w} />
        ))}

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
        />
      )}

      {result.sql && (
        <SqlPanel
          sql={result.sql}
          originalSql={result.original_sql}
          error={result.sql_error}
          retryCount={result.retry_count}
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
          <ResultTable columns={result.columns} rows={result.rows} />
        </Card>
      )}
    </div>
  );
}
