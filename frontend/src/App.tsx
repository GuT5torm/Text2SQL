import { useCallback, useEffect, useState } from 'react';
import { HashRouter, Navigate, Route, Routes, useNavigate, useSearchParams } from 'react-router-dom';
import { ConfigProvider, Layout, Popover, Space, Tag, Tooltip, Typography } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AlertCenter } from '@/pages/AlertCenter';
import { ChatPage } from '@/pages/ChatPage';
import { HistoryPage } from '@/pages/HistoryPage';
import { SharePage } from '@/pages/SharePage';
import { SessionSidebar } from '@/components/SessionSidebar';
import { SettingsModal } from '@/components/SettingsModal';
import { DataSourceModal } from '@/components/DataSourceModal';
import { fetchHealth } from '@/api/session';
import type { DatasourceConnectResult } from '@/api/datasource';
import type { HealthInfo } from '@/api/types';

const { Header, Content } = Layout;
const { Text } = Typography;

/**
 * 只读说明（FR-I-1）：一个常驻的徽标，点击展开详情。
 *
 * 「只读」是本系统的能力而非提示——之前只在被拦截时弹黄条，常驻说明解决
 * 「为什么只能问不能改」的疑问。徽标位于顶栏左侧，紧邻项目名，避免被业务
 * 信息淹没。
 */
function ReadOnlyBadge() {
  const items: { label: string; text: string }[] = [
    { label: '连接', text: '业务库以只读模式连接，所有写入操作都会被沙箱拒绝' },
    { label: '语句', text: '生成的查询限定为 SELECT，且必须带行数上限与时间边界' },
    { label: '字段', text: '字段访问以语义层登记的表与字段为白名单，越权访问被拦截' },
    { label: '结果', text: '返回结果只含聚合数据，不暴露个人身份字段' },
  ];
  return (
    <Popover
      placement="bottomLeft"
      trigger="click"
      content={
        <div style={{ maxWidth: 320, fontSize: 12, lineHeight: 1.7 }}>
          <Text strong style={{ fontSize: 13 }}>系统只读说明</Text>
          <ul style={{ margin: '8px 0 0', paddingLeft: 16 }}>
            {items.map((it) => (
              <li key={it.label}>
                <Text strong>{it.label}：</Text>
                <Text type="secondary">{it.text}</Text>
              </li>
            ))}
          </ul>
        </div>
      }
    >
      <Tag color="default" style={{ cursor: 'help', borderRadius: 4 }}>
        只读
      </Tag>
    </Popover>
  );
}

function Shell() {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [datasourceOpen, setDatasourceOpen] = useState(false);
  // 会话列表刷新信号：一轮问答落库后 bump，让侧边栏重拉（标题 / 轮次才定下来）
  const [sessionsVersion, setSessionsVersion] = useState(0);
  const bumpSessions = useCallback(() => setSessionsVersion((v) => v + 1), []);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const sessionId = params.get('session');

  useEffect(() => {
    let timer: number | undefined;
    const pull = () => {
      fetchHealth()
        .then(setHealth)
        .catch(() => setHealth(null));
    };
    pull();
    // FR-O-4：未读角标需要随事件 / 已读动作刷新，30s 轮询 /api/health 足够；
    // 告警中心页内的操作走自己的即时刷新，不依赖这个轮询。
    timer = window.setInterval(pull, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  // 新建对话：只导航到不带会话参数的路由，由 ChatPage 清空对话区；会话在
  // 发出第一句提问时才惰性创建，避免每次点击都在库里留下一条空会话。
  const handleNew = useCallback(() => {
    navigate('/');
  }, [navigate]);

  // 设置保存后刷新健康状态，让顶栏模式标识立即更新
  const handleSettingsSaved = useCallback(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => undefined);
  }, []);

  // 数据源切换后刷新健康状态，让顶栏表数 / 模式标识立即更新
  const handleDatasourceConnected = useCallback((_result: DatasourceConnectResult) => {
    fetchHealth()
      .then(setHealth)
      .catch(() => undefined);
  }, []);

  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--wb-bg)' }}>
      <SessionSidebar
        activeId={sessionId}
        collapsed={collapsed}
        onToggle={() => setCollapsed((v) => !v)}
        onSelect={(id) => navigate(`/?session=${encodeURIComponent(id)}`)}
        onNew={handleNew}
        onOpenDatasource={() => setDatasourceOpen(true)}
        onOpenSettings={() => setSettingsOpen(true)}
        alertUnread={health?.alert_unread ?? 0}
        refreshToken={sessionsVersion}
      />
      <Layout style={{ flex: 1, minWidth: 0, background: 'var(--wb-bg)' }}>
        <Header
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            background: 'var(--wb-surface)',
            borderBottom: '1px solid var(--wb-border)',
            boxShadow: '0 1px 3px rgba(0, 0, 0, 0.04)',
            padding: '0 20px',
            height: 56,
          }}
        >
          <Space size={12}>
            <div
              style={{
                width: 30,
                height: 30,
                borderRadius: 8,
                background: 'linear-gradient(135deg, #2f6bff, #6f9bff)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontWeight: 700,
                fontSize: 12,
                letterSpacing: 0.5,
              }}
            >
              T2
            </div>
            <Text strong style={{ fontSize: 16 }}>
              Text2SQL 电商数据分析 Agent
            </Text>
            <ReadOnlyBadge />
            {health && (
              <Tooltip
                title={
                  health.llm_mock
                    ? '当前为桩实现模式：未配置密钥，SQL 来自固定模板。在「设置」中填入密钥并关闭桩实现即可切换'
                    : `模型 ${health.model} · 指标 ${health.metrics} 个 · 表 ${health.tables} 张`
                }
              >
                <Tag color={health.llm_mock ? 'orange' : 'green'} style={{ borderRadius: 4 }}>
                  {health.llm_mock ? '桩实现模式' : health.model}
                </Tag>
              </Tooltip>
            )}
            {health?.alert_unhealthy && (
              <Tooltip
                title={`异常订阅连续求值失败 ${health.alert_consecutive_failures} 次（阈值 ${health.alert_failures_threshold}），请检查规则与链路`}
              >
                <Tag color="red" style={{ borderRadius: 4 }}>
                  告警求值异常
                </Tag>
              </Tooltip>
            )}
          </Space>
        </Header>
        <Content style={{ padding: 16, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          <Routes>
            <Route path="/" element={<ChatPage onTurnComplete={bumpSessions} />} />
            <Route path="/history" element={<HistoryPage />} />
            <Route path="/alerts" element={<AlertCenter />} />
            <Route path="/share/:token" element={<SharePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Content>
      </Layout>
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={handleSettingsSaved}
      />
      <DataSourceModal
        open={datasourceOpen}
        onClose={() => setDatasourceOpen(false)}
        onConnected={handleDatasourceConnected}
      />
    </div>
  );
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{ token: { colorPrimary: '#2f6bff', borderRadius: 8, fontSize: 14 } }}
    >
      <HashRouter>
        <Shell />
      </HashRouter>
    </ConfigProvider>
  );
}
