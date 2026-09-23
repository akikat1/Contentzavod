import pytest

from factory.core.schema import CONTENT_TYPES, Beat
from factory.engagement.beatsheet_rules import peak_indices, validate_beats

from .helpers import beat, sheet


@pytest.mark.parametrize("ct", CONTENT_TYPES)
@pytest.mark.parametrize("target", [75, 420, 600])
def test_fixture_sheets_pass_for_every_format(cfg, ct, target):
    sh = sheet(cfg, ct, target)
    assert validate_beats(sh.beats, cfg.section("beatsheet"), target) == []


def good() -> list[dict]:
    return [beat("b01", "cold_open", 8, 0.9, "parallax"), beat("b02", "promise", 20, 0.5, "text", opens_loop="q"),
            beat("b03", "context", 30, 0.35, "stock"), beat("b04", "rising", 30, 0.6, "image"),
            beat("b05", "twist", 30, 0.8, "counter"), beat("b06", "explanation", 30, 0.4, "diagram"),
            beat("b07", "climax", 30, 1.0, "parallax", closes_loop="q"), beat("b08", "finale", 15, 0.6, "text",
                                                                                callback_to="b01")]


def check(beats):
    return validate_beats([Beat.model_validate(b) for b in beats], {"min_beats": 7}, None)


def test_good_sheet_passes():
    assert check(good()) == []


@pytest.mark.parametrize("mut, needle", [
    (lambda b: b[0].update(act="context"), "cold_open"),
    (lambda b: b[0].update(target_duration_s=25), "cold_open длится"),
    (lambda b: b[6].update(closes_loop=None), "нигде не закрыта"),
    (lambda b: b[3].update(visual_register="stock"), "одном визуальном регистре"),
    (lambda b: b[7].update(callback_to=None), "callback"),
    (lambda b: b[4].update(target_duration_s=70), "без смены подачи"),
    (lambda b: [x.update(tension=0.5) for x in b], "плоская"),
    (lambda b: b[6].update(closes_loop="q", target_duration_s=5) or b[1].update(target_duration_s=3) or
     [x.update(target_duration_s=3) for x in b[2:6]], "слишком рано"),
])
def test_violations_are_reported(mut, needle):
    b = good()
    mut(b)
    probs = check(b)
    assert any(needle in p for p in probs), probs


def test_peaks_need_prominence():
    assert peak_indices([0.9, 0.4, 0.8, 0.3, 1.0, 0.6], 0.15) == [0, 2, 4]
    assert peak_indices([0.5, 0.55, 0.5, 0.52], 0.15) == []
