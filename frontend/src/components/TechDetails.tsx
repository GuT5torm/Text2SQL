import { Alert, Collapse, Descriptions, Tag, Typography } from 'antd';
import type { FixHistoryItem, GuardNotice, Slots, StageEvent } from '@/api/types';
import { SqlPanel } from './SqlPanel';

const { Text } = Typography;

interface Props {
  stages: StageEvent[];
  sql: string;
  originalSql?: string;
  sqlError?: string;
  guardWarnings: string[];
  /** FR-I-2：结构化双通道告警。为空时退回 guardWarnings 老渲染。 */
  guardNotices?: GuardNotice[];
  retryCount?: number;
  /** FR-C-2：会话ID，传给 SqlPanel 调用 SQL 解释接口 */
  sessionId?: string;
  /** FR-C-2：用户原始问题，传给 SqlPanel 调用 SQL 解释接口 */
  question?: string;
  /** FR-C-1：写法依据，透传给 SqlPanel */
  sqlReasoning?: string;
  /** FR-C-1：最后一次修正理由，透传给 SqlPanel */
  fixReasoning?: string;
  /** FR-A-5：结构化槽位，路由节点的语义解析结果。为空时不渲染该区块 */
  slots?: Slots | null;
  /** FR-H-3：用户原话。未传时按 question 兜底（流式过程中 result 还没到） */
  originalQuestion?: string;
  /** FR-H-3：路由改写后的问句。空串表示未改写 */
  rewrittenQuestion?: string;
  /** FR-E-4：修正历史。空数组或 undefined 表示未发生过修正 */
  fixHistory?: FixHistoryItem[];
}

/**
 * 统一技术细节入口（FR-P-3）。
 *
 * 阶段时序、SQL、守卫告警都属「研发信息」：默认收在折叠里，需要时再开。
 * 不在本面板展示耗时本身——耗时已经出现在阶段时间线条目里，这里只汇总，不
 * 重复渲染。
 *
 * FR-P-1 / FR-P-2：耗时从摘要行移除，仅在阶段条目里保留；与该条一致。
 */
