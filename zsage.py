#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zsage —— 一句话拉起 ZCode 用量看板，任意路径、任意终端可用。

    zsage                确保服务在跑；页面没开就打开，已经开着就只报一句状态
    zsage --app          弹出独立窗口（Chromium app 模式，无地址栏无标签，最接近"浮窗"）
    zsage --system       强制用系统默认浏览器打开（在 ZCode 终端里也用系统浏览器）
    zsage --open         即使已经有标签在看，也再打开一次
    zsage --random-port  不优先默认端口，直接随机端口
    zsage stop           停掉服务
    zsage status         服务、端口、运行模式、几个标签在看、数据规模
    zsage doctor         体检：Python 版本 / 数据库 / 单价表 / 端口 / 浏览器 / shim
    zsage install        把 zsage 命令装到 PATH（Windows 可加 --autostart 开机自启服务）
    zsage uninstall      移除 shim 与开机自启（不动正在运行的服务）

设计要点：

* 服务默认**常驻**：关掉标签页不会退出，下次 zsage 直接复用，秒开。
* **数据不需要"刷新"**：服务端每次请求都现读 ZCode 的 db.sqlite，不缓存统计结果，
  所以只要调用接口拿到的一定是最新数据；页面本身还每 15 秒自动重拉一次。
* 在 ZCode 内置终端里运行时，地址会包成 OSC 8 超链接输出 —— ZCode 给终端 xterm 装了
  linkHandler，点一下就在内置浏览器面板打开；同时复制到剪贴板做备用。
  ZCode 没有对外开放"自动打开内置面板"的接口，这是当前最接近全自动的方式。
* 想要完全不用点击：`zsage --app`（Chromium app 模式独立窗口）或 `zsage --system`
  （系统默认浏览器）。两者都在 ZCode 之外，但全自动。

环境变量：

* ``ZCODE_DB``       覆盖数据库路径（默认 ``~/.zcode/cli/db/db.sqlite``）
* ``ZSAGE_BIN_DIR``  覆盖 install 的 shim 目录（默认 ``~/.local/bin``）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import ensure_running as er  # noqa: E402  复用幂等启动/停止逻辑

__version__ = "0.2.0"
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"
PREFERRED_PORTS = (8787, 8788, 8789)

# app 模式窗口用的 Chromium 系浏览器（按顺序找第一个存在的）
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def in_zcode_terminal() -> bool:
    """ZCode 的集成终端会注入 ZCODE_* 环境变量，系统终端里没有。"""
    return any(os.environ.get(k) for k in ("ZCODE_ENV", "ZCODE_APP_VERSION", "ZCODE_DATA_BASE_DIR"))


def hyperlink(url: str, text: str | None = None) -> str:
    """把地址包成 OSC 8 超链接。

    ZCode 的终端给 xterm 装了 linkHandler，点击这种超链接会调 onOpenBrowserUrl
    （→ zcode:open-browser-url IPC → 内置浏览器面板）。裸文本 URL 是不可点的。
    不是 tty（被重定向/管道）时不发转义序列，免得把日志搞脏。
    """
    if not sys.stdout.isatty():
        return text or url
    return f"\x1b]8;;{url}\x1b\\{text or url}\x1b]8;;\x1b\\"


def copy_to_clipboard(text: str) -> bool:
    """尽力而为。剪贴板可能被别的程序占用（Windows 的 clip.exe 会报"拒绝访问"），
    失败不算错误，调用方要把地址照样打印出来。"""
    attempts: list[list[str]] = []
    if os.name == "nt":
        attempts.append(["clip"])
    else:
        attempts += [["wl-copy"], ["xclip", "-selection", "clipboard"], ["pbcopy"]]
    for _ in range(2):
        for cmd in attempts:
            try:
                payload = text.encode("utf-16le") if cmd[0] == "clip" else text.encode("utf-8")
                subprocess.run(cmd, input=payload, check=True, timeout=8,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
        if os.name == "nt":  # Windows 的兜底：环境变量传值，避开引号与编码问题
            try:
                env = dict(os.environ, ZSAGE_CLIP=text)
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $env:ZSAGE_CLIP"],
                    env=env, check=True, timeout=8,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                pass
        import time

        time.sleep(0.4)
    return False


def find_browser() -> str | None:
    import shutil

    for exe in BROWSER_CANDIDATES:
        if os.path.isfile(exe):
            return exe
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_app_window(url: str, size: str = "560,820") -> str | None:
    """用 Chromium 的 app 模式开一个无地址栏无标签的独立窗口（像个独立小应用，最接近"浮窗"）。

    这是唯一能"全自动弹出"的形式 —— 内置面板没有对外的打开接口。
    """
    exe = find_browser()
    if not exe:
        return None
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) if os.name == "nt" else 0
    subprocess.Popen([exe, f"--app={url}", f"--window-size={size}"], close_fds=True, creationflags=flags)
    return exe


