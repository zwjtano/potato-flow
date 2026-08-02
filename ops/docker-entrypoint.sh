#!/usr/bin/env bash
set -euo pipefail

# App-created directories default to 0750 and files to 0640. Credentials are
# tightened separately to 0700/0600.
umask 0027

DATA_DIR="${DATA_DIR:-/data}"
APP_DIR="/app"
APP_CODE_DIR="${APP_DIR}/potatoflow-app"
APP_USER="potatoflow"
APP_GROUP="potatoflow"
LAYOUT_MARKER="${DATA_DIR}/.potato-flow-layout-v2"

migrate_directory() {
  local legacy="$1"
  local canonical="$2"
  if [[ ! -e "${legacy}" || -L "${legacy}" ]]; then
    return
  fi
  if [[ -e "${canonical}" ]]; then
    echo "[entrypoint] 保留未自动合并的旧目录: ${legacy}" >&2
    return
  fi
  mkdir -p "$(dirname "${canonical}")"
  mv "${legacy}" "${canonical}"
  echo "[entrypoint] 已迁移目录: ${legacy} -> ${canonical}"
}

migrate_file() {
  local legacy="$1"
  local canonical="$2"
  if [[ ! -e "${legacy}" || -L "${legacy}" ]]; then
    return
  fi
  if [[ -e "${canonical}" ]]; then
    echo "[entrypoint] 保留未自动合并的旧文件: ${legacy}" >&2
    return
  fi
  mkdir -p "$(dirname "${canonical}")"
  mv "${legacy}" "${canonical}"
  echo "[entrypoint] 已迁移文件: ${legacy} -> ${canonical}"
}

# One-way, non-overwriting migration from implementation-specific names to the
# PotatoFlow data layout. Existing canonical paths always win.
migrate_directory "${DATA_DIR}/.bridge" "${DATA_DIR}/state/pipeline"
migrate_directory "${DATA_DIR}/bridge" "${DATA_DIR}/state/recording"
migrate_directory "${DATA_DIR}/cookies" "${DATA_DIR}/credentials/cookies"
migrate_directory "${DATA_DIR}/security" "${DATA_DIR}/credentials/security"
migrate_directory "${DATA_DIR}/db" "${DATA_DIR}/database"
migrate_directory "${DATA_DIR}/static-covers" "${DATA_DIR}/covers"
migrate_directory "${DATA_DIR}/temp" "${DATA_DIR}/runtime"
migrate_file "${DATA_DIR}/bridge.config.json" "${DATA_DIR}/config/pipeline.json"

install -d -m 0750 -o "${APP_USER}" -g "${APP_GROUP}" \
  "${DATA_DIR}" \
  "${DATA_DIR}/config" \
  "${DATA_DIR}/database" \
  "${DATA_DIR}/downloads" \
  "${DATA_DIR}/logs" \
  "${DATA_DIR}/recordings" \
  "${DATA_DIR}/covers" \
  "${DATA_DIR}/runtime" \
  "${DATA_DIR}/state" \
  "${DATA_DIR}/state/pipeline" \
  "${DATA_DIR}/state/recording"
install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" \
  "${DATA_DIR}/credentials" \
  "${DATA_DIR}/credentials/cookies" \
  "${DATA_DIR}/credentials/security"

PIPELINE_CONFIG="${DATA_DIR}/config/pipeline.json"
if [[ ! -f "${PIPELINE_CONFIG}" ]]; then
  install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" \
    "${APP_CODE_DIR}/bridge.config.example.json" "${PIPELINE_CONFIG}"
fi

# Resolve the historical relative database path against the new config
# location. Custom absolute paths are preserved.
gosu "${APP_USER}" python3 - "${PIPELINE_CONFIG}" "${DATA_DIR}" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(0)
if str(payload.get("state_db") or "") in {
    "", ".bridge/state.sqlite3", "state/pipeline/state.sqlite3"
}:
    payload["state_db"] = str(data_dir / "state" / "pipeline" / "state.sqlite3")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
PY

