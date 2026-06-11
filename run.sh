#!/usr/bin/env bash
set -euo pipefail

APP_NAME="agnes-pipeline"
APP_MODULE="web.app:app"
HOST="0.0.0.0"
PORT=8010
PID_FILE="/tmp/${APP_NAME}.pid"
LOG_FILE="/tmp/${APP_NAME}.log"

red()   { echo -e "\033[31m$*\033[0m"; }
green() { echo -e "\033[32m$*\033[0m"; }
yellow(){ echo -e "\033[33m$*\033[0m"; }

get_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    # fallback: 通过端口查找
    lsof -ti:"$PORT" 2>/dev/null || true
}

is_running() {
    local pid
    pid=$(get_pid)
    [ -n "$pid" ]
}

case "${1:-help}" in
    start)
        if is_running; then
            yellow "已在运行 (PID=$(get_pid), 端口=$PORT)"
            exit 0
        fi
        green "启动 $APP_NAME → $HOST:$PORT"
        nohup uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        # 等待启动
        for i in $(seq 1 10); do
            if curl -sf "http://localhost:$PORT/api/status" > /dev/null 2>&1; then
                green "启动成功 (PID=$(get_pid))"
                green "日志: tail -f $LOG_FILE"
                exit 0
            fi
            sleep 1
        done
        red "启动超时，请检查日志: $LOG_FILE"
        exit 1
        ;;

    stop)
        if ! is_running; then
            yellow "未在运行"
            exit 0
        fi
        pid=$(get_pid)
        yellow "停止 $APP_NAME (PID=$pid)..."
        kill "$pid" 2>/dev/null || true
        for i in $(seq 1 10); do
            if ! kill -0 "$pid" 2>/dev/null; then
                rm -f "$PID_FILE"
                green "已停止"
                exit 0
            fi
            sleep 1
        done
        red "优雅停止超时，强制终止"
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        green "已强制停止"
        ;;

    restart)
        $0 stop
        sleep 1
        $0 start
        ;;

    status)
        if is_running; then
            green "运行中 (PID=$(get_pid), 端口=$PORT)"
            curl -sf "http://localhost:$PORT/api/status" | python -m json.tool 2>/dev/null || true
        else
            yellow "未运行"
        fi
        ;;

    log)
        if [ -f "$LOG_FILE" ]; then
            tail -f "$LOG_FILE"
        else
            yellow "日志文件不存在: $LOG_FILE"
        fi
        ;;

    pipeline)
        shift
        case "${1:-status}" in
            start)
                curl -sf -X POST "http://localhost:$PORT/api/start" | python -m json.tool
                ;;
            stop)
                curl -sf -X POST "http://localhost:$PORT/api/stop" | python -m json.tool
                ;;
            status)
                curl -sf "http://localhost:$PORT/api/status" | python -m json.tool
                ;;
            items)
                curl -sf "http://localhost:$PORT/api/items" | python -m json.tool
                ;;
            logs)
                curl -sf "http://localhost:$PORT/api/logs" | python -m json.tool
                ;;
            theme)
                shift
                if [ -z "${1:-}" ]; then
                    yellow "用法: $0 pipeline theme <主题>"
                    exit 1
                fi
                curl -sf -X POST "http://localhost:$PORT/api/settings" \
                    -H "Content-Type: application/json" \
                    -d "{\"theme\":\"$*\"}" | python -m json.tool
                ;;
            *)
                echo "用法: $0 pipeline {start|stop|status|items|logs|theme <主题>}"
                exit 1
                ;;
        esac
        ;;

    help|*)
        echo "用法: $0 {start|stop|restart|status|log|pipeline}"
        echo ""
        echo "命令:"
        echo "  start    启动服务 (端口 $PORT)"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看服务状态"
        echo "  log      实时查看日志"
        echo "  pipeline 流水线控制:"
        echo "             pipeline start       启动流水线"
        echo "             pipeline stop        停止流水线"
        echo "             pipeline status      流水线状态"
        echo "             pipeline items       查看条目"
        echo "             pipeline logs        查看失败日志"
        echo "             pipeline theme <主题> 设置主题"
        ;;
esac
