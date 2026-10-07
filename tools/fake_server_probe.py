#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假服务端探针：验证 glibc 客户端 ICD 的完整启动链（不需要真服务端、不需要 GPU）。

客户端初始化链条（src/main.c + include/vortek.h）：
    1. connect(VORTEK_SOCKET_PATH)
    2. 往 socket 写 8 字节 header: { requestCode=REQUEST_CODE_CREATE_CONTEXT(1), length=0 }
    3. recvmsg 收 2 个 fd（server ring / client ring，走 SCM_RIGHTS）
    4. RingBuffer_create() 建两个环，分配全局内存池
    5. 真正发第一个调用: ring 里写 { REQUEST_CODE_VK_CREATE_INSTANCE(100), size }

本探针把这些"服务端该做的事"做一个最小实现，然后看客户端能不能走到第 5 步。
能走到第 5 步 = glibc 产物在 Termux 那套 socket + 共享内存机制下是活的。

用法:
    python3 tools/fake_server_probe.py out-local/lib/libvulkan_vortek.so
"""
import argparse
import ctypes
import json
import mmap
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

SERVER_RING = 4194304          # include/vortek.h: SERVER_RING_BUFFER_SIZE
CLIENT_RING = 262144           # include/vortek.h: CLIENT_RING_BUFFER_SIZE
BUF_OFFSET = 20                # offsetof(struct Offsets, buffer) = 5 * sizeof(atomic_uint)
REQ_CREATE_CONTEXT = 1
REQ_VK_CREATE_INSTANCE = 100

NAME = {0: '无', REQ_CREATE_CONTEXT: 'REQUEST_CODE_CREATE_CONTEXT',
        REQ_VK_CREATE_INSTANCE: 'REQUEST_CODE_VK_CREATE_INSTANCE'}

state = {}


def make_shm(size):
    fd = os.memfd_create('vortek-ring', 0)
    os.ftruncate(fd, size)
    return fd


def send_two_fds(conn, fds):
    payload = b''.join(struct.pack('i', f) for f in fds)
    conn.sendmsg([b'\x00'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, payload)])


def server_thread(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s.bind(path)
    s.listen(1)
    state['listening'] = True
    print('[server] 监听', path, flush=True)

    conn, _ = s.accept()
    state['connected'] = True
    print('[server] ✅ 客户端已 connect', flush=True)

    hdr = b''
    while len(hdr) < 8:
        chunk = conn.recv(8 - len(hdr))
        if not chunk:
            break
        hdr += chunk
    if len(hdr) == 8:
        code, length = struct.unpack('ii', hdr)
        state['handshake'] = (code, length)
        print('[server] ✅ 收到握手 header: code=%d(%s) length=%d'
              % (code, NAME.get(code, '?'), length), flush=True)
    else:
        print('[server] ❌ 握手 header 不完整:', hdr, flush=True)

    fds = [make_shm(SERVER_RING + BUF_OFFSET), make_shm(CLIENT_RING + BUF_OFFSET)]
    send_two_fds(conn, fds)
    state['fds_sent'] = True
    print('[server] ✅ 已通过 SCM_RIGHTS 发出 2 个 fd: server_ring=%d(4MB) client_ring=%d(256KB)'
          % (fds[0], fds[1]), flush=True)

    ring = mmap.mmap(fds[0], SERVER_RING + BUF_OFFSET, mmap.MAP_SHARED,
                     mmap.PROT_READ | mmap.PROT_WRITE)
    mask = SERVER_RING - 1
    deadline = time.time() + 25
    while time.time() < deadline:
        head, tail = struct.unpack_from('II', ring, 0)
        if tail != head:
            off = BUF_OFFSET + (head & mask)
            code, size = struct.unpack_from('ii', ring, off)
            state['request'] = (code, size)
            raw = bytes(ring[off:off + 32])
            print('[server] ✅✅ 客户端从 ring 里发出了调用: code=%d(%s) size=%d'
                  % (code, NAME.get(code, '?'), size), flush=True)
            print('[server]     ring 原始前 32 字节:', raw.hex(), flush=True)
            print('[server] => 结论: glibc 客户端启动链完整可用（connect→握手→收 fd→建 ring→发请求）',
                  flush=True)
            os._exit(0)
        time.sleep(0.05)
    print('[server] ❌ 25 秒内没等到客户端写 ring', flush=True)
    os._exit(2)


def watchdog():
    time.sleep(40)
    print('[watchdog] 超时，state =', state, flush=True)
    os._exit(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('so')
    ap.add_argument('--socket', default='/tmp/vortek_probe.sock')
    ap.add_argument('--via-fallback', action='store_true',
                    help='不设 VORTEK_SOCKET_PATH：把 socket 放在 \$PREFIX/tmp 候选里，TMPDIR 指向空目录')
    args = ap.parse_args()

    so = os.path.abspath(args.so)
    if not os.path.isfile(so):
        print('找不到 .so:', so)
        return 1

    root = tempfile.mkdtemp(prefix='vortekprobe-')
    os.makedirs(os.path.join(root, 'lib'))
    os.makedirs(os.path.join(root, 'share', 'vulkan', 'icd.d'))
    shutil.copy2(so, os.path.join(root, 'lib', 'libvulkan_vortek.so'))
    manifest = {'file_format_version': '1.0.0',
                'ICD': {'library_path': '../../../lib/libvulkan_vortek.so',
                        'api_version': '1.1.128'}}
    mpath = os.path.join(root, 'share', 'vulkan', 'icd.d', 'vortek_icd.relocatable.aarch64.json')
    with open(mpath, 'w') as f:
        json.dump(manifest, f, indent=4)

    if args.via_fallback:
        # 模拟真实拓扑：服务端在 \$PREFIX/tmp，而客户端的 TMPDIR 是另一个目录
        prefix = tempfile.mkdtemp(prefix='vortekprefix-')
        empty = tempfile.mkdtemp(prefix='vortekempty-')   # 故意空的 TMPDIR
        args.socket = os.path.join(prefix, 'tmp', '.vortek', 'V0')
        os.environ['PREFIX'] = prefix
        os.environ['TMPDIR'] = empty
        os.environ.pop('VORTEK_SOCKET_PATH', None)
    else:
        os.environ['VORTEK_SOCKET_PATH'] = args.socket
    os.environ['VK_ICD_FILENAMES'] = mpath
    os.environ.setdefault('VK_LOADER_DEBUG', 'error,warn,driver')
    print('[probe] socket =', args.socket)
    print('[probe] manifest =', mpath)
    print('[probe] .so =', so, flush=True)

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=server_thread, args=(args.socket,), daemon=True).start()
    while not state.get('listening'):
        time.sleep(0.05)

    vk = ctypes.CDLL('libvulkan.so.1')
    ai = (ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
          ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32)

    class AI(ctypes.Structure):
        _fields_ = [('sType', ctypes.c_int), ('pNext', ctypes.c_void_p),
                    ('pApp', ctypes.c_char_p), ('appVer', ctypes.c_uint32),
                    ('pEng', ctypes.c_char_p), ('engVer', ctypes.c_uint32),
                    ('api', ctypes.c_uint32)]

    class CI(ctypes.Structure):
        _fields_ = [('sType', ctypes.c_int), ('pNext', ctypes.c_void_p),
                    ('flags', ctypes.c_uint32), ('pAI', ctypes.POINTER(AI)),
                    ('lc', ctypes.c_uint32), ('pl', ctypes.c_void_p),
                    ('ec', ctypes.c_uint32), ('pe', ctypes.c_void_p)]

    inst = ctypes.c_void_p()
    print('[probe] 调用 vkCreateInstance（会阻塞在等服务端响应）...', flush=True)
    res = vk.vkCreateInstance(ctypes.byref(CI(1, None, 0, ctypes.cast(
        ctypes.byref(AI(0, None, b'p', 1, b'e', 1, (1 << 22) | (1 << 12))),
        ctypes.POINTER(AI)), 0, None, 0, None)), None, ctypes.byref(inst))
    print('[probe] vkCreateInstance 返回', res, '（能返回说明服务端没响应也没崩）', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())