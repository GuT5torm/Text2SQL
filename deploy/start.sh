#!/usr/bin/env bash
# Text2SQL 电商数据分析 Agent —— 后端进程启停脚本
#
# 适用：Linux / macOS / Git Bash。
# Windows PowerShell 下直接跑 uvicorn 即可，等价命令见 README 第三步。
#
# 用法：
# USAGE_BEGIN
#   ./deploy/start.sh start     启动服务
#   ./deploy/start.sh stop      停止服务
#   ./deploy/start.sh restart   重启（修改 .env 后使用）
#   ./deploy/start.sh status    查看进程状态
# USAGE_END
#
# 为什么默认只起 1 个 worker：
#   会话记忆默认用 MemorySaver，图状态保存在进程内存中。起多个 worker 时，
#   同一个 session 的请求可能被分到不同进程，历史上下文会读不全。
#   需要先配 CHECKPOINTER=sqlite 把状态落到 data/app.db，再调大 WORKERS。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
LOG_DIR="$ROOT_DIR/deploy/logs"
LOG_FILE="$LOG_DIR/backend.log"
PID_FILE="$LOG_DIR/backend.pid"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
WORKERS="${WORKERS:-1}"

# Windows 的虚拟环境脚本目录与 Unix 不同，自动探测
if [[ -x "$BACKEND_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON="$BACKEND_DIR/.venv/Scripts/python.exe"
elif [[ -x "$BACKEND_DIR/.venv/bin/python" ]]; then
    PYTHON="$BACKEND_DIR/.venv/bin/python"
else
    PYTHON="python3"
    echo "未找到虚拟环境解释器，使用系统 python3。建议先建 backend/.venv" >&2
fi

usage() {
    # 按标记区间抽取而不是写死行号：头部注释增删一行帮助信息就错位
    awk '/^# USAGE_BEGIN$/{flag=1;next} /^# USAGE_END$/{flag=0} flag {sub(/^# ?/, ""); print}' \
        "${BASH_SOURCE[0]}"
}

is_running() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    [[ -n "$pid" ]] || return 1
    # 只看 kill -0 不够：进程被强杀后 PID 文件残留，且 PID 会被系统复用给别的
    # 进程，会误报「已在运行」。再比对一次命令行，确认这个 PID 确实是本服务。
    if [[ -r "/proc/$pid/cmdline" ]]; then
        tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "app.main:app" || return 1
    fi
    kill -0 "$pid" 2>/dev/null
}

port_in_use() {
    # 启动前预检端口占用。不做预检的话，端口被别的进程占着时启动会失败，
    # 而失败信息藏在日志里，不如在这里直接说清楚
    command -v ss >/dev/null 2>&1 || return 1
    ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]" || return 1
    return 0
}

ensure_ready() {
    if ! is_running; then
        echo "服务未运行"
        exit 1
    fi
}

do_start() {
    if is_running; then
        echo "服务已在运行，PID $(cat "$PID_FILE")"
        return 0
    fi
    if [[ ! -f "$BACKEND_DIR/data/ecommerce.db" ]]; then
        echo "未找到样例数据库，先造数：$PYTHON data/seed.py" >&2
        exit 1
    fi
    if port_in_use; then
        echo "端口 $PORT 已被占用。先停掉占用进程，或用 PORT=8001 $0 start 换个端口" >&2
        exit 1
    fi

    mkdir -p "$LOG_DIR"
    cd "$BACKEND_DIR"
    # 环境变量 PYTHONUTF8 保证 Windows 上日志中的中文不乱码
    PYTHONUTF8=1 PYTHONUNBUFFERED=1 nohup "$PYTHON" -m uvicorn app.main:app \
        --host "$HOST" --port "$PORT" --workers "$WORKERS" \
        >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"

    # 轮询存活探针，最多等 15 秒。启动失败时把日志尾部打出来而不是让用户自己猜
    for _ in $(seq 1 15); do
        sleep 1
        if is_running; then
            local resp
            resp="$(curl -s "http://${HOST}:${PORT}/api/health" 2>/dev/null || true)"
            if [[ -n "$resp" ]]; then
                echo "服务已就绪，PID $(cat "$PID_FILE")"
                echo "$resp"
                return 0
            fi
        else
            echo "进程启动后即退出，日志尾部：" >&2
            tail -n 20 "$LOG_FILE" >&2 || true
            rm -f "$PID_FILE"
            exit 1
        fi
    done

    echo "已启动但健康检查 15 秒内未响应，请查看日志 $LOG_FILE"
}

do_stop() {
    ensure_ready
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
        is_running || break
        sleep 1
    done
    if is_running; then
        echo "进程未响应 SIGTERM，改用 SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "服务已停止"
}

do_status() {
    if is_running; then
        echo "运行中，PID $(cat "$PID_FILE")，探针 $HOST:$PORT/api/health"
        curl -s "http://${HOST}:${PORT}/api/health" || true
    else
        echo "未运行"
    fi
}

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) is_running && do_stop || true; do_start ;;
    status)  do_status ;;
    *)       usage; exit 1 ;;
esac
