import { useEffect, useState } from 'react';
import {
  Alert,
  Form,
  Input,
  Modal,
  Switch,
  Typography,
  message,
} from 'antd';
import { getSettings, updateSettings, type SettingsView } from '@/api/settings';

const { Text } = Typography;

interface Props {
  open: boolean;
  onClose: () => void;
  /** 保存成功后回调，用于刷新顶栏模式标识 / 健康检查 */
  onSaved: (view: SettingsView) => void;
}

interface FormValues {
  llm_api_key?: string;
  llm_base_url: string;
  llm_model: string;
  llm_mock: boolean;
  llm_fallback_model?: string;
}

/**
 * 运行时设置弹窗：让普通用户配置 LLM 密钥等，保存即生效，无需手改 .env 或重启。
 * 密钥字段从不回填明文，只以「已配置 / 未配置」提示，避免在前端暴露敏感值。
 */
export function SettingsModal({ open, onClose, onSaved }: Props) {
  const [form] = Form.useForm<FormValues>();
  const [loading, setLoading] = useState(false);
  const [hasKey, setHasKey] = useState(false);

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    getSettings()
      .then((view) => {
        setHasKey(view.has_api_key);
        form.setFieldsValue({
          llm_base_url: view.llm_base_url,
          llm_model: view.llm_model,
          llm_mock: view.llm_mock,
          llm_fallback_model: view.llm_fallback_model,
        });
      })
      .catch(() => undefined);
  }, [open, form]);

  const handleOk = async () => {
    const values = await form.validateFields();
    const patch: Parameters<typeof updateSettings>[0] = {
      llm_base_url: values.llm_base_url,
      llm_model: values.llm_model,
      llm_mock: values.llm_mock,
      llm_fallback_model: values.llm_fallback_model ?? '',
    };
    // 仅在用户实际填入时才传密钥，避免误清空已配置的值
    if (values.llm_api_key && values.llm_api_key.trim()) {
      patch.llm_api_key = values.llm_api_key.trim();
    }
    setLoading(true);
    try {
      const view = await updateSettings(patch);
      message.success('已保存，新的配置已即时生效');
      onSaved(view);
      onClose();
    } catch {
      message.error('保存失败，请检查后端服务是否运行');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      title="运行时设置"
      open={open}
      onOk={handleOk}
      onCancel={onClose}
      confirmLoading={loading}
      okText="保存"
      cancelText="取消"
      destroyOnClose
      width={520}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="配置写入后端 .env 并即时生效，无需重启。单机本地运行，接口仅本机可达。"
      />
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          label="API Key"
          tooltip="敏感信息，不会回填明文。已配置时留空表示保持不变。"
          extra={hasKey ? '当前已配置密钥，留空即不改动。' : '尚未配置，将使用桩实现模式。'}
        >
          <Input.Password
            placeholder="sk-..."
            autoComplete="off"
            value={undefined}
            onChange={(e) => form.setFieldValue('llm_api_key', e.target.value)}
          />
        </Form.Item>
        <Form.Item
          label="Base URL"
          name="llm_base_url"
          rules={[{ required: true, message: '请填写模型服务地址' }]}
        >
          <Input placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </Form.Item>
        <Form.Item
          label="模型"
          name="llm_model"
          rules={[{ required: true, message: '请填写模型名' }]}
        >
          <Input placeholder="qwen-plus" />
        </Form.Item>
        <Form.Item label="降级模型（可选）" name="llm_fallback_model">
          <Input placeholder="主模型连续失败时降级使用，留空不启用" />
        </Form.Item>
        <Form.Item
          label="桩实现模式"
          name="llm_mock"
          valuePropName="checked"
          tooltip="开启后不调用真实模型，SQL 来自固定模板。关闭并填入密钥即走真实模型。"
        >
          <Switch checkedChildren="开" unCheckedChildren="关" />
        </Form.Item>
        <Text type="secondary" style={{ fontSize: 12 }}>
          关闭「桩实现模式」且已填密钥后，下一次提问即走真实模型生成 SQL 与结论。
        </Text>
      </Form>
    </Modal>
  );
}
