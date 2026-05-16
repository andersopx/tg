import unittest
from backtest import Candle, BacktestParams, evaluate_window, normalize_open_time_ms, run_backtest, taker_fee_per_share, eval_index_for_seconds_left


def make_candles(start=0, n=40, base=100000.0, step=20.0):
    out = []
    price = base
    for i in range(n):
        open_p = price
        close_p = price + step
        high = max(open_p, close_p) + 5
        low = min(open_p, close_p) - 5
        out.append(Candle(open_time_ms=(start + i * 60) * 1000, open=open_p, high=high, low=low, close=close_p, volume=1.0))
        price = close_p
    return out


class BacktestCoreTests(unittest.TestCase):
    def test_taker_fee_shape(self):
        self.assertAlmostEqual(taker_fee_per_share(0.5, 0.07), 0.0175, places=6)
        self.assertLess(taker_fee_per_share(0.9, 0.07), taker_fee_per_share(0.5, 0.07))

    def test_eval_index_no_lookahead(self):
        self.assertEqual(eval_index_for_seconds_left(10, 60), 13)
        self.assertEqual(eval_index_for_seconds_left(10, 12), 13)
        self.assertEqual(eval_index_for_seconds_left(10, 120), 12)

    def test_evaluate_window_up_signal(self):
        candles = make_candles(n=50, step=30.0)
        p = BacktestParams(entry_seconds_left=60, min_model_prob=0.55, min_price_distance_usd=5.0, assumed_ask=0.55)
        row = evaluate_window(candles, 20, p)
        self.assertIsNotNone(row)
        self.assertEqual(row.direction, "UP")
        self.assertEqual(row.outcome, "UP")
        self.assertEqual(row.win, 1)
        self.assertIsNotNone(row.pnl_per_share)


    def test_normalize_open_time_ms_handles_binance_vision_microseconds(self):
        self.assertEqual(normalize_open_time_ms(1746057600000000), 1746057600000)
        self.assertEqual(normalize_open_time_ms(1746057600000), 1746057600000)

    def test_run_backtest_finds_multiple_windows(self):
        candles = make_candles(n=80, step=15.0)
        p = BacktestParams(entry_seconds_left=60, min_model_prob=0.55, min_price_distance_usd=5.0)
        rows = run_backtest(candles, p)
        self.assertGreater(len(rows), 2)
        self.assertTrue(all(r.direction == "UP" for r in rows))


if __name__ == "__main__":
    unittest.main()
