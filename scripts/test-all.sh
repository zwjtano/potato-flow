#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POTATO_PYTHON="${POTATO_PYTHON:-${PROJECT_ROOT}/potatoflow-app/.venv/bin/python}"

if [[ ! -x "${POTATO_PYTHON}" ]]; then
  POTATO_PYTHON="python3"
fi

(
  cd "${PROJECT_ROOT}"
  "${POTATO_PYTHON}" -m unittest discover -s tests
)

(
  cd "${PROJECT_ROOT}/potatoflow-app"
  "${POTATO_PYTHON}" -m unittest discover -s tests
)
