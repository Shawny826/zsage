#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zsage —— 一句话拉起 ZCode 用量看板，任意路径、任意终端可用。

    zsage                确保服务在跑；页面没开就打开，已经开着就只报一句状态
    zsage --app          弹出独立窗口（Chromium app 模式，无地址栏无标签，最接近"浮窗"）
    zsage --system       强制用系统默认浏览器打开（在 ZCode 终端里也用系统浏览器）
    zsage --open         即使已经有标签在看，也再打开一次
    zsage -p PORT        指定端口（例如 zsage -p 9000）
    zsage --random-port  不优先默认端口，直接随机端口
    zsage restart        重启服务（沿用当前端口；zsage restart -p 9000 可换端口）
    zsage stop           停掉服务
    zsage status         服务、端口、运行模式、几个标签在看、数据规模
    zsage doctor         体检：Python 版本 / 数据库 / 单价表 / 端口 / 浏览器 / shim
    zsage sync-prices    从 models.dev 给未定价的模型补价格
    zsage setup-auto-sync         设置每日自动同步（北京时间 8:00）
    zsage setup-auto-sync --remove 移除自动同步任务
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

__version__ = "0.3.0"
DEFAULT_BIN_DIR = Path.home() / ".local" / "bin"
PREFERRED_PORTS = (8787, 8788, 8789)
PRICES_PATH = BASE_DIR / "prices.json"

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
                app_window: bool = False, port: int | None = None) -> int:
    code, runtime = er.ensure(random_port=random_port, port=port)
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
MODELS_DEV_URL = "https://models.dev/api.json"


def _normalize_model(name: str) -> str:
    """归一化模型名：只留小写字母数字，便于跨厂商比对（glm-5.3 / GLM_5.3 -> glm53）。"""
    import re

    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# 同一模型常被多个 provider 收录（官方 + 各种转售/聚合）。
# 归一化名冲突时优先取官方，免得 gpt-5.6-sol 拿到某个转售商的价格。
FIRST_PARTY_PROVIDERS = {
    "zhipuai", "zai", "zai-coding-plan", "zhipuai-coding-plan",
    "anthropic", "openai", "google", "google-vertex", "google-vertex-anthropic",
    "deepseek", "moonshotai", "moonshotai-cn", "xai", "meta", "mistral",
    "alibaba", "qwen", "cohere", "amazon-bedrock", "azure", "perplexity",
    "minimax", "baidu", "bytedance", "stepfun",
}