export function TechDetails({
  stages,
  sql,
  originalSql,
  sqlError,
  guardWarnings,
  guardNotices,
  retryCount,
  sessionId,
  question,
  sqlReasoning,
  fixReasoning,
  slots,
  originalQuestion,
  rewrittenQuestion,
  fixHistory,
}: Props) {
  // FR-I-2：rejected 黄色（语句被拒绝），rewritten 蓝色（改写后放行）。
  // 老版本没有结构化通道时，退回纯字符串的黄色告警保持旧行为。
  const notices: GuardNotice[] = guardNotices?.length
    ? guardNotices
    : guardWarnings.map((message) => ({ kind: 'rejected' as const, code: '', message }));

  // FR-A-5：只有路由节点产出过槽位时才渲染这一格。
  // 历史回看老数据缺 slots 时 turn.slots 为 undefined，这里直接跳过。
  const hasSlots = Boolean(
    slots &&
      (slots.metrics?.length ||
        slots.dimensions?.length ||
        slots.filters?.length ||
        slots.time_range ||
        slots.order_by ||
        slots.top_n),
  );

  // FR-H-3：路由节点判定需要补全时会改写问句。展示原话与改写后的问句，
  // 让用户能核对「系统替我补了什么」。未改写时 rewrittenQuestion 为空，
  // 原话与 question 同值——此时仍展示一格，让用户知道问句没有被改写。
  const originalQ = originalQuestion ?? question ?? '';
  const rewrittenQ = rewrittenQuestion ?? '';
  const hasQuestionTrace = Boolean(originalQ || rewrittenQ);
  const rewritten = rewrittenQ.length > 0 && rewrittenQ !== originalQ;

  const items = [
    ...(hasQuestionTrace
      ? [
          {
            key: 'question_trace',
            label: (
              <span>
                问句来源
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                  {rewritten ? '已改写' : '未改写'}
                </Text>
              </span>
            ),
            children: (
              <Descriptions
                size="small"
                column={1}
                labelStyle={{ width: 80, fontSize: 12 }}
                contentStyle={{ fontSize: 12 }}
                items={[
                  {
                    key: 'original',
                    label: '原话',
                    children: originalQ ? (
                      <Text>{originalQ}</Text>
                    ) : (
                      <Text type="secondary">未记录</Text>
                    ),
                  },
                  ...(rewritten
                    ? [
                        {
                          key: 'rewritten',
                          label: '改写后',
                          children: <Text type="warning">{rewrittenQ}</Text>,
                        },
                      ]
                    : []),
                ]}
              />
            ),
          },
        ]
      : []),
    {
      key: 'stages',
      label: (
        <span>
          阶段时序
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
            共 {stages.length} 个节点
          </Text>
        </span>
      ),
      children: stages.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 12 }}>本轮无阶段进度（历史回看）</Text>
      ) : (
        <ol style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7, fontSize: 12 }}>
          {stages.map((s, i) => (
            <li key={`${s.node}-${i}`}>
              <Text strong>{s.label}</Text>
              {s.detail ? <Text type="secondary">　{s.detail}</Text> : null}
              <Text type="secondary" style={{ marginLeft: 8 }}>
                {(s.elapsed_ms ?? 0).toFixed(0)} ms
              </Text>
            </li>
          ))}
        </ol>
      ),
    },
    {
      key: 'sql',
      label: (
        <span>
          SQL 与安全沙箱
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
            {sql ? '查看执行语句' : '本轮无执行语句'}
          </Text>
        </span>
      ),
      children: (
        <SqlPanel
          sql={sql}
          originalSql={originalSql}
          error={sqlError}
          retryCount={retryCount}
          sessionId={sessionId}
          question={question}
          sqlReasoning={sqlReasoning}
          fixReasoning={fixReasoning}
        />
      ),
    },
    // FR-E-4：修正历史。fix 节点每次回环追加一条，结构与 fix.py::_render_fix_history
    // 一致。只有发生过修正的轮次才渲染，避免给正常轮次加空面板。
    ...(fixHistory && fixHistory.length > 0
      ? [
          {
            key: 'fix_history',
            label: (
              <span>
                修正历史
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                  共 {fixHistory.length} 次
                </Text>
              </span>
            ),
            children: (
              <ol style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7, fontSize: 12 }}>
                {fixHistory.map((item, i) => (
                  <li key={`fix-${i}`}>
                    <Text strong>阶段 {item.stage || 'unknown'}</Text>
                    <Text type="secondary">　报错：{(item.error || '').slice(0, 300)}</Text>
                  </li>
                ))}
              </ol>
            ),
          },
        ]
      : []),
    ...(hasSlots && slots
      ? [
          {
            key: 'slots',
            label: (
              <span>
                问题槽位
                <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                  FR-A-5 结构化解析
                </Text>
              </span>
            ),
            children: (
              <Descriptions
                size="small"
                column={1}
                labelStyle={{ width: 80, fontSize: 12 }}
                contentStyle={{ fontSize: 12 }}
                items={[
                  {
                    key: 'metrics',
                    label: '指标',
                    children: slots.metrics?.length
                      ? slots.metrics.map((m) => <Tag key={m} color="blue">{m}</Tag>)
                      : <Text type="secondary">未识别</Text>,
                  },
                  {
                    key: 'dimensions',
                    label: '维度',
                    children: slots.dimensions?.length
                      ? slots.dimensions.map((d) => <Tag key={d} color="cyan">{d}</Tag>)
                      : <Text type="secondary">无</Text>,
                  },
                  {
                    key: 'filters',
                    label: '过滤',
                    children: slots.filters?.length
                      ? slots.filters.map((f, i) => (
                          <Tag key={`f-${i}`} color="orange">
                            {f.field}{f.op ? ` ${f.op}` : '='}{f.value}
                          </Tag>
                        ))
                      : <Text type="secondary">无</Text>,
                  },
                  {
                    key: 'time_range',
                    label: '时间',
                    children: slots.time_range || <Text type="secondary">未识别</Text>,
                  },
                  {
                    key: 'order_by',
                    label: '排序',
                    children: slots.order_by
                      ? `${slots.order_by.field} ${slots.order_by.direction}`
                      : <Text type="secondary">无</Text>,
                  },
                  {
                    key: 'top_n',
                    label: 'TopN',
                    children: slots.top_n != null
                      ? String(slots.top_n)
                      : <Text type="secondary">无</Text>,
                  },
                ]}
              />
            ),
          },
        ]
      : []),
    {
      key: 'guard',
      label: (
        <span>
          安全沙箱告警
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
            {notices.length > 0 ? `${notices.length} 条` : '无'}
          </Text>
        </span>
      ),
      children:
        notices.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {notices.map((notice, i) => (
              <Alert
                key={`${notice.code || 'x'}-${i}`}
                type={notice.kind === 'rewritten' ? 'info' : 'warning'}
                showIcon
                message={
                  <span>
                    {notice.code && (
                      <Text strong style={{ marginRight: 6 }}>
                        {notice.code}
                      </Text>
                    )}
                    {notice.kind === 'rewritten' ? '语句改写后放行：' : '语句被沙箱拒绝：'}
                    {notice.message}
                  </span>
                }
              />
            ))}
          </div>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            本轮未触发安全沙箱告警。
          </Text>
        ),
    },
  ];

  return <Collapse size="small" items={items} />;
}
