/* ring buffer 回归测试 + 收发合并的微基准
 *
 * 编译/运行：
 *   cc -O2 -o /tmp/ring_test tests/test_ring_buffer.c client/vortek/src/ring_buffer.c -lpthread
 *   /tmp/ring_test          # 功能测试
 *   /tmp/ring_test bench    # 微基准（旧：2×RingBuffer_write / 新：RingBuffer_write2）
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/mman.h>
#include <time.h>

#include "ring_buffer.h"

#define RING_SIZE 8192u

static int failures = 0;

#define CHECK(cond, ...)                                        \
    do {                                                        \
        if (!(cond)) {                                          \
            failures++;                                         \
            printf("  FAIL %s:%d  ", __FILE__, __LINE__);        \
            printf(__VA_ARGS__);                                \
            printf("\n");                                       \
        }                                                       \
    } while (0)

static int makeShmFd(uint32_t size) {
    int fd = memfd_create("vortek-test", 0);
    if (fd < 0) { perror("memfd_create"); exit(2); }
    if (ftruncate(fd, (off_t)RingBuffer_getSHMemSize(size)) != 0) { perror("ftruncate"); exit(2); }
    return fd;
}

static void fill(uint8_t* buf, uint32_t n, uint8_t seed, uint32_t tag) {
    for (uint32_t i = 0; i < n; i++) buf[i] = (uint8_t)(seed + i * 7 + tag);
}

/* 用「合并写 + peek/commit」走一遍 header+payload，校验字节完全一致 */
static void roundTrip(RingBuffer* tx, RingBuffer* rx, uint32_t payloadSize, uint32_t tag) {
    uint8_t header[8];
    uint8_t* payload = malloc(payloadSize ? payloadSize : 1);
    uint8_t* got = malloc(payloadSize ? payloadSize : 1);
    uint8_t gotHeader[8];
    uint32_t headBefore = RingBuffer_getHead(tx);

    *(uint32_t*)(header + 0) = 0x1000 + tag;
    *(uint32_t*)(header + 4) = payloadSize;
    fill(payload, payloadSize, 0xA0, tag);

    if (!RingBuffer_write2(rx, header, 8, payload, payloadSize)) {
        CHECK(0, "write2 failed size=%u", payloadSize);
        free(payload);
        free(got);
        return;
    }

    CHECK(RingBuffer_waitForRead(tx, 8), "waitForRead header size=%u", payloadSize);
    CHECK(RingBuffer_peekAt(tx, 0, gotHeader, 8), "peekAt header size=%u", payloadSize);
    CHECK(*(uint32_t*)(gotHeader + 0) == 0x1000 + tag, "header tag size=%u", payloadSize);
    CHECK(*(uint32_t*)(gotHeader + 4) == payloadSize, "header size field size=%u", payloadSize);

    if (payloadSize) {
        CHECK(RingBuffer_waitForRead(tx, 8 + payloadSize), "waitForRead body size=%u", payloadSize);
        CHECK(RingBuffer_peekAt(tx, 8, got, payloadSize), "peekAt body size=%u", payloadSize);
        CHECK(memcmp(got, payload, payloadSize) == 0, "body mismatch size=%u tag=%u", payloadSize, tag);
    }
    /* 未 commit 前 head 不应前进 */
    CHECK(RingBuffer_getHead(tx) == headBefore, "head advanced before commit");

    RingBuffer_commit(tx, 8 + payloadSize);
    CHECK(RingBuffer_size(tx) == 0, "ring not drained after commit (size=%u)", RingBuffer_size(tx));

    free(payload);
    free(got);
}