def summarize(url: str) -> str:
    """给一行"数据确实是新的"的证据。"""
    try:
        boot = http_json(url + "api/bootstrap")
    except Exception:
        return ""
    rows = boot.get("total_rows") or 0
    last = (boot.get("range") or {}).get("max") or 0
    when = ""
    if last:
        import datetime

        when = datetime.datetime.fromtimestamp(last / 1000).strftime("%m-%d %H:%M")
    return f"{rows} 条请求记录（最新 {when}）"


# --------------------------------------------------------------------------- #
# 默认动作：起服务 + 打开
# --------------------------------------------------------------------------- #
def cmd_default(force_system: bool, force_open: bool, random_port: bool,
                app_window: bool = False) -> int:
    code, runtime = er.ensure(random_port=random_port)
    if code != 0 or not runtime:
        print("启动失败：服务没在 10 秒内就绪，检查 runtime.json 与端口占用", file=sys.stderr)
        return 1
    url = runtime["url"]

    viewers = 0
    try:
        viewers = http_json(url + "api/viewers").get("viewers", 0)
    except Exception:
        pass

    if viewers and not force_open:
        print(f"看板已开在 {url}（{viewers} 个标签在看，数据实时刷新）")
        extra = summarize(url)
        if extra:
            print(f"数据：{extra}")
        return 0

    if app_window:
        exe = open_app_window(url)
        if exe:
            print(f"已弹出独立窗口（{os.path.basename(exe)} app 模式）：{url}")
            extra = summarize(url)
            if extra:
                print(f"数据：{extra}")
            return 0
        print("没找到 Edge/Chrome，退回默认浏览器方式。", file=sys.stderr)

    if force_system or not in_zcode_terminal():
        import webbrowser

        webbrowser.open(url)
        print(f"已在默认浏览器打开：{url}")
    else:
        copied = copy_to_clipboard(url)
        # 终端里的超链接一点就进内置面板；剪贴板只是备用（它可能被别的程序占着）
        print(f"看板地址：{hyperlink(url)}")
        print("↑ 直接点这个地址就能在内置浏览器面板打开")
        if copied:
            print("（地址也已复制到剪贴板，需要时可直接粘贴）")
        else:
            print("（剪贴板被别的程序占用了，用上面那行链接即可）")
    extra = summarize(url)
    if extra:
        print(f"数据：{extra}")
    return 0


def cmd_status() -> int:
    runtime = er.read_runtime()
    if not er.probe(runtime):
        print("服务未运行")
        return 2
    try:
        info = http_json(runtime["url"] + "api/viewers")
        viewers = info.get("viewers", 0)
        mode = "常驻" if not info.get("auto_shutdown") else "临时（没人看就退出）"
    except Exception as exc:
        viewers, mode = "?", f"未知（{exc}）"
    print(f"运行中：{runtime['url']}")
    print(f"模式：{mode}    查看中的标签：{viewers}")
    extra = summarize(runtime["url"])
    if extra:
        print(f"数据：{extra}")
    return 0


