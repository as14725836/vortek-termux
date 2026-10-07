#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wine 进程任务管理器（PyQt5）—— 单文件，面向 Termux / Termux:X11。

对齐 WinNative/Winlator 任务管理器的功能面，且**不依赖 guest 侧模块**：
  * 顶部统计: 全局 CPU% + 每核心 CPU% + 内存占用/总量（WinNative 的 TaskManagerHeader）
  * 进程表  : PID / Windows 程序名 / 架构(wow64) / 内存 RSS / CPU% / 状态 / CPU 亲和
             + 父子树缩进（wineserver → *.exe 的层次一眼看出来）
  * 控制    : 挂起(SIGSTOP) 恢复(SIGCONT) 结束(SIGTERM) 强杀(SIGKILL)
             / 亲和性勾选（每个核心一个复选框，对应 WinNative 的 affinity mask）
             / 新建任务（在 Termux 侧起命令，如 `wine app.exe`）
  * 定时刷新（默认 1s），只作用于当前 uid 的进程

名称/架构怎么来的（无需 guest）:
  /proc/<pid>/exe 是真实路径（如 .../drive_c/Games/xxx.exe），
  读它的 PE 头 machine 字段 → i386 / x86_64 / ARM64，
  i386 在 64 位 Wine 下即为 wow64。

用法:
  python3 wine_task_manager.py                  # 只看 wine 相关进程
  python3 wine_task_manager.py --all            # 当前 uid 全部进程
  python3 wine_task_manager.py --no-tree        # 关掉树缩进（平铺）
  python3 wine_task_manager.py --dump           # 不开窗口，打印统计+进程表
  QT_QPA_PLATFORM=offscreen python3 wine_task_manager.py --self-test

依赖: PyQt5（Termux 原生 `pkg install python-pyqt5`；glibc 前缀 `pacman -S python-pyqt5`）
显示: DISPLAY=:0（Termux:X11）
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import struct
import subprocess
import sys
import time

CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

WINE_PATTERNS = re.compile(
    r'(wine|wineserver|winhandler|winedevice|plugplay|services\.exe|explorer\.exe|'
    r'conhost|start\.exe|steam|proton|\.exe$|\.exe\b)', re.I)

# PE machine -> 展示名
PE_MACHINE = {
    0x014c: 'i386',
    0x8664: 'x86_64',
    0x01c0: 'ARM',
    0x01c4: 'ARMv7',
    0xaa64: 'ARM64',
}


# ==========================================================================
# 采数层（不依赖 Qt）
# ==========================================================================
class Proc:
    __slots__ = ('pid', 'ppid', 'comm', 'cmdline', 'exe', 'arch', 'wow64', 'pe',
                 'rss_kb', 'state', 'utime', 'stime', 'affinity', 'cpu', 'depth')

    def __init__(self):
        self.pid = 0
        self.ppid = 0
        self.comm = ''
        self.cmdline = ''
        self.exe = ''
        self.arch = ''
        self.wow64 = False
        self.pe = False
        self.rss_kb = 0
        self.state = '?'
        self.utime = 0
        self.stime = 0
        self.affinity = None
        self.cpu = 0.0
        self.depth = 0

    @property
    def display(self):
        return self.exe or self.comm or self.cmdline or ('pid %d' % self.pid)


def _read(path, limit=4096):
    try:
        with open(path, 'rb') as f:
            return f.read(limit)
    except (OSError, PermissionError):
        return b''


def read_cmdline(pid):
    raw = _read('/proc/%d/cmdline' % pid, 8192)
    if not raw:
        return ''
    return ' '.join(p.decode('utf-8', 'replace') for p in raw.split(b'\0') if p)


def read_stat(pid):
    """-> (comm, state, ppid, utime, stime) 或 None"""
    raw = _read('/proc/%d/stat' % pid, 4096)
    if not raw:
        return None
    try:
        s = raw.decode('utf-8', 'replace')
        lp = s.index('(')
        rp = s.rindex(')')
        comm = s[lp + 1:rp]
        rest = s[rp + 2:].split()
        return comm, rest[0], int(rest[1]), int(rest[11]), int(rest[12])
    except (ValueError, IndexError):
        return None


