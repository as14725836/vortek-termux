#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wine 进程任务管理器（PyQt5）—— 单文件，专为 Termux / Termux:X11 这类小屏环境写。

设计参考了 WinNative/Winlator 的任务管理器，但**不依赖 guest 侧**：
本工具跑在 Termux 原生（bionic）侧，直接看 `/proc`，所以能看到的进程
和服务端 `vortekrenderer-cli` 看到的是同一批（wine / wineserver / *.exe 等）。

功能:
  * 进程表: PID / 名称 / 内存(RSS) / CPU% / 位数 / 状态 / CPU 亲和
  * 结束（SIGTERM，可强制 SIGKILL）/ 挂起（SIGSTOP）/ 恢复（SIGCONT）
  * 逐个进程设置 CPU 亲和（勾选核心）
  * 新建任务（在 Termux 侧起一条命令，例如 `wine app.exe`）
  * 定时刷新（默认 1s），只对当前 uid 的进程生效

用法:
  python3 wine_task_manager.py                 # 只看 wine 相关进程
  python3 wine_task_manager.py --all           # 当前 uid 的全部进程
  python3 wine_task_manager.py --filter 'proton|wine'
  python3 wine_task_manager.py --dump          # 不开窗口，打印一张表（可在无 X 环境下验证）
  QT_QPA_PLATFORM=offscreen python3 wine_task_manager.py --self-test   # 自检（CI 里可用）

依赖: PyQt5。Termux 原生: `pkg install python-pyqt5 x11-repo termux-x11`；
      glibc 前缀: `pacman -S python-pyqt5`（具体包名以你的源为准）。
显示: `DISPLAY=:0`（Termux:X11）。
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time

CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100

# 和 Winlator/WinNative 的 SESSION_PROCESS_FILTERS 思路一致：认这些名字
WINE_PATTERNS = re.compile(
    r'(wine|wineserver|winhandler|winedevice|plugplay|services\.exe|explorer\.exe|'
    r'conhost|start\.exe|steam|proton|\.exe$|\.exe\b)', re.I)


# --------------------------------------------------------------------------
# /proc 读取（不依赖 Qt，所以 --dump 在没有 PyQt5 的机器上也能跑）
# --------------------------------------------------------------------------
class Proc:
    __slots__ = ('pid', 'name', 'cmdline', 'rss_kb', 'bits', 'state',
                 'utime', 'stime', 'affinity', 'cpu')

    def __init__(self, pid, name, cmdline, rss_kb, bits, state, utime, stime, affinity):
        self.pid = pid
        self.name = name
        self.cmdline = cmdline
        self.rss_kb = rss_kb
        self.bits = bits
        self.state = state
        self.utime = utime
        self.stime = stime
        self.affinity = affinity
        self.cpu = 0.0


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
    parts = [p.decode('utf-8', 'replace') for p in raw.split(b'\0') if p]
    return ' '.join(parts)


def read_stat(pid):
    """返回 (comm, state, utime, stime)，解析 /proc/<pid>/stat（注意 comm 里有括号）。"""
    raw = _read('/proc/%d/stat' % pid, 4096)
    if not raw:
        return None
    try:
        s = raw.decode('utf-8', 'replace')
        lp = s.index('(')
        rp = s.rindex(')')
        comm = s[lp + 1:rp]
        rest = s[rp + 2:].split()
        state = rest[0]
        utime = int(rest[11])
        stime = int(rest[12])
        return comm, state, utime, stime
    except (ValueError, IndexError):
        return None


