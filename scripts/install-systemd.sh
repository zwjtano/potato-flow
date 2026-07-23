#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "此安装脚本仅支持 Linux。" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/y2a-auto/.venv/bin/python"
SERVICE_NAME="potato-flow"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [[ ! -x "${PYTHON}" ]]; then
  echo "尚未安装 Python 环境，请先运行 scripts/install-linux.sh。" >&2
  exit 1
fi

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

ROOT_ESCAPED="$(escape_sed "${ROOT}")"
PYTHON_ESCAPED="$(escape_sed "${PYTHON}")"
USER_ESCAPED="$(escape_sed "$(id -un)")"
GROUP_ESCAPED="$(escape_sed "$(id -gn)")"

sed \
  -e "s|@ROOT@|${ROOT_ESCAPED}|g" \
  -e "s|@PYTHON@|${PYTHON_ESCAPED}|g" \
  -e "s|@USER@|${USER_ESCAPED}|g" \
  -e "s|@GROUP@|${GROUP_ESCAPED}|g" \
  "${ROOT}/deploy/potato-flow.service" > "${TMPDIR:-/tmp}/${SERVICE_NAME}.service"

run_root install -m 0644 "${TMPDIR:-/tmp}/${SERVICE_NAME}.service" "${SERVICE_PATH}"
run_root systemctl daemon-reload
run_root systemctl enable --now "${SERVICE_NAME}.service"

echo "已启动 ${SERVICE_NAME}.service"
echo "状态: sudo systemctl status ${SERVICE_NAME}"
echo "日志: journalctl -u ${SERVICE_NAME} -f"
