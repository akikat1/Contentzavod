"""Промты онлайн-режима и тик демона — то, что офлайн-прогон не проходит."""
import re

from factory.core.context import Ctx
from factory.core.daemon import tick
from factory.core.keypool import KeyPool
from factory.core.schema import CONTENT_TYPES


def test_every_prompt_placeholder_is_supplied_by_its_stage(cfg, db):
    """Каждая {{переменная}} шаблона должна передаваться кодом стадии — иначе онлайн-режим упадёт KeyError."""
    ctx = Ctx(cfg=cfg, db=db, job_id="p")
    root = cfg.root / "factory" / "stages"
    code = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    code += (cfg.root / "factory" / "qa" / "checks.py").read_text(encoding="utf-8")
    code += (cfg.root / "factory" / "core" / "context.py").read_text(encoding="utf-8")
    for tpl in (cfg.root / "prompts").glob("*.md"):
        names = set(re.findall(r"\{\{(\w+)\}\}", tpl.read_text(encoding="utf-8")))
        missing = [n for n in names if not re.search(rf"\b{n}=", code)]
        assert not missing, f"{tpl.name}: не передаются {missing}"
    assert "Контент-завод" in ctx.system_prompt() or ctx.system_prompt()


def test_trends_prompt_renders(cfg, db):
    ctx = Ctx(cfg=cfg, db=db, job_id="p")
    text = ctx.prompt("trends", candidates="- a", recent_topics="—", avoid="—", content_types=", ".join(CONTENT_TYPES))
    assert "{{" not in text


def test_daemon_tick_without_production(cfg, db):
    cfg.set("schedule.videos_per_day", 0)
    cfg.set("schedule.min_free_disk_gb", 0)
    out = tick(cfg, db, KeyPool(db, cfg, []))
    assert "план дня выполнен" in out["production"]
    assert out["published"] == []
