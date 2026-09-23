"""Глобальный мьютекс GPU.

На GTX 1650 (4 ГБ) две модели одновременно не помещаются: Whisper + MiDaS или
RVC + SDXL-Turbo гарантированно дают OOM. Поэтому любая стадия, которая
трогает CUDA, берёт межпроцессную блокировку и при выходе выгружает модель.
"""
from __future__ import annotations

import gc
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("factory.gpu")

try:                                   # Linux/macOS
    import fcntl

    def _try_lock(fh) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
except ImportError:                    # Windows
    import msvcrt

    def _try_lock(fh) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def free_cuda() -> None:
    gc.collect()
    try:
        import torch  # noqa: PLC0415 — torch необязателен

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def gpu_session(lock_path: Path, owner: str, unload: Callable[[], None] | None = None,
                timeout_s: float = 3600) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")  # noqa: SIM115 — держим открытым на время блокировки
    t0 = time.time()
    while not _try_lock(fh):
        if time.time() - t0 > timeout_s:
            fh.close()
            raise TimeoutError(f"GPU занят дольше {timeout_s} с, стадия {owner} не дождалась")
        time.sleep(0.5)
    waited = time.time() - t0
    if waited > 1:
        log.info("GPU получен стадией %s после ожидания %.1f с", owner, waited)
    try:
        yield
    finally:
        try:
            if unload:
                unload()
        finally:
            free_cuda()
            _unlock(fh)
            fh.close()
