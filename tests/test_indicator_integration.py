import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from buy_plan import _is_dip_candidate
from entry_engine import evaluate
from factor_data_import_service import FactorDataImporter
from market_data_provider import MarketDataProvider
from market_phase import determine_market_phase, extract_phase_inputs
from pyramid_multifactor_strategy import PyramidMultifactorStrategy


def _entry_stock(**indicators):
    technical = {
        "rsi14": 50,
        "kdj": {"j": 20},
        "volume_price_signal": "量价平稳",
        "ma_alignment": "mixed",
        **indicators,
    }
    return {
        "current_price": 100,
        "alpha_context": {"momentum": 0.5, "cycle": 0.5, "turnaround": 0.5},
        "trend_signal": {
            "available": True,
            "ma": {"ma20": 100, "ma60": 95},
            "returns": {"return_5d": 1, "return_20d": 2},
            "volatility_20d": 2,
            "technical_indicators": technical,
        },
    }


class TechnicalIndicatorTests(unittest.TestCase):
    def test_atr_bias_volume_and_drawdown_use_latest_first_order(self):
        closes = [100.0] * 21
        highs = [102.0] * 21
        lows = [98.0] * 21
        volumes = [200.0] + [100.0] * 20

        self.assertEqual(MarketDataProvider._atr(highs, lows, closes), (4.0, 4.0))
        self.assertEqual(MarketDataProvider._bias(closes, 20), 0.0)
        self.assertEqual(MarketDataProvider._volume_ratio(volumes), 2.0)
        self.assertAlmostEqual(MarketDataProvider._max_drawdown([80, 90, 100], 3), -20.0)

    def test_quality_score_checks_open_and_stale_data(self):
        closes = [10.0] * 60
        opens = [10.0] * 60
        highs = [11.0] * 60
        lows = [9.0] * 60
        volumes = [100.0] * 60
        opens[0] = 12.0

        score, flags = MarketDataProvider._daily_quality_score(
            closes, highs, lows, volumes, opens=opens, stale=True)

        self.assertEqual(score, 45.0)
        self.assertIn("invalid_ohlc", flags)
        self.assertIn("stale_cache", flags)

    def test_historical_trend_does_not_request_live_price(self):
        provider = MarketDataProvider.__new__(MarketDataProvider)
        bars = [
            {"trade_date": f"2026-01-{min(i + 1, 28):02d}", "open": 100,
             "high": 101, "low": 99, "close": 100, "volume": 1000}
            for i in range(70)
        ]
        provider.get_bars = lambda *args, **kwargs: {
            "bars": bars, "stale": False, "data_date": "2026-01-28", "source": "test"}
        provider.get_price = lambda *args, **kwargs: self.fail("historical path requested live price")

        result = provider.get_trend_signal("600000", "A股", as_of_date="2026-01-28")

        self.assertTrue(result["available"])


class FundamentalIndicatorTests(unittest.TestCase):
    def test_peg_reads_forecast_from_financial_data(self):
        strategy = PyramidMultifactorStrategy.__new__(PyramidMultifactorStrategy)
        strategy.ind_enabled = False
        strategy.ind_config = {}
        strategy.middle_layer_config = {}
        scoring = {
            "pe": {"excellent": 10, "good": 20, "average": 30},
            "peg": {"excellent": 0.5, "very_good": 0.8, "good": 1.0,
                    "average": 1.5, "below_avg": 2.5},
        }

        result = strategy.calculate_composite_score(
            {"pe": 20}, [], {"forecast_min": 20, "forecast_max": 30},
            override_weights={"value": 1, "growth": 0, "quality": 0, "momentum": 0},
            override_scoring=scoring)
        details = result["factor_details"]["value"]

        self.assertEqual(details["peg"], 1.0)
        self.assertEqual(details["peg_growth"], 20.0)

    def test_negative_cash_flow_is_not_treated_as_missing(self):
        self.assertEqual(FactorDataImporter._parse_cf_value("-12.5亿"), -1_250_000_000)


