#!/usr/bin/env bash
# cwf 调用入口 —— 供 agent / 脚本调用 ComfyUI 工作流工具
#
# 用法：bash cwf.sh <子命令> [参数...]
#   bash cwf.sh ws list
#   bash cwf.sh outline 某工作流
#   bash cwf.sh beautify 某流 --in-place
#
# 环境变量（都可省略）：
#   CWF_HOME       cwf 仓库根目录。省略时按下面的顺序自己找
#   CWF_PYTHON     Python 解释器。省略时按下面的顺序自己找
#   CWF_WORKFLOWS  工作流库目录（不设就由 cwf 自己探测）
set -uo pipefail

# ---- 找仓库根
if [ -z "${CWF_HOME:-}" ]; then
  # 本脚本可能位于 <repo>/skill/comfyui-workflow/scripts/，也可能在别处被调用
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  for cand in "$_here/../../.." "$_here/../.." "$_here/.." "$PWD"; do
    if [ -f "$cand/cwf_run.py" ]; then CWF_HOME="$cand"; break; fi
  done
fi

if [ -z "${CWF_HOME:-}" ] || [ ! -f "$CWF_HOME/cwf_run.py" ]; then
  echo "✖ 找不到 cwf 仓库（该目录下要有 cwf_run.py）。" >&2
  echo "  用 CWF_HOME=<克隆下来的仓库路径> 指定。" >&2
  exit 2
fi

# 把 Windows 风格路径转成 git-bash 能用的形式
to_posix() {
  case "$1" in
    [A-Za-z]:\\*|[A-Za-z]:/*)
      local d="${1:0:1}" rest="${1:2}"
      rest="${rest//\\//}"
      printf '/%s%s' "$(printf '%s' "$d" | tr 'A-Z' 'a-z')" "$rest"
      ;;
    *) printf '%s' "$1" ;;
  esac
}
CWF_HOME_POSIX="$(to_posix "$CWF_HOME")"

# ---- 找 Python
if [ -z "${CWF_PYTHON:-}" ]; then
  for cand in \
    "$CWF_HOME_POSIX/.venv/bin/python" \
    "$CWF_HOME_POSIX/.venv/Scripts/python.exe" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    if [ -n "$cand" ] && [ -x "$cand" ]; then CWF_PYTHON="$cand"; break; fi
  done
fi

if [ -z "${CWF_PYTHON:-}" ]; then
  echo "✖ 找不到可用的 Python。用 CWF_PYTHON=<python 路径> 指定。" >&2
  exit 2
fi

exec "$CWF_PYTHON" -X utf8 "$CWF_HOME_POSIX/cwf_run.py" "$@"
