#ifndef WINLATOR_SOCKET_UTILS_H
#define WINLATOR_SOCKET_UTILS_H

#include <sys/socket.h>

#define MAX_FDS 32

static inline int send_fds(int sockFd, int* fds, int numFds, void* data, int size) {
    if (!data) size = 1;
    char zero = 0;
    struct iovec iovmsg = {.iov_base = data ? data : &zero, .iov_len = size};
    struct {
        struct cmsghdr align;
        int fds[MAX_FDS];
    } ctrlmsg;

    struct msghdr msg = {
        .msg_name = NULL,
        .msg_namelen = 0,
        .msg_iov = &iovmsg,
        .msg_iovlen = 1,
        .msg_flags = 0,
        .msg_control = &ctrlmsg,
        .msg_controllen = CMSG_SPACE(numFds * sizeof(int))
    };
    if (numFds < 0 || numFds > MAX_FDS) return -1;
    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type = SCM_RIGHTS;
    cmsg->cmsg_len = CMSG_LEN(numFds * sizeof(int));
    for (int i = 0; i < numFds; i++) ((int*)CMSG_DATA(cmsg))[i] = fds[i];
    int res;
    do { res = (int)sendmsg(sockFd, &msg, 0); } while (res < 0 && errno == EINTR);
    return res;
}

static inline int recv_fds(int sockFd, int* outFds, int* outNumFds, void* outData, int size) {
    if (!outData) size = 1;
    char zero = 0;
    struct iovec iovmsg = {.iov_base = outData ? outData : &zero, .iov_len = size};
    struct {
        struct cmsghdr align;
        int fds[MAX_FDS];
    } ctrlmsg;

    struct msghdr msg = {
        .msg_name = NULL,
        .msg_namelen = 0,
        .msg_iov = &iovmsg,
        .msg_iovlen = 1,
        .msg_flags = 0,
        .msg_control = &ctrlmsg,
        .msg_controllen = CMSG_SPACE(MAX_FDS * sizeof(int))
    };
    *outNumFds = 0;
    int res;
    do { res = (int)recvmsg(sockFd, &msg, 0); } while (res < 0 && errno == EINTR);
    if (res > 0) {
        struct cmsghdr* cmsg;
        for (cmsg = CMSG_FIRSTHDR(&msg); cmsg; cmsg = CMSG_NXTHDR(&msg, cmsg)) {
            if (cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS) {
                int numFds = (cmsg->cmsg_len - CMSG_LEN(0)) / sizeof(int);
                if (numFds > 0) {
                    for (int i = 0; i < numFds; i++) outFds[i] = ((int*)CMSG_DATA(cmsg))[i];
                    *outNumFds = numFds;
                }
            }
        }
    }
    return res;
}

static inline int sock_read(int fd, char* buffer, int size) {
    char *ptr = buffer;
    int left;
    int result;

    left = size;
    do {
        result = (int)read(fd, ptr, left);
        if (result < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (result == 0) return 0;
        left -= result;
        ptr += result;
    }
    while (left);

    return size;
}

static inline int sock_write(int fd, char* buffer, int size) {
    char *ptr = buffer;
    int left;
    int result;
    left = size;
    do {
        result = (int)write(fd, ptr, left);
        if (result < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        left -= result;
        ptr += result;
    }
    while (left);

    return size;
}

#endif