import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Button,
  Card,
  Input,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { archiveSession, deleteSession, getSession, listSessions } from '@/api/session';
import type { SessionItem } from '@/api/types';
import { turnToChatTurn } from '@/hooks/useChatStream';
import { downloadBlob, localTimestamp, slugifyQuestion } from '@/lib/exporters';
import { buildSessionPptx } from '@/lib/pptxBuilder';
import { buildSessionMarkdown } from '@/lib/markdownBuilder';
import { renderChartImage } from '@/lib/chartImage';

const { Text } = Typography;

type Filter = 'active' | 'archived' | 'all';

/**
 * 历史会话。归档是软删除：归档后不出现在默认列表，改筛选即可找回，
 * 删除才是物理移除 —— 两个动作分开，避免误操作直接抹掉记录。
 */
export function HistoryPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<SessionItem[]>([]);
  const [keyword, setKeyword] = useState('');
  const [filter, setFilter] = useState<Filter>('active');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportingId, setExportingId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listSessions(keyword || undefined, filter !== 'active');
      const filtered =
        filter === 'all' ? res.items : res.items.filter((s) => s.archived === (filter === 'archived'));
      setItems(filtered);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [keyword, filter]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onArchive = async (item: SessionItem) => {
    try {
      await archiveSession(item.id, !item.archived);
      void message.success(item.archived ? '已恢复' : '已归档');
      await reload();
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const onDelete = async (item: SessionItem) => {
    try {
      await deleteSession(item.id);
      void message.success('已删除');
      await reload();
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  };

  /**
   * FR-N-2 导出整轮会话为 PPTX，一页一回合。
   * 历史会话的轮次已落库，可以从 getSession 一次性拉全。
   * 图表用静态展示——历史回看的 chart.option 在快照里是结构化配置，
   * 这里不渲染 echarts 实例（脱离了 DOM 上下文），用文本占位。
   */
  const onExportPptx = async (item: SessionItem) => {
    setExportingId(item.id);
    try {
      const detail = await getSession(item.id);
      const turns = detail.turns.map(turnToChatTurn);
      if (!turns.length) {
        void message.warning('该会话还没有问答轮次');
        return;
      }
      // 数据截至：取最近一轮的 data_freshness（result 里可能没有，回退到最近一轮 created_at）
      const lastTurn = turns[turns.length - 1];
      const dataCutoff =
        lastTurn.result?.data_freshness?.orders_cutoff ||
        lastTurn.result?.data_freshness?.refunds_cutoff ||
        lastTurn.createdAt ||
        null;
      const blob = await buildSessionPptx(turns, {
        sessionTitle: item.title,
        dataCutoff,
        // FR-N-2：图表以图片嵌入。历史回看没有挂载中的 echarts 实例，这里用
        // 离屏 DOM 按同一份 option 渲一遍再取图，导出的图与界面同源。
        // 取图失败返回 null，该页就是没有图，不中断整份导出。
        getChartImage: async (turn) => renderChartImage(turn.result?.chart?.option ?? null),
      });
      const slug = slugifyQuestion(item.title || 'session');
      downloadBlob(`${slug}_${localTimestamp()}.pptx`, blob);
      void message.success(`已导出 ${turns.length} 页 PPTX`);
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setExportingId(null);
    }
  };

  /**
   * FR-H-4 导出整轮会话为 Markdown。
   * 与 PPTX 走同一份 turns（已落库 + 后端脱敏后的 rows），不再请求原始数据。
   * 文件名带生成时间。Markdown 比 PPTX 轻：结果表按 Markdown 表格语法输出，
   * 行数上限 50 行，超过在表后追加截断提示。
   */
  const onExportMarkdown = async (item: SessionItem) => {
    setExportingId(item.id);
    try {
      const detail = await getSession(item.id);
      const turns = detail.turns.map(turnToChatTurn);
      if (!turns.length) {
        void message.warning('该会话还没有问答轮次');
        return;
      }
      const lastTurn = turns[turns.length - 1];
      const dataCutoff =
        lastTurn.result?.data_freshness?.orders_cutoff ||
        lastTurn.result?.data_freshness?.refunds_cutoff ||
        lastTurn.createdAt ||
        null;
      const md = buildSessionMarkdown(turns, { sessionTitle: item.title, dataCutoff });
      const slug = slugifyQuestion(item.title || 'session');
      const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
      downloadBlob(`${slug}_${localTimestamp()}.md`, blob);
      void message.success(`已导出 ${turns.length} 轮 Markdown`);
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setExportingId(null);
    }
  };

  const columns: ColumnsType<SessionItem> = [
    {
      title: '标题',
      dataIndex: 'title',
      ellipsis: true,
      render: (title: string, item) => (
        <Space size={6}>
          <a onClick={() => navigate(`/?session=${encodeURIComponent(item.id)}`)}>{title}</a>
          {item.archived && <Tag>已归档</Tag>}
        </Space>
      ),
    },
    { title: '轮次', dataIndex: 'turn_count', width: 80, render: (n: number) => `${n} 轮` },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      width: 180,
      render: (t: string) => <Text type="secondary">{t.replace('T', ' ')}</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (_v, item) => (
        <Space size={4} wrap>
          <Button size="small" type="link" onClick={() => navigate(`/?session=${encodeURIComponent(item.id)}`)}>
            查看
          </Button>
          <Button
            size="small"
            type="link"
            loading={exportingId === item.id}
            disabled={
              item.turn_count === 0 || (exportingId !== null && exportingId !== item.id)
            }
            onClick={() => void onExportPptx(item)}
          >
            导出 PPTX
          </Button>
          <Button
            size="small"
            type="link"
            loading={exportingId === item.id}
            disabled={
              item.turn_count === 0 || (exportingId !== null && exportingId !== item.id)
            }
            onClick={() => void onExportMarkdown(item)}
          >
            导出 MD
          </Button>
          <Button size="small" type="link" onClick={() => void onArchive(item)}>
            {item.archived ? '恢复' : '归档'}
          </Button>
          <Popconfirm title="删除后不可恢复，确认删除？" onConfirm={() => void onDelete(item)}>
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Card style={{ borderRadius: 8 }}>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input.Search
          allowClear
          placeholder="按标题或提问内容搜索"
          style={{ width: 280 }}
          onSearch={(v) => setKeyword(v.trim())}
          // allowClear 只清输入框，不点搜索的话不会触发 onSearch。
          // 这里补一次 onChange，点 X 时立即恢复完整列表
          onChange={(e) => {
            if (!e.target.value) setKeyword('');
          }}
        />
        <Segmented<Filter>
          value={filter}
          onChange={setFilter}
          options={[
            { label: '未归档', value: 'active' },
            { label: '已归档', value: 'archived' },
            { label: '全部', value: 'all' },
          ]}
        />
        <Button onClick={() => void reload()}>刷新</Button>
      </Space>

      {error && (
        <Text type="danger" style={{ display: 'block', marginBottom: 8 }}>
          {error}
        </Text>
      )}

      <Table<SessionItem>
        size="small"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={items}
        locale={{ emptyText: '没有符合条件的会话' }}
        pagination={items.length > 10 ? { pageSize: 10, size: 'small' } : false}
      />
    </Card>
  );
}
