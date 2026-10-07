# vortek-termux

winlator 的 vortek 渲染器（Vulkan 兼容层）的 **Termux 移植**。

一句话说明用法：**服务端跑在 Termux 原生（bionic）侧，客户端跑在 glibc 前缀（Hangover / Wine）里，两边通过 UNIX socket + 共享内存 ring 通信。**

---

## 一、架构拓扑

```
┌─────────────────────────────┐            ┌──────────────────────────────┐
│  glibc 侧 ($PREFIX/glibc)   │            │  Termux 原生 (bionic)        │
│  Hangover / Wine            │            │                              │
│  libvulkan_vortek.so        │  AF_UNIX   │  vortekrenderer-cli          │
│   (client ICD)  ────────────┼──socket────┼──► (server)                  │
│      ▲                      │  +SCM_RIGHTS   │      │                   │
│      │ dlopen               │  +ring 共享内存 │      ▼                  │
│  Vulkan loader              │            │  libadrenotools              │
│  (VK_ICD_FILENAMES)         │            │      │                       │
└─────────────────────────────┘            │      ▼                       │
                                           │  宿主 GPU 驱动 (vendor)      │
                                           │  + Termux:X11 DRI3 呈现      │
                                           └──────────────────────────────┘
```

| 角色 | 构建目标 | 目标 libc | 产物 |
|---|---|---|---|
| client（ICD） | `client/vortek` | **glibc** (`aarch64-linux-gnu`) | `lib/libvulkan_vortek.so` + `share/vulkan/icd.d/*.json` |
| server（CLI） | `server/vortekrenderer` `-DVORTEK_BUILD_CLI=ON` | **bionic** (`aarch64-linux-android26`) | `vortekrenderer-cli` |
| server（JNI） | `server/vortekrenderer` `-DVORTEK_BUILD_JNI=ON` | bionic | `libvortekrenderer.so`（给 App 用，非本场景） |

> ⚠️ 两边 **libc 不同是设计如此**，不是混搭错误：服务端要直接调 Android 的 vendor 驱动与 AHB，客户端要被 glibc 的 Vulkan loader dlopen。

---

## 二、目录结构

```
client/vortek/          # 客户端 ICD（glibc）
  include/ src/
  third_party/vulkan-platform/     # 仓库自带的 vk_icd.h / vk_layer.h（vulkan-headers 不提供）
  vortek_icd.aarch64.json          # 绝对路径 manifest（$PREFIX/glibc/lib）
  CMakeLists.txt
server/
  vortekrenderer/       # 服务端（bionic）：JNI + CLI 两种形态
  winlator/             # Winlator 公共代码（ring buffer 等）
  libadrenotools/       # 加载/打补丁 vendor 驱动
cmake/toolchain-termux-glibc.cmake   # 客户端 glibc 工具链
scripts/build-termux-glibc.sh        # 客户端：设备上直接构建
scripts/verify-termux-glibc.sh       # 客户端产物校验（NEEDED/RPATH/ICD 入口/GLIBC 版本）
tools/icd_load_probe.py              # 真实 Vulkan loader 加载探针（无需 GPU/服务端）
tools/fake_server_probe.py           # 假服务端探针：验证整条启动链
tests/test_ring_buffer.c             # ring buffer 回归 + 微基准
```

---

## 三、构建

### 1) 服务端（bionic，在 Termux 里）

依赖：`clang`、`cmake`、`ninja`、`x11` / `xcb-dri3` / `xcb-present` / `xcb-sync` / `xshmfence` 开发包、`android-shmem`，
外加 NDK（`-target aarch64-linux-android26`）。

```bash
cmake -S server/vortekrenderer -B build-server -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DVORTEK_BUILD_CLI=ON -DVORTEK_BUILD_JNI=OFF \
      -DVORTEK_CLI_BUNDLE_WINLATOR_SOURCES=ON \
      -DADRENOTOOLS_INCLUDE_DIR="$PWD/server/libadrenotools/include" \
      -DWINLATOR_SOURCE_DIR="$PWD/server/winlator" \
      -DCMAKE_C_COMPILER=clang
cmake --build build-server -j"$(nproc)"
```

### 2) 客户端（glibc）

```bash
bash scripts/build-termux-glibc.sh          # 默认前缀 $PREFIX/glibc
bash scripts/verify-termux-glibc.sh out-termux-glibc/lib/libvulkan_vortek.so
```

或手动：

```bash
cmake -S client/vortek -B build-client -G Ninja \
      -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-termux-glibc.cmake \
      -DTERMUX_GLIBC_PREFIX="$PREFIX/glibc" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$PREFIX/glibc"
cmake --build build-client -j"$(nproc)" && cmake --install build-client
```

---

## 四、运行（**顺序很重要**）

```bash
# ① 先起服务端（Termux 原生）
vortekrenderer-cli                       # 默认 socket: $TMPDIR/.vortek/V0
# 或显式指定：
vortekrenderer-cli -s "$PREFIX/tmp/.vortek/V0"

# ② 再起 glibc 侧（Hangover / Wine）
export VK_ICD_FILENAMES="$PREFIX/glibc/share/vulkan/icd.d/vortek_icd.aarch64.json"
# 若两端 TMPDIR 不同，直接告诉客户端：
export VORTEK_SOCKET_PATH="$PREFIX/tmp/.vortek/V0"
```

