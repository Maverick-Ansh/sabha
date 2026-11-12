"""System telemetry and platform detection.

Every 5 s: GPUs from nvidia-smi, CPU / RAM (psutil if importable, else /proc on
Linux, else zeros) and disk usage of the data dir, broadcast as the ephemeral
sys.telemetry event. In --demo mode on a box without a GPU we synthesise two
"Tesla T4 (demo)" cards whose load follows the number of active mock runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
import time

log = logging.getLogger("sabha.telemetry")

try:
    import psutil  # optional
except ImportError:  # pragma: no cover - depends on the box
    psutil = None

INTERVAL_S = 5.0
SMI_QUERY = "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
T4_MEM_MB = 15360


def detect_platform() -> str:
    if os.environ.get("COLAB_RELEASE_TAG"):
        return "colab"
    if os.environ.get("KAGGLE_KERNEL_RUN_TYPE"):
        return "kaggle"
    return "local"


def _num(text: str) -> float:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return 0.0


class Telemetry:
    def __init__(self, hub):
        self.hub = hub
        self.smi = shutil.which("nvidia-smi")
        self.last_event: dict | None = None
        self._task: asyncio.Task | None = None
        self._cpu_prev: tuple[float, float] | None = None
        self._fake_util = [3.0, 2.0]
        self._fake_mem = [420.0, 380.0]
        self._rng = random.Random()
        if psutil is not None:
            psutil.cpu_percent(None)  # prime: the first call always returns 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                data = await self.sample()
                self.last_event = self.hub.bus.emit("sys.telemetry", data, ephemeral=True)
            except Exception:
                log.exception("telemetry sample failed")
            await asyncio.sleep(INTERVAL_S)

    async def sample(self) -> dict:
        gpus = await self._gpus()
        if not gpus and self.hub.cfg.demo:
            gpus = self._fake_gpus()
        ram_used, ram_total = self._ram()
        disk_used, disk_total = self._disk()
        return {
            "gpus": gpus,
            "cpu_pct": round(self._cpu(), 1),
            "ram_used_mb": ram_used,
            "ram_total_mb": ram_total,
            "disk_used_gb": disk_used,
            "disk_total_gb": disk_total,
            "uptime_s": round(time.time() - self.hub.started_ts, 1),
            "session_limit_s": self.hub.session_limit_s,
        }

    async def _gpus(self) -> list[dict]:
        if not self.smi:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                self.smi, SMI_QUERY, "--format=csv,noheader,nounits",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError:
            return []
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 4)
        except asyncio.TimeoutError:
            proc.kill()
            return []
        gpus = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6:
                continue
            gpus.append({
                "index": int(_num(parts[0])),
                "name": parts[1],
                "util": _num(parts[2]),
                "mem_used_mb": _num(parts[3]),
                "mem_total_mb": _num(parts[4]),
                "temp_c": _num(parts[5]),
            })
        return gpus

    def _fake_gpus(self) -> list[dict]:
        active = self.hub.missions.active_count("mock")
        per_gpu = [(active + 1) // 2, active // 2]
        gpus = []
        for i in range(2):
            n = per_gpu[i]
            target = min(96.0, 12.0 + 41.0 * n) if n else 2.0
            util = self._fake_util[i]
            util += (target - util) * 0.45 + self._rng.gauss(0, 3.5 if n else 0.8)
            util = min(100.0, max(0.0, util))
            self._fake_util[i] = util
            mem_target = 410.0 + 4700.0 * n
            mem = self._fake_mem[i] + (mem_target - self._fake_mem[i]) * 0.5 + self._rng.gauss(0, 40)
            mem = min(float(T4_MEM_MB - 200), max(300.0, mem))
            self._fake_mem[i] = mem
            gpus.append({
                "index": i,
                "name": "Tesla T4 (demo)",
                "util": round(util, 1),
                "mem_used_mb": round(mem),
                "mem_total_mb": T4_MEM_MB,
                "temp_c": round(37 + util * 0.36 + self._rng.uniform(-0.6, 0.6), 1),
            })
        return gpus

    def _cpu(self) -> float:
        if psutil is not None:
            return float(psutil.cpu_percent(None))
        try:
            with open("/proc/stat") as f:
                fields = [float(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return 0.0
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        total = sum(fields)
        prev = self._cpu_prev
        self._cpu_prev = (idle, total)
        if prev is None or total <= prev[1]:
            return 0.0
        return 100.0 * (1.0 - (idle - prev[0]) / (total - prev[1]))

    def _ram(self) -> tuple[float, float]:
        if psutil is not None:
            vm = psutil.virtual_memory()
            return round((vm.total - vm.available) / 2**20), round(vm.total / 2**20)
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = float(rest.split()[0])  # kB
            total = info["MemTotal"]
            avail = info.get("MemAvailable", info.get("MemFree", 0.0))
            return round((total - avail) / 1024), round(total / 1024)
        except (OSError, KeyError, ValueError, IndexError):
            return 0, 0

    def _disk(self) -> tuple[float, float]:
        try:
            du = shutil.disk_usage(self.hub.cfg.data_dir)
            return round(du.used / 2**30, 1), round(du.total / 2**30, 1)
        except OSError:
            return 0.0, 0.0
