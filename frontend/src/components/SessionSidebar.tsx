import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Badge, Button, Checkbox, Input, Modal, Popconfirm, Space, Tooltip, Typography, message } from 'antd';
import {
  BellOutlined,
  CheckSquareOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  EditOutlined,
  HistoryOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { deleteSession, listSessions, renameSession } from '@/api/session';
import type { SessionItem } from '@/api/types';

const { Text } = Typography;

/**
 * 会话按时间分组（对齐 DeepSeek 的对话列表）。
 *
 * 后端 updated_at 是不带时区的本地时间串（如 2026-09-22T16:31:00），
 * new Date 会按本地时区解析，与当前时间同基准，因此可以直接比较日历日。
 */
const GROUP_ORDER = ['今天', '昨天', '7 天内', '30 天内', '更早'] as const;
type GroupLabel = (typeof GROUP_ORDER)[number];
const DAY_MS = 86_400_000;

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

function groupOf(iso: string, now: Date): GroupLabel {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '更早';
  const days = Math.round((startOfDay(now) - startOfDay(t)) / DAY_MS);
  if (days <= 0) return '今天';
  if (days === 1) return '昨天';
  if (days <= 7) return '7 天内';
  if (days <= 30) return '30 天内';
  return '更早';
}

/** 组内条目的时间戳：近两天只显示时分，更早显示月-日，避免与分组标签重复。 */
function stampText(iso: string, now: Date): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  const hh = String(t.getHours()).padStart(2, '0');
  const mm = String(t.getMinutes()).padStart(2, '0');
  const g = groupOf(iso, now);
  if (g === '今天' || g === '昨天') return `${hh}:${mm}`;
  const mo = String(t.getMonth() + 1).padStart(2, '0');
  const dd = String(t.getDate()).padStart(2, '0');
  return `${mo}-${dd}`;
}

interface Props {
  /** 当前会话 ID，用于高亮。来自地址栏 ?session= */
  activeId: string | null;
  collapsed: boolean;
  onToggle: () => void;
  onSelect: (id: string) => void;
  onNew: () => void;
  /** 打开数据源配置弹窗（连接新库） */
  onOpenDatasource: () => void;
  /** 打开运行时设置弹窗（API Key / 模型 / 桩实现） */
  onOpenSettings: () => void;
  /** 告警订阅未读数，用于底部导航角标 */
  alertUnread: number;
}

/**
 * 常驻左侧会话栏。替代原先独立的「历史会话」页成为日常切换入口：
 * 新建对话、搜索、切换、重命名、删除都在这里完成；较重的批量管理
 * （导出 PPTX / Markdown、归档）仍走底部「历史管理」链接进入 HistoryPage。
 *
 * 列表数据自管（listSessions + 关键字搜索），切换与新建通过回调交给 Shell
 * 统一导航；删除/重命名直接打接口后重新拉取。
 */
