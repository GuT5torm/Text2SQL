import * as echarts from 'echarts';

/**
 * 把后端生成的 chart.option 渲成 PNG 的 dataURL（FR-N-2）。
 *
 * 场景是 PPTX 导出：历史会话页没有处于挂载状态的 echarts 实例，拿不到
 * `getDataURL()`。做法是在离屏 DOM 上临时 init 一次、渲染、取图、立刻销毁——
 * 与界面上那张图同源（同一份 option），不重新算数据，因此不会出现
 * 「导出的图与屏幕上的图不是一个数」的情况。
 *
 * 离屏容器必须给出显式尺寸：隐藏元素（display:none）拿不到布局尺寸，
 * echarts 会以 0 宽高初始化，取到的图是空白。这里用绝对定位移出视口，
 * 而不是 display:none。
 */
export async function renderChartImage(
  option: Record<string, unknown> | null | undefined,
  width = 960,
  height = 540,
): Promise<string | null> {
  if (!option || typeof document === 'undefined') return null;

  const host = document.createElement('div');
  host.style.cssText = [
    'position:absolute',
    'left:-10000px',
    'top:0',
    `width:${width}px`,
    `height:${height}px`,
    'pointer-events:none',
  ].join(';');
  document.body.appendChild(host);

  let instance: echarts.ECharts | null = null;
  try {
    instance = echarts.init(host, undefined, { width, height });
    // 关掉动画：动画未播完时 getDataURL 会截到中间态，图例与柱体可能缺失
    instance.setOption({ ...option, animation: false }, true);
    // 让浏览器完成一次布局与绘制，避免首帧空白
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    return instance.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#ffffff' });
  } catch {
    // 导出不能因为一张图失败而中断整份 PPTX，这里吞掉异常，该页就是没有图
    return null;
  } finally {
    instance?.dispose();
    host.remove();
  }
}
