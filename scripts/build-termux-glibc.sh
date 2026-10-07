#!/data/data/com.termux/files/usr/bin/bash
# 在 Termux glibc 环境下构建 Vortek client ICD（aarch64-linux-gnu）
#
# 前置：
#   - clang、cmake、ninja        （普通 Termux: pkg install clang cmake ninja）
#   - Termux glibc 前缀           （termux-pacman，内含 include/ 与 lib/ld-linux-aarch64.so.1）
#   - X11/xcb 头                  （vulkan.h 会引 <xcb/xcb.h>；glibc 侧若有会自动加入）
#
# 用法：
#   bash scripts/build-termux-glibc.sh
#   TERMUX_GLIBC_PREFIX=/data/data/com.termux/files/usr/glibc bash scripts/build-termux-glibc.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
G="${TERMUX_GLIBC_PREFIX:-${PREFIX:-/data/data/com.termux/files/usr}/glibc}"
out="${OUT_DIR:-$here/out-termux-glibc}"

if [ ! -f "$G/lib/ld-linux-aarch64.so.1" ]; then
  echo "错误: 在 $G 下找不到 ld-linux-aarch64.so.1" >&2
  echo "      请确认 Termux glibc 已安装，或用 TERMUX_GLIBC_PREFIX=... 指定前缀" >&2
  exit 1
fi

# 汇集 Vulkan platform 头（仓库自带 vulkan_xcb.h / vulkan_xlib.h）+ 服务端的 vk_video
inc="$here/.build-tg-include"
mkdir -p "$inc"
[ -d "$here/server/vortekrenderer/include/vulkan" ] && cp -r "$here/server/vortekrenderer/include/vulkan" "$inc/"
[ -d "$here/server/vortekrenderer/include/vk_video" ] && cp -r "$here/server/vortekrenderer/include/vk_video" "$inc/"
cp -f "$here/client/vortek/third_party/vulkan-platform/vulkan/vk_icd.h" "$inc/vulkan/" 2>/dev/null || true

cflags=""
for d in "$G/include" "${PREFIX:-}/include"; do
  [ -n "$d" ] && [ -d "$d/xcb" ] && cflags="$cflags -I$d"
done
echo "Termux glibc 前缀: $G"
echo "X11/xcb 头: ${cflags:-未找到（若报 <xcb/xcb.h> 缺失，请先在 glibc 侧装 libx11/xcb）}"

cmake -S "$here/client/vortek" -B "$here/build-termux-glibc" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$here/cmake/toolchain-termux-glibc.cmake" \
  -DTERMUX_GLIBC_PREFIX="$G" \
  -DCMAKE_BUILD_TYPE=Release \
  -DVORTEK_VULKAN_INCLUDE_DIR="$inc" \
  -DCMAKE_C_FLAGS="$cflags" \
  -DCMAKE_INSTALL_PREFIX="$out"
cmake --build "$here/build-termux-glibc" -j"$(nproc 2>/dev/null || echo 4)"
cmake --install "$here/build-termux-glibc"

echo
echo "产物: $out"
ls -la "$out/lib" "$out/share/vulkan/icd.d" 2>/dev/null || true
echo
echo "跑 Wine/Hangover 时用:"
echo "  export VK_ICD_FILENAMES=$out/share/vulkan/icd.d/vortek_icd.aarch64.json"
