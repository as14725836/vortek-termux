#define HEADER_SIZE 8
#define DEVICE_NAME "Vortek (%s)"
#define ENABLE_VALIDATION_LAYER 0 // FIXME set to 0 and remove libVkLayer from jniLibs
#define DEBUG_MODE 0 // FIXME set to 0
#define MEMORY_POOL_MAX_SIZE 262144
#define SERVER_RING_BUFFER_SIZE 4194304
#define CLIENT_RING_BUFFER_SIZE 262144
#define VORTEK_TMPDIR_DEFAULT "/data/data/com.termux/files/usr/tmp"
#define VORTEK_SOCKET_SUBDIR ".vortek"
#define VORTEK_SOCKET_NAME "V0"
/* 显式覆盖优先；否则跟服务端 CLI 一样用 $TMPDIR */
#define VORTEK_SOCKET_PATH_ENV "VORTEK_SOCKET_PATH"
#define VORTEK_SERVER_PATH VORTEK_TMPDIR_DEFAULT "/" VORTEK_SOCKET_SUBDIR "/" VORTEK_SOCKET_NAME
#define VORTEK_CONNECT_MAX_ATTEMPTS 50
#define VORTEK_CONNECT_RETRY_US 20000
/* 运行时可调的线程池上限（0/未设置 => 用 THREAD_POOL_NUM_THREADS） */
#define VORTEK_THREADS_ENV "VORTEK_THREADS"
#define VK_HANDLE_BYTE_COUNT 8
#define THREAD_POOL_NUM_THREADS 8

#include "winlator.h"

#if defined(__ANDROID__) && !defined(VT_CLIENT)
#define VT_SERVER 1
#define VK_NO_PROTOTYPES 1
#endif

#define VT_CMD_ENQUEUE(cmdName, requestCode, batch, ...) \
    do { \
        int bufferSize = vt_sizeof_##cmdName(__VA_ARGS__); \
        ENSURE_ARRAY_CAPACITY(batch->size + bufferSize + HEADER_SIZE, batch->capacity, batch->buffer, 1); \
        char* chunk = batch->buffer + batch->size; \
        *(int*)(chunk + 0) = requestCode; \
        *(int*)(chunk + 4) = bufferSize; \
        vt_serialize_##cmdName(__VA_ARGS__, chunk + HEADER_SIZE); \
        batch->size += bufferSize + HEADER_SIZE; \
    } \
    while (0)

#ifdef VT_SERVER
#define VT_SERIALIZE_CMD(cmdName, ...) \
    int bufferSize = vt_sizeof_##cmdName(__VA_ARGS__); \
    char* outputBuffer = vt_alloc(&context->memoryPool, bufferSize); \
    vt_serialize_##cmdName(__VA_ARGS__, outputBuffer)
#else
#define VT_SERIALIZE_CMD(cmdName, ...) \
    int bufferSize = vt_sizeof_##cmdName(__VA_ARGS__); \
    char* outputBuffer = vt_alloc(&globalMemoryPool, bufferSize); \
    vt_serialize_##cmdName(__VA_ARGS__, outputBuffer)
#endif

#define VT_RETURN 1

#define VT_SEND_CHECKED(requestCode, ...) \
    do { \
        int bytesSent = vt_send(serverRing, requestCode, outputBuffer, bufferSize); \
        if (bytesSent != bufferSize) { \
            VT_CALL_UNLOCK(); \
            return __VA_OPT__(VK_ERROR_DEVICE_LOST); \
        } \
    } \
    while (0)

#define VT_RECV_CHECKED(...) \
    char* inputBuffer = NULL; \
    int result; \
    do { \
        result = vt_recv(clientRing, &inputBuffer, NULL, &globalMemoryPool); \
        if (result == VK_ERROR_DEVICE_LOST) { \
            VT_CALL_UNLOCK(); \
            return __VA_OPT__(VK_ERROR_DEVICE_LOST); \
        } \
    } \
    while (0)

#define IS_DESCRIPTOR_IMAGE_INFO(type) (type == VK_DESCRIPTOR_TYPE_SAMPLER || type == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER || type == VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE || type == VK_DESCRIPTOR_TYPE_STORAGE_IMAGE || type == VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT)
#define IS_DESCRIPTOR_BUFFER_INFO(type) (type == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER || type == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER || type == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC || type == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER_DYNAMIC)
#define IS_DESCRIPTOR_TEXEL_BUFFER_VIEW(type) (type == VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER || type == VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER)

#ifndef VORTEK_H
#define VORTEK_H

#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <stdarg.h>
#include <string.h>
#include <malloc.h>
#include <unistd.h>
#include <sys/socket.h>
#include <pthread.h>

#include "vulkan/vulkan.h"
#include "request_codes.h"
#include "vk_object.h"
#include "arrays.h"
#include "events.h"
#include "time_utils.h"
#include "socket_utils.h"
#include "ring_buffer.h"
#include "thread_pool.h"

typedef struct MemoryPool {
    void* data;
    int size;
    ArrayList allocationList;
} MemoryPool;

typedef struct VkContext VkContext;

#ifdef VT_SERVER

#include <jni.h>
#include <android/log.h>

#include "resource_memory.h"
#include "shader_inspector.h"

typedef struct JMethods {
    JavaVM* jvm;
    JNIEnv* env;
    jobject obj;
    jmethodID getWindowWidth;
    jmethodID getWindowHeight;
    jmethodID getWindowHardwareBuffer;
    jmethodID updateWindowContent;
} JMethods;

#else // VT_SERVER

