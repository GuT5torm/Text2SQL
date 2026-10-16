import { Alert, Button, Card, Collapse, Skeleton, Space, Tag, Tooltip, Typography } from 'antd';
import type { ChatTurn } from '@/hooks/useChatStream';
import type { DrilldownRequest, Finding } from '@/api/types';
import { localTimestamp } from '@/lib/exporters';
import { TERMINAL_COPY, normalizeGuardNotices } from '@/lib/streamState';
import { AnswerText } from './AnswerText';
import { ChartPanel } from './ChartPanel';
import { DefinitionPanel } from './DefinitionPanel';
import { ExportMenu } from './ExportMenu';
import { FeedbackBar } from './FeedbackBar';
import { FindingList } from './FindingList';
import { MetricBadge } from './MetricBadge';
import { ResultTable } from './ResultTable';
import { StageIndicator } from './StageIndicator';
import { TechDetails } from './TechDetails';

const { Text, Paragraph } = Typography;

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
  /**
   * 下钻入口（FR-L-2）。由页面把槽位换成问句后重新提交，
   * 不传则本卡片不出现下钻按钮——历史回看没有可提交的会话。
   */
  onDrilldown?: (slots: DrilldownRequest) => void;
  /**
   * 当前会话 ID。FeedbackBar 与 ExportMenu 据此调用 feedback / share 接口。
   * 历史回看与对话流都应传入；不传时不渲染这两块。
   */
  sessionId?: string;
}

/**
 * 一轮回答的卡片。四个终态共用同一个容器，只在提示条与内容区块上有差别：
 * ok 走完整链路，chitchat 只给文本，clarify 给出补充信息的引导，
 * error 必须带上最后一次语句与报错 —— 失败不能是一片空白。
 */
