import pytest
from pydantic import ValidationError

from factory.core.schema import Script, plain_text

from .helpers import script


def test_fixture_script_is_valid(cfg):
    sc = script(cfg)
    assert sc.segments and sc.beats


def _mutate(cfg, fn):
    data = script(cfg).model_dump()
    fn(data)
    return data


@pytest.mark.parametrize("mut, err", [
    (lambda d: d["segments"][0].update(block_id="b99"), "несуществующий блок"),
    (lambda d: d["segments"][0].update(source_ids=["zzz"]), "неизвестный источник"),
    (lambda d: d["segments"][0].update(text="[emph]не закрыт"), "не закрыт"),
    (lambda d: d["segments"][0].update(text="[pause:9999] долго"), "pause"),
    (lambda d: d["segments"][0].update(role="quote", speaker=None), "требует speaker"),
    (lambda d: d["shorts_candidates"].append({"block_ids": ["b01", "b03"], "hook": "x"}), "подряд идущих"),
    (lambda d: d["segments"][0].update(visual={"marker": "COUNTER"}), "требует поля"),
])
def test_contract_violations_are_caught(cfg, mut, err):
    with pytest.raises(ValidationError, match=err):
        Script.model_validate(_mutate(cfg, mut))


def test_plain_text_strips_markup():
    assert plain_text("[emph]Девять[/emph] минут. [pause:300] [slow]Итак[/slow]") == "Девять минут. Итак"
