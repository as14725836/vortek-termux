#!/bin/bash
set -e
if [ -z "${ROOTFS:-}" ]; then
    if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/glibc" ]; then
        ROOTFS="$PREFIX/glibc"
    else
        ROOTFS="/data/data/com.termux/files/usr/glibc"
    fi
fi
export ROOTFS
# 原来把 -Wl,-rpath 放进 CFLAGS，编译/链接语义混乱；这里拆开
export CFLAGS="${CFLAGS:--O3 -DNDEBUG -fno-semantic-interposition -fno-plt}"
export LDFLAGS="${LDFLAGS:--Wl,-rpath=$ROOTFS/lib -Wl,-O2,--gc-sections,--as-needed}"
cmake -S . -B build -DCMAKE_INSTALL_PREFIX="$ROOTFS" \
      -DCMAKE_C_FLAGS_RELEASE="$CFLAGS" \
      -DCMAKE_EXE_LINKER_FLAGS="$LDFLAGS" \
      -DCMAKE_SHARED_LINKER_FLAGS="$LDFLAGS" \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"${JOBS:-8}"
cmake --install build
if [ -f create-asset.sh ]; then
    bash create-asset.sh
fi
