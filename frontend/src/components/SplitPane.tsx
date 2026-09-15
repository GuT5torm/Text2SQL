import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

interface Props {
  left: ReactNode;
  right: ReactNode;
  /** 左侧初始宽度与拖拽范围 */
  defaultWidth?: number;
  min?: number;
  max?: number;
}

/**
 * 左右分栏，中间可拖拽。用 pointer 事件而不是 mousedown + document 监听，
 * 是因为 setPointerCapture 能把后续移动事件都收拢到手柄上，不需要处理全局解绑。
 */
export function SplitPane({ left, right, defaultWidth = 420, min = 320, max = 560 }: Props) {
  const [width, setWidth] = useState(defaultWidth);
  const draggingRef = useRef(false);

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    draggingRef.current = true;
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      // 容器左边界到指针的距离即新的左栏宽度
      const containerLeft = event.currentTarget.parentElement?.getBoundingClientRect().left ?? 0;
      const next = Math.min(max, Math.max(min, event.clientX - containerLeft));
      setWidth(next);
    },
    [min, max],
  );

  const onPointerUp = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    draggingRef.current = false;
    event.currentTarget.releasePointerCapture(event.pointerId);
  }, []);

  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1023px)');
    const apply = () => setNarrow(query.matches);
    apply();
    query.addEventListener('change', apply);
    return () => query.removeEventListener('change', apply);
  }, []);

  if (narrow) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minHeight: 0, height: '100%' }}>
        <div style={{ minHeight: 320 }}>{left}</div>
        <div>{right}</div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', gap: 16, minHeight: 0, height: '100%' }}>
      <div style={{ width, flexShrink: 0, minHeight: 0 }}>{left}</div>
      <div
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        style={{
          width: 6,
          cursor: 'col-resize',
          background: '#f5f5f5',
          borderRadius: 3,
          flexShrink: 0,
        }}
      />
      <div style={{ flex: 1, minWidth: 640, minHeight: 0, overflowY: 'auto' }}>{right}</div>
    </div>
  );
}
