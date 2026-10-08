#!/bin/bash
# CatPaw Anthropic Relay 管理脚本
# 用法: ~/catpaw-relay.sh {start|stop|restart|status|log}

RELAY="$HOME/catpaw-anthropic-relay.py"
LOG="$HOME/catpaw-anthropic-relay.log"
PORT=8789

is_running() {
  pgrep -f "catpaw-anthropic-relay.py" >/dev/null 2>&1
}

case "$1" in
  start)
    if is_running; then
      echo "relay 已在运行 (端口 $PORT)"
    else
      nohup python3 "$RELAY" --port "$PORT" > "$LOG" 2>&1 &
      sleep 1
      echo "relay 已启动 (端口 $PORT)，日志: $LOG"
    fi
    ;;
  stop)
    pkill -f "catpaw-anthropic-relay.py" && echo "relay 已停止" || echo "relay 未在运行"
    ;;
  restart)
    pkill -f "catpaw-anthropic-relay.py" 2>/dev/null
    sleep 1
    nohup python3 "$RELAY" --port "$PORT" > "$LOG" 2>&1 &
    sleep 1
    echo "relay 已重启 (端口 $PORT)"
    ;;
  status)
    if is_running; then
      echo "运行中:"
      pgrep -fl "catpaw-anthropic-relay.py"
      curl -s -m 3 "http://127.0.0.1:$PORT/health" && echo
    else
      echo "未运行"
    fi
    ;;
  log)
    tail -50 "$LOG"
    ;;
  *)
    echo "用法: $0 {start|stop|restart|status|log}"
    ;;
esac
