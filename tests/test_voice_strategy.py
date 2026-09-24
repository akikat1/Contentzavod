"""Voice Strategy: схема голосов по типу контента и инварианты смены голоса."""
import pytest

from factory.core.schema import Script
from factory.engagement import voice_strategy as vs
from factory.providers.tts.voice_library import VoiceLibrary

from .helpers import beat, script, seg

LONG = "Это длинная фраза нарратора, которая звучит достаточно долго, чтобы быть отдельным смысловым блоком. " * 2


def build(ct, beats, segs, speakers=()):
    return Script.model_validate({
        "job_id": "j", "content_type": ct, "topic": "t", "title_candidates": [{"text": "x"}],
        "target_duration_s": 120, "speakers": list(speakers), "beats": beats, "segments": segs,
        "sources": [{"id": "r01", "url": "u"}]})


def five_beats():
    return [beat("b01", "cold_open", 8, 0.9, "parallax"), beat("b02", "context", 20, 0.4, "stock"),
            beat("b03", "myth", 20, 0.5, "text"), beat("b04", "rising", 20, 0.7, "image"),
            beat("b05", "finale", 15, 0.6, "counter")]


@pytest.fixture
def lib(cfg):
    return VoiceLibrary.for_config(cfg)


@pytest.mark.parametrize("ct, expected", [
    ("investigation", "dual_narrator"), ("listicle", "dual_narrator"), ("comparison", "dual_narrator"),
    ("myth_vs_fact", "dual_opposing"), ("story", "cast"), ("case_study", "cast"), ("timeline", "single_plus_quote"),
    ("explainer", "single_plus_quote"),
])
def test_strategy_by_content_type(cfg, ct, expected):
    sc = script(cfg, ct)
    assert vs.choose_strategy(sc, cfg)[0] == expected


def test_simple_explainer_gets_one_voice(cfg):
    segs = [seg(f"s{i}", f"b0{i}", "main_narrator", LONG) for i in range(1, 6)]
    sc = build("explainer", five_beats(), segs)
    assert vs.choose_strategy(sc, cfg)[0] == "single"


def test_story_without_characters_is_downgraded(cfg):
    segs = [seg(f"s{i}", f"b0{i}", "main_narrator", LONG) for i in range(1, 6)]
    strategy, reasons = vs.choose_strategy(build("story", five_beats(), segs), cfg)
    assert strategy == "single" and any("понижена" in r for r in reasons)


def test_no_narrator_switch_inside_block(cfg):
    segs = [seg("s1", "b01", "main_narrator", LONG), seg("s2", "b02", "main_narrator", LONG),
            seg("s3", "b02", "secondary_narrator", "Короткая вставка."), seg("s4", "b03", "main_narrator", LONG),
            seg("s5", "b04", "main_narrator", LONG), seg("s6", "b05", "main_narrator", LONG)]
    sc = build("investigation", five_beats(), segs)
    out, log = vs.normalize_roles(sc, "dual_narrator", cfg)
    assert {s.role for s in out if s.block_id == "b02"} == {"main_narrator"}


def test_short_secondary_run_collapses_to_main(cfg):
    segs = [seg("s1", "b01", "main_narrator", LONG), seg("s2", "b02", "main_narrator", LONG),
            seg("s3", "b03", "secondary_narrator", "Всего одна короткая фраза."),
            seg("s4", "b04", "main_narrator", LONG), seg("s5", "b05", "main_narrator", LONG)]
    out, log = vs.normalize_roles(build("investigation", five_beats(), segs), "dual_narrator", cfg)
    assert all(s.role == "main_narrator" for s in out)
    assert any("< 12" in line for line in log)


def test_frame_is_main_narrator(cfg):
    segs = [seg("s1", "b01", "secondary_narrator", LONG), seg("s2", "b02", "main_narrator", LONG),
            seg("s3", "b03", "main_narrator", LONG), seg("s4", "b04", "main_narrator", LONG),
            seg("s5", "b05", "secondary_narrator", LONG)]
    out, _ = vs.normalize_roles(build("investigation", five_beats(), segs), "dual_narrator", cfg)
    assert out[0].role == "main_narrator" and out[-1].role == "main_narrator"


