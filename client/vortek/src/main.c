#include <sys/socket.h>
#include <sys/un.h>
#include <errno.h>
#include <sys/mman.h>

#include "vortek.h"

int serverFd = -1;
uint16_t maxClientRequestId = 1;
MemoryPool globalMemoryPool = {0};
RingBuffer* serverRing = NULL;
RingBuffer* clientRing = NULL;

static int vortekServerConnect() {
    const char* socketPath = vortekServerPath();
    struct sockaddr_un server_addr;
    memset(&server_addr, 0, sizeof(server_addr));
    server_addr.sun_family = AF_LOCAL;
    if (strlen(socketPath) >= sizeof(server_addr.sun_path)) {
        println("vortek: socket path too long (%zu >= %zu): %s",
                strlen(socketPath), sizeof(server_addr.sun_path), socketPath);
        return -1;
    }
    strncpy(server_addr.sun_path, socketPath, sizeof(server_addr.sun_path) - 1);
    /* 服务端可能比客户端晚一点起来（Termux 里很常见），
     * 对 ENOENT / ECONNREFUSED 做有限次退避重试 */
    for (int attempt = 0; attempt < VORTEK_CONNECT_MAX_ATTEMPTS; attempt++) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) return -1;
        int res;
        do {
            res = 0;
            if (connect(fd, (struct sockaddr*)&server_addr, sizeof(struct sockaddr_un)) < 0) res = -errno;
        }
        while (res == -EINTR);
        if (res == 0) return fd;
        close(fd);
        if (res != -ENOENT && res != -ECONNREFUSED) return -1;
        usleep(VORTEK_CONNECT_RETRY_US);
    }
    return -1;
}

static bool createVkContext() {
    char header[HEADER_SIZE];
    *(int*)(header + 0) = REQUEST_CODE_CREATE_CONTEXT;
    *(int*)(header + 4) = 0;
    
    int res = write(serverFd, header, HEADER_SIZE);
    if (res < 0) return false;
    
    int shmFds[2];
    int numFds;
    recv_fds(serverFd, shmFds, &numFds, NULL, 0);
    if (numFds != 2) return false;
    
    serverRing = RingBuffer_create(shmFds[0], SERVER_RING_BUFFER_SIZE);
    if (!serverRing) return false;
    
    clientRing = RingBuffer_create(shmFds[1], CLIENT_RING_BUFFER_SIZE);
    if (!clientRing) return false;
    
    close(shmFds[0]);
    close(shmFds[1]);
    
    if (!globalMemoryPool.data) {
        globalMemoryPool.data = malloc(MEMORY_POOL_MAX_SIZE);
        memset(globalMemoryPool.data, 0, MEMORY_POOL_MAX_SIZE);        
    }
    return true;
}

static void terminationCallback() {
    CLOSEFD(serverFd);
    if (serverRing) RingBuffer_free(serverRing);
    if (clientRing) RingBuffer_free(clientRing);
    
    vt_free(&globalMemoryPool);
    MEMFREE(globalMemoryPool.data);
}

bool vortekInitOnce() {
    static bool exitHandlerRegistered = false;
    if (!exitHandlerRegistered) {
        exitHandlerRegistered = true;
        atexit(terminationCallback);
    }
    if (serverFd == -1) {
        serverFd = vortekServerConnect();

        if (serverFd > 0) {
            if (!createVkContext()) return false;
#if DEBUG_MODE
            println("vortek: connected serverFd=%d pid=%d\n", serverFd, getpid());
#endif
        }
    }

    return serverFd > 0;
}