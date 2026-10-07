#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实的 Vulkan loader 验证 ICD 的注册与加载（不需要 GPU、不需要设备）。

用法:
    python3 tools/icd_load_probe.py <libvulkan_vortek.so> [relative|absolute]

做两件事:
  1) 准备一棵临时的「安装树」:
         <tmp>/lib/libvulkan_vortek.so
         <tmp>/share/vulkan/icd.d/vortek_icd.aarch64.json
  2) 设 VK_ICD_FILENAMES 指向该 manifest，调用真实的 vkCreateInstance /
     vkEnumeratePhysicalDevices，观察 loader 是否真的 dlopen 到了我们的 .so。

relative 模式把 library_path 写成相对 manifest 的路径（../../lib/...），
用来验证「可重定位安装」是否成立；absolute 模式写绝对路径。
"""
import ctypes
import json
import os
import shutil
import sys
import tempfile

VK_MAKE_VERSION = lambda ma, mi, pa: (ma << 22) | (mi << 12) | pa


class VkApplicationInfo(ctypes.Structure):
    _fields_ = [
        ('sType', ctypes.c_int), ('pNext', ctypes.c_void_p),
        ('pApplicationName', ctypes.c_char_p), ('applicationVersion', ctypes.c_uint32),
        ('pEngineName', ctypes.c_char_p), ('engineVersion', ctypes.c_uint32),
        ('apiVersion', ctypes.c_uint32),
    ]


class VkInstanceCreateInfo(ctypes.Structure):
    _fields_ = [
        ('sType', ctypes.c_int), ('pNext', ctypes.c_void_p), ('flags', ctypes.c_uint32),
        ('pApplicationInfo', ctypes.POINTER(VkApplicationInfo)),
        ('enabledLayerCount', ctypes.c_uint32), ('ppEnabledLayerNames', ctypes.c_void_p),
        ('enabledExtensionCount', ctypes.c_uint32), ('ppEnabledExtensionNames', ctypes.c_void_p),
    ]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    so = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else 'relative'
    if not os.path.isfile(so):
        print('找不到 .so:', so)
        return 1

    root = tempfile.mkdtemp(prefix='icdprobe-')
    libdir = os.path.join(root, 'lib')
    icddir = os.path.join(root, 'share', 'vulkan', 'icd.d')
    os.makedirs(libdir)
    os.makedirs(icddir)
    shutil.copy2(so, os.path.join(libdir, 'libvulkan_vortek.so'))

    # 注意层级：manifest 在 <root>/share/vulkan/icd.d/，到 <root>/lib/ 需要三层 ../
    lib_path = '../../../lib/libvulkan_vortek.so' if mode == 'relative' else os.path.join(libdir, 'libvulkan_vortek.so')
    manifest = {'file_format_version': '1.0.0',
                'ICD': {'library_path': lib_path, 'api_version': '1.1.128'}}
    mpath = os.path.join(icddir, 'vortek_icd.aarch64.json')
    with open(mpath, 'w') as f:
        json.dump(manifest, f, indent=4)

    print('== 探针模式:', mode)
    print('== manifest:', mpath)
    print('== library_path:', lib_path)
    print('== 解析结果:', os.path.normpath(os.path.join(icddir, lib_path))
          if mode == 'relative' else lib_path)

    os.environ['VK_ICD_FILENAMES'] = mpath
    os.environ.setdefault('VK_LOADER_DEBUG', 'all')

    vk = ctypes.CDLL('libvulkan.so.1')
    ai = VkApplicationInfo(0, None, b'icd-probe', 1, b'icd-probe', 1, VK_MAKE_VERSION(1, 1, 0))
    ci = VkInstanceCreateInfo(1, None, 0,
                              ctypes.cast(ctypes.byref(ai), ctypes.POINTER(VkApplicationInfo)),
                              0, None, 0, None)
    inst = ctypes.c_void_p()
    res = vk.vkCreateInstance(ctypes.byref(ci), None, ctypes.byref(inst))
    print('== vkCreateInstance ->', res)
    if res == 0:
        cnt = ctypes.c_uint32()
        r2 = vk.vkEnumeratePhysicalDevices(inst, ctypes.byref(cnt), None)
        print('== vkEnumeratePhysicalDevices ->', r2, 'devices =', cnt.value)
    print('== 临时树:', root)
    return 0


if __name__ == '__main__':
    sys.exit(main())