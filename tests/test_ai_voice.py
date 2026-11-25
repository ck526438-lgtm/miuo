import pandas as pd
import pytest

from miuo.analysis import analyze, fr, trade_speech
from miuo.config import MiuoConfig
from miuo.data import synthetic
from miuo.execution import PaperBroker
from miuo.live import LiveEngine
from miuo.validation import report_path, validate
from miuo.voice import Voice


@pytest.fixture
def cfg(tmp_path):
    c = MiuoConfig()
    c.data_dir = str(tmp_path)
    c.broker.live_bars = 600
    return c


def test_ai_learns_conditional_edge_and_is_loaded_live(cfg):
    """La tendance n'existe que de 8 h à 14 h : l'IA doit l'apprendre et améliorer l'espérance hors échantillon."""
    cfg.strategy.name = "trend_pullback"
    rep = validate(synthetic(100_000, regime_drift=0.35, regime_len=150, seed=4, drift_hours=(8, 14)), cfg)
    comp = rep.ai_comparison
    assert rep.ai_enabled
    assert comp["avec_ia"]["expectancy_r"] > comp["sans_ia"]["expectancy_r"] + 0.05
    rep.save(report_path(cfg))
    eng = LiveEngine(cfg, PaperBroker(synthetic(1500, seed=9), cfg), log=lambda m: None,
                     voice=Voice(speaker=lambda t: None))
    assert eng.prepare() and eng.ai is not None and eng.ai.ready


def test_ai_disabled_when_it_does_not_help(cfg):
    rep = validate(synthetic(60_000, regime_drift=0.25, seed=3), cfg)
    if not rep.ai_enabled:
        assert rep.ai_comparison["avec_ia"]["expectancy_r"] <= rep.ai_comparison["sans_ia"]["expectancy_r"] \
            or rep.ai_comparison["avec_ia"]["trades"] < cfg.gate.min_trades


def test_analysis_and_speech():
    df = synthetic(3000, regime_drift=0.3, seed=2)
    v = analyze(df, "XAUUSD")
    assert v.volatility in ("faible", "normale", "élevée")
    assert v.trend_h4 in ("haussière", "baissière", "sans direction claire")
    assert "l'or" in v.speech() and "Tendance" in v.text()
    s = trade_speech("XAUUSD", 1, 2650.4, 0.12, 2646.9, 2657.4, 2.0, "cassure", v, 0.55)
    assert s.startswith("Achat sur l'or à 2650,40") and "0,12 lot" in s and "55 pour cent" in s
    assert fr(1.5, 1) == "1,5"


def test_live_announces_trades_and_closes_by_voice(cfg):
    said = []
    voice = Voice(speaker=said.append)
    df = synthetic(1600, regime_drift=0.25, seed=5)
    analyses = []
    LiveEngine(cfg, PaperBroker(df, cfg), log=lambda m: None, voice=voice, on_analysis=analyses.append).run()
    voice.wait()
    assert said[0].startswith("Miuo est connecté")
    assert any(t.startswith(("Achat sur l'or", "Vente sur l'or")) and "Stop à" in t for t in said)
    assert any(t.startswith("Trade clôturé") for t in said)
    assert analyses and "Tendance" in analyses[-1]


def test_auto_reconnect(cfg):
    class Flaky(PaperBroker):
        calls = 0

        def reconnect(self):
            Flaky.calls += 1

    said = []
    voice = Voice(speaker=said.append)
    eng = LiveEngine(cfg, Flaky(synthetic(800, seed=1), cfg), log=lambda m: None, voice=voice)
    eng._reconnect()
    voice.wait()
    assert Flaky.calls == 1 and said[-1] == "Reconnecté."


def test_config_roundtrip_new_sections(tmp_path):
    c = MiuoConfig()
    c.voice.rate, c.ai.enabled, c.broker.auto_start = 3, False, True
    c.save(tmp_path / "c.json")
    d = MiuoConfig.load(tmp_path / "c.json")
    assert d.voice.rate == 3 and d.ai.enabled is False and d.broker.auto_start is True
