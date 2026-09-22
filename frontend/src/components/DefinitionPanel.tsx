import { useEffect, useMemo, useState } from 'react';
import { Empty, Skeleton, Tag, Typography } from 'antd';
import {
  fetchTables,
  getEnumDefinitions,
  getMetric,
  getTablesForMetric,
  type FieldDoc,
  type MetricDoc,
  type TableDoc,
} from '@/api/semantic';

const { Title, Paragraph, Text } = Typography;

/**
 * 口径定义面板（FR-D-3）。
 *
 * 展示六项内容，全部从语义层读，前端不做重新计算：
 *   ① 口径表达式  ② 单位
 *   ③ 分母与状态过滤（denominator + required_filters）
 *   ④ 必备维度   ⑤ caveats（口径边界）
 *   ⑥ 来源表里 enum 字段的划分依据（enum + definition）
 *
 * 渲染降级：取数失败只显示一行提示，不抛出。失败的面板不应阻塞问题答答本身。
 */
interface Props {
  /** 面板要展示的若干个指标名（取自 result.metric_used） */
  metricNames: string[];
  /** 直接传入来源表名；若为空则从指标的 `tables` 字段推导 */
  sourceTables?: string[];
}

interface Bundle {
  metrics: MetricDoc[];
  tables: TableDoc[];
  enumFields: FieldDoc[];
}

export function DefinitionPanel({ metricNames, sourceTables }: Props) {
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (metricNames.length === 0) return;
    let cancelled = false;
    setBundle(null);
    setError(null);
    (async () => {
      try {
        // 同时取指标与表。表要么用调用方传的 sourceTables，要么从每个指标的
        // tables 字段推导。
        const metrics = (
          await Promise.all(metricNames.map((n) => getMetric(n)))
        ).filter((m): m is MetricDoc => m !== null);
        const tableNames =
          sourceTables && sourceTables.length > 0
            ? sourceTables
            : Array.from(new Set(metrics.flatMap(() => [])));
        const tables =
          tableNames.length > 0
            ? await getTablesForMetric(tableNames)
            : await fetchTables().then((all) => all.slice(0, 0));
        const enumFields = await getEnumDefinitions(tables);
        if (!cancelled) setBundle({ metrics, tables, enumFields });
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : '语义层数据加载失败');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [metricNames.join(','), sourceTables?.join(',')]); // eslint-disable-line react-hooks/exhaustive-deps

  const hasContent = useMemo(
    () => bundle !== null && (bundle.metrics.length > 0 || bundle.tables.length > 0 || bundle.enumFields.length > 0),
    [bundle],
  );

  if (error) {
    return <Text type="warning" style={{ fontSize: 12 }}>口径面板加载失败：{error}</Text>;
  }
  if (!bundle) return <Skeleton active paragraph={{ rows: 3 }} title={false} />;
  if (!hasContent) return <Empty description="该指标未在语义层登记" image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, fontSize: 13 }}>
      {bundle.metrics.map((m) => (
        <section key={m.name} data-testid={`def-${m.name}`}>
          <Title level={5} style={{ margin: 0, fontSize: 13 }}>
            {m.display}
            <Text type="secondary" style={{ marginLeft: 6, fontWeight: 400 }}>
              （{m.name}）
            </Text>
          </Title>
          <Item label="① 口径表达式">
            <code style={monoStyle}>{m.expression}</code>
          </Item>
          <Item label="② 单位">{m.unit ?? <EmptyToken text="未登记" />}</Item>
          <Item label="③ 分母与状态过滤">
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {m.denominator ? (
                <Tag color="blue" style={{ borderRadius: 4, marginInlineEnd: 0 }}>{m.denominator}</Tag>
              ) : (
                <EmptyToken text="未声明 denominator" />
              )}
              {(m.required_filters ?? []).map((f) => (
                <code key={f} style={{ ...monoStyle, background: '#fafafa', border: '1px solid #f0f0f0' }}>
                  {f}
                </code>
              ))}
            </div>
          </Item>
          <Item label="④ 必备维度">
            {m.dimensions && m.dimensions.length > 0 ? (
              <Space size={4} wrap>
                {m.dimensions.map((d) => (
                  <Tag key={d} color="geekblue" style={{ borderRadius: 4, marginInlineEnd: 0 }}>{d}</Tag>
                ))}
              </Space>
            ) : (
              <EmptyToken text="未声明可下钻维度" />
            )}
          </Item>
          <Item label="⑤ 口径边界（caveats）">
            <Paragraph
              type="secondary"
              style={{ margin: 0, fontSize: 12, lineHeight: 1.6 }}
              ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
            >
              {m.caveats || '未填写'}
            </Paragraph>
          </Item>
        </section>
      ))}

      {bundle.tables.length > 0 && (
        <section>
          <Title level={5} style={{ margin: 0, fontSize: 13 }}>涉及表</Title>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {bundle.tables.map((t) => (
              <Tag key={t.name} color="default" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
                {t.display}（{t.name}）
              </Tag>
            ))}
          </div>
        </section>
      )}

      {bundle.enumFields.length > 0 && (
        <section>
          <Title level={5} style={{ margin: 0, fontSize: 13 }}>⑥ 枚举字段的划分依据</Title>
          <ul style={{ margin: 0, paddingLeft: 16, lineHeight: 1.7 }}>
            {bundle.enumFields.map((f) => (
              <li key={`${f.name}`}>
                <Text strong>{f.display ?? f.name}</Text>
                <Text type="secondary">：</Text>
                <span>{f.definition}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 6 }}>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div>{children}</div>
    </div>
  );
}

function EmptyToken({ text }: { text: string }) {
  return <Text type="secondary" style={{ fontSize: 12 }}>{text}</Text>;
}

function Space({ size = 8, children, wrap = false }: { size?: number; children: React.ReactNode; wrap?: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        gap: size,
        flexWrap: wrap ? 'wrap' : 'nowrap',
        alignItems: 'center',
      }}
    >
      {children}
    </div>
  );
}

const monoStyle: React.CSSProperties = {
  fontFamily: 'Consolas, Monaco, "Courier New", monospace',
  fontSize: 12,
  padding: '2px 6px',
  background: '#f5f5f5',
  borderRadius: 4,
};
