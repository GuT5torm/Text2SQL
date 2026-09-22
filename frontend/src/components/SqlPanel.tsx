import { useState } from 'react';
import { Alert, Button, Collapse, List, Skeleton, Space, Typography } from 'antd';
import { apiSend } from '@/api/client';
import type { SqlExplainResponse, SqlExplainSegment } from '@/api/types';
import { downloadText, localTimestamp, slugifyQuestion } from '@/lib/exporters';

const { Paragraph, Text } = Typography;

interface Props {
  /** 实际执行的语句，可能已被行数上限包裹改写 */
  sql: string;
  /** 模型生成的原始语句。与 sql 不同时说明守卫改写过 */
  originalSql?: string;
  error?: string;
  retryCount?: number;
  /** 文件名命名用的问句简写。默认从 sql 第一行提取 */
  exportPrefix?: string;
  /** 会话ID，传给 SQL 解释接口用于日志关联 */
  sessionId?: string;
  /** 用户原始问题，传给 SQL 解释接口 */
  question?: string;
  /** FR-C-1：generate 节点给出的写法依据 */
  sqlReasoning?: string;
  /** FR-C-1：fix 节点最后一次修正的理由。无修正时为空 */
  fixReasoning?: string;
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
 *
 * FR-C-2：提供「解释」按钮，按需调用 /api/sql/explain 获取分段说明。
 */
export function SqlPanel({
  sql,
  originalSql,
  error,
  retryCount,
  exportPrefix,
  sessionId,
  question,
  sqlReasoning,
  fixReasoning,
}: Props) {
  const [explainSegments, setExplainSegments] = useState<SqlExplainSegment[] | null>(null);
  const [explainLoading, setExplainLoading] = useState(false);
  const [explainError, setExplainError] = useState<string | null>(null);

  if (!sql && !originalSql && !error) return null;

  const rewritten = Boolean(originalSql && sql && originalSql.trim() !== sql.trim());
  const items = [];

  const handleDownload = (content: string) => {
    const filename = `${exportPrefix ?? slugifyQuestion(content)}_${localTimestamp()}.sql`;
    downloadText(filename, content + '\n', 'application/sql;charset=utf-8');
  };

  const handleExplain = async () => {
    if (!sql) return;
    setExplainLoading(true);
    setExplainError(null);
    try {
      const resp = await apiSend<SqlExplainResponse>('POST', '/api/sql/explain', {
        sql,
        question: question ?? '',
        session_id: sessionId ?? '',
      });
      if (resp.error) {
        setExplainError(resp.error);
      } else {
        setExplainSegments(resp.segments);
      }
    } catch (err) {
      setExplainError(err instanceof Error ? err.message : String(err));
    } finally {
      setExplainLoading(false);
    }
  };

  if (sql) {
    const extraChildren = (
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <SqlBlock code={sql} />
        <Space>
          <Button
            size="small"
            onClick={() => navigator.clipboard?.writeText(sql)}
          >
            复制
          </Button>
          <Button size="small" onClick={() => handleDownload(sql)}>
            下载 .sql
          </Button>
          {/* FR-C-2：按需 SQL 解释 */}
          <Button
            size="small"
            loading={explainLoading}
            onClick={handleExplain}
          >
            解释
          </Button>
        </Space>
        {explainError && (
          <Alert
            type="error"
            message="SQL 解释失败"
            description={explainError}
            showIcon
            style={{ margin: 0 }}
          />
        )}
        {explainLoading && <Skeleton active paragraph={{ rows: 2 }} />}
        {explainSegments && explainSegments.length > 0 && (
          <Collapse
            size="small"
            defaultActiveKey={['explain']}
            items={[
              {
                key: 'explain',
                label: (
                  <Space size={8}>
                    <span>SQL 解释</span>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      （FR-C-2 纯结构化解析）
                    </Text>
                  </Space>
                ),
                children: (
                  <List
                    size="small"
                    dataSource={explainSegments}
                    renderItem={(seg: SqlExplainSegment) => (
                      <List.Item>
                        <Text style={{ fontSize: 13 }}>
                          {seg.index}. {seg.text}
                        </Text>
                      </List.Item>
                    )}
                  />
                ),
              },
            ]}
          />
        )}
      </Space>
    );
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
      children: extraChildren,
    });
  }

  // FR-C-1：写法说明落位到 SQL 面板。此前 sql_reasoning 只存在于返回体与前端
  // 类型声明里，没有任何渲染路径接住它，等于没交付。历史回看与桩模式下该字段
  // 可能为空，此时整项不渲染，不占版面。
  if (sqlReasoning || fixReasoning) {
    items.push({
      key: 'reasoning',
      label: (
        <Space size={8}>
          <span>写法说明</span>
          <Text type="secondary" style={{ fontSize: 12 }}>
            （模型给出的写法依据）
          </Text>
        </Space>
      ),
      children: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {sqlReasoning ? (
            <Paragraph style={{ fontSize: 13, margin: 0, whiteSpace: 'pre-wrap' }}>
              {sqlReasoning}
            </Paragraph>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              本轮未返回写法依据。
            </Text>
          )}
          {fixReasoning ? (
            <>
              <Text strong style={{ fontSize: 12 }}>
                最后一次修正的理由
              </Text>
              <Paragraph style={{ fontSize: 13, margin: 0, whiteSpace: 'pre-wrap' }}>
                {fixReasoning}
              </Paragraph>
            </>
          ) : null}
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
