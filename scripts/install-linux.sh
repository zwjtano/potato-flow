#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "此安装脚本仅支持 Linux。" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="${ROOT}/potatoflow-app"
LEGACY_APP_ROOT="${ROOT}/y2a-auto"

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

if command -v apt-get >/dev/null 2>&1; then
  run_root apt-get update
  run_root apt-get install -y \
    ca-certificates chromium curl ffmpeg build-essential pkg-config libssl-dev \
    python3 python3-venv python3-pip
elif command -v dnf >/dev/null 2>&1; then
  run_root dnf install -y \
    ca-certificates chromium curl ffmpeg gcc gcc-c++ make pkgconf-pkg-config openssl-devel \
    python3 python3-pip
else
  echo "未识别包管理器，请手动安装 Python 3、FFmpeg、编译工具、OpenSSL 和 CA 证书。" >&2
fi

if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  export PATH="${HOME}/.cargo/bin:${PATH}"
fi

for runtime_dir in config cookies db downloads logs recordings security temp; do
  legacy_path="${LEGACY_APP_ROOT}/${runtime_dir}"
  canonical_path="${APP_ROOT}/${runtime_dir}"
  if [[ -e "${legacy_path}" && ! -e "${canonical_path}" ]]; then
    mv "${legacy_path}" "${canonical_path}"
    echo "已迁移旧数据目录: ${legacy_path} -> ${canonical_path}"
  fi
done

python3 -m venv "${APP_ROOT}/.venv"
"${APP_ROOT}/.venv/bin/python" -m pip install --upgrade pip
"${APP_ROOT}/.venv/bin/pip" install -r "${APP_ROOT}/requirements.lock"

(
  cd "${ROOT}/recorder-core"
  cargo build --release -p biliup-cli
)

if [[ ! -f "${ROOT}/bridge.config.json" ]]; then
  cp "${ROOT}/bridge.config.example.json" "${ROOT}/bridge.config.json"
fi

mkdir -p \
  "${ROOT}/.bridge" \
  "${ROOT}/docker-data/recordings" \
  "${APP_ROOT}/config" \
  "${APP_ROOT}/logs" \
  "${APP_ROOT}/temp"

echo
echo "安装完成。"
echo "直接启动: ${APP_ROOT}/.venv/bin/python ${ROOT}/run.py"
echo "安装 systemd: ${ROOT}/scripts/install-systemd.sh"