# --------------------------------------------------------------------------- #
# install / uninstall / doctor
# --------------------------------------------------------------------------- #
def _python_for_shim() -> str:
    """shim 里写死的解释器：优先 pythonw（Windows，无控制台），否则当前解释器。"""
    exe = Path(sys.executable)
    if os.name == "nt":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def _write_shims(bin_dir: Path) -> list[Path]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = _python_for_shim()
    entry = BASE_DIR / "zsage.py"
    written = []

    if os.name == "nt":
        cmd_lines = [
            "@echo off",
            "rem zsage - open the ZCode usage dashboard (cmd.exe / PowerShell entry)",
            f'set "ZSAGE_PY={entry}"',
            "where python >nul 2>nul || goto :with_fullpath",
            'python "%ZSAGE_PY%" %*',
            "exit /b %errorlevel%",
            "",
            ":with_fullpath",
            f'"{python}" "%ZSAGE_PY%" %*',
            "exit /b %errorlevel%",
            "",
        ]
        # 关键：批处理必须 CRLF + ASCII，否则 cmd.exe 会静默失败
        target = bin_dir / "zsage.cmd"
        target.write_bytes("\r\n".join(cmd_lines).encode("ascii"))
        written.append(target)

    bash = bin_dir / "zsage"
    bash.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{python}" "{entry}" "$@"\n',
        encoding="utf-8",
    )
    try:
        bash.chmod(0o755)
    except OSError:
        pass
    written.append(bash)
    return written


def _path_contains(haystack: str, needle: str) -> bool:
    parts = [p.strip().strip('"').lower() for p in haystack.split(os.pathsep) if p.strip()]
    return str(needle).lower() in parts


def _ensure_on_windows_user_path(bin_dir: Path) -> bool:
    """把 shim 目录追加进用户 PATH（只在缺失时）。返回是否发生了修改。"""
    ps = (
        "$b='" + str(bin_dir).replace("'", "''") + "';"
        "$p=[Environment]::GetEnvironmentVariable('PATH','User');"
        "if(($p -split ';') -contains $b){'present'}else{"
        "[Environment]::SetEnvironmentVariable('PATH', ($p.TrimEnd(';') + ';' + $b), 'User');'added'}"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           text=True, timeout=20)
        return "added" in (r.stdout or "")
    except Exception:
        return False


def _write_autostart() -> Path | None:
    pythonw = _python_for_shim()
    ensure = BASE_DIR / "ensure_running.py"
    if os.name == "nt":
        startup = (Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
                   / "Microsoft/Windows/Start Menu/Programs/Startup")
        startup.mkdir(parents=True, exist_ok=True)
        vbs = startup / "zsage-service.vbs"
        # ASCII + CRLF：Windows 脚本宿主对编码挑剔
        lines = [
            "' zsage: start the usage dashboard service at logon (persistent, no popup).",
            "Option Explicit",
            "Dim sh",
            "Set sh = CreateObject(\"WScript.Shell\")",
            f"sh.Run \"\"\"{pythonw}\"\" \"\"{ensure}\"\"\", 0, False",
            "",
        ]
        vbs.write_bytes("\r\n".join(lines).encode("ascii"))
        return vbs
    autostart = Path.home() / ".config/autostart"
    autostart.mkdir(parents=True, exist_ok=True)
    desktop = autostart / "zsage-service.desktop"
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=zsage service\n"
        f"Exec={sys.executable} {ensure}\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return desktop


def cmd_install(autostart: bool, bin_dir: Path | None) -> int:
    bin_dir = bin_dir or Path(os.environ.get("ZSAGE_BIN_DIR") or DEFAULT_BIN_DIR)
    written = _write_shims(bin_dir)
    print(f"已写入命令入口：{', '.join(str(w) for w in written)}")

    changed = False
    if os.name == "nt":
        changed = _ensure_on_windows_user_path(bin_dir)
        on_path = _path_contains(os.environ.get("PATH", ""), str(bin_dir)) or changed
    else:
        on_path = _path_contains(os.environ.get("PATH", ""), str(bin_dir))
    if not on_path:
        print(f"注意：{bin_dir} 不在 PATH 上。加进去并重开终端即可，例如：")
        print(f'  export PATH="{bin_dir}:$PATH"')
    elif changed:
        print(f"已把 {bin_dir} 追加进用户 PATH（已开的终端要重开才生效）")

    if autostart:
        path = _write_autostart()
        if path:
            print(f"已配置开机自启（仅拉起常驻服务，不弹窗）：{path}")

    db = os.environ.get("ZCODE_DB") or str(Path.home() / ".zcode/cli/db/db.sqlite")
    print(f"安装完成（v{__version__}）。重开终端后任意路径敲 `zsage` 即可。")
    print(f"数据库：{db}")
    return 0