link_persistent_path() {
  local source="$1"
  local target="$2"
  if [[ -L "${target}" ]]; then
    [[ "$(readlink "${target}")" == "${source}" ]] && return
    unlink "${target}"
  elif [[ -e "${target}" ]]; then
    # Preserve packaged defaults when replacing known /app paths. Refuse to
    # remove anything outside the immutable image tree.
    if [[ -d "${target}" && -z "$(find "${target}" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      rmdir "${target}"
    elif [[ "${target}" == "${APP_DIR}/"* ]]; then
      if [[ -d "${target}" && -d "${source}" ]]; then
        cp -a -n "${target}/." "${source}/"
      fi
      rm -rf -- "${target}"
    else
      echo "[entrypoint] 无法建立持久化链接，目标非空: ${target}" >&2
      exit 1
    fi
  fi
  ln -s "${source}" "${target}"
}

link_persistent_path "${DATA_DIR}/state/recording" "${APP_DIR}/.bridge"
link_persistent_path "${PIPELINE_CONFIG}" "${APP_CODE_DIR}/bridge.config.json"
link_persistent_path "${DATA_DIR}/config" "${APP_CODE_DIR}/config"
link_persistent_path "${DATA_DIR}/credentials/cookies" "${APP_CODE_DIR}/cookies"
link_persistent_path "${DATA_DIR}/database" "${APP_CODE_DIR}/db"
link_persistent_path "${DATA_DIR}/downloads" "${APP_CODE_DIR}/downloads"
link_persistent_path "${DATA_DIR}/logs" "${APP_CODE_DIR}/logs"
link_persistent_path "${DATA_DIR}/recordings" "${APP_DIR}/recordings"
# 兼容旧工具仍从 potatoflow-app/recordings 读取文件。
link_persistent_path "${APP_DIR}/recordings" "${APP_CODE_DIR}/recordings"
link_persistent_path "${DATA_DIR}/credentials/security" "${APP_CODE_DIR}/security"
link_persistent_path "${DATA_DIR}/covers" "${APP_CODE_DIR}/static/covers"
link_persistent_path "${DATA_DIR}/runtime" "${APP_CODE_DIR}/temp"

# PID 与心跳只对当前容器进程命名空间有效，不能跨重建保留。
rm -f \
  "${DATA_DIR}/runtime/biliup-recorder.pid" \
  "${DATA_DIR}/runtime/biliup-recorder-status.json"
export POTATO_FLOW_CONTAINER_START=1
export BRIDGE_CONFIG="${BRIDGE_CONFIG:-${PIPELINE_CONFIG}}"

# Full recursion is expensive for large recording trees, so run it once per
# layout version. The inherited umask keeps all later files compliant.
if [[ ! -f "${LAYOUT_MARKER}" ]]; then
  chown -R "${APP_USER}:${APP_GROUP}" \
    "${DATA_DIR}/config" "${DATA_DIR}/database" "${DATA_DIR}/downloads" \
    "${DATA_DIR}/logs" "${DATA_DIR}/recordings" "${DATA_DIR}/covers" \
    "${DATA_DIR}/runtime" "${DATA_DIR}/state" "${DATA_DIR}/credentials"
  find "${DATA_DIR}/config" "${DATA_DIR}/database" "${DATA_DIR}/downloads" \
    "${DATA_DIR}/logs" "${DATA_DIR}/recordings" "${DATA_DIR}/covers" \
    "${DATA_DIR}/runtime" "${DATA_DIR}/state" -type d -exec chmod 0750 {} +
  find "${DATA_DIR}/config" "${DATA_DIR}/database" "${DATA_DIR}/downloads" \
    "${DATA_DIR}/logs" "${DATA_DIR}/recordings" "${DATA_DIR}/covers" \
    "${DATA_DIR}/runtime" "${DATA_DIR}/state" -type f -exec chmod 0640 {} +
  find "${DATA_DIR}/credentials" -type d -exec chmod 0700 {} +
  find "${DATA_DIR}/credentials" -type f -exec chmod 0600 {} +
  chmod 0600 "${PIPELINE_CONFIG}"
  install -m 0640 -o "${APP_USER}" -g "${APP_GROUP}" /dev/null "${LAYOUT_MARKER}"
fi

# 启动斗鱼弹幕统计 daemon（后台常驻，可独立关闭便于隔离测试）
if [[ "${DOUYU_STATS_ENABLED:-1}" == "1" && -f "${APP_CODE_DIR}/modules/douyu_stats_daemon.py" ]]; then
  gosu "${APP_USER}" python3 "${APP_CODE_DIR}/modules/douyu_stats_daemon.py" &
  echo "[entrypoint] douyu_stats_daemon started (pid $!)"
fi

exec gosu "${APP_USER}" "$@"
