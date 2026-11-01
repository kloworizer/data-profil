"""Settings page: values are checked, kept across restarts, and switch EDA features."""
from __future__ import annotations

import pytest

from autoeda import config, settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")
    yield
    settings.reset()


def test_save_check_and_reset():
    settings.save({"EDA_MULTIVARIATE": False, "LLM_BATCH": "8", "PDF_ENGINE": "plumber"})
    assert config.EDA_MULTIVARIATE is False and config.LLM_BATCH == 8 and config.PDF_ENGINE == "plumber"
    assert settings.saved() == {"EDA_MULTIVARIATE": False, "LLM_BATCH": 8, "PDF_ENGINE": "plumber"}
    for bad in ({"LLM_BATCH": 0}, {"PDF_ENGINE": "magic"}, {"EDA_QUALITY": "yes"}, {"NOPE": 1}):
        with pytest.raises(ValueError):
            settings.save(bad)
    config.EDA_MULTIVARIATE = True               # as after a restart: the file brings it back
    settings.apply()
    assert config.EDA_MULTIVARIATE is False
    settings.reset()
    assert config.EDA_MULTIVARIATE is True and settings.saved() == {}


def test_every_setting_is_described_in_both_languages():
    for lang in ("en", "id"):
        for s in settings.describe(lang):
            assert s["label"] and s["group_label"] and s["value"] is not None


def _csv(tmp_path):
    p = tmp_path / "sales.csv"
    p.write_text("region,units,price,revenue\n" + "".join(
        f"{'North' if i % 2 else 'South'},{i},{10 + i % 7},{i * (10 + i % 7)}\n" for i in range(1, 80))
        + ",,,\nNorth,5,N/A,50\n", encoding="utf-8")
    return p


def test_features_switched_off(tmp_path):
    from autoeda.pipeline import run
    on = run(_csv(tmp_path), tmp_path / "on.html", use_llm=False)["tables"][0]
    assert on["correlations"] and any(c["flags"] for c in on["columns"]) and on["duplicates"] is not None
    settings.save({"EDA_MULTIVARIATE": False, "EDA_QUALITY": False, "EDA_UNIVARIATE": False, "EDA_DUPLICATES": False})
    res = run(_csv(tmp_path), tmp_path / "off.html", use_llm=False)
    t = res["tables"][0]
    assert t["correlations"] == [] and t["duplicates"] is None and t["n_warnings"] == 0
    assert not any(c["flags"] or c["histogram"] for c in t["columns"])
    assert res["features"] == {"descriptive": True, "univariate": False, "multivariate": False, "quality": False,
                               "duplicates": False, "memory": True}
    html = (tmp_path / "off.html").read_text(encoding="utf-8")
    assert 'class="box lead' not in html and "noq" in html
    # without univariate analysis a column's card still opens, just without charts
    assert 'class="col"' in html and 'class="hist"' not in html
