import os
import tempfile
import unittest

from database import Database
from risk_manager import RiskManager
from polymarket_client import PolymarketClient
from config import config, _is_placeholder_secret


class FakeFeed:
    def get_price_change_pct(self, minutes):
        return 0.0


class TestConfigSafety(unittest.TestCase):
    def test_placeholder_secrets_are_treated_as_missing(self):
        self.assertTrue(_is_placeholder_secret("your_private_key_here"))
        self.assertTrue(_is_placeholder_secret("your_wallet_address_here"))
        self.assertFalse(_is_placeholder_secret("0xabc123realish"))


class TestDatabase(unittest.TestCase):
    def test_insert_trade_fields_not_shifted(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            trade_id = db.insert_trade({
                "window_ts": 1778257800,
                "market_slug": "btc-updown-5m-1778257800",
                "market_question": "BTC Up or Down 5m",
                "token_id": "token-up",
                "direction": "Up",
                "entry_price": 0.55,
                "size": 1.0,
                "shares": 1.818,
                "cost": 1.0,
                "signal_data": {"x": 1},
            })
            row = db.get_trade_by_id(trade_id)
            self.assertEqual(row["token_id"], "token-up")
            self.assertEqual(row["direction"], "Up")
            self.assertEqual(row["market_question"], "BTC Up or Down 5m")


class TestRiskManager(unittest.TestCase):
    def test_balance_unavailable_does_not_halt(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            rm = RiskManager(db, FakeFeed())
            chk = rm.can_trade(None)
            self.assertFalse(chk.can_trade)
            self.assertFalse(rm.is_halted)
            self.assertIn("Balance unavailable", chk.reason)

    def test_low_balance_halts(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            rm = RiskManager(db, FakeFeed())
            chk = rm.can_trade(0.0)
            self.assertFalse(chk.can_trade)
            self.assertTrue(rm.is_halted)


class TestPolymarketParser(unittest.TestCase):
    def test_parse_market_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            pm = PolymarketClient(db)
            raw = {
                "slug":"btc-updown-5m-1778257800",
                "question":"BTC Up or Down 5m",
                "description":"Resolution source is Chainlink BTC/USD data stream. Price to Beat: $104,250.75. End greater than or equal to start resolves Up.",
                "outcomes":"[\"Up\",\"Down\"]",
                "outcomePrices":"[\"0.51\",\"0.49\"]",
                "clobTokenIds":"[\"111\",\"222\"]"
            }
            m = pm._parse_market(raw)
            self.assertEqual(m["up_token_id"], "111")
            self.assertEqual(m["down_token_id"], "222")
            self.assertAlmostEqual(m["price_to_beat"], 104250.75)
            self.assertTrue(m["rules_chainlink_ok"])

    def test_normalize_unmatched_no_fill(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            pm = PolymarketClient(db)
            r = pm._normalize_order_response({"status":"unmatched"}, "FOK", 0.5, 2, 1)
            self.assertTrue(r["success"])
            self.assertEqual(r["filled_cost"], 0.0)
            self.assertEqual(r["filled_shares"], 0.0)

    def test_normalize_matched_estimates_fill(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            pm = PolymarketClient(db)
            r = pm._normalize_order_response({"status":"matched"}, "FOK", 0.5, 2, 1)
            self.assertTrue(r["success"])
            self.assertGreater(r["filled_cost"], 0)
            self.assertGreater(r["filled_shares"], 0)


if __name__ == "__main__":
    unittest.main()

class TestV12SafetyFixes(unittest.TestCase):
    def test_clob_token_ids_follow_outcome_order_not_index_order(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            pm = PolymarketClient(db)
            raw = {
                "slug": "btc-updown-5m-1778257800",
                "question": "BTC Up or Down 5m",
                "description": "Resolution source is Chainlink BTC/USD data stream. Price to Beat: $104,250.75.",
                "outcomes": "[\"Down\",\"Up\"]",
                "outcomePrices": "[\"0.49\",\"0.51\"]",
                "clobTokenIds": "[\"down-token\",\"up-token\"]",
            }
            m = pm._parse_market(raw)
            self.assertEqual(m["up_token_id"], "up-token")
            self.assertEqual(m["down_token_id"], "down-token")

    def test_replay_gate_fails_when_no_priced_rows(self):
        from pathlib import Path
        import json
        from auto_pipeline import evaluate_polymarket_replay_summary
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "polymarket_replay_trades.summary.json"
            p.write_text(json.dumps({
                "input_rows": 10,
                "matched_markets": 10,
                "priced_rows": 0,
                "wins": 0,
                "win_rate_priced": 0.0,
                "avg_pnl_per_share_priced": None,
            }))
            ok, reason, _ = evaluate_polymarket_replay_summary(p)
            self.assertFalse(ok)
            self.assertIn("priced_rows", reason)