def read_rss_kb(pid):
    raw = _read('/proc/%d/statm' % pid, 256)
    if raw:
        try:
            return int(raw.split()[1]) * (os.sysconf('SC_PAGE_SIZE') // 1024)
        except (ValueError, IndexError):
            pass
    st = _read('/proc/%d/status' % pid, 8192).decode('utf-8', 'replace')
    m = re.search(r'VmRSS:\s+(\d+)\s+kB', st)
    return int(m.group(1)) if m else 0


def read_affinity(pid):
    try:
        return os.sched_getaffinity(pid)
    except (OSError, PermissionError, AttributeError):
        return None


_pe_cache = {}          # pid -> (exe_path, arch, wow64, pe, ts)


def parse_pe(data):
    """从文件头字节解出 (架构名, 是否 PE, 是否 wow64)。独立函数，方便单测。"""
    if data[:2] != b'MZ' or len(data) < 0x40:
        if data[:4] == b'\x7fELF':
            return 'ELF-%d' % (64 if data[4] == 2 else 32), False, False
        return '', False, False
    try:
        e_lfanew = struct.unpack_from('<I', data, 0x3C)[0]
        if e_lfanew + 26 > len(data) or data[e_lfanew:e_lfanew + 4] != b'PE\0\0':
            return '', False, False
        machine = struct.unpack_from('<H', data, e_lfanew + 4)[0]
        arch = PE_MACHINE.get(machine, '0x%04x' % machine)
        return arch, True, arch == 'i386'
    except (struct.error, IndexError):
        return '', False, False


def read_pe_info(pid):
    """读 /proc/<pid>/exe：返回 (路径, 架构名, 是否 wow64, 是否 PE)。

    Wine 进程的 /proc/<pid>/exe 通常直接指向 drive_c 里的真实 exe，
    所以这就是"Windows 程序名"的来源。PE 头给出 machine，用来区分
    32/64 位；i386 跑在 64 位 Wine 下即 wow64。
    """
    now = time.time()
    hit = _pe_cache.get(pid)
    if hit and now - hit[4] < 2.0:
        return hit[:4]
    path = ''
    try:
        path = os.readlink('/proc/%d/exe' % pid)
    except (OSError, PermissionError):
        pass
    arch, wow64, is_pe = ('', False, False)
    if path:
        # 注意：/proc/<pid>/exe 不是普通文件，用读方式打开随 tag 即可
        arch, is_pe, wow64 = parse_pe(_read('/proc/%d/exe' % pid, 4096))
    _pe_cache[pid] = (path, arch, wow64, is_pe, now)
    return path, arch, wow64, is_pe


def iter_pids():
    try:
        for entry in os.listdir('/proc'):
            if entry.isdigit():
                yield int(entry)
    except OSError:
        return


def snapshot(rx, uid_only=True, want_pe=True):
    my_uid = os.getuid()
    out = []
    for pid in iter_pids():
        if uid_only:
            try:
                if os.stat('/proc/%d' % pid).st_uid != my_uid:
                    continue
            except OSError:
                continue
        st = read_stat(pid)
        if not st:
            continue
        comm, state, ppid, utime, stime = st
        cmdline = read_cmdline(pid)
        p = Proc()
        p.pid, p.ppid, p.comm, p.cmdline = pid, ppid, comm, cmdline
        p.state, p.utime, p.stime = state, utime, stime
        if want_pe:
            p.exe, p.arch, p.wow64, p.pe = read_pe_info(pid)
        p.rss_kb = read_rss_kb(pid)
        p.affinity = read_affinity(pid)
        if rx is not None:
            blob = ' '.join((p.comm, p.cmdline, p.exe or ''))
            if not rx.search(blob):
                continue
        out.append(p)
    out.sort(key=lambda x: x.rss_kb, reverse=True)
    return out


def arrange_tree(procs):
    """按 ppid 组树：父在前、子缩进，无关进程放后面。"""
    by_parent = {}
    for p in procs:
        by_parent.setdefault(p.ppid, []).append(p)
    known = {p.pid for p in procs}
    ordered = []
    seen = set()

    def walk(node, depth):
        if node.pid in seen:
            return
        seen.add(node.pid)
        node.depth = depth
        ordered.append(node)
        for child in by_parent.get(node.pid, []):
            walk(child, depth + 1)

    for p in procs:
        # 根：父不在集合里（wineserver / 顶层 wine 进程）
        if p.ppid not in known:
            walk(p, 0)
    for p in procs:
        walk(p, 0)
    return ordered


def compute_cpu(new_list, prev_map, elapsed):
    if elapsed <= 0:
        return
    for p in new_list:
        prev = prev_map.get(p.pid)
        if prev is not None:
            d = (p.utime - prev.utime) + (p.stime - prev.stime)
            p.cpu = max(0.0, d / CLK_TCK / elapsed * 100.0)


# ---------------- 全局统计（WinNative 头部那一排） ----------------
class Sampler:
    """全局/每核心 CPU 与内存采样，靠两次差值算占用率。"""

    def __init__(self):
        self.prev_total = None
        self.prev_cores = []
        self.cpu_percent = 0.0
        self.core_percent = []
        self.mem_used_kb = 0
        self.mem_total_kb = 0
        self.mem_percent = 0.0
        self.note = ''            # /proc 读不到时的提示（例如 proot 里 /proc/stat 被拦）

    def sample(self):
        # /proc/stat 第一行是总量，后面是 cpu0..cpuN
        raw = _read('/proc/stat', 65536).decode('utf-8', 'replace')
        self.note = '' if raw else '读不到 /proc/stat（权限受限），全局/每核心 CPU 不可用'
        total, cores = None, []
        for line in raw.splitlines():
            if not line.startswith('cpu'):
                continue
            parts = line.split()
            vals = [int(x) for x in parts[1:9]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            busy = sum(vals) - idle
            if parts[0] == 'cpu':
                total = (busy, idle)
            else:
                cores.append((busy, idle))
        if total:
            if self.prev_total:
                db = total[0] - self.prev_total[0]
                di = total[1] - self.prev_total[1]
                self.cpu_percent = 100.0 * db / max(1, db + di)
            self.prev_total = total
        if cores and len(cores) == len(self.prev_cores):
            pct = []
            for (b, i), (pb, pi) in zip(cores, self.prev_cores):
                db, di = b - pb, i - pi
                pct.append(100.0 * db / max(1, db + di))
            self.core_percent = pct
        self.prev_cores = cores
        # /proc/meminfo
        mi = _read('/proc/meminfo', 8192).decode('utf-8', 'replace')
        vals = {k: int(v) for k, v in re.findall(r'^(\w+):\s+(\d+) kB', mi, re.M)}
        self.mem_total_kb = vals.get('MemTotal', 0)
        avail = vals.get('MemAvailable', vals.get('MemFree', 0))
        self.mem_used_kb = max(0, self.mem_total_kb - avail)
        self.mem_percent = (100.0 * self.mem_used_kb / self.mem_total_kb
                            if self.mem_total_kb else 0.0)


def fmt_mem(kb):
    if kb >= 1024 * 1024:
        return '%.2f G' % (kb / 1024 / 1024)
    if kb >= 1024:
        return '%.1f M' % (kb / 1024)
    return '%d K' % kb


def fmt_affinity(mask):
    if mask is None:
        return '?'
    return ','.join(str(c) for c in sorted(mask)) if mask else '-'


def header_line(s: Sampler):
    cores = ' '.join('%d:%.0f%%' % (i, v) for i, v in enumerate(s.core_percent))
    return ('CPU %.1f%%  [%s]   内存 %s / %s (%.0f%%)'
            % (s.cpu_percent, cores or '-', fmt_mem(s.mem_used_kb),
               fmt_mem(s.mem_total_kb), s.mem_percent) +
            (('   ⚠ ' + s.note) if s.note else ''))


def fmt_row(p: Proc):
    return '%-7d %s%-30s %-7s %9s %6.1f%% %-3s %s' % (
        p.pid, '  ' * p.depth, p.display[:30], p.arch or '?', fmt_mem(p.rss_kb),
        p.cpu, p.state, fmt_affinity(p.affinity))


# ==========================================================================
# PyQt5 GUI
# ==========================================================================
def run_gui(args):
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QColor, QFont
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox,
                                 QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                                 QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                                 QVBoxLayout, QWidget)

    COLS = ['PID', '进程', '架构', '内存', 'CPU%', '状态', 'CPU 亲和']

    class TaskManager(QWidget):
        def __init__(self):
            super().__init__()
            self.rx = None if args.all else (
                re.compile(args.filter) if args.filter else WINE_PATTERNS)
            self.sampler = Sampler()
            self.prev = {}
            self.prev_time = time.time()
            self.last_rows = []
            self.setWindowTitle('Wine 进程管理器')
            self.setStyleSheet("""
                QWidget { background:#14161a; color:#e6e6e6; font-size:13px; }
                QTableWidget { background:#1b1e24; gridline-color:#2a2f38;
                               selection-background-color:#2f6fb3; }
                QHeaderView::section { background:#22262e; color:#cfd6e0; border:0; padding:4px; }
                QPushButton { background:#2a2f38; border:1px solid #3a414d; padding:6px 10px;
                              border-radius:6px; }
                QPushButton:hover { background:#333a45; }
                QPushButton:pressed { background:#1f242b; }
                QPushButton#danger { background:#5a2226; border-color:#7a2f34; }
                QLineEdit { background:#1b1e24; border:1px solid #3a414d; padding:4px;
                            border-radius:4px; }
            """)
            if args.frameless:
                self.setWindowFlags(Qt.FramelessWindowHint)

            # ---- 头部：全局 CPU / 每核心 / 内存 ----
            self.headerCpu = QLabel('CPU -')
            self.headerCpu.setStyleSheet('font-size:15px; color:#8fd3ff;')
            self.headerCores = QLabel('')
            self.headerCores.setStyleSheet('color:#93a1b0;')
            self.headerMem = QLabel('内存 -')
            self.headerMem.setStyleSheet('color:#b7e3a1;')

            self.table = QTableWidget(0, len(COLS))
            self.table.setHorizontalHeaderLabels(COLS)
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.setSelectionMode(QTableWidget.SingleSelection)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setShowGrid(False)
            hh = self.table.horizontalHeader()
            hh.setSectionResizeMode(1, QHeaderView.Stretch)
            for i in (0, 2, 3, 4, 5, 6):
                hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)

            self.search = QLineEdit()
            self.search.setPlaceholderText('过滤名称/路径/命令行…')
            self.search.textChanged.connect(lambda *_: self.refresh())
            self.status = QLabel('就绪')
            self.summary = QLabel('')
            self.summary.setStyleSheet('color:#93a1b0;')

            def btn(text, slot, danger=False):
                b = QPushButton(text)
                if danger:
                    b.setObjectName('danger')
                b.clicked.connect(slot)
                return b

            bar = QHBoxLayout()
            bar.addWidget(btn('刷新', self.refresh))
            bar.addWidget(btn('挂起', lambda: self.signal_selected(signal.SIGSTOP, '挂起')))
            bar.addWidget(btn('恢复', lambda: self.signal_selected(signal.SIGCONT, '恢复')))
            bar.addWidget(btn('结束', lambda: self.signal_selected(signal.SIGTERM, '结束')))
            bar.addWidget(btn('强杀', lambda: self.signal_selected(signal.SIGKILL, '强杀'), True))
            bar.addWidget(btn('亲和性…', self.edit_affinity))
            bar.addWidget(btn('新建任务…', self.new_task))
            bar.addStretch(1)

            root = QVBoxLayout(self)
            root.addWidget(self.headerCpu)
            root.addWidget(self.headerCores)
            root.addWidget(self.headerMem)
            root.addLayout(bar)
            root.addWidget(self.search)
            root.addWidget(self.summary)
            root.addWidget(self.table, 1)
            root.addWidget(self.status)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.timer.start(int(args.interval * 1000))
            self.refresh()

        # ------------- 数据 -------------
        def refresh(self):
            self.sampler.sample()
            now = time.time()
            rows = snapshot(self.rx, uid_only=not args.all_uid)
            text = self.search.text().strip().lower()
            if text:
                rows = [p for p in rows if text in ' '.join(
                    (p.comm, p.cmdline, p.exe or '')).lower()]
            compute_cpu(rows, self.prev, now - self.prev_time)
            self.prev = {p.pid: p for p in rows}
            self.prev_time = now
            if not args.no_tree:
                rows = arrange_tree(rows)
            self.last_rows = rows
            self.render(rows)

        def render(self, rows):
            s = self.sampler
            self.headerCpu.setText('CPU %.1f%%' % s.cpu_percent)
            core_txt = '  '.join('%d:%.0f%%' % (i, v) for i, v in enumerate(s.core_percent))
            self.headerCores.setText(((core_txt + '  ') if core_txt else '') + s.note)
            self.headerMem.setText('内存 %s / %s  (%.0f%%)' % (
                fmt_mem(s.mem_used_kb), fmt_mem(s.mem_total_kb), s.mem_percent))

            sel = self.selected_pid()
            self.table.setRowCount(len(rows))
            for r, p in enumerate(rows):
                arch = p.arch or '?'
                if p.wow64:
                    arch += '·wow64'
                cells = ['%d' % p.pid, ('  ' * p.depth) + p.display[:64], arch,
                         fmt_mem(p.rss_kb), '%.1f' % p.cpu, p.state,
                         fmt_affinity(p.affinity)]
                for c, v in enumerate(cells):
                    it = QTableWidgetItem(v)
                    if c in (0, 4):
                        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if c == 4 and p.cpu >= 25:
                        it.setForeground(QColor('#ffb454'))
                    if c == 2 and p.wow64:
                        it.setForeground(QColor('#c792ea'))
                    self.table.setItem(r, c, it)
                if p.pid == sel:
                    self.table.selectRow(r)
            self.summary.setText('%d 个进程 · 合计 %s' % (
                len(rows), fmt_mem(sum(p.rss_kb for p in rows))))

        # ------------- 操作 -------------
        def selected_pid(self):
            r = self.table.currentRow()
            if r < 0 or r >= len(self.last_rows):
                return None
            return self.last_rows[r].pid

        def signal_selected(self, sig, label):
            pid = self.selected_pid()
            if pid is None:
                self.status.setText('先选中一个进程')
                return
            try:
                os.kill(pid, sig)
                self.status.setText('%s 已发送给 PID %d' % (label, pid))
            except OSError as e:
                self.status.setText('失败: %s' % e)

        def edit_affinity(self):
            pid = self.selected_pid()
            if pid is None:
                self.status.setText('先选中一个进程')
                return
            n = os.cpu_count() or 1
            cur = read_affinity(pid) or set(range(n))
            dlg = QDialog(self)
            dlg.setWindowTitle('CPU 亲和性 — PID %d' % pid)
            v = QVBoxLayout(dlg)
            row = QHBoxLayout()
            boxes = []
            for i in range(n):
                cb = QCheckBox(str(i))
                cb.setChecked(i in cur)
                boxes.append(cb)
                row.addWidget(cb)
            v.addLayout(row)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            v.addWidget(bb)
            if dlg.exec_() == QDialog.Accepted:
                mask = {i for i, cb in enumerate(boxes) if cb.isChecked()}
                if not mask:
                    QMessageBox.warning(self, '亲和性', '至少要选一个核心')
                    return
                try:
                    os.sched_setaffinity(pid, mask)
                    self.status.setText('PID %d 亲和性 -> %s' % (pid, fmt_affinity(mask)))
                except OSError as e:
                    self.status.setText('设置亲和失败: %s' % e)
                self.refresh()

        def new_task(self):
            cmd, ok = QInputDialog.getText(self, '新建任务', '命令（在 Termux 侧执行）:',
                                           QLineEdit.Normal, 'wine ')
            if not ok or not cmd.strip():
                return
            try:
                subprocess.Popen(cmd, shell=True, start_new_session=True)
                self.status.setText('已启动: %s' % cmd)
            except OSError as e:
                self.status.setText('启动失败: %s' % e)

    app = QApplication(sys.argv)
    app.setFont(QFont('DejaVu Sans', 11))
    w = TaskManager()
    w.resize(args.width, args.height)
    w.show()
    if args.self_test:
        QTimer.singleShot(1200, lambda: (
            print('SELF_TEST_OK rows=%d cpu=%.1f%% mem=%.0f%% cores=%d'
                  % (len(w.last_rows), w.sampler.cpu_percent, w.sampler.mem_percent,
                     len(w.sampler.core_percent))), app.quit()))
    return app.exec_()


