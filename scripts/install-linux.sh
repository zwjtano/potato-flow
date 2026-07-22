#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "此安装脚本仅支持 Linux。" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
    ca-certificates curl ffmpeg build-essential pkg-config libssl-dev \
    python3 python3-venv python3-pip
elif command -v dnf >/dev/null 2>&1; then
  run_root dnf install -y \
    ca-certificates curl ffmpeg gcc gcc-c++ make pkgconf-pkg-config openssl-devel \
    python3 python3-pip
else
  echo "未识别包管理器，请手动安装 Python 3、FFmpeg、编译工具、OpenSSL 和 CA 证书。" >&2
fi

if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  export PATH="${HOME}/.cargo/bin:${PATH}"
fi

python3 -m venv "${ROOT}/y2a-auto/.venv"
"${ROOT}/y2a-auto/.venv/bin/python" -m pip install --upgrade pip
"${ROOT}/y2a-auto/.venv/bin/pip" install -r "${ROOT}/y2a-auto/requirements.txt"

(
  cd "${ROOT}/upstream-biliup"
  cargo build --release -p biliup-cli
)

if [[ ! -f "${ROOT}/bridge.config.json" ]]; then
  cp "${ROOT}/bridge.config.example.json" "${ROOT}/bridge.config.json"
fi

mkdir -p \
  "${ROOT}/.bridge" \
  "${ROOT}/y2a-auto/config" \
  "${ROOT}/y2a-auto/logs" \
  "${ROOT}/y2a-auto/recordings" \
  "${ROOT}/y2a-auto/temp"

echo
echo "安装完成。"
echo "直接启动: ${ROOT}/y2a-auto/.venv/bin/python ${ROOT}/run.py"
echo "安装 systemd: ${ROOT}/scripts/install-systemd.sh"
