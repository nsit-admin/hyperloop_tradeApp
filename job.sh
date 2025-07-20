#!/bin/bash

APP_NAME="hyperloop"
SCRIPT="/var/www/jobs/main.py"
LOG_DIR="/var/www/jobs"
LOG_FILE="$LOG_DIR/${APP_NAME}_$(date +'%Y-%m-%d').log"
PID_FILE="/var/www/jobs/${APP_NAME}.pid"

start_app() {
  if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "$APP_NAME is already running with PID $(cat $PID_FILE)"
    exit 1
  fi

  echo "Starting $APP_NAME..."
  cd /var/www/jobs
  nohup python3 "$SCRIPT" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  echo "$APP_NAME started with PID $!"
}

stop_app() {
  if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "Stopping $APP_NAME with PID $(cat $PID_FILE)..."
    kill $(cat "$PID_FILE")
    rm -f "$PID_FILE"
    echo "$APP_NAME stopped."
  else
    echo "$APP_NAME is not running."
  fi
}

case "$1" in
  start)
    start_app
    ;;
  stop)
    stop_app
    ;;
  restart)
    stop_app
    sleep 2
    start_app
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
