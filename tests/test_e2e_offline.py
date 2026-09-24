"""Сквозной офлайн-прогон всех 16 стадий: фикстурный LLM, espeak-ng, процедурные ассеты,
публикация в песочницу. Проверяет артефакты, идемпотентность и защиту от публикации синтетики."""
import json
import shutil

import pytest

from factory.core import media
from factory.core.pipeline import create_job, run_job
from factory.providers.audio.procedural import bootstrap
from factory.stages import s16_publish

pytestmark = [pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("espeak-ng")),
                                 reason="нужны ffmpeg и espeak-ng"), pytest.mark.e2e]


@pytest.fixture
def small(cfg):
    for k, v in {"beatsheet.target_duration_s": 90, "render.width": 320, "render.height": 180, "render.fps": 12,
                 "render.quality": "draft", "shorts.width": 180, "shorts.height": 320, "shorts.min_s": 8}.items():
        cfg.set(k, v)
    bootstrap(cfg.path("paths.assets") / "generated")
    return cfg


def test_full_pipeline_offline(small, db):
    cfg = small
    job = create_job(db, cfg, "Как карта холеры Джона Сноу остановила эпидемию", "story")
    manifest = run_job(cfg, db, job)
    root = cfg.path("paths.workspace") / "jobs" / job
    master = root / "out" / "master.mp4"
    assert master.exists() and media.duration(master) > 30
    qa = json.loads((root / "qa_report.json").read_text())
    assert qa["passed"], qa["failed"]
    vp = json.loads((root / "voiceplan.json").read_text())
    assert vp["strategy"] == "cast" and vp["invariant_problems"] == []
    assert len(vp["speaker_voices"]) == 2, "у двух персонажей — свои голоса"
    assert list((root / "shorts").glob("short_*.mp4")), "шортсы должны быть"
    assert (root / "out" / "thumbnail.jpg").exists()
    assert manifest["synthetic"], "фикстурный контент помечается синтетическим"

    rows = db.all("SELECT * FROM publications WHERE job_id=?", (job,))
    assert rows and all(r["url"].startswith("sandbox://") for r in rows if r["status"] == "done")
    before = len(rows)
    s16_publish.process_due(cfg, db, only_job=job)          # повтор не публикует дважды
    assert len(db.all("SELECT * FROM publications WHERE job_id=?", (job,))) == before
    assert db.one("SELECT COUNT(*) AS n FROM platform_daily")["n"] == 0, "песочница не тратит лимиты площадок"

    again = run_job(cfg, db, job)                           # идемпотентность: всё уже сделано
    assert all(s.get("skipped") for s in again["stages"].values())
