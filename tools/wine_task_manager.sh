#!/data/data/com.termux/files/usr/bin/bash
# Termux 专用启动器：自动用 Termux 自带的 python3 + 自动准备好 DISPLAY。
#
# 用法:
#   bash wine_task_manager.sh              # 只看 wine / *.exe
#   bash wine_task_manager.sh --all        # 全部进程
#   bash wine_task_manager.sh --dump       # 不开窗口（不需要 PyQt5）
#
# 可用环境变量:
#   TERMUX_PYTHON   指定 python3（默认 $PREFIX/bin/python3，再退回 PATH 里的 python3）
#   TERMUX_GLIBC_PREFIX  指定 glibc 前缀（用于 find_wine 探测 wine）
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/wine_task_manager.py"
[ -f "$script" ] || { echo "找不到 $script" >&2; exit 1; }

# 1) 选 python：优先 Termux 原生
PY="${TERMUX_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in "${PREFIX:-/data/data/com.termux/files/usr}/bin/python3" \
           /data/data/com.termux/files/usr/bin/python3 \
           "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] && { PY="$c"; break; }
  done
fi
[ -n "$PY" ] || { echo "找不到 python3（先 pkg install python）" >&2; exit 1; }

# 2) 如果没有 DISPLAY，试着用 Termux:X11 的默认值
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  if [ -S "${TMPDIR:-${PREFIX:-/data/data/com.termux/files/usr}/tmp}/.X11-unix/X0" ] \
     || [ -S "/data/data/com.termux/files/usr/tmp/.X11-unix/X0" ]; then
    export DISPLAY=:0
    echo "已自动设置 DISPLAY=:0（检测到 Termux:X11 socket）"
  else
    echo "提示: 未检测到 X socket。请先打开 Termux:X11 应用，然后 export DISPLAY=:0" >&2
  fi
fi

# 3) glibc 前缀（find_wine 会用它找 wine）
if [ -z "${TERMUX_GLIBC_PREFIX:-}" ] && [ -d "${PREFIX:-/data/data/com.termux/files/usr}/glibc" ]; then
  export TERMUX_GLIBC_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}/glibc"
fi

echo "python: $PY"
echo "DISPLAY=${DISPLAY:-（未设置）}"
exec "$PY" "$script" "$@"