def test_switch_rate_is_capped(cfg):
    beats = [beat(f"b{i:02d}", "cold_open" if i == 1 else "finale" if i == 12 else "rising", 8,
                  0.9 if i in (1, 11) else 0.4, "parallax" if i % 2 else "stock",
                  **({"callback_to": "b01"} if i == 12 else {})) for i in range(1, 13)]
    text = "Средняя фраза нарратора примерно на тринадцать секунд звучания, чтобы пройти порог длины роли. " * 2
    segs = [seg(f"s{i}", f"b{i:02d}", "secondary_narrator" if i % 2 == 0 else "main_narrator", text)
            for i in range(1, 13)]
    sc = build("investigation", beats, segs)
    out, _ = vs.normalize_roles(sc, "dual_narrator", cfg)
    fixed = sc.model_copy(update={"segments": out})
    assert not [p for p in vs.check_invariants(fixed, cfg) if "смен нарратора" in p]


def test_short_direct_speech_is_read_by_narrator(cfg):
    segs = [seg("s1", "b01", "main_narrator", LONG), seg("s2", "b02", "main_narrator", LONG),
            seg("s3", "b02", "quote", "Да.", speaker="Капитан"), seg("s4", "b03", "main_narrator", LONG),
            seg("s5", "b04", "main_narrator", LONG), seg("s6", "b05", "main_narrator", LONG)]
    sc = build("timeline", five_beats(), segs, [{"name": "Капитан", "kind": "quote_author"}])
    out, _ = vs.normalize_roles(sc, "single_plus_quote", cfg)
    assert next(s for s in out if s.id == "s3").role == "main_narrator"


def test_dialogue_inside_block_is_allowed(cfg, lib):
    sc = script(cfg, "story")
    segs, log = vs.normalize_roles(sc, "cast", cfg)
    assert sum(1 for s in segs if s.role == "character") >= 2
    plan = vs.assign_voices(sc, segs, "cast", lib, cfg, None)
    fixed = sc.model_copy(update={"segments": segs, "voice_plan": plan})
    assert vs.check_invariants(fixed, cfg) == []
    assert len(set(plan.speaker_voices.values())) == len(plan.speaker_voices), "разным персонажам — разные голоса"


def test_foreign_speaker_gets_accented_voice_and_archive_quote_gets_radio(cfg, lib):
    segs = [seg("s1", "b01", "main_narrator", LONG), seg("s2", "b02", "main_narrator", LONG),
            seg("s3", "b02", "character", "Мы не можем это отменить, слишком поздно.", speaker="Mr. Smith"),
            seg("s4", "b03", "main_narrator", LONG),
            seg("s5", "b03", "quote", "Катастрофа, которой можно было избежать.", speaker="The Times, 1912"),
            seg("s6", "b04", "main_narrator", LONG), seg("s7", "b05", "main_narrator", LONG)]
    sc = build("story", five_beats(), segs, [
        {"name": "Mr. Smith", "kind": "character", "gender": "male", "origin": "foreign"},
        {"name": "The Times, 1912", "kind": "quote_author", "gender": "male", "origin": "native", "era": "1912"}])
    norm, _ = vs.normalize_roles(sc, "cast", cfg)
    plan = vs.assign_voices(sc, norm, "cast", lib, cfg, None)
    smith = lib.get(plan.speaker_voices["Mr. Smith"])
    times = lib.get(plan.speaker_voices["The Times, 1912"])
    assert not smith.native and smith.gender == "male"
    assert times.fx_chain == "radio_1940"


def test_character_voices_rotate_between_episodes(cfg, lib, db):
    first = script(cfg, "story", job_id="ep1")
    segs, _ = vs.normalize_roles(first, "cast", cfg)
    plan1 = vs.assign_voices(first, segs, "cast", lib, cfg, db)
    vs.record_voice_history(db, first, plan1, "main")
    second = script(cfg, "story", job_id="ep2")
    segs2, _ = vs.normalize_roles(second, "cast", cfg)
    plan2 = vs.assign_voices(second, segs2, "cast", lib, cfg, db)
    assert set(plan1.speaker_voices.values()).isdisjoint(plan2.speaker_voices.values()), \
        "персонажи нового выпуска не должны звучать голосами прошлого"
    assert plan1.assignments["main_narrator"] == plan2.assignments["main_narrator"], "голос канала постоянен"


def test_explain_lists_every_switch_with_reason(cfg, lib):
    sc = script(cfg, "story")
    segs, _ = vs.normalize_roles(sc, "cast", cfg)
    sc = sc.model_copy(update={"segments": segs, "voice_plan": vs.assign_voices(sc, segs, "cast", lib, cfg, None)})
    lines = vs.explain_switches(sc, cfg)
    assert lines and all("—" in ln for ln in lines)