def read_rss_kb(pid):
    raw = _read('/proc/%d/statm' % pid, 256)
    if raw:
        try:
            # statm: size resident shared ... (页)
            resident = int(raw.split()[1])
            return resident * (os.sysconf('SC_PAGE_SIZE') // 1024)
        except (ValueError, IndexError):
            pass
    st = _read('/proc/%d/status' % pid, 8192).decode('utf-8', 'replace')
    m = re.search(r'VmRSS:\s+(\d+)\s+kB', st)
    return int(m.group(1)) if m else 0


def read_bits(pid):
    """32/64 位：读 /proc/<pid>/exe 的 ELF class（1=32bit, 2=64bit）。"""
    try:
        with open('/proc/%d/exe' % pid, 'rb') as f:
            magic = f.read(5)
        if magic[:4] == b'\x7fELF':
            return 64 if magic[4] == 2 else 32
    except (OSError, PermissionError):
        pass
    return 0


def read_affinity(pid):
    try:
        return os.sched_getaffinity(pid)
    except (OSError, PermissionError, AttributeError):
        return None


def iter_pids():
    try:
        for entry in os.listdir('/proc'):
            if entry.isdigit():
                yield int(entry)
    except OSError:
        return


def match(p: Proc, rx: re.Pattern) -> bool:
    return bool(rx.search(p.name or '') or rx.search(p.cmdline or ''))


def snapshot(rx: re.Pattern | None, uid_only: bool = True) -> list[Proc]:
    """采集一次进程快照。rx=None 表示全要。"""
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
        comm, state, utime, stime = st
        cmdline = read_cmdline(pid)
        p = Proc(pid, comm, cmdline or comm, read_rss_kb(pid), read_bits(pid),
                 state, utime, stime, read_affinity(pid))
        if rx is not None and not match(p, rx):
            continue
        out.append(p)
    out.sort(key=lambda x: x.rss_kb, reverse=True)
    return out


def compute_cpu(new_list, prev_map, elapsed):
    """CPU% = 进程 CPU 时间增量 / 墙钟时间（按单核 100% 计），和 top 的思路一致。"""
    if elapsed <= 0:
        return
    for p in new_list:
        prev = prev_map.get(p.pid)
        if prev is not None:
            d = (p.utime - prev.utime) + (p.stime - prev.stime)
            p.cpu = max(0.0, d / CLK_TCK / elapsed * 100.0)


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


def fmt_row(p: Proc):
    return '%-7d %-28s %9s %6.1f%% %3s %-3s %s' % (
        p.pid, (p.name or '')[:28], fmt_mem(p.rss_kb), p.cpu,
        ('%d' % p.bits) if p.bits else '?', p.state, fmt_affinity(p.affinity))


# --------------------------------------------------------------------------
# PyQt5 GUI
# --------------------------------------------------------------------------
def run_gui(args):
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QFont, QColor
    from PyQt5.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                                 QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
                                 QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                                 QWidget)

    COLS = ['PID', '名称', '内存', 'CPU%', '位数', '状态', 'CPU 亲和']

    class TaskManager(QWidget):
        def __init__(self):
            super().__init__()
            self.rx = None if args.all else (
                re.compile(args.filter) if args.filter else WINE_PATTERNS)
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
                QLineEdit { background:#1b1e24; border:1px solid #3a414d; padding:4px; border-radius:4px; }
            """)
            if args.frameless:
                self.setWindowFlags(Qt.FramelessWindowHint)

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

            self.status = QLabel('就绪')
            self.summary = QLabel('')
            self.summary.setStyleSheet('color:#93a1b0;')

            self.search = QLineEdit()
            self.search.setPlaceholderText('过滤名称/命令行…')
            self.search.textChanged.connect(lambda *_: self.refresh())

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
            root.addLayout(bar)
            root.addWidget(self.search)
            root.addWidget(self.summary)
            root.addWidget(self.table, 1)
            root.addWidget(self.status)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.timer.start(int(args.interval * 1000))
            self.refresh()

        # ---------------- 数据 ----------------
        def refresh(self):
            text = self.search.text().strip().lower()
            rows = snapshot(self.rx, uid_only=not args.all_uid)
            if text:
                rows = [p for p in rows
                        if text in (p.name or '').lower() or text in (p.cmdline or '').lower()]
            now = time.time()
            compute_cpu(rows, self.prev, now - self.prev_time)
            self.prev = {p.pid: p for p in rows}
            self.prev_time = now
            self.last_rows = rows
            self.render(rows)

        def render(self, rows):
            sel = self.selected_pid()
            self.table.setRowCount(len(rows))
            for r, p in enumerate(rows):
                cells = ['%d' % p.pid, (p.name or '')[:64], fmt_mem(p.rss_kb),
                         '%.1f' % p.cpu, ('%d' % p.bits) if p.bits else '?',
                         p.state, fmt_affinity(p.affinity)]
                for c, v in enumerate(cells):
                    it = QTableWidgetItem(v)
                    if c in (0, 3):
                        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if c == 3 and p.cpu >= 25:
                        it.setForeground(QColor('#ffb454'))
                    self.table.setItem(r, c, it)
                if p.pid == sel:
                    self.table.selectRow(r)
            total = sum(p.rss_kb for p in rows)
            self.summary.setText('%d 个进程 · 合计 %s' % (len(rows), fmt_mem(total)))

        # ---------------- 选中 ----------------
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
            boxes = []
            row = QHBoxLayout()
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
        # 跑一轮刷新后自动退出（可在 offscreen 下验证代码路径）
        QTimer.singleShot(1200, lambda: (print('SELF_TEST_OK rows=%d' % len(w.last_rows)),
                                         app.quit()))
    return app.exec_()


def run_dump(args, rounds=1):
    rx = None if args.all else (re.compile(args.filter) if args.filter else WINE_PATTERNS)
    print('PID     名称                             内存       CPU%  位  状态 CPU 亲和')
    print('-' * 78)
    for i in range(rounds):
        rows = snapshot(rx, uid_only=not args.all_uid)
        now = time.time()
        compute_cpu(rows, getattr(run_dump, '_prev', {}), now - getattr(run_dump, '_t', now))
        run_dump._prev = {p.pid: p for p in rows}
        run_dump._t = now
        if i:
            print()
        for p in rows:
            print(fmt_row(p))
        if i + 1 < rounds:
            time.sleep(args.interval)
    return 0


def main():
    ap = argparse.ArgumentParser(description='Wine 进程任务管理器（PyQt5）')
    ap.add_argument('--all', action='store_true', help='不按 wine 过滤，列出全部进程')
    ap.add_argument('--filter', default='', help='自定义过滤正则（名称或命令行）')
    ap.add_argument('--interval', type=float, default=1.0, help='刷新间隔秒，默认 1.0')
    ap.add_argument('--all-uid', action='store_true', help='不过滤 uid（默认只看当前 uid）')
    ap.add_argument('--dump', action='store_true', help='不开窗口，打印进程表')
    ap.add_argument('--rounds', type=int, default=1, help='--dump 时采样轮数')
    ap.add_argument('--self-test', action='store_true', help='建窗口→刷新一次→退出')
    ap.add_argument('--frameless', action='store_true', help='无边框（配 Termux:X11 全屏用）')
    ap.add_argument('--width', type=int, default=760)
    ap.add_argument('--height', type=int, default=520)
    args = ap.parse_args()

    if args.dump:
        return run_dump(args, max(1, args.rounds))
    try:
        import PyQt5  # noqa: F401
    except ImportError:
        print('未安装 PyQt5。Termux 原生: pkg install python-pyqt5；'
              'glibc 前缀: pacman -S python-pyqt5\n'
              '（只想看列表可以用 --dump，不需要 PyQt5）', file=sys.stderr)
        return 2
    return run_gui(args)


if __name__ == '__main__':
    sys.exit(main())