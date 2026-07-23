#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
APP_DIR="/app"
Y2A_DIR="${APP_DIR}/y2a-auto"

mkdir -p \
  "${DATA_DIR}/bridge" \
  "${DATA_DIR}/config" \
  "${DATA_DIR}/cookies" \
  "${DATA_DIR}/db" \
  "${DATA_DIR}/downloads" \
  "${DATA_DIR}/logs" \
  "${DATA_DIR}/recordings" \
  "${DATA_DIR}/security" \
  "${DATA_DIR}/static-covers" \
  "${DATA_DIR}/temp"

if [[ ! -f "${DATA_DIR}/bridge.config.json" ]]; then
  cp "${APP_DIR}/bridge.config.example.json" "${DATA_DIR}/bridge.config.json"
fi

link_persistent_path() {
  local source="$1"
  local target="$2"
  rm -rf "${target}"
  ln -s "${source}" "${target}"
}

link_persistent_path "${DATA_DIR}/bridge" "${APP_DIR}/.bridge"
link_persistent_path "${DATA_DIR}/bridge.config.json" "${APP_DIR}/bridge.config.json"
link_persistent_path "${DATA_DIR}/config" "${Y2A_DIR}/config"
link_persistent_path "${DATA_DIR}/cookies" "${Y2A_DIR}/cookies"
link_persistent_path "${DATA_DIR}/db" "${Y2A_DIR}/db"
link_persistent_path "${DATA_DIR}/downloads" "${Y2A_DIR}/downloads"
link_persistent_path "${DATA_DIR}/logs" "${Y2A_DIR}/logs"
link_persistent_path "${DATA_DIR}/recordings" "${Y2A_DIR}/recordings"
link_persistent_path "${DATA_DIR}/security" "${Y2A_DIR}/security"
link_persistent_path "${DATA_DIR}/static-covers" "${Y2A_DIR}/static/covers"
link_persistent_path "${DATA_DIR}/temp" "${Y2A_DIR}/temp"

# PID 与心跳只对当前容器进程命名空间有效，不能跨重建保留。
rm -f \
  "${DATA_DIR}/temp/biliup-recorder.pid" \
  "${DATA_DIR}/temp/biliup-recorder-status.json"
export POTATO_FLOW_CONTAINER_START=1

# 配置、数据库和日志可能从原生安装迁移而来，需一次性修正其文件
# 所有权；录播和下载目录通常很大，只修正目录本身，已有媒体只需可读。
for writable_dir in bridge config cookies db logs security static-covers temp; do
  chown -R biliup-y2a:biliup-y2a "${DATA_DIR}/${writable_dir}"
done
chown biliup-y2a:biliup-y2a \
  "${DATA_DIR}" \
  "${DATA_DIR}/bridge.config.json" \
  "${DATA_DIR}/downloads" \
  "${DATA_DIR}/recordings"

exec gosu biliup-y2a "$@"
