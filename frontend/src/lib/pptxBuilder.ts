/**
 * 整轮会话的 PPTX 生成（FR-N-2）。
 *
 * 一页一回合。每页含：
 *   - 问句（页标题）
 *   - 结论文本（要点列表）
 *   - 数据时效 / 口径（页脚）
 *   - 来源表 / 异常标记（如有）
 *   - 图表（如有 chart.option，echarts 实例由调用方在 DOM 渲染后 getDataURL）
 *
 * 数据截至 / 生成时间（FR-N-4）放首页页脚，符合周会模板习惯。
 *
 * 浏览器侧生成 PPTX 用 pptxgenjs。它生成的 .pptx 在 PowerPoint 与 WPS 都能打开，
 * 不依赖任何服务端能力。
 */

import pptxgen from 'pptxgenjs';
import type { ChatTurn } from '@/hooks/useChatStream';
import { localTimestamp } from './exporters';

const PAGE_W = 10; // inches，默认 4:3
const PAGE_H = 7.5;

/**
 * 把一个会话的全部轮次做成 PPTX。
 * @param turns 已落库的轮次列表（streaming 中的轮次会被跳过）
 * @param options.dataCutoff 数据截至时间字符串
 * @param options.getChartImage 由调用方提供：根据 turn 返回 base64 PNG（含 data: 前缀）
 *        或 null 表示本轮没图。前端不能直接在这里访问 echarts 实例，
 *        必须由调用方在 DOM 上有图表时取一次 getDataURL。
 */
export async function buildSessionPptx(
  turns: ChatTurn[],
  options: {
    sessionTitle: string;
    dataCutoff?: string | null;
    getChartImage?: (turn: ChatTurn) => Promise<string | null>;
  },
): Promise<Blob> {
  const pptx = new pptxgen();
  pptx.defineLayout({ name: 'T2SQL', width: PAGE_W, height: PAGE_H });
  pptx.layout = 'T2SQL';

  const now = localTimestamp();
  const dataCutoff = options.dataCutoff ?? '未知';
  const footerText = `数据截至：${dataCutoff}　生成时间：${now}`;

  // 首页：标题 + 数据截至 / 生成时间（FR-N-4）
  const titleSlide = pptx.addSlide();
  titleSlide.background = { color: 'F5F5F5' };
  titleSlide.addText(options.sessionTitle || '会话汇报', {
    x: 0.5,
    y: 2.5,
    w: PAGE_W - 1,
    h: 1.2,
    fontSize: 36,
    bold: true,
    color: '333333',
    align: 'center',
  });
  titleSlide.addText(footerText, {
    x: 0.5,
    y: PAGE_H - 1.2,
    w: PAGE_W - 1,
    h: 0.5,
    fontSize: 12,
    color: '666666',
    align: 'center',
  });

  for (let i = 0; i < turns.length; i += 1) {
    const turn = turns[i];
    const result = turn.result;
    if (!result) continue;

    const slide = pptx.addSlide();
    // 页标题：第 N 回合 + 问句
    slide.addText(`第 ${i + 1} 回合`, {
      x: 0.3,
      y: 0.2,
      w: PAGE_W - 0.6,
      h: 0.4,
      fontSize: 12,
      color: '999999',
    });
    slide.addText(turn.question, {
      x: 0.3,
      y: 0.55,
      w: PAGE_W - 0.6,
      h: 0.8,
      fontSize: 20,
      bold: true,
      color: '222222',
    });

    let cursorY = 1.5;

    if (result.answer) {
      slide.addText('结论', {
        x: 0.3,
        y: cursorY,
        w: PAGE_W - 0.6,
        h: 0.35,
        fontSize: 12,
        bold: true,
        color: '1677FF',
      });
      cursorY += 0.4;
      // 结论文本可能很长，超出的会被 pptxgenjs 自动截断，因此限制长度
      const answer = result.answer.length > 600 ? `${result.answer.slice(0, 600)}…` : result.answer;
      slide.addText(answer, {
        x: 0.3,
        y: cursorY,
        w: PAGE_W - 0.6,
        h: 1.2,
        fontSize: 14,
        color: '333333',
        valign: 'top',
      });
      cursorY += 1.3;
    }

    // 口径
    const metricUsed = result.metric_used ?? [];
    if (metricUsed.length > 0) {
      slide.addText(`口径：${metricUsed.join('、')}`, {
        x: 0.3,
        y: cursorY,
        w: PAGE_W - 0.6,
        h: 0.3,
        fontSize: 11,
        color: '666666',
      });
      cursorY += 0.35;
    }

    // 时间范围
    if (result.time_range?.resolved) {
      slide.addText(`时间范围：${result.time_range.resolved}`, {
        x: 0.3,
        y: cursorY,
        w: PAGE_W - 0.6,
        h: 0.3,
        fontSize: 11,
        color: '666666',
      });
      cursorY += 0.35;
    }

    // 异常标记
    const findings = result.findings ?? [];
    if (findings.length > 0) {
      const findingText = findings
        .map((f) => `[${f.severity}] ${f.dimension ?? ''} ${f.slice ?? ''}：${f.reason}`)
        .join('\n');
      slide.addText(`异常标记：\n${findingText}`, {
        x: 0.3,
        y: cursorY,
        w: PAGE_W - 0.6,
        h: 0.8,
        fontSize: 10,
        color: '874D00',
        valign: 'top',
      });
      cursorY += 0.85;
    }

    // 图表（图片）
    if (options.getChartImage) {
      const image = await options.getChartImage(turn);
      if (image) {
        const imgH = PAGE_H - cursorY - 0.8;
        // 保持 16:9，宽度自适应
        const maxW = PAGE_W - 0.6;
        const maxH = Math.max(1.5, imgH);
        let w = maxW;
        let h = (w * 9) / 16;
        if (h > maxH) {
          h = maxH;
          w = (h * 16) / 9;
        }
        const x = (PAGE_W - w) / 2;
        slide.addImage({ data: image, x, y: cursorY, w, h });
        cursorY += h + 0.1;
      }
    }

    // 页脚：数据截至 + 生成时间
    slide.addText(footerText, {
      x: 0.3,
      y: PAGE_H - 0.45,
      w: PAGE_W - 0.6,
      h: 0.3,
      fontSize: 9,
      color: '999999',
      align: 'center',
    });
  }

  // 写出 Blob。pptxgenjs 在浏览器侧用 write({ outputType: 'blob' }) 返回 Blob
  const result = (await pptx.write({ outputType: 'blob' })) as Blob;
  return result;
}
