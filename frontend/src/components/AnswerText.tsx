import { Fragment, ReactNode } from 'react';
import { Typography } from 'antd';

const { Paragraph, Text } = Typography;

/**
 * 结论文本的轻量排版。后端返回的是带换行、列表与 ```sql 代码块的纯文本，
 * 引一整个 Markdown 库只为这两种语法不划算，这里做最小解析。
 * 注意：不使用 dangerouslySetInnerHTML，所有内容以文本节点渲染。
 */
export function AnswerText({ text }: { text: string }) {
  const lines = text.split('\n');
  const blocks: ReactNode[] = [];

  let paragraph: string[] = [];
  let list: string[] = [];
  let code: string[] = [];
  let inCode = false;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(
      <Paragraph key={`p-${blocks.length}`} style={{ margin: '0 0 8px', fontSize: 15, lineHeight: 1.75 }}>
        {paragraph.join('\n')}
      </Paragraph>,
    );
    paragraph = [];
  };
  const flushList = () => {
    if (!list.length) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} style={{ margin: '0 0 8px', paddingLeft: 20, lineHeight: 1.75 }}>
        {list.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>,
    );
    list = [];
  };
  const flushCode = () => {
    if (!code.length) return;
    blocks.push(
      <pre
        key={`code-${blocks.length}`}
        style={{
          margin: '0 0 8px',
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
        {code.join('\n')}
      </pre>,
    );
    code = [];
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (inCode) {
        flushCode();
        inCode = false;
      } else {
        flushParagraph();
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(line);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph();
      list.push(line.replace(/^\s*[-*]\s+/, ''));
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushCode();
  flushParagraph();
  flushList();

  return <Fragment>{blocks.length ? blocks : <Text type="secondary">（无文本内容）</Text>}</Fragment>;
}
