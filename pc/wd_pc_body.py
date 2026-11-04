# -*- coding: utf-8 -*-
"""wd_pc_body.py —— 肥鱼 PC 身体看门狗（2026-11-05，目标：半夜进程挂了能自愈）

端口探活（进程无关，谁占着都算活）：
  :7340  dsh-pet standalone（桌宠本体）
  :17860 pc_body 桥
挂了就用对应命令拉起（DETACHED 无窗口）。单实例互斥 = 独占绑定 :17861。
用法：python wd_pc_body.py        # 常驻循环，60s 一查
      python wd_pc_body.py once   # 单次检查后退出（测试用）
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).parent
PYW = Path(sys.executable)
PYTHON = PYW.with_name('python.exe')          # pythonw 同目录必有 python
NODE = Path(r'C:\Program Files\nodejs\node.exe')
LOG_FILE = DIR / 'wd_pc_body.log'
MUTEX_PORT = 17861
INTERVAL = 60

CHECKS = [
    (7340, [str(NODE), 'lib/standalone.js', '--port', '7340']),
    (17860, [str(PYTHON), 'pc_body.py']),
]
CREATE_FLAGS = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(msg):
    line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    print(line, flush=True)


def alive(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def spawn(cmd):
    subprocess.Popen(cmd, cwd=str(DIR), creationflags=CREATE_FLAGS,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def check_pass():
    for port, cmd in CHECKS:
        if alive(port):
            continue
        time.sleep(1.5)          # 复检：瞬时探活误判会导致重复拉起（Windows 双绑坑）
        if alive(port):
            continue
        log('DOWN :%d -> 拉起 %s' % (port, ' '.join(cmd[1:])))
        try:
            spawn(cmd)
        except Exception as e:
            log('spawn 失败: %s' % e)


def main():
    once = len(sys.argv) > 1 and sys.argv[1] == 'once'
    if once:
        # 单次检查不抢常驻互斥（bat 启动时与常驻看门狗并发检查是常态，
        # 重复拉起由 pc_body 的独占绑定兜底成"单胜者"，无害）
        check_pass()
        return
    mutex = socket.socket()
    try:
        mutex.bind(('127.0.0.1', MUTEX_PORT))
    except OSError:
        print('已有看门狗在跑（:17861 被占），退出', flush=True)
        return
    mutex.listen(1)
    log('watchdog start (interval=%ds)' % INTERVAL)
    while True:
        try:
            check_pass()
        except Exception as e:
            log('check 异常: %s' % e)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
