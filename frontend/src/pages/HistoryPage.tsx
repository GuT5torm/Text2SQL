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
import { archiveSession, deleteSession, listSessions } from '@/api/session';
import type { SessionItem } from '@/api/types';

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
      width: 160,
      render: (_v, item) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => navigate(`/?session=${encodeURIComponent(item.id)}`)}>
            查看
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
