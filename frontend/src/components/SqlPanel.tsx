import { Button, Collapse, Space, Typography } from 'antd';

const { Paragraph, Text } = Typography;

interface Props {
  /** 实际执行的语句，可能已被行数上限包裹改写 */
  sql: string;
  /** 模型生成的原始语句。与 sql 不同时说明守卫改写过 */
  originalSql?: string;
  error?: string;
  retryCount?: number;
}

function SqlBlock({ code }: { code: string }) {
  return (
    <pre
      style={{
        margin: 0,
        padding: 12,
        background: '#fafafa',
        border: '1px solid #f0f0f0',
        borderRadius: 6,
        fontSize: 13,
        fontFamily: 'Consolas, Monaco, "Courier New", monospace',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {code}
    </pre>
  );
}

/**
 * SQL 折叠面板。默认折叠：绝大多数用户关心的是数字与结论，
 * 语句留作核对入口，展开即可见，不占用首屏。
 */
export function SqlPanel({ sql, originalSql, error, retryCount }: Props) {
  if (!sql && !originalSql && !error) return null;

  const rewritten = Boolean(originalSql && sql && originalSql.trim() !== sql.trim());
  const items = [];

  if (sql) {
    items.push({
      key: 'executed',
      label: (
        <Space size={8}>
          <span>执行的 SQL</span>
          {rewritten && <Text type="secondary" style={{ fontSize: 12 }}>（已被安全沙箱改写）</Text>}
          {retryCount ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              修正 {retryCount} 次
            </Text>
          ) : null}
        </Space>
      ),
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <SqlBlock code={sql} />
          <Button
            size="small"
            onClick={() => navigator.clipboard?.writeText(sql)}
            style={{ alignSelf: 'flex-start' }}
          >
            复制
          </Button>
        </Space>
      ),
    });
  }

  if (rewritten) {
    items.push({
      key: 'original',
      label: '模型生成的原始 SQL',
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Paragraph type="secondary" style={{ fontSize: 12, margin: 0 }}>
            改写通常由强制行数上限或补写 LIMIT 引起，核对口径时用这一条。
          </Paragraph>
          <SqlBlock code={originalSql as string} />
        </Space>
      ),
    });
  }

  if (error) {
    items.push({
      key: 'error',
      label: <Text type="danger">执行报错</Text>,
      children: <SqlBlock code={error} />,
    });
  }

  return <Collapse size="small" items={items} />;
}
