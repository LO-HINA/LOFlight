"""真 Chrome + CDP 连接：绕过 whaleguard 这类自动化检测。

为什么不用 Playwright 自带启动：``p.chromium.launch()`` 会设 ``navigator.webdriver``
并在启动参数里留下自动化痕迹，携程/同程的 whaleguard 一眼识破。
改成「自己起一个正常 Chrome，开 --remote-debugging-port，再 CDP 连过去」，
浏览器侧看到的就是一台普通 Chrome。

代价：必须有桌面（headed），无头模式认不出。窗口会真的弹出来。
"""
import os
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
]


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and Path(path).exists():
            return path
    raise RuntimeError("找不到 Chrome，请确认已安装 Google Chrome")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_cdp(port: int, timeout: float = 20.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


@contextmanager
def real_chrome(port: Optional[int] = None, width: int = 1440, height: int = 1100,
                profile: Optional[str] = None, keep_open: bool = False):
    """启动真 Chrome 并通过 CDP 连接，产出 Playwright Browser。

    用法::

        with real_chrome() as browser:
            page = browser.contexts[0].new_page()
            page.goto(url)
    """
    from playwright.sync_api import sync_playwright

    port = port or free_port()
    profile = profile or tempfile.mkdtemp(prefix="travel-chrome-")
    proc = subprocess.Popen([
        find_chrome(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        f"--window-size={width},{height}",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not _wait_cdp(port):
            raise RuntimeError("Chrome 调试端口没有起来")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                yield browser
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        if not keep_open:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