def run_dump(args):
    rx = None if args.all else (re.compile(args.filter) if args.filter else WINE_PATTERNS)
    s = Sampler()
    prev, t0 = {}, time.time()
    for i in range(max(1, args.rounds)):
        s.sample()
        rows = snapshot(rx, uid_only=not args.all_uid)
        now = time.time()
        compute_cpu(rows, prev, now - t0)
        prev, t0 = {p.pid: p for p in rows}, now
        if not args.no_tree:
            rows = arrange_tree(rows)
        if i:
            print()
        print(header_line(s))
        print('PID     进程                             架构    内存       CPU%  状态 CPU 亲和')
        print('-' * 92)
        for p in rows:
            print(fmt_row(p))
        print('合计 %d 个进程，RSS %s' % (len(rows), fmt_mem(sum(p.rss_kb for p in rows))))
        if i + 1 < args.rounds:
            time.sleep(args.interval)
    return 0


def main():
    ap = argparse.ArgumentParser(description='Wine 进程任务管理器（PyQt5）')
    ap.add_argument('--all', action='store_true', help='不按 wine 过滤，列出全部进程')
    ap.add_argument('--filter', default='', help='自定义过滤正则（名称/路径/命令行）')
    ap.add_argument('--interval', type=float, default=1.0, help='刷新间隔秒')
    ap.add_argument('--all-uid', action='store_true', help='不过滤 uid')
    ap.add_argument('--no-tree', action='store_true', help='关闭父子树缩进')
    ap.add_argument('--dump', action='store_true', help='不开窗口，打印统计与进程表')
    ap.add_argument('--rounds', type=int, default=1, help='--dump 采样轮数')
    ap.add_argument('--self-test', action='store_true', help='建窗口→刷新→退出')
    ap.add_argument('--frameless', action='store_true', help='无边框（Termux:X11 全屏用）')
    ap.add_argument('--width', type=int, default=780)
    ap.add_argument('--height', type=int, default=560)
    args = ap.parse_args()

    if args.dump:
        return run_dump(args)
    try:
        import PyQt5  # noqa: F401
    except ImportError:
        print('未安装 PyQt5。Termux 原生: pkg install python-pyqt5；'
              'glibc 前缀: pacman -S python-pyqt5\n'
              '（只看列表可用 --dump，不需要 PyQt5）', file=sys.stderr)
        return 2
    return run_gui(args)


if __name__ == '__main__':
    sys.exit(main())