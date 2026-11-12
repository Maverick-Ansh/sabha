"""Public link for the hub.

cloudflared: a "quick tunnel" (no account) via
    cloudflared tunnel --url http://127.0.0.1:<port> --no-autoupdate
The binary is downloaded into <data>/.cloudflared/ when it is not on PATH. The
public URL shows up on its log output as https://<words>.trycloudflare.com.

ngrok: through pyngrok, token from env NGROK_AUTHTOKEN, optional reserved
domain from env NGROK_DOMAIN.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .util import IS_WINDOWS, group_kwargs, kill_tree, read_lines

log = logging.getLogger("sabha.tunnel")

CF_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CF_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
URL_WAIT_S = 60.0


class TunnelError(Exception):
    pass


def _cf_asset() -> str:
    machine = platform.machine().lower()
    if IS_WINDOWS:
        return "cloudflared-windows-amd64.exe"
    if platform.system() == "Darwin":
        raise TunnelError("on macOS install cloudflared with: brew install cloudflared")
    if machine in ("aarch64", "arm64"):
        return "cloudflared-linux-arm64"
    return "cloudflared-linux-amd64"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "sabha-hub"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    os.replace(tmp, dest)
    if not IS_WINDOWS:
        dest.chmod(0o755)


async def cloudflared_binary(data_dir: Path) -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    asset = _cf_asset()
    dest = data_dir / ".cloudflared" / ("cloudflared.exe" if IS_WINDOWS else "cloudflared")
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        return str(dest)
    url = CF_RELEASES + asset
    print(f"Downloading cloudflared from {url} ...", flush=True)
    try:
        await asyncio.to_thread(_download, url, dest)
    except OSError as e:
        raise TunnelError(f"could not download cloudflared: {e}") from None
    return str(dest)


class Tunnel:
    def __init__(self, kind: str, port: int, data_dir: Path):
        self.kind = kind
        self.port = port
        self.data_dir = data_dir
        self.url: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._ngrok_url: str | None = None

    async def start(self) -> str:
        if self.kind == "cloudflared":
            self.url = await self._start_cloudflared()
        elif self.kind == "ngrok":
            self.url = await self._start_ngrok()
        else:
            raise TunnelError(f"unknown tunnel {self.kind!r}")
        return self.url

    async def _start_cloudflared(self) -> str:
        exe = await cloudflared_binary(self.data_dir)
        log_path = self.data_dir / "cloudflared.log"
        self._proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate",
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **group_kwargs(),
        )
        found: asyncio.Future = asyncio.get_running_loop().create_future()
        log_file = open(log_path, "a", encoding="utf-8")

        def on_line(line: str) -> None:
            log_file.write(line + "\n")
            log_file.flush()
            if not found.done():
                for url in CF_URL.findall(line):
                    if not url.startswith("https://api."):  # api.trycloudflare.com shows up in errors
                        found.set_result(url)
                        break

        async def pump() -> None:
            try:
                await read_lines(self._proc.stdout, on_line)
            finally:
                log_file.close()
                if not found.done():
                    found.set_exception(TunnelError(f"cloudflared exited; see {log_path}"))

        self._reader = asyncio.create_task(pump())
        try:
            return await asyncio.wait_for(asyncio.shield(found), URL_WAIT_S)
        except asyncio.TimeoutError:
            found.cancel()
            await self.stop()
            raise TunnelError(f"no trycloudflare.com URL within {URL_WAIT_S:.0f} s; see {log_path}") from None
        except TunnelError:
            await self.stop()
            raise

    async def _start_ngrok(self) -> str:
        def connect() -> str:
            try:
                from pyngrok import ngrok
            except ImportError:
                raise TunnelError("pyngrok is not installed: pip install pyngrok") from None
            token = os.environ.get("NGROK_AUTHTOKEN", "")
            if token:
                try:
                    ngrok.set_auth_token(token)
                except Exception as e:  # pyngrok raises its own types
                    log.warning("ngrok set_auth_token failed: %s", e)
            domain = os.environ.get("NGROK_DOMAIN", "").strip()
            try:
                if domain:
                    tunnel = ngrok.connect(self.port, "http", url=f"https://{domain}")
                else:
                    tunnel = ngrok.connect(self.port, "http")
            except Exception as e:
                raise TunnelError(f"ngrok failed: {e}") from None
            url = tunnel.public_url
            return "https://" + url[len("http://"):] if url.startswith("http://") else url

        self._ngrok_url = await asyncio.to_thread(connect)
        return self._ngrok_url

    async def stop(self) -> None:
        if self._proc is not None:
            await kill_tree(self._proc)
            self._proc = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._ngrok_url:
            url = self._ngrok_url
            self._ngrok_url = None

            def disconnect() -> None:
                try:
                    from pyngrok import ngrok
                    ngrok.disconnect(url)
                    ngrok.kill()
                except Exception:
                    pass

            await asyncio.to_thread(disconnect)
