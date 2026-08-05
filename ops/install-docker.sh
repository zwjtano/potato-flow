#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Docker 一键安装脚本仅支持 Linux。" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR_MODE="${POTATOFLOW_CHINA_MIRROR:-auto}"
WEB_PORT="${POTATOFLOW_PORT:-5001}"

fetch_quiet() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --connect-timeout 3 --max-time 6 "$1" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- --timeout=6 "$1" 2>/dev/null
  else
    return 1
  fi
}

detect_china_mirror() {
  case "${MIRROR_MODE,,}" in
    1|true|yes|cn|china) return 0 ;;
    0|false|no|global|official) return 1 ;;
    auto) ;;
    *)
      echo "POTATOFLOW_CHINA_MIRROR 仅支持 auto、1 或 0。" >&2
      exit 2
      ;;
  esac

  local trace country
  trace="$(fetch_quiet "https://www.cloudflare.com/cdn-cgi/trace" || true)"
  country="$(printf '%s\n' "${trace}" | sed -n 's/^loc=//p' | head -n 1)"
  if [[ "${country}" == "CN" ]]; then
    return 0
  fi
  if [[ "${country}" =~ ^[A-Z]{2}$ ]]; then
    return 1
  fi

  # 定位接口不可用时，以官方依赖源的连通性作为兜底判断。
  if fetch_quiet "https://registry-1.docker.io/v2/" >/dev/null \
    && fetch_quiet "https://pypi.org/simple/" >/dev/null; then
    return 1
  fi
  return 0
}

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_docker() {
  local needs_engine=0
  local needs_compose=0
  command -v docker >/dev/null 2>&1 || needs_engine=1
  if ! docker compose version >/dev/null 2>&1 \
    && ! { command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; }; then
    needs_compose=1
  fi
  if [[ "${needs_engine}" -eq 0 && "${needs_compose}" -eq 0 ]]; then
    return
  fi

  echo "[PotatoFlow] 正在安装缺少的 Docker/Compose 组件…"
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    if [[ "${needs_engine}" -eq 1 ]]; then
      run_root apt-get install -y ca-certificates curl docker.io
    fi
    if [[ "${needs_compose}" -eq 1 ]]; then
      run_root apt-get install -y docker-compose-v2 \
        || run_root apt-get install -y docker-compose-plugin \
        || run_root apt-get install -y docker-compose
    fi
  elif command -v dnf >/dev/null 2>&1; then
    if [[ "${needs_engine}" -eq 1 ]]; then
      run_root dnf install -y ca-certificates curl docker
    fi
    if [[ "${needs_compose}" -eq 1 ]]; then
      run_root dnf install -y docker-compose-plugin \
        || run_root dnf install -y docker-compose
    fi
  else
    echo "未识别包管理器，请先安装 Docker Engine 与 Compose。" >&2
    exit 1
  fi
}

docker_run() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    if [[ -n "${POTATOFLOW_PORT:-}" ]]; then
      run_root env "POTATOFLOW_PORT=${POTATOFLOW_PORT}" docker "$@"
    else
      run_root docker "$@"
    fi
  fi
}

compose() {
  if docker_run compose version >/dev/null 2>&1; then
    docker_run compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    if docker-compose version >/dev/null 2>&1; then
      docker-compose "$@"
    else
      run_root docker-compose "$@"
    fi
  else
    echo "没有找到可用的 Docker Compose。" >&2
    exit 1
  fi
}

install_docker
if command -v systemctl >/dev/null 2>&1; then
  run_root systemctl enable --now docker
fi

cd "${ROOT}"
mkdir -p docker-data/recordings

build_args=()
if detect_china_mirror; then
  build_args+=(
    --build-arg "RUST_IMAGE=${POTATOFLOW_RUST_IMAGE:-m.daocloud.io/docker.io/library/rust:bookworm}"
    --build-arg "PYTHON_IMAGE=${POTATOFLOW_PYTHON_IMAGE:-m.daocloud.io/docker.io/library/python:3.11-slim-bookworm}"
    --build-arg "DEBIAN_MIRROR=${POTATOFLOW_DEBIAN_MIRROR:-https://mirrors.aliyun.com/debian}"
    --build-arg "PYPI_INDEX_URL=${POTATOFLOW_PYPI_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
    --build-arg "PYTORCH_INDEX_URL=${POTATOFLOW_PYTORCH_INDEX_URL:-https://mirrors.aliyun.com/pytorch-wheels/cpu}"
    --build-arg "CARGO_MIRROR_URL=${POTATOFLOW_CARGO_MIRROR_URL:-sparse+https://rsproxy.cn/index/}"
    --build-arg "DENO_DOWNLOAD_BASE=${POTATOFLOW_DENO_DOWNLOAD_BASE:-https://gh-proxy.com/https://github.com/denoland/deno/releases/download}"
  )
  echo "[PotatoFlow] 检测为国内网络，已启用国内 Docker、Debian、PyPI、PyTorch、Cargo 与 GitHub 下载源。"
else
  echo "[PotatoFlow] 检测为海外网络，使用各项目官方源。"
fi

echo "[PotatoFlow] 正在构建生产镜像…"
compose build "${build_args[@]}" potato-flow
echo "[PotatoFlow] 正在启动服务…"
POTATOFLOW_PORT="${WEB_PORT}" compose up -d --no-deps potato-flow

for _ in {1..60}; do
  if curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1; then
    echo "[PotatoFlow] 安装完成：http://127.0.0.1:${WEB_PORT}"
    exit 0
  fi
  sleep 2
done

echo "容器已启动，但健康检查在 120 秒内未通过。" >&2
compose ps >&2 || true
compose logs --tail=100 potato-flow >&2 || true
exit 1
