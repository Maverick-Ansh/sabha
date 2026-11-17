"""Command line: `python -m sabha` starts the hub (PROTOCOL.md section 7).

    python -m sabha [--host 127.0.0.1] [--port 8787] [--data ./sabha_data] [--token T]
                    [--no-auth] [--demo] [--tunnel none|cloudflared|ngrok] [--session-hours 12]
                    [--research-repo DIR] [--model NAME=URL ...] [--no-think]

It starts the aiohttp server, optionally opens a tunnel, prints one link with
the token in the URL hash, and on Ctrl+C / SIGTERM stops every child process
and flushes state to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import signal
import socket
import sys
from pathlib import Path

from aiohttp import web

from . import __version__
from .server import LOOPBACK, Config, Hub, create_app
from .store import DataDirBusy
from .tunnel import Tunnel, TunnelError


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m sabha",
                                description="Sabha hub: run and watch AI agents on borrowed GPUs.")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    p.add_argument("--data", default=None,
                   help="data dir (default ./sabha_data, or ./sabha_data_demo with --demo)")
    p.add_argument("--token", default=None, help="shared token (default: env SABHA_TOKEN, else random)")
    p.add_argument("--no-auth", action="store_true", help="no token at all; only with --host 127.0.0.1")
    p.add_argument("--demo", action="store_true", help="seed a mock crew and start two demo missions")
    p.add_argument("--tunnel", choices=("none", "cloudflared", "ngrok"), default="none",
                   help="publish the hub through a tunnel")
    p.add_argument("--session-hours", type=float, default=12.0,
                   help="how long the box lives, for the UI's countdown (default 12)")
    p.add_argument("--research-repo", default=os.environ.get("SABHA_RESEARCH_REPO", ""),
                   help="data repo of the research daemon, for the Research view (default: env SABHA_RESEARCH_REPO)")
    p.add_argument("--model", action="append", default=[], metavar="NAME=URL",
                   help="register an OpenAI-compatible server that is already running, for example "
                        "reader=http://127.0.0.1:8002/v1 (NAME is also the served name; repeatable; skipped if NAME exists)")
    p.add_argument("--no-think", action="store_true",
                   help="models added with --model get chat_template_kwargs.enable_thinking=false on every request")
    p.add_argument("--log-level", default="INFO", help="logging level (default INFO)")
    args = p.parse_args(argv)
    if args.no_auth and args.host not in LOOPBACK:
        p.error("--no-auth is allowed only with --host 127.0.0.1")
    if args.no_auth and args.tunnel != "none":
        p.error("--no-auth cannot be combined with a tunnel: the link would be public with no token")
    if not 0 < args.port < 65536:
        p.error("--port must be between 1 and 65535")
    for spec in args.model:
        name, _, url = spec.partition("=")
        if not name.strip() or not url.strip().startswith(("http://", "https://")):
            p.error(f"--model takes NAME=URL with an http(s) URL, got {spec!r}")
    return args


def _banner(lines: list[str]) -> None:
    bar = "=" * 72
    print(bar, flush=True)
    for line in lines:
        print("  " + line, flush=True)
    print(bar, flush=True)


def _bind(host: str, port: int) -> socket.socket:
    """Bind the listening socket before anything else, so a busy port fails
    fast and leaves the data dir untouched."""
    host = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=128)


def _register_models(hub: Hub, specs: list[str], no_think: bool) -> None:
    """--model NAME=URL: add servers that already run on the box as plain base_url models, once."""
    have = {m.get("name") for m in hub.store.models.values()}
    for spec in specs:
        name, _, url = spec.partition("=")
        name, url = name.strip(), url.strip()
        if name in have:
            continue
        body = {"name": name, "kind": "chat", "base_url": url, "served_name": name, "notes": "added with --model"}
        if no_think:
            body["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        hub.models.create(body)
        have.add(name)


async def serve(cfg: Config, tunnel_kind: str, models: list[str] | None = None, no_think: bool = False) -> int:
    try:
        sock = _bind(cfg.host, cfg.port)
    except OSError as e:
        print(f"Could not listen on {cfg.host}:{cfg.port}: {e}", file=sys.stderr, flush=True)
        return 1
    try:
        hub = Hub(cfg)
    except DataDirBusy as e:
        sock.close()
        print(f"Not starting: {e}", file=sys.stderr, flush=True)
        return 1
    app = create_app(hub)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()  # runs startup: recovery, demo seed, demo missions
    await web.SockSite(runner, sock).start()
    _register_models(hub, models or [], no_think)

    local_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
    suffix = "" if cfg.no_auth else f"/#token={cfg.token}"
    local_link = f"http://{local_host}:{cfg.port}{suffix}"
    tunnel = None
    public_link = None
    if tunnel_kind != "none":
        tunnel = Tunnel(tunnel_kind, cfg.port, cfg.data_dir)
        try:
            public_link = (await tunnel.start()).rstrip("/") + suffix
        except TunnelError as e:
            print(f"Tunnel failed: {e}", file=sys.stderr, flush=True)
            tunnel = None

    info = [
        f"Sabha hub {__version__}" + ("  (demo)" if cfg.demo else "") + f"  platform={hub.platform}",
        f"data:   {cfg.data_dir}",
        f"local:  {local_link}",
    ]
    if cfg.research_repo:
        info.append(f"research: {cfg.research_repo}" + ("" if hub.research.available() else "  (no missions/ folder there yet)"))
    if public_link:
        info.append(f"public: {public_link}")
    if not cfg.no_auth:
        info.append(f"token:  {cfg.token}")
    _banner(info)
    print(f"Sabha link: {public_link or local_link}", flush=True)
    hub.notifier.on_link(public_link or local_link)  # the owner's phone gets the link at once (sabha/notify.py)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows: Ctrl+C arrives as task cancellation instead
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break, or CTRL_BREAK_EVENT from a parent
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await stop.wait()
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
    print("Shutting down: stopping runs and child processes...", flush=True)
    if tunnel is not None:
        await tunnel.stop()
    await runner.cleanup()
    print("Sabha hub stopped; state saved.", flush=True)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(args.data or ("./sabha_data_demo" if args.demo else "./sabha_data")).expanduser().resolve()
    token = args.token or os.environ.get("SABHA_TOKEN") or secrets.token_urlsafe(18)
    cfg = Config(host=args.host, port=args.port, data_dir=data, token=token, no_auth=args.no_auth,
                 demo=args.demo, session_hours=args.session_hours,
                 research_repo=str(Path(args.research_repo).expanduser().resolve()) if args.research_repo else "")
    try:
        return asyncio.run(serve(cfg, args.tunnel, args.model, args.no_think))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
