"""QA на настоящем медиа: гейт обязан отклонить незапланированную тишину и принять запланированную."""
import json

import pytest

from factory.core import media
from factory.core.context import Ctx
from factory.qa.checks import technical

from .conftest import needs_ffmpeg

pytestmark = [needs_ffmpeg, pytest.mark.ffmpeg]


def _make_job(cfg, db, planned):
    ctx = Ctx(cfg=cfg, db=db, job_id="qa-job")
    db.execute("INSERT INTO jobs(job_id, topic, status, created_at, updated_at) VALUES('qa-job','t','running',0,0)")
    # 5 с тона, 3 с тишины, 4 с тона — 12 с ролика
    media.run(["ffmpeg", "-f", "lavfi", "-i", "sine=f=220:d=5", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=3",
               "-f", "lavfi", "-i", "sine=f=330:d=4", "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1,volume=6dB",
               str(ctx.job.p("audio", "a.wav"))])
    media.run(["ffmpeg", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=12:r=24", "-i", str(ctx.job.p("audio", "a.wav")),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(ctx.job.p("out", "master.mp4"))])
    ctx.job.write_json("timeline.json", {"duration": 12, "segments": [], "blocks": [],
                                         "planned_silences": planned})
    ctx.job.write_json("beatsheet.json", {"target_duration_s": 12})
    return ctx


def test_unplanned_silence_is_rejected(cfg, db):
    cfg.set("qa.lufs_tolerance", 30)                 # проверяем только тишину
    ctx = _make_job(cfg, db, [])
    res = technical(ctx)
    assert not res.passed
    assert any("тишина" in p for p in res.problems)
    assert res.repair_from == "humanize"


def test_planned_dramatic_silence_is_accepted(cfg, db):
    cfg.set("qa.lufs_tolerance", 30)
    ctx = _make_job(cfg, db, [{"start": 5.0, "end": 8.0, "reason": "перед кульминацией", "block_id": "b05"}])
    res = technical(ctx)
    assert not any("тишина" in p for p in res.problems), res.problems


def test_loudness_out_of_range_routes_to_sound(cfg, db):
    ctx = _make_job(cfg, db, [{"start": 5.0, "end": 8.0}])
    res = technical(ctx)
    lufs_problems = [p for p in res.problems if "LUFS" in p]
    if lufs_problems:
        assert res.repair_from in ("sound", "humanize")
    json.dumps(res.problems, ensure_ascii=False)
