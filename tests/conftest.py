from __future__ import annotations

import shutil

import pytest

from factory.core.config import load_config
from factory.core.db import DB

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")


@pytest.fixture
def cfg(tmp_path):
    return load_config({
        "paths.workspace": str(tmp_path / "ws"), "paths.db": str(tmp_path / "f.db"),
        "paths.cache": str(tmp_path / "cache"), "paths.assets": str(tmp_path / "assets"),
        "paths.gpu_lock": str(tmp_path / "gpu.lock"), "paths.logs": str(tmp_path / "logs"),
        "sound.sfx_dir": str(tmp_path / "assets/generated/sfx"), "sound.music_dir": str(tmp_path / "assets/music"),
        "voice.room_ir.file": str(tmp_path / "assets/generated/ir/room_small.wav"),
        "llm.keys_file": str(tmp_path / "keys.yaml"), "runtime.offline": "true", "runtime.dry_run": "true",
    })


@pytest.fixture
def db(cfg):
    d = DB(cfg.path("paths.db"))
    yield d
    d.close()