class MarketPhaseTests(unittest.TestCase):
    def test_outflows_and_falling_margin_reduce_phase_score(self):
        result = determine_market_phase(
            breadth_pct=50, index_momentum_20d=0,
            north_flow_5d=-10, margin_trend="down")

        self.assertEqual(result["score"], -2)
        self.assertEqual(result["phase"], "decline")

    def test_untrusted_or_stale_north_flow_is_ignored(self):
        for north_bound in (
            {"available": True, "usable_for_phase": False, "north_5d_buy": 100},
            {"available": True, "usable_for_phase": True, "age_days": 30,
             "north_5d_buy": 100},
        ):
            inputs = extract_phase_inputs({"north_bound": north_bound})
            self.assertIsNone(inputs["north_flow_5d"])

    def test_insufficient_phase_data_does_not_add_trading_constraints(self):
        result = determine_market_phase(breadth_pct=20)

        self.assertTrue(result["allow_new_buy"])
        self.assertFalse(result["dip_only"])


class EntryConstraintTests(unittest.TestCase):
    def test_cci_bonus_requires_actual_oversold_level(self):
        oversold = evaluate(_entry_stock(cci14=-120))
        not_oversold = evaluate(_entry_stock(cci14=-80))

        self.assertGreater(oversold["technical_score"], not_oversold["technical_score"])

    def test_bearish_alignment_vetoes_new_buy(self):
        stock = _entry_stock(ma_alignment="bearish")
        stock["alpha_context"] = {"momentum": 1, "cycle": 1, "turnaround": 1}

        result = evaluate(stock)

        self.assertEqual(result["signal"]["action_type"], "NONE")
        self.assertLess(result["entry_score"], 0.45)

    def test_real_atr_is_used_in_risk_snapshot(self):
        result = evaluate(_entry_stock(atr_20_pct=3.25))

        self.assertEqual(result["state_snapshot"]["risk"]["atr_pct"], 3.25)

    def test_volume_ratio_uses_price_direction(self):
        rising = evaluate(_entry_stock(volume_ratio=2.0))
        rising_base = evaluate(_entry_stock())
        falling_stock = _entry_stock(volume_ratio=2.0)
        falling_stock["trend_signal"]["returns"]["return_5d"] = -1
        falling = evaluate(falling_stock)
        falling_base_stock = _entry_stock()
        falling_base_stock["trend_signal"]["returns"]["return_5d"] = -1
        falling_base = evaluate(falling_base_stock)

        self.assertGreater(rising["technical_score"], rising_base["technical_score"])
        self.assertLess(falling["technical_score"], falling_base["technical_score"])

    def test_deep_drawdown_only_penalizes_continued_decline(self):
        deep_drawdown = _entry_stock(max_drawdown_20d_pct=-25)
        deep_drawdown["trend_signal"]["returns"]["return_5d"] = -1
        shallow_drawdown = _entry_stock(max_drawdown_20d_pct=-10)
        shallow_drawdown["trend_signal"]["returns"]["return_5d"] = -1

        self.assertLess(evaluate(deep_drawdown)["technical_score"],
                        evaluate(shallow_drawdown)["technical_score"])

    def test_dip_candidate_requires_low_position_and_stabilization(self):
        candidate = {
            "trend": {
                "returns": {"return_5d": 0.5},
                "technical_indicators": {
                    "rsi14": 42, "bias": {"ma20": -2},
                    "bollinger": {"percent_b": 0.3}, "kdj": {"j": 15},
                    "cci14": -80, "ma_alignment": "mixed",
                },
            },
        }
        self.assertTrue(_is_dip_candidate(candidate))

        candidate["trend"]["technical_indicators"]["ma_alignment"] = "bearish"
        self.assertFalse(_is_dip_candidate(candidate))


if __name__ == "__main__":
    unittest.main()
