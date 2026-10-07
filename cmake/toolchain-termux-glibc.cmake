# 面向 Termux glibc（termux-pacman / glibc-runner）环境的 aarch64-linux-gnu 工具链
#
# 与 Android NDK 那套的区别：
#   - 目标 aarch64-linux-gnu（glibc），而非 aarch64-linux-android（bionic）
#   - 不链 liblog / libandroid，__ANDROID__ 未定义（println 走 stdout）
#   - 动态链接器与 rpath 指向 Termux glibc 前缀，避免 bionic/glibc 混搭
#
# 用法：
#   cmake -S client/vortek -B build-tg -G Ninja \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-termux-glibc.cmake \
#         -DTERMUX_GLIBC_PREFIX=/data/data/com.termux/files/usr/glibc

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(TERMUX_GLIBC_PREFIX "/data/data/com.termux/files/usr/glibc"
    CACHE PATH "Termux glibc 前缀（内含 include/ 与 lib/ld-linux-aarch64.so.1）")

set(CMAKE_C_COMPILER clang)
set(CMAKE_C_COMPILER_TARGET aarch64-linux-gnu)

# 交叉编译（x86_64 runner）时可指定 glibc sysroot；aarch64 原生构建留空即可
set(TERMUX_GLIBC_SYSROOT "" CACHE PATH "可选的 glibc sysroot（交叉编译时用）")
set(_tg_flags "--target=aarch64-linux-gnu")
if(TERMUX_GLIBC_SYSROOT)
    string(APPEND _tg_flags " --sysroot=${TERMUX_GLIBC_SYSROOT} -isystem ${TERMUX_GLIBC_SYSROOT}/usr/include")
endif()
set(CMAKE_C_FLAGS_INIT "${_tg_flags}")

# 运行期兼容：加载器与库搜索路径都指向 Termux glibc 前缀
set(_tg_run "-Wl,--dynamic-linker=${TERMUX_GLIBC_PREFIX}/lib/ld-linux-aarch64.so.1 -Wl,-rpath,${TERMUX_GLIBC_PREFIX}/lib -Wl,--enable-new-dtags")
set(CMAKE_EXE_LINKER_FLAGS_INIT "${_tg_run}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "-Wl,-rpath,${TERMUX_GLIBC_PREFIX}/lib -Wl,--enable-new-dtags")

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