export function AnswerCard({
  turn,
  compact = false,
  focused = false,
  onFocus,
  onRetry,
  onDrilldown,
  sessionId,
}: Props) {
  const { result, stages, streaming, errorMessage } = turn;

  if (errorMessage) {
    // FR-E-3：aborted / empty / network 三态。aborted 是用户主动行为，
    // 用中性提示且不给重试；empty / network 才是故障，配色与重试入口不同。
    const kind = turn.errorKind ?? 'network';
    const copy = TERMINAL_COPY[kind];
    return (
      <Card
        size="small"
        style={{ borderRadius: 12, borderColor: 'var(--wb-border)', boxShadow: 'var(--wb-shadow-card)' }}
      >
        <StageIndicator stages={stages} running={false} />
        <Alert
          type={copy.alertType}
          showIcon
          message={copy.title}
          // network 态优先展示底层错误文案（含状态码 / detail），
          // 其余两态用固定说明，避免把「已中断」误描述成故障。
          description={kind === 'network' ? errorMessage : copy.description}
          action={
            copy.retryable && onRetry ? (
              <Button size="small" onClick={onRetry}>
                重试
              </Button>
            ) : undefined
          }
          style={{ marginTop: 8 }}
        />
      </Card>
    );
  }

  if (!result) {
    const stalled = !streaming;
    return (
      <Card
        size="small"
        style={{ borderRadius: 12, borderColor: 'var(--wb-border)', boxShadow: 'var(--wb-shadow-card)' }}
      >
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
  const isOffline = (result.metric_used ?? []).length === 0 && result.intent === 'query';

  // 复制结论用的文本：附来源标识与时间戳，避免脱离上下文后不可读（FR-B-4）
  const buildCopyText = () => {
    const lines: string[] = [];
    if (result.answer) lines.push(result.answer);
    if (result.time_range?.resolved) lines.push(`时间范围：${result.time_range.resolved}`);
    if (result.metric_used?.length) lines.push(`口径：${result.metric_used.join('、')}`);
    if (result.caveats?.length) {
      lines.push('边界：');
      for (const c of result.caveats) lines.push(`- ${c}`);
    }
    lines.push(`来源：Text2SQL 生成于 ${localTimestamp()}`);
    return lines.join('\n');
  };

  // 下钻的主指标：基准里带的那个优先于召回列表的第一个——基准只有一个主指标，
  // 而 metric_used 可能召回多个（「成交总额与订单量」）。时间限定沿用本轮说法，
  // 丢掉时间会让下钻问句查出的数字与触发它的这一轮对不上。
  const drillMetric = result.baselines?.metric ?? result.metric_used?.[0] ?? null;
  const drillTime = result.time_range?.label ?? null;

  const handleFindingDrill = (finding: Finding) => {
    onDrilldown?.({
      source: 'finding',
      metric: finding.metric || drillMetric,
      dimension: finding.dimension,
      slice: finding.slice === null || finding.slice === undefined ? null : String(finding.slice),
      time_label: drillTime,
    });
  };

  const handleCellDrill = ({ dimension, value }: { dimension: string; value: string }) => {
    onDrilldown?.({ source: 'cell', metric: drillMetric, dimension, slice: value, time_label: drillTime });
  };

  return (
    <Card
      size="small"
      style={{
        borderRadius: 12,
        borderColor: focused ? 'var(--wb-primary)' : 'var(--wb-border)',
        boxShadow: 'var(--wb-shadow-card)',
        cursor: onFocus ? 'pointer' : undefined,
      }}
      onClick={onFocus}
    >
      <StageIndicator stages={stages} running={streaming} />

      {result.status === 'clarify' && (
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

      {/* 超范围与归因：系统判对了才走这两条路，本轮不生成 SQL，
          因此提示条要说清「为什么没有数据」，而不是呈现成一个失败。 */}
      {(result.status === 'out_of_scope' || result.status === 'attribution') && (
        <Alert
          type="warning"
          showIcon
          message={result.status === 'out_of_scope' ? '超出可答范围，未生成查询' : '问的是原因，已给出拆解问法'}
          description={
            result.status === 'out_of_scope'
              ? '这个问题的数据不在当前数据集里，或不属于统计查询能回答的类型。'
              : '一次聚合只能给出结果、给不出原因，下面给出可以定位原因的问法。'
          }
          style={{ marginTop: 8 }}
        />
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

      {isOffline && result.status === 'ok' && (
        // FR-D-4：未命中口径的显式提示。chitchat / clarify 命中口径为空是正常的，
        // 只有问 query 类型却召回不到任何已登记指标时才提示「未命中口径」。
        <Alert
          type="warning"
          showIcon
          message="本次未命中已登记指标"
          description="系统未在语义层找到与本问题匹配的指标定义，结论与数字仅来自生成式总结，请谨慎采纳。"
          style={{ marginTop: 8 }}
        />
      )}

      {result.answer && (
        <div style={{ marginTop: 10 }}>
          <Space style={{ marginBottom: 4 }} size={6}>
            <Text type="secondary" style={{ fontSize: 12 }}>结论</Text>
            {result.masked && (
              <Tooltip
                title={
                  result.masked_fields?.length
                    ? `已对敏感字段脱敏：${result.masked_fields.join('、')}`
                    : '结果中的敏感字段已脱敏'
                }
              >
                <Tag color="purple" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
                  已脱敏
                </Tag>
              </Tooltip>
            )}
            <Tooltip title="复制结论文本（含口径与生成时间）">
              <Button
                size="small"
                onClick={(e) => {
                  e.stopPropagation();
                  navigator.clipboard?.writeText(buildCopyText());
                }}
              >
                复制结论
              </Button>
            </Tooltip>
          </Space>
          <Paragraph style={{ margin: 0 }}>
            <AnswerText text={result.answer} />
          </Paragraph>
        </div>
      )}

      {isQuery && (result.number_checks?.length ?? 0) > 0 && (
        // FR-B-3：结论里有数字无法从结果集回溯。中性提示，不改动结论本身——
        // 核对器只负责指出，删改结论会让「模型说了什么」与「界面显示什么」脱节。
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 8 }}
          message="以下数字未能从结果集回溯，请人工核对"
          description={
            <Space size={[6, 6]} wrap>
              {result.number_checks?.map((item) => (
                <Tag key={item.text} color="default" style={{ borderRadius: 4 }}>
                  {item.text}
                </Tag>
              ))}
            </Space>
          }
        />
      )}

      {isQuery && (result.slot_gaps?.length ?? 0) > 0 && (
        // 槽位覆盖：语句合法、口径也对，但问到的某处没写进语句。这类偏差全程
        // 无报错，只能在这里点出来让用户核对，不能替用户改结论。
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message="这一轮的结果可能没覆盖你问到的部分，建议核对"
          description={
            <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
              {result.slot_gaps?.map((gap, i) => (
                <li key={`${gap.kind}-${i}`}>
                  {gap.expected}
                  {gap.detail ? `（${gap.detail}）` : ''}
                </li>
              ))}
            </ul>
          }
        />
      )}

      {!result.answer && result.insight && <AnswerText text={result.insight} />}

      {result.highlights.length > 0 && (
        <ul style={{ margin: '8px 0 0', paddingLeft: 20, lineHeight: 1.75 }}>
          {result.highlights.map((h, i) => (
            <li key={i}>{h}</li>
          ))}
        </ul>
      )}

      {/* FR-D-2：数据截至要常驻，因此即使本轮未命中已登记指标也渲染这一行 */}
      {isQuery && (result.metric_used.length > 0 || result.baselines || result.data_freshness) && (
        <div style={{ marginTop: 10 }}>
          <MetricBadge
            metricUsed={result.metric_used}
            caveats={result.caveats}
            timeRange={result.time_range}
            baselines={result.baselines}
            partialPeriod={result.partial_period}
            timeRangeConsistent={result.time_range_consistent}
            dataFreshness={result.data_freshness}
          />
        </div>
      )}

      {isQuery && result.findings.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <FindingList
            findings={result.findings}
            onDrilldown={onDrilldown ? handleFindingDrill : undefined}
          />
        </div>
      )}

      {isQuery && result.metric_used.length > 0 && (
        // FR-D-3：口径定义面板。折叠默认收起，避免每轮都拉两份接口。
        <div style={{ marginTop: 8 }}>
          <Collapse
            size="small"
            items={[
              {
                key: 'def',
                label: (
                  <span>
                    口径定义
                    <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                      {result.metric_used.length} 项指标 · {result.source_tables?.length ?? 0} 张来源表
                    </Text>
                  </span>
                ),
                children: (
                  <DefinitionPanel
                    metricNames={result.metric_used}
                    sourceTables={result.source_tables ?? []}
                  />
                ),
              },
            ]}
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
            exportPrefix={turn.question.slice(0, 20)}
            partialPeriod={result.partial_period}
          />
        </div>
      )}

      {isQuery && (
        // FR-P-3：研发信息统一收敛在一处，不与业务信息混在卡片主区。
        <div style={{ marginTop: 10 }}>
          <Collapse
            size="small"
            items={[
              {
                key: 'tech',
                label: '技术细节',
                children: (
                  <TechDetails
                    stages={stages}
                    sql={result.sql ?? ''}
                    originalSql={result.original_sql ?? ''}
                    sqlError={result.sql_error ?? ''}
                    guardWarnings={result.guard_warnings ?? []}
                    guardNotices={normalizeGuardNotices(result.guard_notices)}
                    retryCount={result.retry_count}
                    sessionId={sessionId}
                    question={turn.question}
                    sqlReasoning={result.sql_reasoning}
                    fixReasoning={result.fix_reasoning}
                    slots={result.slots ?? null}
                    originalQuestion={turn.originalQuestion ?? turn.question}
                    rewrittenQuestion={turn.rewrittenQuestion ?? result.rewritten_question ?? ''}
                    fixHistory={result.fix_history ?? []}
                  />
                ),
              },
            ]}
          />
        </div>
      )}

      {hasRows && !compact && (
        <div style={{ marginTop: 10 }}>
          <ResultTable
            columns={result.columns}
            rows={result.rows}
            exportPrefix={turn.question.slice(0, 20)}
            onCellDrill={onDrilldown ? handleCellDrill : undefined}
            truncated={result.truncated}
            totalRowCount={result.row_count}
          />
          {result.truncated && (
            <Text type="warning" style={{ display: 'block', fontSize: 12, marginTop: 6 }}>
              结果已被行数上限截断
            </Text>
          )}
        </div>
      )}

      {/* FR-G-1 反馈闭环与 FR-N-1/3 导出分享。
          已落库（turn.turnId 非空）且 sessionId 已知时才显示；
          streaming 中或落库失败的轮次不渲染，避免后端 404。 */}
      {sessionId && turn.turnId && result.status !== 'error' && (
        <div className="t2sql-no-print" style={{ marginTop: 8 }}>
          <Space style={{ justifyContent: 'space-between', width: '100%' }}>
            <FeedbackBar turnId={turn.turnId} sessionId={sessionId} />
            <ExportMenu turn={turn} sessionId={sessionId} />
          </Space>
        </div>
      )}
    </Card>
  );
}
