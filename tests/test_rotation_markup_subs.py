from factory.engagement.format_rotation import choose_format, record_format, template_similarity
from factory.engagement.visual_rhythm import parse_number, shot_length, split_durations
from factory.providers.tts.markup import parse
from factory.render.subtitles import group_lines, write_ass, write_srt


def test_format_rotation_blocks_recent(cfg, db):
    for i, ct in enumerate(["story", "listicle", "timeline"]):
        record_format(db, f"j{i}", ct, ["cold_open"], ["stock"])
    picked, _ = choose_format(db, cfg, suggested="story", seed="x")
    assert picked not in ("story", "listicle", "timeline")
    ok, reason = choose_format(db, cfg, suggested="comparison", seed="x")
    assert ok == "comparison" and "предложен" in reason


def test_template_similarity(db):
    record_format(db, "a", "story", ["cold_open", "rising", "finale"], ["stock", "text", "image"])
    sims = template_similarity(db, "b", ["cold_open", "rising", "finale"], ["stock", "text", "image"])
    assert sims[0]["acts"] == 1.0


def test_markup_units():
    plan = parse("[emph]Девять минут[/emph]. [pause:450] Столько у них было. А почему? [breath] Длинная фраза. "
                 "[pause:700] [slow]И вот ответ.[/slow]")
    texts = [u.text for u in plan.units]
    assert texts == ["Девять минут.", "Столько у них было.", "А почему?", "Длинная фраза.", "И вот ответ."]
    assert plan.units[1].pause_before_ms == 450 and plan.units[2].is_question
    assert plan.units[3].breath_before and plan.units[4].slow and plan.units[4].pause_before_ms == 700
    assert [w.text for w in plan.units[0].words if w.emph] == ["Девять", "минут."]


def test_rhythm():
    assert shot_length("rising", 0.9, {}) < shot_length("rising", 0.2, {})
    assert shot_length("climax", 1.0, {}) > shot_length("rising", 0.2, {})
    assert all(d >= 1.4 for d in split_durations(5.0, 1.0, 1.4))
    assert parse_number("2224") == (2224.0, "") and parse_number("46%") == (46.0, "%")
    assert parse_number("слово") is None


def _words(n, gap=0.3):
    return [{"w": f"слово{i}" + ("." if i % 6 == 5 else ""), "start": i * gap, "end": i * gap + 0.25, "emph": i == 2}
            for i in range(n)]


def test_subtitle_lines_and_srt(tmp_path):
    lines = group_lines(_words(14), max_words=5)
    assert all(len(ln) <= 5 for ln in lines)
    srt = write_srt(_words(14), tmp_path / "a.srt").read_text()
    blocks = [b for b in srt.strip().split("\n\n") if b]
    times = [b.split("\n")[1].split(" --> ") for b in blocks]
    for (_a1, b1), (a2, _) in zip(times, times[1:], strict=False):
        assert b1 <= a2, "субтитры не должны перекрываться"


def test_long_word_gets_smaller_font(tmp_path):
    w = [{"w": "достопримечательностями", "start": 0, "end": 1, "emph": False}]
    txt = write_ass(w, tmp_path / "s.ass", 360, 640).read_text()
    assert "\\fs" in txt
