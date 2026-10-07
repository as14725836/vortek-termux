# 更新记录

## 性能与兼容性优化

### 性能
- ring buffer 收发合并提交：新增 RingBuffer_write2 / RingBuffer_peekAt / RingBuffer_commit；
  vt_send / vt_recv 由两次提交改为一次，每次调用少一次 waitForWrite 与一次 futex 唤醒。
  本机 aarch64 实测（8B header + 64B payload，20 万次）：4253 ns -> 2858 ns，约 -33%。
- MEMORY_POOL_MAX_SIZE 由 64KB 提升到 256KB，减少大请求 fallback 到 malloc。
- Release 构建参数：-O3 -DNDEBUG -fno-semantic-interposition -fno-plt -ffunction-sections / --gc-sections。
- 客户端 build.sh 把 -Wl,-rpath 从 CFLAGS 拆到 LDFLAGS；新增 VORTEK_ENABLE_LTO（默认关）。

### 兼容与健壮性
- socket 路径支持 VORTEK_SOCKET_PATH，其次 TMPDIR（与服务端 CLI 默认一致）；长度 >= 108 字节直接报错。
- 客户端连接对 ENOENT / ECONNREFUSED 做有限次退避重试。
- sock_read / sock_write / recv_fds / send_fds 补 EINTR 重试；SCM_RIGHTS 改用 CMSG_SPACE / CMSG_LEN。
- CLOSEFD 由 x > 0 改为 x >= 0；修正 APP_CACHE_DIR；atexit 只注册一次。
- 补齐 vulkan_xcb.h / vulkan_xlib.h（third_party/vulkan-platform）。
- 补齐 string_utils.h 的 stdlib.h / string.h 与 time_utils.h 的 time.h（NDK r29 下直接编译失败）。
- Android 上显式链接 liblog。

### 构建校验
- 新增 tests/test_ring_buffer.c：ring buffer 回归 + 收发合并微基准。
- 新增 .github/workflows/build.yml：主机测试 / 客户端 ICD（arm64-v8a）/ 服务端（arm64-v8a JNI）。
