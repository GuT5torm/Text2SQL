import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, Card, Empty, Skeleton, Space, Tag, Tooltip, Typography } from 'antd';
import type { ShareReadResponse } from '@/api/types';
import { readShare } from '@/api/share';
import { AnswerText } from '@/components/AnswerText';
import { ChartPanel } from '@/components/ChartPanel';
import { FindingList } from '@/components/FindingList';
import { MetricBadge } from '@/components/MetricBadge';
import { ResultTable } from '@/components/ResultTable';

const { Title, Text, Paragraph } = Typography;

/**
 * 只读分享页（FR-N-3）。
 *
 * 路由 /#/share/{token}。token 即访问凭据，不要求登录。
 * 页面只展示快照里的「已落库」内容，不发起提问，也没有提问入口。
 * 脱敏在落库前已完成（FR-I-3），快照中的 rows 即为脱敏后的数据。
 */
export function SharePage() {
  const { token = '' } = useParams<{ token: string }>();
  const [data, setData] = useState<ShareReadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) {
      setError('缺少 token');
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    readShare(token)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (loading) {
    return (
      <Card style={{ borderRadius: 8 }}>
        <Skeleton active paragraph={{ rows: 4 }} />
      </Card>
    );
  }

  if (error) {
    return (
      <Card style={{ borderRadius: 8 }}>
        <Alert
          type="error"
          showIcon
          message="分享链接不可用"
          description={
            <Space direction="vertical">
              <Text>{error}</Text>
              <Text type="secondary">
                链接可能已被撤销、过期，或本身不存在。可联系分享者重新生成。
              </Text>
            </Space>
          }
        />
      </Card>
    );
  }

  if (!data) {
    return (
      <Card style={{ borderRadius: 8 }}>
        <Empty description="快照为空" />
      </Card>
    );
  }

  const s = data.snapshot;
  const hasRows = (s.result?.rows?.length ?? 0) > 0;

  return (
    <Card style={{ borderRadius: 8 }}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Title level={5} style={{ margin: 0 }}>
            只读分享
          </Title>
          <Space size={8}>
            <Tag color="default">只读</Tag>
            {data.expires_at && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                过期时间：{data.expires_at.replace('T', ' ')}
              </Text>
            )}
          </Space>
        </Space>

        <Card size="small" style={{ borderRadius: 6 }} styles={{ body: { padding: '8px 12px' } }}>
          <Text strong style={{ fontSize: 13 }}>
            {s.question}
          </Text>
        </Card>

        {s.answer && (
          <Card size="small" style={{ borderRadius: 6 }}>
            <Space style={{ marginBottom: 4 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                结论
              </Text>
              {s.masked && (
                <Tooltip
                  title={
                    s.masked_fields?.length
                      ? `已对敏感字段脱敏：${s.masked_fields.join('、')}`
                      : '结果中的敏感字段已脱敏'
                  }
                >
                  <Tag color="purple" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
                    已脱敏
                  </Tag>
                </Tooltip>
              )}
            </Space>
            <Paragraph style={{ margin: '4px 0 0' }}>
              <AnswerText text={s.answer} />
            </Paragraph>
          </Card>
        )}

        {(s.number_checks?.length ?? 0) > 0 && (
          <Alert
            type="info"
            showIcon
            message="以下数字未能从结果集回溯，请人工核对"
            description={
              <Space size={[6, 6]} wrap>
                {s.number_checks?.map((item) => (
                  <Tag key={item.text} color="default" style={{ borderRadius: 4 }}>
                    {item.text}
                  </Tag>
                ))}
              </Space>
            }
          />
        )}

        {s.metric_used.length > 0 || s.baselines ? (
          <MetricBadge
            metricUsed={s.metric_used}
            caveats={s.caveats}
            timeRange={s.time_range}
            baselines={s.baselines}
            partialPeriod={s.partial_period}
            timeRangeConsistent={s.time_range_consistent ?? null}
          />
        ) : null}

        {s.findings.length > 0 && <FindingList findings={s.findings} />}

        {hasRows && (
          <ChartPanel
            chart={s.chart}
            columns={s.result.columns}
            rows={s.result.rows}
            title={s.question.slice(0, 40)}
            partialPeriod={s.partial_period}
          />
        )}

        {hasRows && (
          <ResultTable columns={s.result.columns} rows={s.result.rows} exportPrefix={`share_${data.turn_id ?? 'x'}`} />
        )}

        {s.sql && (
          <Card size="small" title="执行语句" style={{ borderRadius: 6 }}>
            <pre
              style={{
                margin: 0,
                padding: 8,
                background: '#fafafa',
                borderRadius: 4,
                fontSize: 12,
                overflowX: 'auto',
                whiteSpace: 'pre-wrap',
              }}
            >
              {s.sql}
            </pre>
          </Card>
        )}

        <Paragraph type="secondary" style={{ fontSize: 11, margin: 0 }}>
          数据截至：{s.created_at ?? '未知'}　快照创建：{data.created_at ?? '未知'}
        </Paragraph>
      </Space>
    </Card>
  );
}
