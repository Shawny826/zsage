#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板的幂等启动器：已经在跑就复用，没跑才拉起（自动挑空闲端口 + 关闭页面即自动退出）。

用 pythonw 运行没有控制台窗口，适合做双击入口或被脚本调用。

    pythonw ensure_running.py                 确保服务在跑（幂等），打印 URL；默认常驻
    pythonw ensure_running.py --system        同上，并额外用系统默认浏览器打开
    pythonw ensure_running.py --auto-shutdown 改回"没人看页面就自动退出"
    pythonw ensure_running.py --random-port   不优先 8787~8789，直接随机端口
    pythonw ensure_running.py --stop          停掉正在跑的实例
    python   ensure_running.py --status       打印当前状态（有控制台时用这个）

端口优先取 8787 → 8788 → 8789（方便把标签页留在面板里反复用），都被占用才交给系统随机分配。

启动后地址写在同目录的 runtime.json 里 —— pythonw 没有标准输出，所以状态一律以该文件为准。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_PATH = os.path.join(BASE_DIR, "runtime.json")
SERVER = os.path.join(BASE_DIR, "server.py")

# 优先用这几个端口，方便把标签页留在面板里反复用；都被占了才让系统随机分配
PREFERRED_PORTS = (8787, 8788, 8789)


def pick_port():
    import socket

    for port in PREFERRED_PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return 0  # 交给操作系统挑一个空闲端口


def _listeners_on(port):
    """列出正在 LISTEN 该端口的 pid。取不到就返回空列表（只做尽力而为的清理）。"""
    import subprocess

    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, encoding="gbk", errors="ignore").stdout
            pids = []
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[3].upper() == "LISTENING"
                        and parts[1].endswith(":" + str(port))):
                    pids.append(parts[4])
            return pids
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout
        return [p for p in out.split() if p.strip()]
    except Exception:
        return []


def _cmdline_of(pid):
    import subprocess

    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine",
                 "/format:list"],
                capture_output=True, text=True, encoding="gbk", errors="ignore").stdout
            return out
        with open(f"/proc/{pid}/cmdline", encoding="utf-8", errors="ignore") as fh:
            return fh.read().replace("\0", " ")
    except Exception:
        return ""


def reclaim_stale_listeners(keep_pid=None):
    """收掉占着优先端口的旧实例。

    Windows 的 SO_REUSEADDR 允许第二个进程静默绑上已占端口（不报错、也收不到请求），
    于是同端口会堆好几个僵尸服务，启动检测永远失败。这里按"命令行指向本目录的
    server.py"精确识别，只清理自己的残留 —— 别的目录里的 zsage 实例不动。
    返回 [(port, pid), ...]。
    """
    import subprocess

    reclaimed = []
    for port in PREFERRED_PORTS:
        for pid in _listeners_on(port):
            if keep_pid is not None and str(pid) == str(keep_pid):
                continue
            cmd = _cmdline_of(pid)
            if "server.py" not in cmd or BASE_DIR not in cmd:
                continue
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=15)
                else:
                    os.kill(int(pid), 9)
                reclaimed.append((port, pid))
            except Exception:
                pass
    return reclaimed


def read_runtime():
    try:
        with open(RUNTIME_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def probe(runtime, timeout=1.5):
    """runtime.json 存在不代表服务活着，真去请求一下。"""
    if not runtime or not runtime.get("url"):
        return False
    try:
        with urllib.request.urlopen(runtime["url"] + "api/live", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def clear_runtime():
    try:
        os.remove(RUNTIME_PATH)
    except OSError:
        pass


def ensure(open_system_browser: bool = False, auto_shutdown: bool = False,
           random_port: bool = False, port: int | None = None):
    runtime = read_runtime()
    if probe(runtime):
        if open_system_browser:
            import webbrowser

            webbrowser.open(runtime["url"])
        return 0, runtime
    clear_runtime()  # 陈旧状态（上次异常退出留下的）直接清掉

    # 端口上可能压着同目录的僵尸实例（Windows 静默双绑定所致），先收掉再挑端口，
    # 否则新进程会"绑上但收不到请求"，表现得像启动失败。
    if port is None and not random_port:
        stale = reclaim_stale_listeners()
        for port_no, pid in stale:
            print(f"清理了端口 {port_no} 上的旧实例（pid {pid}）", file=sys.stderr)

    # 端口选择优先级：指定端口 > 随机 > 默认端口池
    if port is not None:
        actual_port = port
    elif random_port:
        actual_port = 0
    else:
        actual_port = pick_port()

    server_args = [sys.executable, SERVER, "--port", str(actual_port)]
    if auto_shutdown:
        # 默认常驻（关掉标签也继续跑，下次 zsage 秒开）；这个开关恢复"没人看就退出"
        server_args.append("--auto-shutdown")

    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(
        server_args,
        cwd=BASE_DIR,
        close_fds=True,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 等端口就绪，最多 10 秒
    import time

    for _ in range(50):
        time.sleep(0.2)
        runtime = read_runtime()
        if probe(runtime, timeout=0.6):
            if open_system_browser:
                import webbrowser

                webbrowser.open(runtime["url"])
            return 0, runtime
    return 1, None


def stop():
    runtime = read_runtime()
    pid = (runtime or {}).get("pid")
    if not pid:
        clear_runtime()
        return 2, None
    # 只关心返回码：taskkill 的输出是系统本地编码（中文 Windows 为 GBK），按文本解码会炸
    killed = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
    )
    clear_runtime()
    return (0 if killed.returncode == 0 else 3), runtime


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--stop":
        code, runtime = stop()
        pid = (runtime or {}).get("pid")
        messages = {
            0: f"已停止（pid {pid}）",
            2: "没有正在运行的实例",
            3: "进程可能已经不在了，已清理状态文件",
        }
        print(messages.get(code, "停止失败"))
        return code
    if mode == "--status":
        runtime = read_runtime()
        if probe(runtime):
            print(f"运行中：{runtime['url']}（pid {runtime.get('pid')}）")
            return 0
        print("未运行")
        return 2
    code, runtime = ensure(
        open_system_browser="--system" in sys.argv[1:],
        auto_shutdown="--auto-shutdown" in sys.argv[1:],
        random_port="--random-port" in sys.argv[1:],
    )
    if code == 0:
        print(runtime["url"])
        return 0
    print("启动失败：10 秒内没有等到服务就绪，检查 runtime.json 与端口占用", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
