import { useState } from 'react';
import {
  Alert,
  Button,
  Form,
  Input,
  List,
  Modal,
  Select,
  Space,
  Typography,
  message,
} from 'antd';
import {
  connectDatasource,
  testDatasource,
  type DatasourceConnectResult,
  type DatasourceRequest,
  type DatasourceTestResult,
} from '@/api/datasource';

const { Text } = Typography;

interface Props {
  open: boolean;
  onClose: () => void;
  /** 应用切换成功后回调，用于刷新顶栏健康状态 */
  onConnected: (result: DatasourceConnectResult) => void;
}

const DIALECTS = [
  { value: 'sqlite', label: 'SQLite（库文件）' },
  { value: 'postgres', label: 'PostgreSQL（连接串）' },
  { value: 'mysql', label: 'MySQL（连接串）' },
];

/**
 * 连接数据库弹窗：让用户在不改 .env / YAML 的前提下接入一个新库。
 * 流程：填库（路径或连接串）→ 测试连接（只读内省，不改配置）→ 预览语义层草稿
 * （表为事实，指标/维度标「草稿·需人工确认口径」）→ 应用切换（写盘 + 热重载）。
 */
export function DataSourceModal({ open, onClose, onConnected }: Props) {
  const [dialect, setDialect] = useState('sqlite');
  const [path, setPath] = useState('');
  const [dsn, setDsn] = useState('');
  const [testing, setTesting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [intro, setIntro] = useState<DatasourceTestResult | null>(null);

  const reset = () => {
    setIntro(null);
    setPath('');
    setDsn('');
    setDialect('sqlite');
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const buildBody = (): DatasourceRequest => ({
    dialect,
    path: dialect === 'sqlite' ? path.trim() : undefined,
    dsn: dialect !== 'sqlite' ? dsn.trim() : undefined,
  });

  const handleTest = async () => {
    if (dialect !== 'sqlite' && !dsn.trim()) {
      message.warning('请填写连接串');
      return;
    }
    if (dialect === 'sqlite' && !path.trim()) {
      message.warning('请填写 SQLite 库文件绝对路径');
      return;
    }
    setTesting(true);
    setIntro(null);
    try {
      const res = await testDatasource(buildBody());
      setIntro(res);
      message.success(`连接成功，共 ${res.table_count} 张表`);
    } catch (e) {
      message.error((e as Error).message || '连接失败');
    } finally {
      setTesting(false);
    }
  };

  const handleConnect = async () => {
    if (!intro) {
      message.warning('请先测试连接');
      return;
    }
    setConnecting(true);
    try {
      const res = await connectDatasource(buildBody());
      message.success(
        `已切换数据源：${res.dialect}，表 ${res.table_count} 张、指标 ${res.metric_count} 个、维度 ${res.dimension_count} 个`,
      );
      onConnected(res);
      handleClose();
    } catch (e) {
      message.error((e as Error).message || '切换失败');
    } finally {
      setConnecting(false);
    }
  };

  return (
    <Modal
      title="连接数据库"
      open={open}
      onCancel={handleClose}
      footer={[
        <Button key="test" onClick={handleTest} loading={testing}>
          测试连接
        </Button>,
        <Button
          key="apply"
          type="primary"
          onClick={handleConnect}
          loading={connecting}
          disabled={!intro}
        >
          应用并切换
        </Button>,
      ]}
      destroyOnClose
      width={560}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="连接后系统会只读内省库结构并生成语义层草稿，下一次提问即指向新库，无需重启。"
      />
      <Form layout="vertical" requiredMark={false}>
        <Form.Item label="数据库类型" required>
          <Select
            value={dialect}
            onChange={(v) => {
              setDialect(v);
              setIntro(null);
            }}
            options={DIALECTS}
          />
        </Form.Item>
        {dialect === 'sqlite' ? (
          <Form.Item
            label="库文件路径"
            required
            extra="后端运行在本机，填写其可访问的绝对路径，例如 D:/data/other.db"
          >
            <Input
              placeholder="D:/data/your.db 或 /home/user/your.db"
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                setIntro(null);
              }}
            />
          </Form.Item>
        ) : (
          <Form.Item
            label="连接串（DSN）"
            required
            extra="含密码，仅写入后端 .env（已忽略版本库），不在界面回显。例如 postgresql://user:pass@host:5432/db"
          >
            <Input.Password
              placeholder={
                dialect === 'postgres'
                  ? 'postgresql://user:pass@host:5432/dbname'
                  : 'mysql+pymysql://user:pass@host:3306/dbname'
              }
              value={dsn}
              onChange={(e) => {
                setDsn(e.target.value);
                setIntro(null);
              }}
            />
          </Form.Item>
        )}
      </Form>

      {intro && (
        <div style={{ marginTop: 8 }}>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="语义层草稿：表结构为库内事实；指标与维度为启发式推测，口径需人工确认后再用于生产。"
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            将生成 {intro.table_count} 张表，请确认下列结构无误：
          </Text>
          <List
            size="small"
            style={{ maxHeight: 220, overflow: 'auto', marginTop: 6 }}
            bordered
            dataSource={intro.tables}
            renderItem={(t) => (
              <List.Item>
                <Space>
                  <Text strong>{t.name}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {t.fields} 个字段
                    {t.pk.length > 0 ? ` · 主键 ${t.pk.join(', ')}` : ''}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </div>
      )}
    </Modal>
  );
}
