from types import SimpleNamespace

from config import config
from high_win_rate_guard import HighWinRateGuard, wilson_lower_bound


class DummyDb:
    def __init__(self, stats):
        self.stats = stats

    def get_strategy_outcome_stats(self, **kwargs):
        self.kwargs = kwargs
        return dict(self.stats)


def test_wilson_lower_bound_is_conservative_on_tiny_samples():
    assert wilson_lower_bound(3, 3) < 1.0
    assert wilson_lower_bound(0, 3) == 0.0


def test_high_win_rate_guard_blocks_low_model_probability(monkeypatch):
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MODE", True)
    monkeypatch.setattr(config, "TARGET_WIN_RATE", 0.75)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MIN_MODEL_PROB", 0.74)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB", 0.78)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES", 0.03)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MIN_RECENT_SAMPLES", 8)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_REJECT_RECENT_WIN_RATE", 0.50)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_USE_SHADOW_STATS", True)

    db = DummyDb({"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "loss_streak": 0, "pnl": 0.0})
    guard = HighWinRateGuard(db)
    signal = SimpleNamespace(asset="BTC", timeframe="5m", pattern="barrier_reclaim")

    decision = guard.evaluate(signal=signal, strategy_name="barrier_reclaim", model_prob=0.70,
                              token_price=0.50, edge_after_fees=0.20, market_mid=0.50)

    assert not decision.allowed
    assert decision.reason == "high_win_rate_model_prob_low"


def test_high_win_rate_guard_allows_strong_candidate(monkeypatch):
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MODE", True)
    monkeypatch.setattr(config, "TARGET_WIN_RATE", 0.75)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB", 0.78)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES", 0.03)
    monkeypatch.setattr(config, "HIGH_WIN_RATE_MIN_RECENT_SAMPLES", 8)

    db = DummyDb({"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "loss_streak": 0, "pnl": 0.0})
    guard = HighWinRateGuard(db)
    signal = SimpleNamespace(asset="BTC", timeframe="5m", pattern="barrier_reclaim")

    decision = guard.evaluate(signal=signal, strategy_name="barrier_reclaim", model_prob=0.82,
                              token_price=0.44, edge_after_fees=0.34, market_mid=0.45)

    assert decision.allowed
    assert decision.reason == "high_win_rate_model_pass"
