"""A headless Chrome driven over the DevTools protocol (websockets, no Playwright): the 3D
stage is three.js in a page, so exporting it means opening that page and asking it for
frames. Chrome is looked for in $CW_CHROME, on PATH, then in Playwright's download folder."""

from __future__ import annotations

import base64
import glob
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


class BrowserError(Exception):
    pass


def find_chrome() -> str | None:
    if os.environ.get("CW_CHROME") and Path(os.environ["CW_CHROME"]).is_file():
        return os.environ["CW_CHROME"]
    for n in NAMES:
        if shutil.which(n):
            return shutil.which(n)
    found = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else None


class Page:
    def __init__(self, ws: Any):
        self.ws = ws
        self.ids = itertools.count(1)

    def call(self, method: str, timeout: float = 120, **params: Any) -> dict[str, Any]:
        n = next(self.ids)
        self.ws.send(json.dumps({"id": n, "method": method, "params": params}))
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise BrowserError(f"Chrome didn't answer {method} in {timeout:.0f}s.")
            msg = json.loads(self.ws.recv(timeout=left))
            if msg.get("id") == n:
                if "error" in msg:
                    raise BrowserError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def eval(self, expr: str, timeout: float = 120) -> Any:
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise BrowserError((d.get("exception") or {}).get("description") or d.get("text") or "page error")
        return (r.get("result") or {}).get("value")

    def wait_title(self, want: tuple[str, ...] = ("ready", "error"), timeout: float = 180) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            title = self.eval("document.title", timeout=30)
            if title in want:
                return title
            time.sleep(0.25)
        raise BrowserError("The 3D page didn't finish loading in time.")

    def jpeg(self, width: int, height: int, quality: int = 92) -> bytes:
        r = self.call("Page.captureScreenshot", format="jpeg", quality=quality,
                      clip={"x": 0, "y": 0, "width": width, "height": height, "scale": 1})
        return base64.b64decode(r["data"])


class Browser:
    """with Browser(width, height) as b: page = b.open(url)"""

    def __init__(self, width: int, height: int, *, gpu: bool = False, exe: str | None = None):
        self.width, self.height, self.gpu = width, height, gpu
        self.exe = exe or find_chrome()
        if not self.exe:
            raise BrowserError("No Chrome or Chromium found: install one, or set CW_CHROME to its path.")
        self.proc: subprocess.Popen | None = None
        self.dir: str | None = None
        self.ws: Any = None

    def __enter__(self) -> "Browser":
        self.dir = tempfile.mkdtemp(prefix="cw-chrome-")
        gl = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] if not self.gpu else ["--use-angle=gl", "--ignore-gpu-blocklist"]
        self.proc = subprocess.Popen(
            [self.exe, "--headless=new", "--no-sandbox", "--no-first-run", "--no-default-browser-check", "--mute-audio",
             "--hide-scrollbars", "--autoplay-policy=no-user-gesture-required", "--force-device-scale-factor=1",
             f"--window-size={self.width},{self.height}", "--remote-debugging-port=0", f"--user-data-dir={self.dir}",
             *gl, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.dir) / "DevToolsActivePort"
        end = time.monotonic() + 30
        while not port_file.is_file() or not port_file.read_text().strip():
            if time.monotonic() > end or self.proc.poll() is not None:
                self.__exit__(None, None, None)
                raise BrowserError("Chrome didn't start.")
            time.sleep(0.1)
        self.port = int(port_file.read_text().split()[0])
        return self

    def open(self, url: str) -> Page:
        from websockets.sync.client import connect
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            target = json.loads(r.read())
        self._conn = connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=30)
        self.ws = self._conn.__enter__()
        page = Page(self.ws)
        page.call("Page.enable")
        page.call("Runtime.enable")
        page.call("Emulation.setDeviceMetricsOverride", width=self.width, height=self.height, deviceScaleFactor=1,
                  mobile=False)
        page.call("Page.navigate", url=url)
        return page

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.ws is not None:
                self._conn.__exit__(None, None, None)
        except Exception:                               # noqa: BLE001
            pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