static void testFunctional(void) {
    printf("== 功能测试 (ring=%u) ==\n", RING_SIZE);
    int fdA = makeShmFd(RING_SIZE), fdB = makeShmFd(RING_SIZE);
    /* tx = 读端(clientRing)，rx = 写端 */
    RingBuffer* tx = RingBuffer_create(fdA, RING_SIZE);
    RingBuffer* rx = RingBuffer_create(fdB, RING_SIZE);
    CHECK(tx && rx, "RingBuffer_create");
    if (!tx || !rx) return;
    /* 两个 ring 必须互相配对（同一个 shared 区）：把写端的 ring 也给读端用 */
    RingBuffer_free(tx);
    RingBuffer_free(rx);
    close(fdA);
    close(fdB);

    /* 单块 shared 区，收发都指向它 */
    int fd = makeShmFd(RING_SIZE);
    RingBuffer* ring = RingBuffer_create(fd, RING_SIZE);
    CHECK(ring != NULL, "RingBuffer_create");
    if (!ring) return;

    const uint32_t sizes[] = { 0, 1, 8, 64, 512, 1000, 4000, 2000 };
    uint32_t tag = 0;
    for (unsigned i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++)
        roundTrip(ring, ring, sizes[i], ++tag);

    /* 连续两条消息，制造跨环回绕 */
    for (int rep = 0; rep < 40; rep++)
        roundTrip(ring, ring, 1500 + (rep * 37) % 2000, ++tag);

    /* 老接口 RingBuffer_write 仍可用（服务端/兼容路径） */
    {
        uint8_t buf[256];
        fill(buf, sizeof(buf), 0x11, 1);
        CHECK(RingBuffer_write(ring, buf, sizeof(buf)), "legacy write");
        uint8_t out[256];
        CHECK(RingBuffer_read(ring, out, sizeof(out)), "legacy read");
        CHECK(memcmp(buf, out, sizeof(buf)) == 0, "legacy roundtrip mismatch");
    }
    CHECK(RingBuffer_size(ring) == 0, "ring not empty at end");
    RingBuffer_free(ring);
    close(fd);

    /* 单次 commit 语义：写 header+payload 后 head 只前进一次 */
    printf("  功能测试完成，失败 %d 项\n", failures);
}

/* ---------------- 微基准 ---------------- */
static RingBuffer* benchRing;
static volatile int benchStop;
static volatile uint64_t benchConsumed;

static void* consumerThread(void* arg) {
    (void)arg;
    uint8_t header[8];
    uint8_t payload[256];
    while (!benchStop) {
        if (!RingBuffer_waitForRead(benchRing, 8)) break;
        if (!RingBuffer_peekAt(benchRing, 0, header, 8)) break;
        uint32_t size = *(uint32_t*)(header + 4);
        if (size > sizeof(payload)) { CHECK(0, "bad size"); break; }
        if (size) {
            if (!RingBuffer_waitForRead(benchRing, 8 + size)) break;
            if (!RingBuffer_peekAt(benchRing, 8, payload, size)) break;
        }
        RingBuffer_commit(benchRing, 8 + size);
        benchConsumed++;
    }
    return NULL;
}

static double runBench(int usePair, int iterations, uint32_t payloadSize) {
    int fd = makeShmFd(RING_SIZE);
    benchRing = RingBuffer_create(fd, RING_SIZE);
    benchStop = 0;
    benchConsumed = 0;
    pthread_t th;
    pthread_create(&th, NULL, consumerThread, NULL);

    uint8_t header[8];
    uint8_t payload[256];
    *(uint32_t*)(header + 0) = 0x2000;
    *(uint32_t*)(header + 4) = payloadSize;
    memset(payload, 0x5A, sizeof(payload));

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (int i = 0; i < iterations; i++) {
        if (usePair) {
            if (!RingBuffer_write2(benchRing, header, 8, payload, payloadSize)) { CHECK(0, "write2"); break; }
        }
        else {
            if (!RingBuffer_write(benchRing, header, 8)) { CHECK(0, "write hdr"); break; }
            if (payloadSize && !RingBuffer_write(benchRing, payload, payloadSize)) { CHECK(0, "write body"); break; }
        }
    }
    while (benchConsumed < (uint64_t)iterations) { /* 等消费端追平 */ }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    benchStop = 1;
    if (!RingBuffer_hasStatus(benchRing, RING_STATUS_EXIT)) RingBuffer_setStatus(benchRing, RING_STATUS_EXIT);
    pthread_join(th, NULL);

    RingBuffer_free(benchRing);
    close(fd);

    double ns = (t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec);
    return ns / iterations;
}

static void testBench(void) {
    const int iters = 200000;
    const uint32_t payload = 64;
    printf("\n== 微基准 (每次调用 header=8B + payload=%uB, %d 次) ==\n", payload, iters);
    /* 预热 */
    runBench(0, 20000, payload);
    runBench(1, 20000, payload);
    double oldNs = runBench(0, iters, payload);
    double newNs = runBench(1, iters, payload);
    printf("  旧: 2x RingBuffer_write   %8.1f ns/次  (2 次 setTail/futex 唤醒)\n", oldNs);
    printf("  新: RingBuffer_write2     %8.1f ns/次  (1 次 setTail/futex 唤醒)\n", newNs);
    printf("  差: %.1f ns/次  (%.1f%%)\n", oldNs - newNs, (oldNs - newNs) * 100.0 / oldNs);
}

int main(int argc, char** argv) {
    alarm(60); /* 防止意外死等 */
    testFunctional();
    if (argc > 1 && strcmp(argv[1], "bench") == 0) testBench();
    printf("\n结果: %s\n", failures ? "有失败" : "全部通过");
    return failures ? 1 : 0;
}