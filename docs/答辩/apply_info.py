"""把「答辩信息.md」中的个人信息渲染进页面模板。

用法（在 docs/答辩 目录下执行）：

    python apply_info.py

流程：读 答辩信息.md 的字段 → 替换 templates/*.slide 中的 {{字段名}} →
写入 slides/ 目录。slidep 监听着 slides/，文件落盘即重新编译进 pptx。

脚本是幂等的：模板始终保留 {{占位符}}，可以反复运行。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
INFO_FILE = BASE / "答辩信息.md"
TEMPLATE_DIR = BASE / "templates"
OUTPUT_DIR = BASE / "slides"
PPTX_NAME = "答辩PPT.pptx"

# 出现在页面上但不由「答辩信息.md」维护的固定信息。
FIXED = {
    "项目名称": "基于 Text2SQL 的电商数据分析 Agent",
    "副标题": "口径集中配置 · 执行前校验 · 结果可追溯",
}


def load_fields() -> dict[str, str]:
    """从 答辩信息.md 取出「字段」与「可选字段」两节里的键值对。"""
    text = INFO_FILE.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        # 跳过说明性文字（例如「用法（在 docs/答辩 目录下执行）」）
        if not key or len(key) > 12 or " " in key:
            continue
        fields[key] = value
    fields.update(FIXED)
    return fields


def _try_sync(exe: str, filename: str) -> bool:
    """请求渲染服务编译一次，用文件是否真的落盘来判断成败。

    不能只看命令行输出：渲染服务在输出里报成功、但保存被拒的情况下依然
    返回 success，此时磁盘上的还是上一版文件。
    """
    target = BASE / filename
    before = target.stat().st_mtime if target.exists() else None
    try:
        proc = subprocess.run(
            [exe, "--project", str(BASE), "--filename", filename],
            cwd=str(BASE), capture_output=True, text=True, timeout=180,
        )
    except Exception:  # noqa: BLE001 - 同步失败不应中断信息渲染
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    if "occupied" in out:
        return False
    after = target.stat().st_mtime if target.exists() else None
    if after is None:
        return False
    # 新文件写到 FOOTER.htm；已有文件必须比同步前更新，才算真的写进去了。
    return before is None or after > before


def sync_pptx() -> None:
    """把 slides/ 的改动写进 pptx。

    主文件正被 PowerPoint 打开时无法覆写，此时另存带时间戳的副本，避免
    改动丢失或误报成功。渲染服务不可用（例如在没装渲染环境的机器上执行）
    时不必中断，只需要提示手工处理。
    """
    exe = shutil.which("slidep-start")
    if not exe:
        print("未找到渲染服务命令，请手工重新生成 pptx，或直接编辑 pptx 中的对应文字。")
        return
    if _try_sync(exe, PPTX_NAME):
        print(f"{PPTX_NAME} 已同步。")
        return

    # 走到这里基本只有一种原因：文稿正在 PowerPoint 里开着，文件被独占。
    alt = f"答辩PPT-{time.strftime('%m%d-%H%M')}.pptx"
    if _try_sync(exe, alt):
        print(f"{PPTX_NAME} 正被 PowerPoint 占用，无法覆写。")
        print(f"已另存为 {alt}，可直接用这份；")
        print(f"关闭 PowerPoint 后重跑本命令，再同步回 {PPTX_NAME}。")
    else:
        print(f"同步失败，请关闭 PowerPoint 后重跑本命令。若仍无效，检查 {PPTX_NAME} 是否被其他程序锁定。")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not INFO_FILE.exists():
        print(f"找不到 {INFO_FILE}")
        return 1

    fields = load_fields()
    templates = sorted(TEMPLATE_DIR.glob("*.slide"))
    if not templates:
        print(f"{TEMPLATE_DIR} 下没有页面模板")
        return 1

    OUTPUT_DIR.mkdir(exist_ok=True)

    # 只匹配已知字段名。不能写成 \{\{([^{}]+)\}\}：页面源码里的
    # style={{ ... }} 会被同一模式吃掉，整段样式被替换成空串。
    # 字段名按长度倒序排列，避免短名先匹配掉长名的前缀。
    names = sorted(fields, key=len, reverse=True)
    token_re = re.compile(r"\{\{(" + "|".join(re.escape(n) for n in names) + r")\}\}")

    for tpl in templates:
        raw = tpl.read_text(encoding="utf-8")
        rendered = token_re.sub(lambda m: fields[m.group(1)], raw)
        (OUTPUT_DIR / tpl.name).write_text(rendered, encoding="utf-8")

    print(f"已渲染 {len(templates)} 页 → {OUTPUT_DIR}")
    for key in ("姓名", "学号", "专业", "指导老师", "答辩日期"):
        print(f"  {key}：{fields.get(key) or '（留空）'}")

    sync_pptx()

    # 扫一遍残留的可疑占位符，避免模板里写错字段名却静默渲染成空白。
    leftover: set[str] = set()
    for out in sorted(OUTPUT_DIR.glob("*.slide")):
        for match in re.finditer(r"\{\{([^{}\n]+)\}\}", out.read_text(encoding="utf-8")):
            inner = match.group(1).strip()
            if inner and ":" not in inner and "'" not in inner:
                leftover.add(inner)
    if leftover:
        print("以下占位符没有对应的字段，模板中可能写错了字段名：")
        for name in sorted(leftover):
            print(f"  {{{{{name}}}}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
