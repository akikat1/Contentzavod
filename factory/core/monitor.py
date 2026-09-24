"""Логирование и контроль пиков ресурсов.

На этом железе критерий «работает» — это VRAM < 3.6 ГБ и RSS < 12 ГБ за весь
прогон. Монитор опрашивает дерево процессов (включая дочерние ffmpeg) и
nvidia-smi в фоне и пишет пики в манифест джоба.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _tree_rss_bytes(pid: int) -> int:
    """RSS процесса и всех потомков: /proc на Linux, psutil (если стоит) на других ОС."""
    if not os.path.isdir("/proc"):
        try:
            import psutil  # noqa: PLC0415
            p = psutil.Process(pid)
            return p.memory_info().rss + sum(c.memory_info().rss for c in p.children(recursive=True))
        except Exception:  # noqa: BLE001
            return 0
    total = 0
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                parts = fh.read().rsplit(")", 1)[1].split()
            ppid = int(parts[1])
            with open(f"/proc/{entry}/statm") as fh:
                rss[int(entry)] = int(fh.read().split()[1]) * page
            children.setdefault(ppid, []).append(int(entry))
        except (OSError, IndexError, ValueError):
            continue
    stack = [pid]
    while stack:
        p = stack.pop()
        total += rss.get(p, 0)
        stack.extend(children.get(p, []))
    return total


def _vram_used_mb() -> float | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=5)
        if out.returncode == 0:
            return float(out.stdout.decode().split()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return None


class ResourceMonitor:
    def __init__(self, interval_s: float = 1.0):
        self.interval = interval_s
        self.peak_rss_mb = 0.0
        self.peak_vram_mb: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._pid = os.getpid()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval)

    def sample(self) -> None:
        rss = _tree_rss_bytes(self._pid) / 2**20
        self.peak_rss_mb = max(self.peak_rss_mb, rss)
        v = _vram_used_mb()
        if v is not None:
            self.peak_vram_mb = max(self.peak_vram_mb or 0.0, v)

    def __enter__(self) -> ResourceMonitor:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.sample()

    def report(self) -> dict:
        return {"peak_rss_mb": round(self.peak_rss_mb, 1),
                "peak_vram_mb": None if self.peak_vram_mb is None else round(self.peak_vram_mb, 1),
                "sampled_at": time.time()}