def _load_models_dev_catalog() -> dict:
    """拉 models.dev 全量目录，返回 {归一化名: (provider_id, provider_name, model)}。

    只收录带报价的模型 —— coding plan 一类的 provider 报价是空的，收进来没有意义。
    """
    req = urllib.request.Request(
        MODELS_DEV_URL,
        headers={"User-Agent": f"zsage/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    catalog: dict = {}
    for pid, provider in raw.items():
        rank = 0 if pid in FIRST_PARTY_PROVIDERS else 1
        for model in (provider.get("models") or {}).values():
            cost = model.get("cost") or {}
            if not cost.get("input") and not cost.get("output"):
                continue
            key = _normalize_model(model.get("id"))
            if not key:
                continue
            prev = catalog.get(key)
            if prev is None or rank < prev[3]:
                catalog[key] = (pid, provider.get("name") or pid, model, rank)
    return catalog


def _match_catalog(model_id: str, catalog: dict):
    """给一个 ZCode 模型名在目录里找价格。返回 (命中项, 匹配方式说明) 或 (None, "")。

    三级策略：精确 -> 包含 -> 模糊（difflib 相似度）。模糊匹配正是"未识别模型近似匹配"。
    """
    import difflib

    key = _normalize_model(model_id)
    if not key:
        return None, ""
    if key in catalog:
        return catalog[key], "精确"
    subs = [k for k in catalog if k in key or key in k]
    if subs:
        return catalog[max(subs, key=len)], "包含"
    best, score = None, 0.0
    for k in catalog:
        ratio = difflib.SequenceMatcher(None, key, k).ratio()
        if ratio > score:
            best, score = k, ratio
    if best and score >= 0.82:
        return catalog[best], f"近似({score:.2f})"
    return None, ""


def _zcode_model_ids(db_path: str):
    """取 ZCode 库里实际出现过的模型名与请求数，按请求数降序。"""
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=8.0)
    try:
        con.execute("PRAGMA query_only=1")
        return [
            (r[0], r[1]) for r in con.execute(
                "SELECT model_id, COUNT(*) AS n FROM model_usage"
                " WHERE model_id IS NOT NULL GROUP BY model_id ORDER BY n DESC"
            )
        ]
    finally:
        con.close()


def _glob_safe(model_id: str) -> str:
    """模型名里若含通配符元字符，就别包 * ，改用精确串，免得匹配到别的模型。"""
    return model_id if any(ch in model_id for ch in "*?[") else f"*{model_id}*"


def cmd_sync_prices(auto_mode: bool = False) -> int:
    """从 models.dev 补齐价格。

    只处理本地实际用到、且当前没有规则命中的模型；同名模型优先取官方 provider。
    再次运行会刷新此前由本命令生成的规则（source=models.dev），
    标为 official / assumed 的规则一律不动 —— 那些是你手工核对过的。
    """
    import fnmatch
    import time

    try:
        with open(PRICES_PATH, encoding="utf-8") as fh:
            config = json.load(fh)
    except Exception as exc:
        print(f"✗ 读不了 {PRICES_PATH}：{exc}", file=sys.stderr)
        return 1

    rules = config.setdefault("rules", [])
    ignored = [p.lower() for p in config.get("ignored_models", [])]

    db = os.environ.get("ZCODE_DB") or str(Path.home() / ".zcode" / "cli" / "db" / "db.sqlite")
    if not Path(db).is_file():
        print(f"✗ 找不到 ZCode 数据库：{db}", file=sys.stderr)
        print("  用 ZCODE_DB 环境变量指定。", file=sys.stderr)
        return 1

    def rule_for(model_id: str):
        name = (model_id or "").lower()
        for r in rules:
            if fnmatch.fnmatchcase(name, (r.get("match") or "*").lower()):
                return r
        return None

    models = [
        (mid, n) for mid, n in _zcode_model_ids(db)
        if not any(fnmatch.fnmatchcase(mid.lower(), p) for p in ignored)
    ]

    if not auto_mode:
        print(f"本地共 {len(models)} 个模型，正在拉取 models.dev 目录…")
    try:
        catalog = _load_models_dev_catalog()
    except urllib.error.URLError as exc:
        print(f"✗ 拉取 models.dev 失败（网络问题？）：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"✗ 解析 models.dev 失败：{exc}", file=sys.stderr)
        return 1
    if not auto_mode:
        print(f"models.dev 带报价的模型 {len(catalog)} 个")

    today = time.strftime("%Y-%m-%d")
    added, refreshed, unresolved, already, stale = [], [], [], [], []

    for mid, count in models:
        existing = rule_for(mid)
        # 手工规则（official / assumed）一律不碰
        if existing is not None and existing.get("source") != "models.dev":
            already.append((mid, count, existing))
            continue

        hit, how = _match_catalog(mid, catalog)
        if hit is None:
            # 有旧规则就保留（这次没匹配上不代表要删），没有才算真的缺价
            (stale if existing is not None else unresolved).append((mid, count))
            continue

        _pid, pname, model, _rank = hit
        cost = model.get("cost") or {}
        entry = {
            "match": _glob_safe(mid),
            "label": model.get("name") or mid,
            "currency": "USD",
            "input": cost.get("input"),
            "cache_read": cost.get("cache_read"),
            "cache_write": cost.get("cache_write"),
            "output": cost.get("output"),
            "source": "models.dev",
            "note": f"models.dev {how}匹配：{pname} / {model.get('id')}（同步于 {today}）",
        }
        if existing is not None:
            if all(existing.get(k) == entry[k] for k in ("input", "output", "cache_read", "cache_write")):
                continue  # 价格没变，不写盘也不报
            existing.clear()
            existing.update(entry)
            refreshed.append((mid, count, how, pname))
        else:
            rules.append(entry)
            added.append((mid, count, how, pname))

    changed = bool(added or refreshed)
    if changed:
        config["_last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(PRICES_PATH, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

    if auto_mode:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        tail = f"，仍缺 {len(unresolved)} 个" if unresolved else ""
        print(f"[{stamp}] 价格同步：新增 {len(added)}、刷新 {len(refreshed)}{tail}")
        return 0

    print()
    if added:
        print(f"✓ 新增 {len(added)} 条规则（原来未定价）：")
        for mid, count, how, pname in added:
            print(f"    {mid}（{count} 次请求）  ←  {pname}  [{how}]")
    if refreshed:
        print(f"✓ 更新 {len(refreshed)} 条规则价格：")
        for mid, count, how, pname in refreshed:
            print(f"    {mid}（{count} 次请求）  ←  {pname}  [{how}]")
    if not changed:
        print("没有需要补价或更新的模型。")
    if unresolved:
        print(f"\n⚠ {len(unresolved)} 个模型在 models.dev 里找不到可用价格：")
        for mid, count in unresolved:
            print(f"    {mid}（{count} 次请求）")
        print("  可在 prices.json 手工补一条，或启用 unknown_model_price 兜底。")
    if stale:
        print(f"\n{len(stale)} 个模型这次没匹配到，保留原有规则不动："
              + "、".join(m for m, _ in stale))
    if already:
        print(f"\n已有 {len(already)} 个模型命中手工规则（不覆盖）：")
        for mid, count, r in already[:8]:
            print(f"    {mid:<28} {count:>5} 次  ←  {r.get('match')}  [{r.get('source')}]")
        if len(already) > 8:
            print(f"    …另有 {len(already) - 8} 个")
    if changed:
        print(f"\n已写入 {PRICES_PATH}（改完刷新页面即生效）")
    return 0


def cmd_setup_auto_sync() -> int:
    """设置每日自动同步价格的定时任务（北京时间 8:00）"""
    import subprocess
    
    remove_mode = "--remove" in sys.argv
    
    # 获取脚本路径
    auto_sync_script = BASE_DIR / "auto_sync_prices.py"
    if not auto_sync_script.exists():
        print(f"错误：找不到自动同步脚本：{auto_sync_script}", file=sys.stderr)
        return 1
    
    if os.name == "nt":
        # Windows: 使用任务计划程序
        task_name = "zsage-auto-sync-prices"
        
        if remove_mode:
            # 删除任务
            result = subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
                text=True,
                encoding='gbk',
                errors='ignore'
            )
            if result.returncode == 0:
                print(f"✓ 已移除自动同步任务：{task_name}")
                return 0
            else:
                print(f"✗ 移除失败（任务可能不存在）", file=sys.stderr)
                return 1
        
        # 创建任务
        # 北京时间 8:00，Windows 任务计划使用本地时间
        xml_content = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2024-01-01T08:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions>
    <Exec>
      <Command>{sys.executable}</Command>
      <Arguments>"{auto_sync_script}"</Arguments>
      <WorkingDirectory>{BASE_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""
        
        # 写入临时 XML 文件
        xml_path = BASE_DIR / "auto_sync_task.xml"
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml_content)
        
        try:
            # 创建任务
            result = subprocess.run(
                ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
                capture_output=True,
                text=True,
                encoding='gbk',
                errors='ignore'
            )
            
            if result.returncode == 0:
                print(f"✓ 已创建自动同步任务：{task_name}")
                print(f"  执行时间：每天 08:00（北京时间）")
                print(f"  脚本路径：{auto_sync_script}")
                print(f"\n  查看任务：schtasks /Query /TN {task_name} /V /FO LIST")
                print(f"  手动运行：schtasks /Run /TN {task_name}")
                print(f"  删除任务：zsage setup-auto-sync --remove")
                return 0
            else:
                print(f"✗ 创建任务失败：{result.stderr}", file=sys.stderr)
                return 1
        finally:
            # 清理临时文件
            if xml_path.exists():
                xml_path.unlink()
    
    else:
        # Linux/macOS: 使用 cron
        print("Linux/macOS 自动同步设置：")
        print("\n请手动添加以下 cron 任务（每天北京时间 8:00）：")
        print("\n1. 编辑 crontab：")
        print("   crontab -e")
        print("\n2. 添加以下行：")
        # 北京时间 8:00 = UTC 0:00
        print(f"   0 0 * * * {sys.executable} {auto_sync_script}")
        print("\n3. 保存并退出")
        print("\n注意：确保系统时区设置为 Asia/Shanghai，或调整 cron 时间")
        
        if remove_mode:
            print("\n移除任务：编辑 crontab 并删除对应行")
        
        return 0


def _parse_port(argv: list[str]):
    """解析 -p/--port。返回 (端口, 是否合法)；未指定时端口为 None。"""
    for i, arg in enumerate(argv):
        if arg in ("-p", "--port") and i + 1 < len(argv):
            try:
                return int(argv[i + 1]), True
            except ValueError:
                print(f"端口必须是数字：{argv[i + 1]}", file=sys.stderr)
                return None, False
    return None, True


def _wait_port_free(port: int, timeout: float = 3.0) -> None:
    """taskkill 是异步的，端口未必立刻释放。短暂等待，免得新进程 bind 失败。"""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return
        except OSError:
            time.sleep(0.15)
        finally:
            probe.close()


def cmd_restart(port: int | None = None) -> int:
    """重启服务：默认沿用当前端口，`-p` 可换一个。不会自动弹浏览器。"""
    old = er.read_runtime()
    old_port = (old or {}).get("port")
    if old_port:
        er.stop()
        print(f"已停止旧服务（pid {(old or {}).get('pid')}，端口 {old_port}）")
        _wait_port_free(old_port)

    code, runtime = er.ensure(port=port if port is not None else old_port)
    if code != 0 or not runtime:
        print("重启失败：服务没在 10 秒内就绪，检查 runtime.json 与端口占用", file=sys.stderr)
        return 1
    print(f"已重启：{runtime['url']}")
    extra = summarize(runtime["url"])
    if extra:
        print(f"数据：{extra}")
    return 0


# 所有已识别的子命令。写成常量是为了让拼错的命令报错，而不是静默走默认分支
KNOWN_ACTIONS = (
    "stop", "停", "status", "状态", "doctor", "restart", "重启",
    "sync-prices", "setup-auto-sync", "install", "uninstall", "help",
)


def main() -> int:
    argv = sys.argv[1:]
    action = argv[0].lower() if argv and not argv[0].startswith("-") else ""
    port, port_ok = _parse_port(argv)
    if not port_ok:
        return 1

    if action in ("stop", "停"):
        code, runtime = er.stop()
        print({0: f"已停止（pid {(runtime or {}).get('pid')}）",
               2: "没有正在运行的实例",
               3: "进程可能已经不在了，已清理状态文件"}.get(code, "停止失败"))
        return code
    if action in ("restart", "重启"):
        return cmd_restart(port)
    if action in ("status", "状态"):
        return cmd_status()
    if action == "doctor":
        return cmd_doctor()
    if action == "sync-prices":
        return cmd_sync_prices(auto_mode="--auto" in argv)
    if action == "setup-auto-sync":
        return cmd_setup_auto_sync()
    if action == "install":
        return cmd_install(autostart="--autostart" in argv, bin_dir=None)
    if action == "uninstall":
        return cmd_uninstall()
    if action == "help" or any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    if action and action not in KNOWN_ACTIONS:
        print(f"未知命令：{action}", file=sys.stderr)
        print("用 `zsage help` 看可用命令。", file=sys.stderr)
        return 1

    return cmd_default(
        force_system=("--system" in argv),
        force_open=("--open" in argv),
        random_port=("--random-port" in argv),
        app_window=("--app" in argv),
        port=port,
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
