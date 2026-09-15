import { Alert, Button, Card, Skeleton, Space, Typography } from 'antd';
import type { ChatTurn } from '@/hooks/useChatStream';
import { AnswerText } from './AnswerText';
import { ChartPanel } from './ChartPanel';
import { MetricBadge } from './MetricBadge';
import { ResultTable } from './ResultTable';
import { SqlPanel } from './SqlPanel';
import { StageIndicator } from './StageIndicator';

const { Text } = Typography;

interface Props {
  turn: ChatTurn;
  /**
   * 紧凑模式：只保留阶段、结论与口径，图表/SQL/表格交给右侧结果区。
   * 主页面三区布局用 true；历史页整卡回看用 false。
   */
  compact?: boolean;
  focused?: boolean;
  onFocus?: () => void;
  /** 失败态的重试入口。由对话区把这一轮的问题重新提交 */
  onRetry?: () => void;
}

/**
 * 一轮回答的卡片。四个终态共用同一个容器，只在提示条与内容区块上有差别：
 * ok 走完整链路，chitchat 只给文本，clarify 给出补充信息的引导，
 * error 必须带上最后一次语句与报错 —— 失败不能是一片空白。
 */
export function AnswerCard({ turn, compact = false, focused = false, onFocus, onRetry }: Props) {
  const { result, stages, streaming, errorMessage } = turn;

  if (errorMessage) {
    return (
      <Card size="small" style={{ borderRadius: 8 }}>
        <StageIndicator stages={stages} running={false} />
        <Alert
          type="error"
          showIcon
          message="请求失败"
          description={errorMessage}
          action={
            onRetry && (
              <Button size="small" onClick={onRetry}>
                重试
              </Button>
            )
          }
          style={{ marginTop: 8 }}
        />
      </Card>
    );
  }

  if (!result) {
    // 流已结束却什么都没收到。此时不能继续挂骨架屏：它会一直转下去，
    // 既没有报错也没有重试入口，而输入框是可用的，问题很容易被忽略
    const stalled = !streaming;
    return (
      <Card size="small" style={{ borderRadius: 8 }}>
        <StageIndicator stages={stages} running={streaming} />
        {stalled ? (
          <Alert
            type="warning"
            showIcon
            message="未收到服务端结果"
            description="连接在返回结果前已关闭，可重试本轮提问。"
            action={
              onRetry && (
                <Button size="small" onClick={onRetry}>
                  重试
                </Button>
              )
            }
            style={{ marginTop: 8 }}
          />
        ) : (
          <Skeleton active paragraph={{ rows: 2 }} title={false} style={{ marginTop: 8 }} />
        )}
      </Card>
    );
  }

  const isQuery = result.status === 'ok' || (result.status === 'error' && Boolean(result.sql));
  const hasRows = result.rows.length > 0;

  return (
    <Card
      size="small"
      style={{
        borderRadius: 8,
        borderColor: focused ? '#1677ff' : undefined,
        cursor: onFocus ? 'pointer' : undefined,
      }}
      onClick={onFocus}
    >
      <StageIndicator stages={stages} running={streaming} />

      {result.guard_warnings.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {result.guard_warnings.map((w) => (
            <Alert
              key={w}
              type="warning"
              showIcon
              message="SQL 被安全沙箱拦截"
              description={w}
              style={{ marginBottom: 6 }}
            />
          ))}
        </div>
      )}

      {result.status === 'clarify' && (
        // 反问用中性灰，与「拦截」的黄、「失败」的红区分开：它只是信息不足，不是出错
        <div
          style={{
            marginTop: 8,
            padding: '8px 12px',
            background: '#fafafa',
            border: '1px solid #f0f0f0',
            borderRadius: 6,
            fontSize: 13,
            lineHeight: 1.6,
          }}
        >
          <Text strong>需要补充信息</Text>
          <Text type="secondary">　补充时间范围或分析维度后再问一次。</Text>
        </div>
      )}

      {result.status === 'error' && (
        <Alert
          type="error"
          showIcon
          message={result.retry_count ? `重试 ${result.retry_count} 次后仍未成功` : '未能生成可执行查询'}
          description="建议缩小时间范围、减少关联维度，或换一种更具体的问法。"
          style={{ marginTop: 8 }}
        />
      )}

      {result.status === 'ok' && !hasRows && !result.sql_error && (
        <Alert
          type="info"
          showIcon
          message="查询没有返回数据"
          description="可能是筛选条件过严或该时间段内没有符合条件的记录，建议放宽时间范围。"
          style={{ marginTop: 8 }}
        />
      )}

      {result.answer && (
        <div style={{ marginTop: 10 }}>
          <AnswerText text={result.answer} />
        </div>
      )}

      {!result.answer && result.insight && <AnswerText text={result.insight} />}

      {result.highlights.length > 0 && (
        <ul style={{ margin: '8px 0 0', paddingLeft: 20, lineHeight: 1.75 }}>
          {result.highlights.map((h, i) => (
            <li key={i}>{h}</li>
          ))}
        </ul>
      )}

      {isQuery && (
        <div style={{ marginTop: 10 }}>
          <MetricBadge
            metricUsed={result.metric_used}
            caveats={result.caveats}
            timeRange={result.time_range}
          />
        </div>
      )}

      {isQuery && hasRows && !compact && (
        <div style={{ marginTop: 10 }}>
          <ChartPanel
            chart={result.chart}
            columns={result.columns}
            rows={result.rows}
            title={turn.question.slice(0, 40)}
          />
        </div>
      )}

      {/* error 终态不再挂 SqlPanel：give_up 节点的回答里已经原样带了最后一次语句与报错，
          同一个卡片里出现两份会造成视觉重复 */}
      {result.sql && !compact && result.status !== 'error' && (
        <div style={{ marginTop: 10 }}>
          <SqlPanel
            sql={result.sql}
            originalSql={result.original_sql}
            error={result.sql_error}
            retryCount={result.retry_count}
          />
        </div>
      )}

      {hasRows && !compact && (
        <div style={{ marginTop: 10 }}>
          <Space size={8} style={{ marginBottom: 6 }}>
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
        </div>
      )}
    </Card>
  );
}
