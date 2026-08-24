#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${POTATO_PYTHON:-}" ]]; then
  if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
    POTATO_PYTHON="${PROJECT_ROOT}/.venv/bin/python"
  else
    POTATO_PYTHON="${PROJECT_ROOT}/potatoflow-app/.venv/bin/python"
  fi
fi

if [[ ! -x "${POTATO_PYTHON}" ]]; then
  POTATO_PYTHON="python3"
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PYTHON_VERSION="$("${POTATO_PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "3.11" ]]; then
  echo "PotatoFlow tests require Python 3.11; got ${PYTHON_VERSION} from ${POTATO_PYTHON}." >&2
  exit 2
fi

(
  cd "${PROJECT_ROOT}"
  "${POTATO_PYTHON}" -m unittest discover -s tests
)

(
  cd "${PROJECT_ROOT}/potatoflow-app"
  "${POTATO_PYTHON}" -m unittest discover -s tests
)