**为什么必须服务端先起**：客户端是在 `vk_icdGetInstanceProcAddr()` 里做初始化（connect + 建 ring）的。
那一刻连不上服务端，Vulkan loader 会判定该 ICD 无效并报 `Found no drivers!`（`VK_ERROR_INCOMPATIBLE_DRIVER`），
**本次进程内不会再重试**。

**客户端找不到 socket 时会自动回退**，按下面顺序逐个尝试（自动去重）：

```
$VORTEK_SOCKET_PATH  →  $TMPDIR/.vortek/V0  →  $PREFIX/tmp/.vortek/V0
                     →  $PREFIX/glibc/tmp/.vortek/V0  →  /data/data/com.termux/files/usr/tmp/.vortek/V0
```

全部失败时会打印试过的每条路径，并提示先启动服务端。

---

## 五、环境变量

| 变量 | 侧 | 作用 |
|---|---|---|
| `VK_ICD_FILENAMES` | client | 指向 ICD manifest（必须） |
| `VORTEK_SOCKET_PATH` | 两侧 | 显式指定 socket 路径（优先级最高） |
| `TMPDIR` | 两侧 | socket 默认放在 `$TMPDIR/.vortek/V0` |
| `PREFIX` | client | 用于推导 `$PREFIX/tmp`、`$PREFIX/glibc/tmp` 候选 |
| `VORTEK_ADRENOTOOLS_HOOK_DIR` | server | adrenotools hook 目录 |
| `VORTEK_CLI_X11_DRI3` / `VORTEK_CLI_X11_DRI3_DEBUG` | server | DRI3 呈现路径开关/调试 |
| `VORTEK_CONNECT_MAX_ATTEMPTS` / `VORTEK_CONNECT_RETRY_US` | client | 连接退避重试参数（源码默认 50 / 20000μs） |

---

## 六、验证（不需要真 GPU / 不需要真服务端）

```bash
# 1) 产物是否符合 Termux glibc 要求（NEEDED 只有 glibc 系、RPATH、ICD 入口、GLIBC 版本）
bash scripts/verify-termux-glibc.sh <libvulkan_vortek.so>

# 2) 真实 Vulkan loader 能否解析 manifest 并 dlopen 本 ICD
python3 tools/icd_load_probe.py <libvulkan_vortek.so> relative    # 相对路径 manifest
python3 tools/icd_load_probe.py <libvulkan_vortek.so> absolute    # 绝对路径 manifest

# 3) 整条启动链（connect → 握手 → 收 fd → 建 ring → 发出第一个 Vulkan 调用）
python3 tools/fake_server_probe.py <libvulkan_vortek.so>
python3 tools/fake_server_probe.py <libvulkan_vortek.so> --via-fallback   # 模拟两端 TMPDIR 不一致

# 4) ring buffer 回归 + 微基准
cc -O2 -o /tmp/t tests/test_ring_buffer.c client/vortek/src/ring_buffer.c && /tmp/t
```

CI 覆盖：`Build & Verify`（host tests / server JNI / client ICD·Android）与 `Termux glibc (aarch64-linux-gnu)`。

---

## 七、故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `Found no drivers!` + `vkCreateInstance -> -9` | 服务端没起或 socket 路径不一致。先起 `vortekrenderer-cli`，或用 `VORTEK_SOCKET_PATH` 指定；客户端日志会列出试过的路径 |
| `undefined symbol: __android_log_print` | 用 glibc 工具链却链了 bionic 的 `liblog`；glibc 构建不需要 `liblog` |
| `Could not load ... libvulkan_vortek.so` | 用 glibc 工具链构建；跑 `scripts/verify-termux-glibc.sh` 看 NEEDED / RPATH |
| 客户端一连上就异常 | 检查两侧 `SERVER_RING_BUFFER_SIZE` / `CLIENT_RING_BUFFER_SIZE` 是否一致（改过要同步重建两边） |
| Wine 报缺 `kernelbase.dll` 之类 | 与本项目无关（Wine 前缀问题） |

---

### Thanks:

Client: [vortek](https://github.com/brunodev85/vortek)
Server: [winlator](https://github.com/brunodev85/winlator-app)
adrenotools: [libadrenotools](https://github.com/bylaws/libadrenotools)
linkernsbypass: [liblinkernsbypass](https://github.com/bylaws/liblinkernsbypass)

[Xmem](https://github.com/xMeM)
[Mesa-team](https://gitlab.freedesktop.org/mesa/mesa)

---

### 许可

- client 源自上游 [brunodev85/vortek](https://github.com/brunodev85/vortek)，LGPL-2.1。
- `client/vortek/third_party/vulkan-platform/vulkan/vk_icd.h`、`vk_layer.h` 来自 Khronos Vulkan-Loader，Apache-2.0（保留原版权头）。