typedef struct MappedMemory {
    void* data;
    int allocationSize;
    int size;
} MappedMemory;

typedef struct CommandBatch {
    char* buffer;
    int capacity;
    int size;
} CommandBatch;

extern bool vortekInitOnce();
extern int serverFd;
extern uint16_t maxClientRequestId;
extern MemoryPool globalMemoryPool;
extern RingBuffer* serverRing;
extern RingBuffer* clientRing;
#endif

static inline void* findNextVkStructure(void* pNext, VkStructureType type) {
    while (pNext) {
        VkBaseOutStructure* curr = pNext;
        if (curr->sType == type) return pNext;
        pNext = curr->pNext;
    }

    return NULL;
}

static inline void* invertVkStructuresChain(void* pNext) {
    void* pPrev = NULL;

    while (pNext) {
        VkBaseOutStructure* curr = pNext;
        pNext = curr->pNext;
        curr->pNext = pPrev;
        pPrev = curr;
    }

    return pPrev;
}

static inline void* removeNextVkStructure(void* pNext, VkStructureType type) {
    VkBaseOutStructure* prev = NULL;
    void* pFirst = pNext;

    while (pNext) {
        VkBaseOutStructure* curr = pNext;
        pNext = curr->pNext;
        if (curr->sType == type) {
            if (prev) {
                prev->pNext = pNext;
            }
            else pFirst = pNext;
            break;
        }
        prev = curr;
    }

    return pFirst;
}

static inline const char* vortekServerPath(void) {
    static char path[256];
    const char* explicitPath = getenv(VORTEK_SOCKET_PATH_ENV);
    if (explicitPath && explicitPath[0]) {
        snprintf(path, sizeof(path), "%s", explicitPath);
        return path;
    }
    const char* tmpDir = getenv("TMPDIR");
    if (!tmpDir || !tmpDir[0]) tmpDir = VORTEK_TMPDIR_DEFAULT;
    snprintf(path, sizeof(path), "%s/%s/%s", tmpDir, VORTEK_SOCKET_SUBDIR, VORTEK_SOCKET_NAME);
    return path;
}
static inline void* vt_alloc(MemoryPool* memoryPool, int size) {
    bool isFull = (memoryPool->size + size) >= MEMORY_POOL_MAX_SIZE || !memoryPool->data;
    void* chunk;
    if (isFull) {
        chunk = malloc(size);
        ArrayList_add(&memoryPool->allocationList, chunk);
    }
    else {
        chunk = memoryPool->data + memoryPool->size;
        memoryPool->size += size;
    }

    memset(chunk, 0, size);
    return chunk;
}

static inline void vt_free(MemoryPool* memoryPool) {
    if (!memoryPool) return;
    memoryPool->size = 0;

    for (int i = memoryPool->allocationList.size-1; i >= 0; i--) {
        MEMFREE(memoryPool->allocationList.elements[i]);
        ArrayList_removeAt(&memoryPool->allocationList, i);
    }
}

static inline int vt_send(RingBuffer* ring, int requestCode, void* data, int size) {
#ifndef VT_SERVER
    if (size >= SERVER_RING_BUFFER_SIZE) {
        const uint16_t requestId = maxClientRequestId++;
        char header[HEADER_SIZE];
        *(int*)(header + 0) = PACK16(REQUEST_CODE_SEND_EXTRA_DATA, requestId);
        *(int*)(header + 4) = size;

        int bytesSent = sock_write(serverFd, header, HEADER_SIZE) ;
        if (bytesSent != HEADER_SIZE) return 0;

        bytesSent = sock_write(serverFd, data, size);
        if (bytesSent != size) return 0;

        *(int*)(header + 0) = PACK16(requestCode, requestId);
        *(int*)(header + 4) = 0;
        bool result = RingBuffer_write(ring, header, HEADER_SIZE);
        if (!result) return 0;

        return size;
    }
#endif

    char header[HEADER_SIZE];
    *(int*)(header + 0) = requestCode;
    *(int*)(header + 4) = size;
    /* header + payload 一次提交，少一次 futex 唤醒 */
    if (!RingBuffer_write2(ring, header, HEADER_SIZE, size > 0 ? data : NULL,
                           size > 0 ? (uint32_t)size : 0)) return 0;
    return size;
}

static inline int vt_recv(RingBuffer* ring, char** inputBuffer, int* bufferSize, MemoryPool* memoryPool) {
    char header[HEADER_SIZE];
    if (!RingBuffer_waitForRead(ring, HEADER_SIZE)) return VK_ERROR_DEVICE_LOST;
    if (!RingBuffer_peekAt(ring, 0, header, HEADER_SIZE)) return VK_ERROR_DEVICE_LOST;
    int requestCode = *(int*)(header + 0);
    int size = *(int*)(header + 4);
    if (size < 0 || (uint32_t)size > ring->bufferSize - HEADER_SIZE) {
        println("vortek: ring bad payload size %d", size);
        return VK_ERROR_DEVICE_LOST;
    }
    if (size > 0) {
        if (!RingBuffer_waitForRead(ring, HEADER_SIZE + (uint32_t)size)) return VK_ERROR_DEVICE_LOST;
        *inputBuffer = vt_alloc(memoryPool, size);
        if (!RingBuffer_peekAt(ring, HEADER_SIZE, *inputBuffer, (uint32_t)size)) return VK_ERROR_DEVICE_LOST;
    }
    /* header+payload 一次提交，少一次 futex 唤醒 */
    RingBuffer_commit(ring, HEADER_SIZE + (uint32_t)size);

    if (bufferSize) *bufferSize = size;
    return requestCode;
}

#endif
