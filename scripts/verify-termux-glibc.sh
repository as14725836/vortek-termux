#!/usr/bin/env bash
# 校验产物是否「适用于 Termux glibc 环境」：
#   1) aarch64 ELF
#   2) NEEDED 里不含任何 bionic / Android 专有库（只允许 glibc 系）
#   3) RPATH/RUNPATH 指向 Termux glibc 前缀的 lib/
#   4) ICD 入口符号完整
#   5) 打印需要的最高 GLIBC_ 符号版本（便于对目标前缀的 glibc 版本）
set -euo pipefail

SO="${1:-out-termux-glibc/lib/libvulkan_vortek.so}"
[ -f "$SO" ] || { echo "找不到产物: $SO" >&2; exit 1; }

echo "=== file ==="
file "$SO"
file "$SO" | grep -q 'aarch64' || { echo "错误: 不是 aarch64 ELF" >&2; exit 1; }

echo
echo "=== 动态段 ==="
readelf -d "$SO" | grep -E 'NEEDED|RPATH|RUNPATH' || true

echo
echo "=== NEEDED（只允许 glibc 系）==="
needed=$(readelf -d "$SO" | grep NEEDED | awk -F'[][]' '{print $2}')
echo "$needed"
for lib in $needed; do
  case "$lib" in
    libc.so.6|libm.so.6|libdl.so.2|libpthread.so.0|libgcc_s.so.1|ld-linux-aarch64.so.1) ;;
    *) echo "错误: 出现非 glibc 依赖（Android/bionic 污染）: $lib" >&2; exit 1 ;;
  esac
done

echo
echo "=== RPATH / RUNPATH ==="
rpath=$(readelf -d "$SO" | grep -E 'RPATH|RUNPATH' | awk -F'[][]' '{print $2}')
echo "RPATH=$rpath"
case "$rpath" in
  *usr/glibc/lib*) echo "OK: RPATH 指向 Termux glibc 前缀" ;;
  *) echo "错误: RPATH 未指向 Termux glibc 前缀（当前: '$rpath'）" >&2; exit 1 ;;
esac

echo
echo "=== ICD 入口符号 ==="
dyn=$(readelf --dyn-syms -W "$SO")
for s in vk_icdGetInstanceProcAddr vk_icdNegotiateLoaderICDInterfaceVersion; do
  echo "$dyn" | grep -q "$s" || { echo "错误: 缺少 ICD 入口 $s" >&2; exit 1; }
  echo "OK: $s"
done

echo
echo "=== 需要的最高 GLIBC 符号版本 ==="
readelf --version-info "$SO" | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -uV | tail -3

echo
echo "✅ 通过：该产物适用于 Termux glibc 环境 —— $SO"
echo "   设备上使用： export VK_ICD_FILENAMES=<安装前缀>/share/vulkan/icd.d/vortek_icd.aarch64.json"