def cmd_uninstall() -> int:
    bin_dir = Path(os.environ.get("ZSAGE_BIN_DIR") or DEFAULT_BIN_DIR)
    removed = []
    for name in ("zsage.cmd", "zsage"):
        target = bin_dir / name
        if target.exists():
            target.unlink()
            removed.append(str(target))
    if os.name == "nt":
        startup = (Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
                   / "Microsoft/Windows/Start Menu/Programs/Startup/zsage-service.vbs")
    else:
        startup = Path.home() / ".config/autostart/zsage-service.desktop"
    if startup.exists():
        startup.unlink()
        removed.append(str(startup))
    if removed:
        print("已移除：\n  " + "\n  ".join(removed))
    else:
        print("没有找到可移除的安装内容")
    print("（正在运行的服务不受影响，用 `zsage stop` 停）")
    return 0


def cmd_doctor() -> int:
    ok = True

    def check(name: str, good: bool, detail: str = ""):
        nonlocal ok
        ok = ok and good
        print(f"  [{'√' if good else '×'}] {name}" + (f"：{detail}" if detail else ""))

    v = sys.version_info
    check(f"Python >= 3.9（当前 {v.major}.{v.minor}.{v.micro}）", (v.major, v.minor) >= (3, 9))
    db = Path(os.environ.get("ZCODE_DB") or Path.home() / ".zcode/cli/db/db.sqlite")
    check("ZCode 数据库存在", db.is_file(), str(db))
    try:
        with open(BASE_DIR / "prices.json", encoding="utf-8") as fh:
            rules = len(json.load(fh).get("rules", []))
        check("单价表可读", True, f"prices.json，{rules} 条规则")
    except Exception as exc:
        check("单价表可读", False, str(exc))
    import socket

    placed = False
    for port in PREFERRED_PORTS:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            free = True
        except OSError:
            free = False
        finally:
            s.close()
        if free:
            check("默认端口可用", True, str(port))
            placed = True
            break
        print(f"  [i] 端口 {port} 被占用，尝试下一个")
    if not placed:
        check("默认端口可用", False, "8787~8789 全被占用，将使用随机端口")
    browser = find_browser()
    check("找到 Edge/Chrome（--app 窗口用）", bool(browser), browser or "未找到，--app 会退回默认浏览器")
    bin_dir = Path(os.environ.get("ZSAGE_BIN_DIR") or DEFAULT_BIN_DIR)
    shim = bin_dir / ("zsage.cmd" if os.name == "nt" else "zsage")
    check("zsage 命令已安装", shim.is_file(), str(shim))
    runtime = er.read_runtime()
    if er.probe(runtime):
        check("服务运行中", True, runtime["url"])
    else:
        print("  [i] 服务未运行（不影响，zsage 会自动拉起）")
    print()
    print("全部通过" if ok else "存在未通过项，按上面的提示处理即可")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    argv = sys.argv[1:]
    action = argv[0].lower() if argv and not argv[0].startswith("-") else ""

    if action in ("stop", "停"):
        code, runtime = er.stop()
        print({0: f"已停止（pid {(runtime or {}).get('pid')}）",
               2: "没有正在运行的实例",
               3: "进程可能已经不在了，已清理状态文件"}.get(code, "停止失败"))
        return code
    if action in ("status", "状态"):
        return cmd_status()
    if action == "doctor":
        return cmd_doctor()
    if action == "install":
        return cmd_install(autostart="--autostart" in argv, bin_dir=None)
    if action == "uninstall":
        return cmd_uninstall()
    if action in ("help", "-h", "--help"):
        print(__doc__)
        return 0

    return cmd_default(
        force_system=("--system" in argv),
        force_open=("--open" in argv),
        random_port=("--random-port" in argv),
        app_window=("--app" in argv),
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