export function SessionSidebar({
  activeId,
  collapsed,
  onToggle,
  onSelect,
  onNew,
  onOpenDatasource,
  onOpenSettings,
  alertUnread,
}: Props) {
  const navigate = useNavigate();
  const [items, setItems] = useState<SessionItem[]>([]);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(false);
  const [renaming, setRenaming] = useState<SessionItem | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  // 多选模式：进入后列表项点击改为切换勾选，用于批量删除
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listSessions(keyword || undefined, false);
      setItems(res.items.filter((s) => !s.archived));
    } finally {
      setLoading(false);
    }
  }, [keyword]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const now = useMemo(() => new Date(), [items]);
  // 分组顺序固定为 今天 / 昨天 / 7 天内 / 30 天内 / 更早，空组不渲染。
  // 列表本身按 updated_at 倒序返回，分组后组内仍保持倒序。
  const groups = useMemo(() => {
    const map = new Map<GroupLabel, SessionItem[]>();
    for (const it of items) {
      const g = groupOf(it.updated_at, now);
      const arr = map.get(g);
      if (arr) arr.push(it);
      else map.set(g, [it]);
    }
    return GROUP_ORDER.filter((g) => map.has(g)).map((g) => ({
      label: g,
      items: map.get(g) as SessionItem[],
    }));
  }, [items, now]);

  const openRename = (item: SessionItem) => {
    setRenaming(item);
    setRenameDraft(item.title);
  };

  const commitRename = async () => {
    if (!renaming) return;
    const title = renameDraft.trim();
    if (!title || title === renaming.title) {
      setRenaming(null);
      return;
    }
    try {
      await renameSession(renaming.id, title);
      setRenaming(null);
      await reload();
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const onDelete = async (item: SessionItem) => {
    try {
      await deleteSession(item.id);
      if (item.id === activeId) navigate('/');
      await reload();
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    }
  };

  const toggleSelectMode = () => {
    setSelectMode((v) => !v);
    setSelected(new Set()); // 进出多选都清空勾选，避免残留
  };

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allSelected = items.length > 0 && selected.size === items.length;
  const selectAll = () => {
    setSelected(allSelected ? new Set() : new Set(items.map((i) => i.id)));
  };

  /** 批量删除：后端无批量接口，逐个请求后统计成败；删掉的会话若正被查看则回首页。 */
  const batchDelete = async () => {
    const ids = Array.from(selected);
    if (!ids.length) return;
    setDeleting(true);
    try {
      const results = await Promise.allSettled(ids.map((id) => deleteSession(id)));
      const ok = results.filter((r) => r.status === 'fulfilled').length;
      if (ok === ids.length) void message.success(`已删除 ${ok} 个会话`);
      else void message.warning(`已删除 ${ok}/${ids.length} 个会话，部分删除失败`);
      if (activeId && ids.includes(activeId)) navigate('/');
      setSelectMode(false);
      setSelected(new Set());
      await reload();
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  };

  if (collapsed) {
    return (
      <div
        style={{
          width: 56,
          background: 'var(--wb-sidebar)',
          borderRight: '1px solid var(--wb-border)',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          padding: '12px 0',
        }}
      >
        <Tooltip title="展开">
          <Button type="text" icon={<MenuUnfoldOutlined />} onClick={onToggle} />
        </Tooltip>
        <Tooltip title="新建对话">
          <Button type="primary" shape="circle" icon={<PlusOutlined />} style={{ marginTop: 12 }} onClick={onNew} />
        </Tooltip>
        <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'center' }}>
          <Tooltip title="数据源" placement="right">
            <Button type="text" icon={<DatabaseOutlined />} onClick={onOpenDatasource} />
          </Tooltip>
          <Tooltip title="设置" placement="right">
            <Button type="text" icon={<SettingOutlined />} onClick={onOpenSettings} />
          </Tooltip>
          <Tooltip title="告警订阅" placement="right">
            <Badge count={alertUnread} size="small" offset={[-2, 2]}>
              <Button type="text" icon={<BellOutlined />} onClick={() => navigate('/alerts')} />
            </Badge>
          </Tooltip>
          <Tooltip title="历史管理" placement="right">
            <Button type="text" icon={<HistoryOutlined />} onClick={() => navigate('/history')} />
          </Tooltip>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        width: 264,
        background: 'var(--wb-sidebar)',
        borderRight: '1px solid var(--wb-border)',
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
      }}
    >
      <div style={{ padding: '12px 12px 8px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
          }}
        >
          <Text strong style={{ fontSize: 15 }}>会话</Text>
          <Space size={0}>
            <Tooltip title={selectMode ? '退出多选' : '多选'}>
              <Button
                type={selectMode ? 'primary' : 'text'}
                size="small"
                icon={<CheckSquareOutlined />}
                onClick={toggleSelectMode}
              />
            </Tooltip>
            <Tooltip title="收起侧边栏">
              <Button type="text" size="small" icon={<MenuFoldOutlined />} onClick={onToggle} />
            </Tooltip>
          </Space>
        </div>
      </div>

      <div style={{ padding: '0 10px 10px', borderBottom: '1px solid var(--wb-border)' }}>
        <div className="wb-nav-item" onClick={onOpenDatasource}>
          <DatabaseOutlined style={{ fontSize: 15 }} />
          <span>数据源</span>
        </div>
        <div className="wb-nav-item" onClick={onOpenSettings}>
          <SettingOutlined style={{ fontSize: 15 }} />
          <span>设置</span>
        </div>
        <div className="wb-nav-item" onClick={() => navigate('/alerts')}>
          <BellOutlined style={{ fontSize: 15 }} />
          <span style={{ flex: 1 }}>告警订阅</span>
          {alertUnread > 0 && <Badge count={alertUnread} size="small" />}
        </div>
        <div className="wb-nav-item" onClick={() => navigate('/history')}>
          <HistoryOutlined style={{ fontSize: 15 }} />
          <span>历史管理</span>
        </div>
      </div>

      <div style={{ padding: '10px 12px 8px' }}>
        <Input
          allowClear
          size="small"
          prefix={<SearchOutlined />}
          placeholder="搜索会话"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
      </div>

      <div style={{ padding: '0 12px 10px' }}>
        <Button type="primary" block icon={<PlusOutlined />} onClick={onNew}>
          新建对话
        </Button>
      </div>

      {selectMode && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '0 12px 8px',
          }}
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            已选 {selected.size} 项
          </Text>
          <Space size={8}>
            <Button type="link" size="small" style={{ padding: 0 }} onClick={selectAll}>
              {allSelected ? '取消全选' : '全选'}
            </Button>
            <Popconfirm
              title={`删除选中的 ${selected.size} 个会话？删除后不可恢复。`}
              onConfirm={() => void batchDelete()}
              okText="删除"
              cancelText="取消"
              disabled={selected.size === 0}
            >
              <Button
                type="link"
                size="small"
                danger
                disabled={selected.size === 0}
                loading={deleting}
                style={{ padding: 0 }}
              >
                删除
              </Button>
            </Popconfirm>
          </Space>
        </div>
      )}

      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 8px 8px', minHeight: 0 }}>
        {groups.length === 0 && !loading && (
          <Text type="secondary" style={{ display: 'block', padding: '16px 4px', fontSize: 12 }}>
            还没有会话，点上方「新建对话」开始
          </Text>
        )}
        {groups.map((group) => (
          <div key={group.label}>
            <Text
              type="secondary"
              style={{
                display: 'block',
                fontSize: 11,
                fontWeight: 600,
                letterSpacing: 0.4,
                padding: '8px 10px 2px',
              }}
            >
              {group.label}
            </Text>
            {group.items.map((item) => {
              const active = item.id === activeId;
              const checked = selected.has(item.id);
              return (
                <div
                  key={item.id}
                  onClick={() => (selectMode ? toggleSelect(item.id) : onSelect(item.id))}
                  className={`wb-side-item${active && !selectMode ? ' wb-side-item--active' : ''}`}
                  style={selectMode && checked ? { background: 'var(--wb-primary-weak)' } : undefined}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    {selectMode && (
                      // 勾选框只作展示，点击交给整行处理（pointerEvents:none 避免双重切换）
                      <span style={{ pointerEvents: 'none', display: 'inline-flex' }}>
                        <Checkbox checked={checked} onChange={() => undefined} />
                      </span>
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 4 }}>
                        <Text ellipsis style={{ fontSize: 13, flex: 1, color: active ? 'var(--wb-primary)' : undefined }}>
                          {item.title}
                        </Text>
                        {!selectMode && (
                          <Space size={0} onClick={(e) => e.stopPropagation()}>
                            <Tooltip title="重命名">
                              <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openRename(item)} />
                            </Tooltip>
                            <Popconfirm title="删除后不可恢复，确认删除？" onConfirm={() => void onDelete(item)}>
                              <Button type="text" size="small" danger icon={<DeleteOutlined />} />
                            </Popconfirm>
                          </Space>
                        )}
                      </div>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {item.turn_count} 轮 · {stampText(item.updated_at, now)}
                      </Text>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>

      <Modal
        title="重命名会话"
        open={Boolean(renaming)}
        onOk={() => void commitRename()}
        onCancel={() => setRenaming(null)}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Input
          value={renameDraft}
          maxLength={120}
          onChange={(e) => setRenameDraft(e.target.value)}
          onPressEnter={() => void commitRename()}
          autoFocus
        />
      </Modal>
    </div>
  );
}
