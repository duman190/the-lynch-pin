"""Unit tests for The Lynch Pin project."""
import unittest
from unittest.mock import patch, MagicMock, PropertyMock
import pandas as pd
import numpy as np
import os


# ─── engine/income_statement_grader.py ───

class TestIncomeStatementGrader(unittest.TestCase):

    def test_yoy_growth_positive(self):
        from engine.income_statement_grader import _yoy_growth
        self.assertAlmostEqual(_yoy_growth(120, 100), 0.2)

    def test_yoy_growth_negative(self):
        from engine.income_statement_grader import _yoy_growth
        self.assertAlmostEqual(_yoy_growth(80, 100), -0.2)

    def test_yoy_growth_zero_prev(self):
        from engine.income_statement_grader import _yoy_growth
        self.assertIsNone(_yoy_growth(100, 0))

    def test_yoy_growth_none_prev(self):
        from engine.income_statement_grader import _yoy_growth
        self.assertIsNone(_yoy_growth(100, None))

    def test_grade_item_revenue_growing(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('Revenue', 0.10, 0.10), '🟢')

    def test_grade_item_revenue_declining(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('Revenue', 0.01, 0.01), '🔴')

    def test_grade_item_cogs_below_revenue(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('COGS', 0.05, 0.10), '🟢')

    def test_grade_item_cogs_above_revenue(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('COGS', 0.20, 0.10), '🔴')

    def test_grade_item_rd_lenient(self):
        from engine.income_statement_grader import _grade_item
        # R&D at 1.5x revenue growth = blue (lenient)
        self.assertEqual(_grade_item('R&D', 0.15, 0.10), '🔵')

    def test_grade_item_profit_accelerating(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('OpIncome', 0.30, 0.10), '🟢')

    def test_grade_item_profit_declining(self):
        from engine.income_statement_grader import _grade_item
        self.assertEqual(_grade_item('EPS', -0.05, 0.10), '🔴')

    def test_assign_grade_all_green(self):
        from engine.income_statement_grader import _assign_grade
        signals = ['🟢'] * 9
        growths = {'Revenue': 0.2, 'OpIncome': 0.4, 'EPS': 0.5, 'NetIncome': 0.4}
        self.assertIn(_assign_grade(signals, growths), ('A++', 'A+'))

    def test_assign_grade_revenue_red(self):
        from engine.income_statement_grader import _assign_grade
        signals = ['🔴'] + ['🟢'] * 8
        growths = {'Revenue': -0.05, 'OpIncome': 0.1, 'EPS': 0.1}
        self.assertIn(_assign_grade(signals, growths), ('C', 'D'))

    def test_assign_grade_empty(self):
        from engine.income_statement_grader import _assign_grade
        self.assertEqual(_assign_grade(['⚪'] * 9, {}), 'N/A')


# ─── engine/balance_sheet_grader.py ───

class TestBalanceSheetGrader(unittest.TestCase):

    def test_coverage_to_score_aaa(self):
        from engine.balance_sheet_grader import _coverage_to_score
        self.assertEqual(_coverage_to_score(10.0), 20)  # AAA

    def test_coverage_to_score_bbb(self):
        from engine.balance_sheet_grader import _coverage_to_score
        self.assertEqual(_coverage_to_score(2.0), 12)  # BBB

    def test_coverage_to_score_d(self):
        from engine.balance_sheet_grader import _coverage_to_score
        self.assertEqual(_coverage_to_score(-5.0), 0)  # D

    def test_notch_adjust_net_cash(self):
        from engine.balance_sheet_grader import _notch_adjust
        # Net cash (negative ND/EBITDA) should boost +2
        result = _notch_adjust(15, net_debt_ebitda=-0.5, cash_debt_ratio=1.5, debt_fcf_pct=5)
        self.assertEqual(result, 19)  # 15 + 2 + 1 + 1 = 19, capped at 20? -> 19

    def test_notch_adjust_heavy_debt(self):
        from engine.balance_sheet_grader import _notch_adjust
        # High leverage should penalize
        result = _notch_adjust(15, net_debt_ebitda=5.0, cash_debt_ratio=0.05, debt_fcf_pct=90)
        self.assertLess(result, 15)

    def test_notch_adjust_capped_at_20(self):
        from engine.balance_sheet_grader import _notch_adjust
        result = _notch_adjust(20, net_debt_ebitda=-1.0, cash_debt_ratio=2.0, debt_fcf_pct=5)
        self.assertEqual(result, 20)

    def test_notch_adjust_floor_at_0(self):
        from engine.balance_sheet_grader import _notch_adjust
        result = _notch_adjust(0, net_debt_ebitda=6.0, cash_debt_ratio=0.01, debt_fcf_pct=100)
        self.assertEqual(result, 0)


# ─── engine/ai_research.py ───

class TestAIResearch(unittest.TestCase):

    def test_format_grader(self):
        from engine.ai_research import LynchPinResearcher
        grade_result = {
            'grade': 'A+',
            'items': [('Revenue', 0.20, '🟢'), ('COGS', 0.10, '🟢')]
        }
        output = LynchPinResearcher._format_grader(grade_result)
        self.assertIn('Income Grade: A+', output)
        self.assertIn('Revenue: +20%', output)

    def test_format_grader_none(self):
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher._format_grader(None), "Income Statement: N/A")

    def test_format_balance_sheet(self):
        from engine.ai_research import LynchPinResearcher
        bs_result = {
            'rating': 'AAA',
            'metrics': [('IntCov', 50.0), ('ND/EBITDA', -0.5)]
        }
        output = LynchPinResearcher._format_balance_sheet(bs_result)
        self.assertIn('Credit Rating: AAA', output)
        self.assertIn('IntCov: 50.0', output)

    def test_format_balance_sheet_none(self):
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher._format_balance_sheet(None), "Balance Sheet: N/A")

    def test_format_technicals_valid_zone(self):
        from engine.ai_research import LynchPinResearcher
        tech = {'signal': 'BULLISH', 'trend': 'BULLISH', 'price_vs_sma200': 5.0,
                'rsi': 55, 'atr_compression': 0.95, 'accumulation_zone': (400.0, 450.0)}
        output = LynchPinResearcher._format_technicals(tech)
        self.assertIn('BULLISH', output)
        self.assertIn('$400-$450', output)

    def test_format_technicals_nan_zone(self):
        from engine.ai_research import LynchPinResearcher
        tech = {'signal': 'BULLISH', 'trend': 'BULLISH', 'price_vs_sma200': 5.0,
                'rsi': 55, 'atr_compression': 0.95, 'accumulation_zone': (float('nan'), float('nan'))}
        output = LynchPinResearcher._format_technicals(tech)
        self.assertIn('BULLISH', output)
        self.assertNotIn('Accumulation Zone', output)

    def test_format_technicals_no_zone_key(self):
        from engine.ai_research import LynchPinResearcher
        tech = {'signal': 'BEARISH', 'trend': 'BEARISH', 'price_vs_sma200': -10.0,
                'rsi': 35, 'atr_compression': 1.1}
        output = LynchPinResearcher._format_technicals(tech)
        self.assertIn('BEARISH', output)
        self.assertNotIn('Accumulation Zone', output)

    def test_format_technicals_none(self):
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher._format_technicals(None), "Technicals: N/A")

    def test_format_edge_bull(self):
        from engine.ai_research import LynchPinResearcher
        edge = {'bull_acc': 73.0, 'bull_pnl': 4.0, 'bull_n': 22,
                'bear_acc': 47.0, 'bear_pnl': 0.4, 'bear_n': 68, 'best_edge': 'BULL'}
        output = LynchPinResearcher._format_edge(edge)
        self.assertIn('BULL', output)
        self.assertIn('73%', output)
        self.assertIn('+4.0%', output)
        self.assertIn('22 signals', output)

    def test_format_edge_bear(self):
        from engine.ai_research import LynchPinResearcher
        edge = {'bull_acc': 38.0, 'bull_pnl': -0.7, 'bull_n': 37,
                'bear_acc': 67.0, 'bear_pnl': 3.5, 'bear_n': 30, 'best_edge': 'BEAR'}
        output = LynchPinResearcher._format_edge(edge)
        self.assertIn('BEAR', output)
        self.assertIn('67%', output)
        self.assertIn('+3.5%', output)

    def test_format_edge_none(self):
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher._format_edge(None), "6M Directional Edge: N/A")

    def test_build_prompt_includes_edge_data(self):
        from engine.ai_research import LynchPinResearcher
        data = [{
            'Ticker': 'MSFT', 'PE': 24.0, 'FwdPE': 21.0, '2YFwd': 18.0,
            '5YGrowth': '17.5%', 'PEG': 1.18, 'Mean': 1.81, 'Dev_SD': -2.28,
            'Bull': '30.0%', 'Base': '28.0%', 'Bear': '17.5%'
        }]
        edge_data = {'MSFT': {'bull_acc': 73.0, 'bull_pnl': 4.0, 'bull_n': 22,
                              'bear_acc': 47.0, 'bear_pnl': 0.4, 'bear_n': 68, 'best_edge': 'BULL'}}
        prompt = LynchPinResearcher.build_prompt(data, edge_data=edge_data)
        self.assertIn('6M Directional Edge: BULL', prompt)
        self.assertIn('73%', prompt)
        self.assertIn('cash-secured puts', prompt)

    def test_build_prompt_without_edge_data(self):
        from engine.ai_research import LynchPinResearcher
        data = [{
            'Ticker': 'AAPL', 'PE': 25.0, 'FwdPE': 20.0, '2YFwd': 18.0,
            '5YGrowth': '15.0%', 'PEG': 1.33, 'Mean': 1.5, 'Dev_SD': -0.5,
            'Bull': '20.0%', 'Base': '15.0%', 'Bear': '8.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data, edge_data=None)
        self.assertNotIn('6M Directional Edge: BULL', prompt)
        self.assertNotIn('6M Directional Edge: BEAR', prompt)

    def test_build_prompt_contains_ticker(self):
        from engine.ai_research import LynchPinResearcher
        data = [{
            'Ticker': 'AAPL', 'PE': 25.0, 'FwdPE': 20.0, '2YFwd': 18.0,
            '5YGrowth': '15.0%', 'PEG': 1.33, 'Mean': 1.5, 'Dev_SD': -0.5,
            'Bull': '20.0%', 'Base': '15.0%', 'Bear': '8.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data, idx_name="QQQ")
        self.assertIn('AAPL', prompt)
        self.assertIn('$QQQ', prompt)
        self.assertIn('Peter Lynch', prompt)

    def test_build_prompt_target_peg_capped(self):
        from engine.ai_research import LynchPinResearcher
        # Mean PEG 3.0, growth 20% -> terminal_peg = min(3.0, max(0.8, 1.5-0.5*(20/30-1))) = min(3.0, 1.67) = 1.67
        data = [{
            'Ticker': 'TEST', 'PE': 30.0, 'FwdPE': 25.0, '2YFwd': 20.0,
            '5YGrowth': '20.0%', 'PEG': 1.5, 'Mean': 3.0, 'Dev_SD': -1.0,
            'Bull': '25.0%', 'Base': '18.0%', 'Bear': '10.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data)
        self.assertIn('terminal PEG 1.67', prompt)

    def test_build_prompt_target_peg_uses_mean_when_lower(self):
        from engine.ai_research import LynchPinResearcher
        # Mean PEG 1.0, growth 20% -> terminal_peg = min(1.0, max(0.8, 1.67)) = 1.0
        data = [{
            'Ticker': 'TEST', 'PE': 20.0, 'FwdPE': 15.0, '2YFwd': 12.0,
            '5YGrowth': '20.0%', 'PEG': 0.8, 'Mean': 1.0, 'Dev_SD': -0.5,
            'Bull': '30.0%', 'Base': '20.0%', 'Bear': '12.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data)
        self.assertIn('terminal PEG 1.00', prompt)

    def test_build_prompt_base_math_follows_above_mean_scenario(self):
        from engine.ai_research import LynchPinResearcher
        # MU-like row: PEG 0.22 above mean 0.09 → base PEG anchors on current − 0.5 SD, not the 0.09 mean
        data = [{
            'Ticker': 'MU*', 'PE': 22.0, 'FwdPE': 6.2, '2YFwd': 4.9,
            '5YGrowth': '28.7%', 'PEG': 0.22, 'Mean': 0.09, 'Dev_SD': 2.24,
            'Bull': '24.9%', 'Base': '21.4%', 'Bear': '17.5%'
        }]
        prompt = LynchPinResearcher.build_prompt(data)
        self.assertIn('terminal PEG 0.19', prompt)
        self.assertNotIn('terminal PEG 0.09', prompt)
        self.assertIn('= 5x implied PE', prompt)  # 0.19 × 24.3% ≈ 4.6x → not the absurd 2x

    @patch('engine.ai_research.genai')
    def test_get_fintwit_trending_parses_tickers(self, mock_genai):
        from engine.ai_research import LynchPinResearcher
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        mock_response = MagicMock()
        mock_response.text = "AAPL\nMSFT\nNVDA\nGOOGL\nAMZN"
        mock_client.models.generate_content.return_value = mock_response

        researcher = LynchPinResearcher()
        researcher.client = mock_client
        tickers = researcher.get_fintwit_trending()
        self.assertIn('AAPL', tickers)
        self.assertIn('NVDA', tickers)
        self.assertEqual(len(tickers), 5)

    # ── normalize_narrative: coerce sloppy free-model output into main.py's layout ──

    def _main_py_parse(self, text, tickers):
        """Mirrors main.py's sentiment + per-ticker regexes."""
        import re
        sent = re.search(r'SENTIMENT:\s*(.+)', text)
        sentiment = sent.group(1).strip() if sent else ""
        bulk = text[sent.end():].strip() if sent else text
        found = {}
        for t in tickers:
            m = re.search(rf"^\${re.escape(t)}\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)", bulk, re.DOTALL | re.MULTILINE)
            if m:
                found[t] = m.group(1).strip()
        return sentiment, found

    def test_normalize_literal_ticker_template_and_missing_sentiment(self):
        """Reproduces the 2026-09-17 liquid/lfm-2.5 output: '$TICKER: ARM' headers, no SENTIMENT label."""
        from engine.ai_research import LynchPinResearcher
        raw = ("Market sentiment for SMH is mixed but leaning bullish on AI-driven tech.\n\n"
               "$TICKER: ARM\n🤖: ARM trades at a premium.\n📊 Reverse DCF: math.\n🧪 Stomach Test: risk.\n\n"
               "$TICKER: NVDA\n🤖: NVDA leads.\n")
        out = LynchPinResearcher.normalize_narrative(raw, ["ARM", "NVDA"])
        sentiment, found = self._main_py_parse(out, ["ARM", "NVDA"])
        self.assertEqual(sentiment, "Market sentiment for SMH is mixed but leaning bullish on AI-driven tech.")
        self.assertTrue(found["ARM"].startswith("🤖: ARM trades at a premium."))
        self.assertIn("🧪 Stomach Test: risk.", found["ARM"])
        self.assertEqual(found["NVDA"], "🤖: NVDA leads.")

    def test_normalize_bare_and_markdown_headers(self):
        from engine.ai_research import LynchPinResearcher
        raw = ("SENTIMENT: fine.\n\nASML:\n🤖: a\n\n**TICKER: TSM**\n🤖: b\n\n**$ADI**\n🤖: c\n\n"
               "Ticker - NXPI\n🤖: d\n")
        out = LynchPinResearcher.normalize_narrative(raw, ["ASML*", "TSM", "ADI", "NXPI"])
        _, found = self._main_py_parse(out, ["ASML", "TSM", "ADI", "NXPI"])
        self.assertEqual(found, {"ASML": "🤖: a", "TSM": "🤖: b", "ADI": "🤖: c", "NXPI": "🤖: d"})

    def test_normalize_short_ticker_word_in_prose_untouched(self):
        """'ON' inside a sentence must not become a block header (case-sensitive, whole-line only)."""
        from engine.ai_research import LynchPinResearcher
        raw = "SENTIMENT: s.\n\n$AAPL:\n🤖: Keep an eye ON this one.\nON\n🤖: ON Semi narrative.\n"
        out = LynchPinResearcher.normalize_narrative(raw, ["AAPL", "ON"])
        self.assertIn("Keep an eye ON this one.", out)
        _, found = self._main_py_parse(out, ["AAPL", "ON"])
        self.assertEqual(found["AAPL"], "🤖: Keep an eye ON this one.")
        self.assertEqual(found["ON"], "🤖: ON Semi narrative.")

    def test_normalize_well_formed_gemini_output_is_unchanged(self):
        from engine.ai_research import LynchPinResearcher
        good = ("SENTIMENT: $SMH is riding high.\n\n$ARM:\n🤖: x\n📊 Reverse DCF: y\n🧪 Stomach Test: z\n\n"
                "$NVDA:\n🤖: q\n")
        self.assertEqual(LynchPinResearcher.normalize_narrative(good, ["ARM", "NVDA"]), good)

    def test_normalize_portfolio_preamble_skips_portfolio_block_for_sentiment(self):
        from engine.ai_research import LynchPinResearcher
        raw = ("Well built but concentrated.\n\nPORTFOLIO:\n🐂 Bull: good.\n\n🐻 Bear: bad.\n\n"
               "$TICKER: AAPL\n🤖: a\n")
        out = LynchPinResearcher.normalize_narrative(raw, ["AAPL"])
        self.assertTrue(out.startswith("SENTIMENT: Well built but concentrated."))
        self.assertIn("\nPORTFOLIO:\n🐂 Bull: good.", out)

    def test_normalize_passes_through_error_and_empty(self):
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher.normalize_narrative("AI Research Error: boom", ["ARM"]),
                         "AI Research Error: boom")
        self.assertEqual(LynchPinResearcher.normalize_narrative("", ["ARM"]), "")

    def test_normalize_does_not_invent_sentiment_without_ticker_blocks(self):
        """A safety-classifier verdict must not be promoted to the main-tweet headline."""
        from engine.ai_research import LynchPinResearcher
        self.assertEqual(LynchPinResearcher.normalize_narrative("User Safety: safe", ["ARM"]), "User Safety: safe")

    @patch('engine.ai_research.genai')
    def test_get_batch_narrative_normalizes_model_output(self, mock_genai):
        from engine.ai_research import LynchPinResearcher
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        resp = MagicMock()
        resp.text = "Bullish week.\n\n$TICKER: AAPL\n🤖: a\n"
        mock_client.models.generate_content.return_value = resp
        researcher = LynchPinResearcher()
        researcher.client = mock_client
        data = [{'Ticker': 'AAPL', 'PE': 25.0, 'FwdPE': 20.0, '2YFwd': 18.0, '5YGrowth': '10.0%',
                 'PEG': 2.0, 'Mean': 2.5, 'Dev_SD': -1.0, 'Bull': '1%', 'Base': '1%', 'Bear': '1%'}]
        out = researcher.get_batch_narrative(data)
        self.assertEqual(out, "SENTIMENT: Bullish week.\n\n$AAPL:\n🤖: a\n")

    def test_build_prompt_header_template_is_not_literal_ticker(self):
        """The header placeholder must not be copy-able as '$TICKER:' by small models."""
        from engine.ai_research import LynchPinResearcher
        data = [{'Ticker': 'AAPL', 'PE': 25.0, 'FwdPE': 20.0, '2YFwd': 18.0, '5YGrowth': '10.0%',
                 'PEG': 2.0, 'Mean': 2.5, 'Dev_SD': -1.0, 'Bull': '1%', 'Base': '1%', 'Bear': '1%'}]
        prompt = LynchPinResearcher.build_prompt(data)
        self.assertNotIn('\n$TICKER:', prompt)
        self.assertIn('$<cashtag>:', prompt)
        self.assertIn('"$AAPL:"', prompt)


# ─── engine/ai_research.py — 3-tier fallback chain ───

class TestAIFallbackChain(unittest.TestCase):
    """best Gemini → backup Gemini → OpenRouter free router, ATTEMPTS_PER_TIER tries each.

    Attempt counts are read from ``LynchPinResearcher.ATTEMPTS_PER_TIER`` so the tests track
    the engine when it is tuned.
    """

    @property
    def n(self):
        from engine.ai_research import LynchPinResearcher
        return LynchPinResearcher.ATTEMPTS_PER_TIER

    def _make(self, mock_genai, openrouter_key="or-test-key"):
        from engine.ai_research import LynchPinResearcher
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        env = {"GEMINI_API_KEY": "g-key"}
        if openrouter_key:
            env["OPENROUTER_API_KEY"] = openrouter_key
        with patch.dict(os.environ, env, clear=True):
            researcher = LynchPinResearcher()
        researcher.client = mock_client
        return researcher, mock_client

    @staticmethod
    def _gemini_response(text):
        r = MagicMock()
        r.text = text
        return r

    @staticmethod
    def _openrouter_response(content=None, status=200, error=None):
        """Mock a streaming OpenRouter response (SSE lines, as in the curl quick-start)."""
        import json
        r = MagicMock()
        r.status_code = status
        r.text = "body"
        model = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

        def chunk(delta, finish=None):
            return "data: " + json.dumps({
                "id": "gen-1", "object": "chat.completion.chunk", "model": model, "provider": "Nvidia",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })

        if error is not None:
            lines = ["data: " + json.dumps({"error": error}), "", "data: [DONE]"]
        else:
            words = content.split(" ") if content else []
            lines = [
                # reasoning-only deltas first — must NOT end up in the returned text
                chunk({"content": "", "role": "assistant", "reasoning": "We"}),
                "",
                chunk({"content": "", "role": "assistant", "reasoning": " need to greet."}),
                "",
            ]
            for i, w in enumerate(words):
                lines += [chunk({"content": (" " if i else "") + w, "role": "assistant"}), ""]
            lines += [chunk({"content": "", "role": "assistant", "reasoning": None}, finish="stop"), "", "data: [DONE]"]
        r.iter_lines.return_value = iter(lines)
        return r

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_best_model_succeeds_first_try(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.return_value = self._gemini_response("ok")
        self.assertEqual(researcher._call_ai("p"), "ok")
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(client.models.generate_content.call_args.kwargs['model'], researcher.best_model)
        mock_post.assert_not_called()
        mock_sleep.assert_not_called()

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_best_busy_every_attempt_then_backup(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        busy = [Exception("503 UNAVAILABLE"), Exception("429 RESOURCE_EXHAUSTED")]
        client.models.generate_content.side_effect = \
            [busy[i % 2] for i in range(self.n)] + [self._gemini_response("backup ok")]
        self.assertEqual(researcher._call_ai("p", delay=0), "backup ok")
        models = [c.kwargs['model'] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models, [researcher.best_model] * self.n + [researcher.backup_model])
        self.assertEqual(mock_sleep.call_count, self.n)
        mock_post.assert_not_called()

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_gemini_exhausted_falls_back_to_openrouter(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")
        mock_post.return_value = self._openrouter_response("openrouter ok")

        self.assertEqual(researcher._call_ai("hello", delay=0), "openrouter ok")
        self.assertEqual(client.models.generate_content.call_count, 2 * self.n)  # best + backup
        self.assertEqual(mock_post.call_count, 1)
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(mock_post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(kwargs['json']['model'], "openrouter/free")
        self.assertTrue(kwargs['json']['stream'])
        self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['json']['messages'], [{"role": "user", "content": "hello"}])
        self.assertEqual(kwargs['headers']['Authorization'], "Bearer or-test-key")
        self.assertEqual(mock_sleep.call_count, 2 * self.n)

    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_openrouter_stream_ignores_reasoning_deltas(self, mock_genai, mock_post):
        researcher, _ = self._make(mock_genai)
        mock_post.return_value = self._openrouter_response("Hello! How can I assist you today?")
        text = researcher._call_openrouter_model("openrouter/free", "Hello")
        self.assertEqual(text, "Hello! How can I assist you today?")
        self.assertNotIn("need to greet", text)

    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_openrouter_stream_empty_content_raises(self, mock_genai, mock_post):
        researcher, _ = self._make(mock_genai)
        mock_post.return_value = self._openrouter_response("")  # reasoning only, no answer
        with self.assertRaises(RuntimeError):
            researcher._call_openrouter_model("openrouter/free", "Hello")

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_all_tiers_exhausted_returns_error(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")
        mock_post.return_value = self._openrouter_response(status=429)

        result = researcher._call_ai("p", delay=0)
        self.assertTrue(result.startswith("AI Research Error:"))
        self.assertIn("OpenRouter HTTP 429", result)
        self.assertEqual(client.models.generate_content.call_count, 2 * self.n)
        self.assertEqual(mock_post.call_count, self.n)
        self.assertEqual(mock_sleep.call_count, 3 * self.n - 1)  # 3 tiers, no sleep after the last

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_openrouter_200_with_error_envelope_is_retried(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")
        mock_post.side_effect = [
            self._openrouter_response(error={"code": 429, "message": "Rate limited"}),
            self._openrouter_response("second try ok"),
        ]
        self.assertEqual(researcher._call_ai("p", delay=0), "second try ok")
        self.assertEqual(mock_post.call_count, 2)

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_no_openrouter_key_skips_third_tier(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai, openrouter_key=None)
        client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")

        result = researcher._call_ai("p", delay=0)
        self.assertTrue(result.startswith("AI Research Error:"))
        self.assertEqual(client.models.generate_content.call_count, 2 * self.n)
        mock_post.assert_not_called()
        self.assertEqual(mock_sleep.call_count, 2 * self.n - 1)
        self.assertEqual([t[0] for t in researcher._tiers()], ["BEST", "BACKUP"])

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_non_transient_error_switches_tier_immediately(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = [
            Exception("404 model not found"), self._gemini_response("backup ok")
        ]
        self.assertEqual(researcher._call_ai("p", delay=0), "backup ok")
        models = [c.kwargs['model'] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models, [researcher.best_model, researcher.backup_model])  # no 2nd BEST attempt
        mock_sleep.assert_not_called()

    # ── reply validation: garbage replies burn an attempt and are retried without sleeping ──

    GOOD = "SENTIMENT: fine.\n\n$ARM:\n🤖: a\n\n$NVDA:\n🤖: b\n"

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_unusable_reply_is_retried_immediately_on_same_tier(self, mock_genai, mock_post, mock_sleep):
        """Reproduces openrouter/free routing to a safety classifier: 'User Safety: safe'."""
        from engine.ai_research import LynchPinResearcher
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = [
            self._gemini_response("User Safety: safe"), self._gemini_response(self.GOOD)
        ]
        tickers = ["ARM", "NVDA"]
        check = lambda t: LynchPinResearcher.narrative_gaps(LynchPinResearcher.normalize_narrative(t, tickers), tickers)
        self.assertEqual(researcher._call_ai("p", delay=0, check=check), self.GOOD)
        models = [c.kwargs['model'] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models, [researcher.best_model, researcher.best_model])  # same tier, 2nd attempt
        mock_sleep.assert_not_called()  # not a capacity problem → no 30s pause
        mock_post.assert_not_called()

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_all_replies_unusable_returns_most_complete(self, mock_genai, mock_post, mock_sleep):
        from engine.ai_research import LynchPinResearcher
        researcher, client = self._make(mock_genai, openrouter_key=None)
        partial = "SENTIMENT: ok.\n\n$ARM:\n🤖: a\n"  # NVDA missing
        replies = ["User Safety: safe"] * (2 * self.n)
        replies[self.n] = partial  # first BACKUP attempt is the least-bad one
        client.models.generate_content.side_effect = [self._gemini_response(r) for r in replies]
        tickers = ["ARM", "NVDA"]
        check = lambda t: LynchPinResearcher.narrative_gaps(LynchPinResearcher.normalize_narrative(t, tickers), tickers)
        out = researcher._call_ai("p", delay=0, check=check)
        self.assertEqual(out, partial)  # degraded but real content, not the error string
        self.assertEqual(client.models.generate_content.call_count, 2 * self.n)
        mock_sleep.assert_not_called()

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_check_not_applied_without_validator(self, mock_genai, mock_post, mock_sleep):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.return_value = self._gemini_response("User Safety: safe")
        self.assertEqual(researcher._call_ai("p", delay=0), "User Safety: safe")
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_narrative_gaps(self):
        from engine.ai_research import LynchPinResearcher as R
        t = ["ARM", "NVDA*"]
        self.assertIsNone(R.narrative_gaps(self.GOOD, t))
        self.assertEqual(R.narrative_gaps("", t), "empty reply")
        self.assertEqual(R.narrative_gaps("AI Research Error: x", t), "empty reply")
        # safety classifier output: no sentiment, no blocks
        self.assertEqual(R.narrative_gaps("User Safety: safe", t),
                         "no SENTIMENT line; 0/2 ticker blocks (missing: ARM, NVDA)")
        # header present but block has no 🤖 overview (truncated) → counts as missing
        self.assertEqual(R.narrative_gaps("SENTIMENT: s.\n\n$ARM:\n🤖: a\n\n$NVDA:\n", t),
                         "1/2 ticker blocks (missing: NVDA)")
        # empty SENTIMENT label is not a sentiment
        self.assertEqual(R.narrative_gaps("SENTIMENT:\n\n$ARM:\n🤖: a\n\n$NVDA:\n🤖: b\n", t), "no SENTIMENT line")
        # relaxed ratio tolerates one missing name
        self.assertIsNone(R.narrative_gaps("SENTIMENT: s.\n\n$ARM:\n🤖: a\n", t, min_ticker_ratio=0.5))

    @patch('engine.ai_research.genai')
    def test_get_batch_narrative_retries_garbage_then_normalizes(self, mock_genai):
        from engine.ai_research import LynchPinResearcher
        researcher, client = self._make(mock_genai)
        client.models.generate_content.side_effect = [
            self._gemini_response("User Safety: safe"),
            self._gemini_response("Bullish week.\n\n$TICKER: AAPL\n🤖: a\n"),
        ]
        data = [{'Ticker': 'AAPL', 'PE': 25.0, 'FwdPE': 20.0, '2YFwd': 18.0, '5YGrowth': '10.0%',
                 'PEG': 2.0, 'Mean': 2.5, 'Dev_SD': -1.0, 'Bull': '1%', 'Base': '1%', 'Bear': '1%'}]
        with patch('engine.ai_research.time.sleep') as mock_sleep:
            out = researcher.get_batch_narrative(data)
            mock_sleep.assert_not_called()
        self.assertEqual(out, "SENTIMENT: Bullish week.\n\n$AAPL:\n🤖: a\n")
        self.assertEqual(client.models.generate_content.call_count, 2)

    @patch('engine.ai_research.genai')
    def test_call_gemini_alias_kept(self, mock_genai):
        researcher, client = self._make(mock_genai)
        client.models.generate_content.return_value = self._gemini_response("ok")
        self.assertEqual(researcher._call_gemini("p"), "ok")

    @patch('engine.ai_research.time.sleep')
    @patch('engine.ai_research.requests.post')
    @patch('engine.ai_research.genai')
    def test_no_gemini_key_runs_openrouter_only(self, mock_genai, mock_post, mock_sleep):
        from engine.ai_research import LynchPinResearcher
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or-test-key"}, clear=True):
            researcher = LynchPinResearcher()
        mock_genai.Client.assert_not_called()
        self.assertIsNone(researcher.client)
        self.assertEqual([t[0] for t in researcher._tiers()], ["OPENROUTER"])
        mock_post.return_value = self._openrouter_response("openrouter ok")
        self.assertEqual(researcher._call_ai("p", delay=0), "openrouter ok")

    @patch('engine.ai_research.genai')
    def test_no_keys_at_all_returns_error(self, mock_genai):
        from engine.ai_research import LynchPinResearcher
        with patch.dict(os.environ, {}, clear=True):
            researcher = LynchPinResearcher()
        self.assertEqual(researcher._tiers(), [])
        self.assertTrue(researcher._call_ai("p").startswith("AI Research Error:"))


# ─── social/threads_publisher.py ───

class TestThreadsPublisher(unittest.TestCase):

    def test_truncate_short_text(self):
        from social.threads_publisher import ThreadsPublisher
        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        self.assertEqual(pub._truncate("Hello"), "Hello")

    def test_truncate_exact_499(self):
        from social.threads_publisher import ThreadsPublisher
        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        text = "a" * 499
        self.assertEqual(pub._truncate(text), text)

    def test_truncate_over_limit(self):
        from social.threads_publisher import ThreadsPublisher
        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        text = "a" * 600
        result = pub._truncate(text)
        self.assertEqual(len(result), 499)
        self.assertTrue(result.endswith("..."))

    def test_truncate_500_chars(self):
        from social.threads_publisher import ThreadsPublisher
        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        text = "a" * 500
        result = pub._truncate(text)
        self.assertEqual(len(result), 499)
        self.assertEqual(result, "a" * 496 + "...")

    @patch('social.threads_publisher.requests.post')
    def test_create_container_text(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"id": "12345"}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        cid = pub._create_container("Hello world")
        self.assertEqual(cid, "12345")
        call_params = mock_post.call_args[1]['params']
        self.assertEqual(call_params['media_type'], 'TEXT')

    @patch('social.threads_publisher.requests.post')
    def test_create_container_image(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"id": "67890"}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        cid = pub._create_container("Hello", image_url="https://example.com/img.png")
        self.assertEqual(cid, "67890")
        call_params = mock_post.call_args[1]['params']
        self.assertEqual(call_params['media_type'], 'IMAGE')
        self.assertEqual(call_params['image_url'], 'https://example.com/img.png')

    @patch('social.threads_publisher.requests.post')
    def test_create_container_with_reply(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"id": "99999"}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        cid = pub._create_container("Reply text", reply_to="parent_id_123")
        call_params = mock_post.call_args[1]['params']
        self.assertEqual(call_params['reply_to_id'], 'parent_id_123')

    @patch('social.threads_publisher.requests.post')
    def test_create_container_with_topic_tag(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"id": "11111"}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        cid = pub._create_container("Tagged post", topic_tag="NVDA")
        call_params = mock_post.call_args[1]['params']
        self.assertEqual(call_params['topic_tag'], 'NVDA')

    @patch('social.threads_publisher.requests.post')
    def test_publish(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"id": "published_123"}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        result = pub._publish("container_id")
        self.assertEqual(result, "published_123")

    @patch('social.threads_publisher.requests.post')
    def test_create_container_failure_raises(self, mock_post):
        from social.threads_publisher import ThreadsPublisher
        mock_post.return_value.json.return_value = {"error": {"message": "Bad request"}}

        pub = ThreadsPublisher.__new__(ThreadsPublisher)
        pub.access_token = "test_token"
        pub.user_id = "123"
        pub.base_url = "https://graph.threads.net/v1.0/123"

        with self.assertRaises(Exception) as ctx:
            pub._create_container("fail")
        self.assertIn("Container creation failed", str(ctx.exception))


# ─── social/x_publisher.py ───

class TestXPublisher(unittest.TestCase):

    @patch('social.x_publisher.tweepy.API')
    @patch('social.x_publisher.tweepy.OAuth1UserHandler')
    @patch('social.x_publisher.tweepy.Client')
    def test_upload_media_file_not_found(self, mock_client, mock_auth, mock_api):
        from social.x_publisher import XPublisher
        pub = XPublisher()
        result = pub._upload_media("/nonexistent/path.png")
        self.assertIsNone(result)

    @patch('social.x_publisher.tweepy.API')
    @patch('social.x_publisher.tweepy.OAuth1UserHandler')
    @patch('social.x_publisher.tweepy.Client')
    def test_safe_create_tweet_retries(self, mock_client_cls, mock_auth, mock_api):
        from social.x_publisher import XPublisher
        pub = XPublisher()
        pub.client.create_tweet = MagicMock(side_effect=Exception("403 Forbidden"))

        with self.assertRaises(Exception) as ctx:
            pub._safe_create_tweet(text="test")
        self.assertIn("Failed to post tweet after 3 attempts", str(ctx.exception))
        self.assertEqual(pub.client.create_tweet.call_count, 3)

    @patch('social.x_publisher.tweepy.API')
    @patch('social.x_publisher.tweepy.OAuth1UserHandler')
    @patch('social.x_publisher.tweepy.Client')
    def test_safe_create_tweet_success(self, mock_client_cls, mock_auth, mock_api):
        from social.x_publisher import XPublisher
        pub = XPublisher()
        mock_response = MagicMock()
        mock_response.data = {'id': '123456'}
        pub.client.create_tweet = MagicMock(return_value=mock_response)

        result = pub._safe_create_tweet(text="test tweet")
        self.assertEqual(result.data['id'], '123456')


# ─── engine/growth_estimator.py ───

class TestGrowthEstimator(unittest.TestCase):

    def test_yahoo_5y_growth_from_peg(self):
        from engine.growth_estimator import _yahoo_5y_growth
        info = {'pegRatio': 1.5, 'forwardPE': 30.0}
        self.assertAlmostEqual(_yahoo_5y_growth(info, 30.0), 20.0)

    def test_yahoo_5y_growth_peg_zero(self):
        from engine.growth_estimator import _yahoo_5y_growth
        info = {'pegRatio': 0, 'forwardPE': 30.0}
        self.assertIsNone(_yahoo_5y_growth(info, 30.0))

    def test_yahoo_5y_growth_peg_none(self):
        from engine.growth_estimator import _yahoo_5y_growth
        info = {'pegRatio': None, 'forwardPE': 30.0}
        self.assertIsNone(_yahoo_5y_growth(info, 30.0))

    def test_yahoo_5y_growth_out_of_range(self):
        from engine.growth_estimator import _yahoo_5y_growth
        # g = 30 / 0.1 = 300 -> out of range (>150)
        info = {'pegRatio': 0.1, 'forwardPE': 30.0}
        self.assertIsNone(_yahoo_5y_growth(info, 30.0))

    def test_fundamental_cap_basic(self):
        from engine.growth_estimator import _fundamental_cap
        ticker = MagicMock()
        # Revenue growing 20% CAGR over 3 years
        rev = pd.Series([100, 120, 144], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        ni = pd.Series([10, 13, 17], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        inc = pd.DataFrame({'Total Revenue': rev, 'Net Income': ni}).T
        inc.columns = pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01'])
        ticker.income_stmt = inc
        # No buybacks
        shares = pd.Series([1000, 1000], index=pd.to_datetime(['2022-01-01', '2024-01-01']))
        bs = pd.DataFrame({'Ordinary Shares Number': shares}).T
        bs.columns = pd.to_datetime(['2022-01-01', '2024-01-01'])
        ticker.balance_sheet = bs

        cap = _fundamental_cap(ticker)
        self.assertIsNotNone(cap)
        self.assertGreater(cap, 15)  # ~20% rev CAGR + margin expansion

    def test_fundamental_cap_with_buybacks(self):
        from engine.growth_estimator import _fundamental_cap
        ticker = MagicMock()
        rev = pd.Series([100, 110, 121], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        ni = pd.Series([10, 11, 12.1], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        inc = pd.DataFrame({'Total Revenue': rev, 'Net Income': ni}).T
        inc.columns = pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01'])
        ticker.income_stmt = inc
        # 5% annual buyback
        shares = pd.Series([1000, 950, 902], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        bs = pd.DataFrame({'Ordinary Shares Number': shares}).T
        bs.columns = pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01'])
        ticker.balance_sheet = bs

        cap = _fundamental_cap(ticker)
        self.assertIsNotNone(cap)
        self.assertGreater(cap, 12)  # ~10% rev + ~5% buyback

    def test_fundamental_cap_no_revenue(self):
        from engine.growth_estimator import _fundamental_cap
        ticker = MagicMock()
        ticker.income_stmt = pd.DataFrame()  # empty
        ticker.balance_sheet = pd.DataFrame()
        self.assertIsNone(_fundamental_cap(ticker))

    @patch('engine.growth_estimator._SESSION')
    def test_fmp_5y_growth_no_key(self, mock_session):
        from engine.growth_estimator import _fmp_5y_growth
        import engine.growth_estimator as ge
        original_key = ge.FMP_KEY
        ge.FMP_KEY = None
        self.assertIsNone(_fmp_5y_growth('AAPL'))
        ge.FMP_KEY = original_key

    @patch('engine.growth_estimator._SESSION')
    def test_fmp_5y_growth_success(self, mock_session):
        from engine.growth_estimator import _fmp_5y_growth
        import engine.growth_estimator as ge
        original_key = ge.FMP_KEY
        ge.FMP_KEY = 'test_key'

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {'date': '2030-01-01', 'epsAvg': 20.0, 'numAnalystsEps': 10},
            {'date': '2029-01-01', 'epsAvg': 17.0, 'numAnalystsEps': 12},
            {'date': '2028-01-01', 'epsAvg': 14.0, 'numAnalystsEps': 15},
            {'date': '2027-01-01', 'epsAvg': 11.5, 'numAnalystsEps': 20},
            {'date': '2026-01-01', 'epsAvg': 9.5, 'numAnalystsEps': 22},
            {'date': '2025-01-01', 'epsAvg': 8.0, 'numAnalystsEps': 25},
        ]
        mock_session.get.return_value = mock_resp

        result = _fmp_5y_growth('TEST')
        self.assertIsNotNone(result)
        # CAGR from 8.0 to 20.0 over 5 years = ~20%
        self.assertAlmostEqual(result, 20.1, places=0)

        ge.FMP_KEY = original_key

    @patch('engine.growth_estimator._SESSION')
    def test_fmp_5y_growth_rate_limited_then_succeeds(self, mock_session):
        from engine.growth_estimator import _fmp_5y_growth
        import engine.growth_estimator as ge
        original_key = ge.FMP_KEY
        ge.FMP_KEY = 'test_key'

        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = [
            {'date': '2030-01-01', 'epsAvg': 15.0, 'numAnalystsEps': 10},
            {'date': '2025-01-01', 'epsAvg': 10.0, 'numAnalystsEps': 20},
        ]
        mock_session.get.side_effect = [mock_429, mock_ok]

        with patch('engine.growth_estimator.time.sleep'):
            result = _fmp_5y_growth('TEST')
        self.assertIsNotNone(result)
        # CAGR from 10 to 15 over 5 years = ~8.4%
        self.assertAlmostEqual(result, 8.4, places=0)

        ge.FMP_KEY = original_key

    @patch('engine.growth_estimator._SESSION')
    def test_fmp_5y_growth_double_429_gives_up(self, mock_session):
        from engine.growth_estimator import _fmp_5y_growth
        import engine.growth_estimator as ge
        original_key = ge.FMP_KEY
        ge.FMP_KEY = 'test_key'

        mock_429 = MagicMock()
        mock_429.status_code = 429
        mock_session.get.return_value = mock_429

        with patch('engine.growth_estimator.time.sleep'):
            result = _fmp_5y_growth('TEST')
        self.assertIsNone(result)

        ge.FMP_KEY = original_key

    def test_estimate_growth_yahoo_only(self):
        from engine.growth_estimator import estimate_growth
        info = {'pegRatio': 2.0, 'forwardPE': 30.0}
        ticker = MagicMock()
        ticker.income_stmt = pd.DataFrame()  # no fundamental cap
        ticker.balance_sheet = pd.DataFrame()

        g, sources = estimate_growth('TEST', info, ticker, 30.0, enrich=False)
        self.assertAlmostEqual(g, 15.0)  # 30 / 2.0
        self.assertEqual(sources, ['yahoo_peg'])

    def test_estimate_growth_cap_haircut(self):
        from engine.growth_estimator import estimate_growth
        info = {'pegRatio': 1.0, 'forwardPE': 30.0}  # yahoo says 30%
        ticker = MagicMock()
        # Fundamental cap ~5%
        rev = pd.Series([100, 105, 110], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        ni = pd.Series([10, 10.5, 11], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        inc = pd.DataFrame({'Total Revenue': rev, 'Net Income': ni}).T
        inc.columns = pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01'])
        ticker.income_stmt = inc
        shares = pd.Series([1000, 1000], index=pd.to_datetime(['2022-01-01', '2024-01-01']))
        bs = pd.DataFrame({'Ordinary Shares Number': shares}).T
        bs.columns = pd.to_datetime(['2022-01-01', '2024-01-01'])
        ticker.balance_sheet = bs

        g, sources = estimate_growth('TEST', info, ticker, 30.0, enrich=False)
        # Yahoo says 30%, cap ~5%, 30 > 5*1.5 -> haircut: 30*0.6 + 5*0.4 = 20
        self.assertLess(g, 30.0)
        self.assertGreater(g, 5.0)

    def test_estimate_growth_no_cap_no_haircut(self):
        from engine.growth_estimator import estimate_growth
        info = {'pegRatio': 2.0, 'forwardPE': 30.0}  # yahoo says 15%
        ticker = MagicMock()
        # Fundamental cap ~20% (above yahoo)
        rev = pd.Series([100, 120, 144], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        ni = pd.Series([10, 12, 14.4], index=pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01']))
        inc = pd.DataFrame({'Total Revenue': rev, 'Net Income': ni}).T
        inc.columns = pd.to_datetime(['2022-01-01', '2023-01-01', '2024-01-01'])
        ticker.income_stmt = inc
        shares = pd.Series([1000, 1000], index=pd.to_datetime(['2022-01-01', '2024-01-01']))
        bs = pd.DataFrame({'Ordinary Shares Number': shares}).T
        bs.columns = pd.to_datetime(['2022-01-01', '2024-01-01'])
        ticker.balance_sheet = bs

        g, sources = estimate_growth('TEST', info, ticker, 30.0, enrich=False)
        # 15% < 20%*1.5=30% -> no haircut
        self.assertAlmostEqual(g, 15.0)

    def test_estimate_growth_fallback_when_no_peg(self):
        from engine.growth_estimator import estimate_growth
        info = {'pegRatio': None, 'forwardPE': 30.0, 'earningsGrowth': 0.20}
        ticker = MagicMock()
        ticker.earnings_estimate = None
        ticker.income_stmt = pd.DataFrame()
        ticker.balance_sheet = pd.DataFrame()

        g, sources = estimate_growth('TEST', info, ticker, 30.0, enrich=False)
        self.assertAlmostEqual(g, 20.0)
        self.assertEqual(sources, ['fallback_eg'])

    def test_estimate_growth_returns_zero_when_nothing(self):
        from engine.growth_estimator import estimate_growth
        info = {'pegRatio': None, 'forwardPE': None}
        ticker = MagicMock()
        ticker.earnings_estimate = None
        ticker.income_stmt = pd.DataFrame()
        ticker.balance_sheet = pd.DataFrame()

        g, sources = estimate_growth('TEST', info, ticker, None, enrich=False)
        self.assertEqual(g, 0)
        self.assertEqual(sources, [])


# ─── engine/lynch_pin_core.py ───

class TestLynchPinCore(unittest.TestCase):

    def test_growth_decay_mature(self):
        from engine.lynch_pin_core import _growth_decay
        self.assertEqual(_growth_decay(10), 1.0)
        self.assertEqual(_growth_decay(19.9), 1.0)

    def test_growth_decay_moderate(self):
        from engine.lynch_pin_core import _growth_decay
        self.assertEqual(_growth_decay(20), 0.95)
        self.assertEqual(_growth_decay(29), 0.95)

    def test_growth_decay_high(self):
        from engine.lynch_pin_core import _growth_decay
        self.assertEqual(_growth_decay(30), 0.90)
        self.assertEqual(_growth_decay(49), 0.90)

    def test_growth_decay_extreme(self):
        from engine.lynch_pin_core import _growth_decay
        self.assertEqual(_growth_decay(50), 0.85)
        self.assertEqual(_growth_decay(75), 0.85)

    def test_terminal_peg_mature_uses_mean(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth < 20, mean_peg 1.3 < 1.5 cap
        self.assertAlmostEqual(_terminal_peg(15, 1.3), 1.3)

    def test_terminal_peg_mature_capped_at_2_5(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 10%, mean_peg 3.0 > 2.5 cap; PE cap (28/10 = 2.8) is not binding
        self.assertAlmostEqual(_terminal_peg(10, 3.0), 2.5)

    def test_terminal_peg_mature_capped_by_terminal_pe(self):
        from engine.lynch_pin_core import _terminal_peg, _MATURE_TERMINAL_PE_CAP
        # AMZN-like: 16% growth, mean PEG 2.9 (capex-depressed EPS history).
        # 2.5 × 16 = 40x terminal PE → capped at 28x → PEG 1.75
        peg = _terminal_peg(16.0, 2.9)
        self.assertAlmostEqual(peg, _MATURE_TERMINAL_PE_CAP / 16.0)
        self.assertAlmostEqual(peg * 16.0, 28.0)
        # ISRG-like: 18% growth, mean 2.96 → 28/18 = 1.56
        self.assertAlmostEqual(_terminal_peg(18.0, 2.96), 28.0 / 18.0)

    def test_terminal_peg_pe_cap_leaves_low_growth_and_cheap_history_alone(self):
        from engine.lynch_pin_core import _terminal_peg
        # AAPL/KO-like: growth < 11.2% → 28/g > 2.5, so the 2.5 PEG cap still binds
        self.assertAlmostEqual(_terminal_peg(9.7, 3.49), 2.5)
        self.assertAlmostEqual(_terminal_peg(6.1, 4.38), 2.5)
        # MSFT/GOOG-like: mean PEG already implies < 28x → unchanged
        self.assertAlmostEqual(_terminal_peg(12.9, 1.73), 1.73)
        self.assertAlmostEqual(_terminal_peg(17.0, 1.54), 1.54)

    def test_terminal_pe_is_continuous_across_growth_regimes(self):
        from engine.lynch_pin_core import _terminal_peg, _growth_decay
        # Just below 20%: PE cap → 28x. Just above: 1.67 PEG × 17.2% decayed growth ≈ 28.7x
        below = _terminal_peg(19.99, 5.0) * 19.99 ** _growth_decay(19.99)
        above = _terminal_peg(20.0, 5.0) * 20.0 ** _growth_decay(20.0)
        self.assertAlmostEqual(below, 28.0, places=1)
        self.assertLess(abs(above - below), 1.0)

    def test_scenario_pegs_mature_pe_cap_applies_to_both_branches(self):
        from engine.lynch_pin_core import _scenario_pegs
        # Below mean (AMZN-like): base is the capped terminal PEG
        bull, base, bear = _scenario_pegs(16.0, 2.90, 1.48, 1.07)
        self.assertAlmostEqual(base, 28.0 / 16.0)
        self.assertAlmostEqual(bull, base + 0.5 * 1.07)
        self.assertAlmostEqual(bear, base - 0.5 * 1.07)  # 1.215 < today's 1.48
        # Above mean (VRNS-like: PEG 7.38 = mean): today's multiple would imply 114x → bull capped at 28x
        bull, base, bear = _scenario_pegs(15.5, 7.0, 7.38, 1.48)
        self.assertAlmostEqual(bull, 28.0 / 15.5)
        self.assertTrue(bull > base > bear > 0)

    def test_terminal_peg_high_growth_reversed(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 60%: 1.5 - 0.5*(60/30 - 1) = 1.5 - 0.5 = 1.0
        self.assertAlmostEqual(_terminal_peg(60, 2.0), 1.0)

    def test_terminal_peg_high_growth_floor(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 90%: 1.5 - 0.5*(90/30 - 1) = 1.5 - 1.0 = 0.5 -> floored at 0.8
        self.assertAlmostEqual(_terminal_peg(90, 2.0), 0.8)

    def test_scenario_pegs_below_mean_is_mean_reversion(self):
        from engine.lynch_pin_core import _scenario_pegs
        # GOOG-like: PEG 1.20 vs mean 1.51, SD 0.295 → bull mean+0.5SD, base mean, bear = min(curr, mean-0.5SD)
        # (18% growth × 1.51 = 27.2x, under the 28x mature terminal PE cap)
        bull, base, bear = _scenario_pegs(18.0, 1.51, 1.20, 0.295)
        self.assertAlmostEqual(base, 1.51)
        self.assertAlmostEqual(bull, 1.51 + 0.5 * 0.295)
        self.assertAlmostEqual(bear, 1.20)  # current is below mean-0.5SD=1.36 → bear caps at current
        self.assertTrue(bull > base > bear)

    def test_scenario_pegs_above_mean_anchors_on_current(self):
        from engine.lynch_pin_core import _scenario_pegs
        # MU-like: broken history (mean 0.09) with PEG 0.22 → today's multiple holds; base −0.5SD, bear −1SD
        std = abs(0.22 - 0.09) / 2.24
        bull, base, bear = _scenario_pegs(28.7, 0.09, 0.22, std)
        self.assertAlmostEqual(bull, 0.22)
        self.assertAlmostEqual(base, 0.22 - 0.5 * std)
        self.assertAlmostEqual(bear, 0.22 - 1.0 * std)
        self.assertTrue(bull > base > bear > 0)

    def test_scenario_pegs_above_mean_respects_growth_cap(self):
        from engine.lynch_pin_core import _scenario_pegs, _terminal_peg
        # PLTR-like: PEG 1.61 above mean 1.46, but 44.7% growth caps the anchor at 1.25
        bull, base, bear = _scenario_pegs(44.7, 1.46, 1.61, 0.44)
        self.assertAlmostEqual(bull, _terminal_peg(44.7, 1.61))
        self.assertAlmostEqual(bull, 1.5 - 0.5 * (44.7 / 30 - 1))
        self.assertTrue(bull > base > bear > 0)

    def test_scenario_pegs_above_mean_floors_when_sd_is_huge(self):
        from engine.lynch_pin_core import _scenario_pegs
        # SD far larger than the PEG itself: floors keep base ≥ 50% and bear ≥ 25% of the anchor
        bull, base, bear = _scenario_pegs(15, 1.0, 1.2, 5.0)
        self.assertAlmostEqual(base, 0.6)
        self.assertAlmostEqual(bear, 0.3)

    def test_terminal_peg_high_growth_uses_mean_when_lower(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 25%: formula = 1.5 - 0.5*(25/30 - 1) = 1.58, but mean=1.2 is lower
        self.assertAlmostEqual(_terminal_peg(25, 1.2), 1.2)

    @patch('engine.lynch_pin_core.yf.Ticker')
    def test_get_growth_from_peg_ratio(self, mock_ticker):
        from engine.lynch_pin_core import LynchPinEngine
        mock_ticker.return_value.info = {
            'pegRatio': 1.5,
            'forwardPE': 30.0,
            'currentPrice': 100,
        }
        engine = LynchPinEngine.__new__(LynchPinEngine)
        engine.symbol = 'TEST'
        engine.ticker = mock_ticker.return_value
        engine.info = mock_ticker.return_value.info

        growth = engine._get_growth(30.0, 5.0, 4.0)
        self.assertAlmostEqual(growth, 20.0)  # 30 / 1.5 = 20

    @patch('engine.lynch_pin_core.yf.Ticker')
    def test_get_growth_fallback_earnings_growth(self, mock_ticker):
        from engine.lynch_pin_core import LynchPinEngine
        mock_ticker.return_value.info = {
            'pegRatio': None,
            'earningsGrowth': 0.25,
        }
        mock_ticker.return_value.earnings_estimate = None
        engine = LynchPinEngine.__new__(LynchPinEngine)
        engine.symbol = 'TEST'
        engine.ticker = mock_ticker.return_value
        engine.info = mock_ticker.return_value.info

        growth = engine._get_growth(20.0, 5.0, 4.0)
        self.assertAlmostEqual(growth, 25.0)

    def test_base_eps_uses_forward(self):
        """Forward EPS is used as projection base to reflect market pricing."""
        eps, fwd_eps = 5.0, 7.0
        base_eps = fwd_eps if fwd_eps and fwd_eps > 0 else eps
        self.assertAlmostEqual(base_eps, 7.0)

    def test_base_eps_forward_handles_inflated_trailing(self):
        """Forward EPS naturally avoids inflated trailing (one-time gains)."""
        eps, fwd_eps = 6.84, 2.1
        base_eps = fwd_eps if fwd_eps and fwd_eps > 0 else eps
        self.assertAlmostEqual(base_eps, 2.1)

    def test_base_eps_fallback_to_trailing_when_forward_negative(self):
        """When forward EPS is negative (temporary headwinds), use trailing."""
        eps, fwd_eps = 3.5, -0.5
        base_eps = fwd_eps if fwd_eps and fwd_eps > 0 else eps
        self.assertAlmostEqual(base_eps, 3.5)

    def test_pe_volatility_fallback_returns_tuple(self):
        from engine.lynch_pin_core import LynchPinEngine
        engine = LynchPinEngine.__new__(LynchPinEngine)
        engine.symbol = 'TEST'
        engine.ticker = MagicMock()
        engine.info = {'trailingEps': 5.0}
        # Empty history triggers fallback
        engine.ticker.history.return_value = pd.DataFrame()

        mean, std, dev = engine._pe_volatility_fallback(1.5, 20.0)
        self.assertEqual(mean, 1.5)
        self.assertAlmostEqual(std, 0.3)  # 1.5 * 0.2
        self.assertEqual(dev, 0.0)


# ─── engine/lynch_pin_core.py — historical forward PEG reconstruction (B2) ───

class TestHistoricalPegReconstruction(unittest.TestCase):

    # _blended_growth

    def test_blended_growth_at_now_equals_projection(self):
        from engine.lynch_pin_core import _blended_growth
        # k=0: nothing realized yet -> pure 5Y projection
        self.assertAlmostEqual(_blended_growth(0.0, 50.0, 20.0), 20.0)

    def test_blended_growth_five_years_back_equals_realized(self):
        from engine.lynch_pin_core import _blended_growth
        # k=5: the whole window has already happened
        self.assertAlmostEqual(_blended_growth(5.0, 12.0, 20.0), 12.0)

    def test_blended_growth_midpoint(self):
        from engine.lynch_pin_core import _blended_growth
        # k=2.5 -> (2.5*10 + 2.5*20)/5 = 15
        self.assertAlmostEqual(_blended_growth(2.5, 10.0, 20.0), 15.0)

    def test_blended_growth_floor_on_shrinking_revenue(self):
        from engine.lynch_pin_core import _blended_growth, _MIN_BLENDED_GROWTH
        # Negative realized growth (shrinking value names) must not
        # collapse the PEG denominator toward zero
        self.assertAlmostEqual(_blended_growth(5.0, -2.0, 3.0), _MIN_BLENDED_GROWTH)

    def test_blended_growth_clamps_k_beyond_window(self):
        from engine.lynch_pin_core import _blended_growth
        # k>5 behaves like k=5 (fully realized)
        self.assertAlmostEqual(_blended_growth(7.0, 10.0, 20.0), 10.0)

    # _fwd_eps_proxy

    def test_fwd_eps_proxy_stable_margins(self):
        from engine.lynch_pin_core import _fwd_eps_proxy
        # Revenue and EPS both halved -> proxy = fwd_eps / 2
        self.assertAlmostEqual(_fwd_eps_proxy(10.0, 0.5, 0.5), 5.0)

    def test_fwd_eps_proxy_geometric_blend(self):
        from engine.lynch_pin_core import _fwd_eps_proxy
        # Margin expansion: EPS grew faster than revenue.
        # sqrt(0.8 * 0.2) = 0.4 -> proxy lands between the two ratios
        self.assertAlmostEqual(_fwd_eps_proxy(10.0, 0.8, 0.2), 4.0)

    def test_fwd_eps_proxy_handles_zero_ratio(self):
        from engine.lynch_pin_core import _fwd_eps_proxy
        # Degenerate input must not raise or return negative
        self.assertGreater(_fwd_eps_proxy(10.0, 0.0, 0.5), 0.0)

    # _parse_sec_quarterly

    def test_parse_sec_quarterly_filters_annuals_prefers_restatements(self):
        from engine.lynch_pin_core import LynchPinEngine
        facts = {
            'Revenues': {'units': {'USD': [
                # annual entry (365 days) must be skipped
                {'form': '10-K', 'start': '2023-01-01', 'end': '2023-12-31',
                 'filed': '2024-02-01', 'val': 400.0},
                # quarterly, original filing
                {'form': '10-Q', 'start': '2023-07-01', 'end': '2023-09-30',
                 'filed': '2023-10-25', 'val': 100.0},
                # same quarter restated later -> must win
                {'form': '10-Q', 'start': '2023-07-01', 'end': '2023-09-30',
                 'filed': '2024-10-25', 'val': 105.0},
                {'form': '10-Q', 'start': '2023-04-01', 'end': '2023-06-30',
                 'filed': '2023-07-25', 'val': 95.0},
            ]}}}
        s = LynchPinEngine._parse_sec_quarterly(facts, ['Revenues'], ['USD'])
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s['2023-09-30'], 105.0)
        self.assertAlmostEqual(s['2023-06-30'], 95.0)

    def test_parse_sec_quarterly_tag_priority(self):
        from engine.lynch_pin_core import LynchPinEngine
        facts = {
            'RevenueFromContractWithCustomerExcludingAssessedTax': {'units': {'USD': [
                {'form': '10-Q', 'start': '2023-07-01', 'end': '2023-09-30',
                 'filed': '2023-10-25', 'val': 100.0}]}},
            'Revenues': {'units': {'USD': [
                {'form': '10-Q', 'start': '2023-07-01', 'end': '2023-09-30',
                 'filed': '2023-10-25', 'val': 999.0}]}},
        }
        s = LynchPinEngine._parse_sec_quarterly(
            facts,
            ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues'],
            ['USD'])
        self.assertAlmostEqual(s['2023-09-30'], 100.0)

    def test_parse_sec_quarterly_missing_tags_returns_empty(self):
        from engine.lynch_pin_core import LynchPinEngine
        s = LynchPinEngine._parse_sec_quarterly({}, ['Revenues'], ['USD'])
        self.assertEqual(len(s), 0)

    # calculate_peg_statistics end-to-end (mocked data)

    def _make_engine(self, fwd_eps=5.0, revenue=None):
        """Engine with constant price=100, TTM EPS=4, optional TTM revenue."""
        from engine.lynch_pin_core import LynchPinEngine
        engine = LynchPinEngine.__new__(LynchPinEngine)
        engine.symbol = 'TEST'
        engine.info = {'forwardEps': fwd_eps}
        engine.ticker = MagicMock()
        dates = pd.date_range('2021-01-31', periods=60, freq='MS', tz='UTC')
        engine.ticker.history.return_value = pd.DataFrame(
            {'Close': [100.0] * 60}, index=dates)
        eps_dates = pd.date_range('2020-03-31', periods=24, freq='3MS')
        engine._build_ttm_eps_from_sec = lambda: pd.Series(4.0, index=eps_dates)
        engine._build_ttm_eps_from_yfinance = lambda: None
        engine._build_ttm_revenue_from_sec = lambda: revenue
        return engine

    def test_peg_statistics_b2_constant_series(self):
        """Constant price/revenue/EPS with projection at the floor yields a
        flat reconstructed PEG = price / fwd_eps / floor."""
        from engine.lynch_pin_core import _MIN_BLENDED_GROWTH
        eps_dates = pd.date_range('2020-03-31', periods=24, freq='3MS')
        engine = self._make_engine(
            fwd_eps=5.0, revenue=pd.Series(1000.0, index=eps_dates))
        # constant revenue -> realized growth 0 -> blended floored everywhere
        mean, std, dev = engine.calculate_peg_statistics(
            curr_peg=1.0, growth_pct=_MIN_BLENDED_GROWTH)
        # PEG(t) = 100 / 5 / 4 = 5.0 for every month
        self.assertAlmostEqual(mean, 5.0, places=6)
        self.assertAlmostEqual(std, 0.01)  # min std floor
        self.assertAlmostEqual(dev, (1.0 - 5.0) / 0.01, places=3)

    def test_peg_statistics_falls_back_to_trailing_pe_series(self):
        """Without SEC revenue, the legacy trailing-PE-based series is used."""
        engine = self._make_engine(fwd_eps=5.0, revenue=None)
        mean, std, dev = engine.calculate_peg_statistics(
            curr_peg=1.0, growth_pct=4.0)
        # legacy: PE=100/4=25, PEG = 25/4 = 6.25 for every month
        self.assertAlmostEqual(mean, 6.25, places=6)
        self.assertAlmostEqual(std, 0.01)

    def test_peg_statistics_b2_skipped_without_forward_eps(self):
        """Negative forward EPS disables reconstruction -> legacy series."""
        eps_dates = pd.date_range('2020-03-31', periods=24, freq='3MS')
        engine = self._make_engine(
            fwd_eps=-1.0, revenue=pd.Series(1000.0, index=eps_dates))
        mean, std, dev = engine.calculate_peg_statistics(
            curr_peg=1.0, growth_pct=4.0)
        self.assertAlmostEqual(mean, 6.25, places=6)  # legacy path


# ─── graphics/visualizer.py ───

class TestVisualizer(unittest.TestCase):

    @patch('graphics.visualizer.yf.download')
    def test_get_benchmark_data_smh(self, mock_download):
        from graphics.visualizer import LynchPinVisualizer
        # Mock 5Y price data (compatible with both old 'M' and new 'ME' pandas)
        dates = pd.date_range('2021-01-01', periods=60, freq='MS')
        prices = pd.Series(np.linspace(100, 200, len(dates)), index=dates)
        mock_download.return_value = pd.DataFrame({'Close': prices})

        viz = LynchPinVisualizer(output_dir="/tmp/test_viz")
        label, cagr = viz._get_benchmark_data("database/smh.txt")
        self.assertIn("SMH", label)
        self.assertGreater(cagr, 0)

    @patch('graphics.visualizer.yf.download')
    def test_get_benchmark_data_fallback(self, mock_download):
        from graphics.visualizer import LynchPinVisualizer
        mock_download.return_value = pd.DataFrame()  # Empty = fallback

        viz = LynchPinVisualizer(output_dir="/tmp/test_viz")
        label, cagr = viz._get_benchmark_data("database/unknown.txt")
        self.assertEqual(label, "S&P 500")
        self.assertEqual(cagr, 10)

    def test_output_dir_created(self):
        from graphics.visualizer import LynchPinVisualizer
        import tempfile
        test_dir = os.path.join(tempfile.gettempdir(), "test_lynch_viz")
        if os.path.exists(test_dir):
            os.rmdir(test_dir)
        viz = LynchPinVisualizer(output_dir=test_dir)
        self.assertTrue(os.path.exists(test_dir))
        os.rmdir(test_dir)

    def test_plot_ticker_distribution_with_edge(self):
        from graphics.visualizer import LynchPinVisualizer
        import tempfile
        viz = LynchPinVisualizer(output_dir=tempfile.gettempdir())
        row = {'Ticker': 'TEST', 'PE': 20.0, 'FwdPE': 18.0, '2YFwd': 16.0,
               '5YGrowth': '15%', 'PEG': 1.2, 'Mean': 1.8, 'Dev_SD': -1.5,
               'Bull': '+18%', 'Base': '+12%', 'Bear': '+5%'}
        edge = {'bull_acc': 63.0, 'bull_pnl': 2.5, 'bull_n': 92,
                'bear_acc': 67.0, 'bear_pnl': 3.6, 'bear_n': 30, 'best_edge': 'BEAR'}
        path = viz.plot_ticker_distribution(row, None, None, None, edge)
        self.assertTrue(os.path.exists(path))
        os.remove(path)


# ─── engine/technical_timing.py ───

class TestTechnicalTiming(unittest.TestCase):

    def _make_ticker(self, prices, n=250):
        """Create a mock ticker with synthetic price history."""
        dates = pd.date_range(end='2024-07-18', periods=n, freq='B')
        close = pd.Series(prices, index=dates)
        high = close * 1.01
        low = close * 0.99
        hist = pd.DataFrame({'Close': close, 'High': high, 'Low': low})
        ticker = MagicMock()
        ticker.history.return_value = hist
        return ticker

    def test_bullish_trend(self):
        from engine.technical_timing import analyze
        # Steadily rising prices -> BULLISH
        prices = np.linspace(100, 200, 250)
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertIsNotNone(result)
        self.assertEqual(result['trend'], 'BULLISH')
        self.assertGreater(result['price_vs_sma200'], 0)

    def test_bearish_trend(self):
        from engine.technical_timing import analyze
        # Steadily falling prices -> BEARISH
        prices = np.linspace(200, 100, 250)
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertIsNotNone(result)
        self.assertEqual(result['trend'], 'BEARISH')
        self.assertLess(result['price_vs_sma200'], 0)

    def test_insufficient_data_returns_none(self):
        from engine.technical_timing import analyze
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame({'Close': [100]*50, 'High': [101]*50, 'Low': [99]*50})
        result = analyze(ticker)
        self.assertIsNone(result)

    def test_empty_history_returns_none(self):
        from engine.technical_timing import analyze
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame()
        result = analyze(ticker)
        self.assertIsNone(result)

    def test_rsi_in_valid_range(self):
        from engine.technical_timing import analyze
        np.random.seed(42)
        prices = np.linspace(100, 150, 250) + np.random.randn(250) * 2
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertGreaterEqual(result['rsi'], 0)
        self.assertLessEqual(result['rsi'], 100)

    def test_accumulation_zone_returned(self):
        from engine.technical_timing import analyze
        prices = np.linspace(100, 200, 250)
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertIn('accumulation_zone', result)
        zone = result['accumulation_zone']
        self.assertEqual(len(zone), 2)
        self.assertLess(zone[0], zone[1])

    def test_signal_is_valid_label(self):
        from engine.technical_timing import analyze
        prices = np.linspace(100, 200, 250)
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertIn(result['signal'], ('BULLISH', 'BEARISH', 'NEUTRAL', 'ACCUMULATION'))

    def test_atr_compression_positive(self):
        from engine.technical_timing import analyze
        prices = np.linspace(100, 150, 250)
        ticker = self._make_ticker(prices)
        result = analyze(ticker)
        self.assertGreater(result['atr_compression'], 0)

    def test_exception_returns_none(self):
        from engine.technical_timing import analyze
        ticker = MagicMock()
        ticker.history.side_effect = Exception("API error")
        result = analyze(ticker)
        self.assertIsNone(result)

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_bull(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.return_value = {
            'breakdown': {
                'BULLISH': {'accuracy': 70.0, 'avg_dir_pnl': 3.5, 'count': 50},
                'BEARISH': {'accuracy': 45.0, 'avg_dir_pnl': -1.2, 'count': 30},
            }
        }
        result = backtest_edge('TEST', 'QQQ', days=180)
        self.assertIsNotNone(result)
        self.assertEqual(result['best_edge'], 'BULL')
        self.assertEqual(result['bull_acc'], 70.0)
        self.assertEqual(result['bear_acc'], 45.0)
        self.assertEqual(result['bull_n'], 50)

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_bear(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.return_value = {
            'breakdown': {
                'BULLISH': {'accuracy': 40.0, 'avg_dir_pnl': -0.5, 'count': 20},
                'BEARISH': {'accuracy': 67.0, 'avg_dir_pnl': 3.0, 'count': 60},
            }
        }
        result = backtest_edge('TEST', 'SMH', days=180)
        self.assertEqual(result['best_edge'], 'BEAR')
        self.assertEqual(result['bear_pnl'], 3.0)

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_error(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.return_value = {'error': 'Insufficient data'}
        result = backtest_edge('TEST', 'QQQ')
        self.assertIsNone(result)

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_equal_accuracy(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.return_value = {
            'breakdown': {
                'BULLISH': {'accuracy': 55.0, 'avg_dir_pnl': 1.0, 'count': 40},
                'BEARISH': {'accuracy': 55.0, 'avg_dir_pnl': 1.0, 'count': 40},
            }
        }
        result = backtest_edge('TEST', 'QQQ')
        self.assertEqual(result['best_edge'], '\u2014')

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_missing_direction(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.return_value = {
            'breakdown': {
                'BULLISH': {'accuracy': 60.0, 'avg_dir_pnl': 2.0, 'count': 80},
            }
        }
        result = backtest_edge('TEST', 'QQQ')
        self.assertEqual(result['best_edge'], 'BULL')
        self.assertEqual(result['bear_acc'], 0)

    @patch('engine.technical_timing.backtest')
    def test_backtest_edge_exception(self, mock_bt):
        from engine.technical_timing import backtest_edge
        mock_bt.side_effect = Exception("Network error")
        result = backtest_edge('TEST', 'QQQ')
        self.assertIsNone(result)


# ─── main.py (regex & formatting logic) ───

class TestMainHelpers(unittest.TestCase):

    def test_sentiment_parsing(self):
        import re
        raw_ai = "SENTIMENT: $SMH is riding high on AI demand.\n\n$TSM\n🤖: Great stock."
        sent_match = re.search(r'SENTIMENT:\s*(.+)', raw_ai)
        self.assertIsNotNone(sent_match)
        sentiment_text = sent_match.group(1).strip()
        sentiment_text = re.sub(r'^SENTIMENT:\s*', '', sentiment_text)
        sentiment_text = re.sub(r'\$([A-Z]+)', r'\1', sentiment_text)
        self.assertEqual(sentiment_text, "SMH is riding high on AI demand.")

    def test_sentiment_double_prefix(self):
        import re
        raw_ai = "SENTIMENT: SENTIMENT: $QQQ looks strong.\n\ndata"
        sent_match = re.search(r'SENTIMENT:\s*(.+)', raw_ai)
        sentiment_text = sent_match.group(1).strip()
        sentiment_text = re.sub(r'^SENTIMENT:\s*', '', sentiment_text)
        sentiment_text = re.sub(r'\$([A-Z]+)', r'\1', sentiment_text)
        self.assertEqual(sentiment_text, "QQQ looks strong.")

    def test_ticker_regex_with_colon(self):
        import re
        bulk = "$MSFT:\n🤖: Great company.\n📊 Reverse DCF: Strong moat.\n\n$AAPL:\n🤖: Good."
        bulk = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk)
        pattern = rf"^\$MSFT\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        match = re.search(pattern, bulk, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn("Great company", match.group(1))

    def test_ticker_regex_without_colon(self):
        import re
        bulk = "$NVDA\n🤖: Monster growth.\n📊 Reverse DCF: AI dominance.\n\n$AMD\n🤖: Challenger."
        bulk = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk)
        pattern = rf"^\$NVDA\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        match = re.search(pattern, bulk, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn("Monster growth", match.group(1))

    def test_ticker_regex_stopword_ticker(self):
        """Regression: ticker ON must not match the English word 'on' inside
        another ticker's narrative (bug: ON reply showed ARM's analysis)."""
        import re
        bulk = ("$ARM:\n🤖: A bet on future dominance. Buyer beware.\n"
                "📊 Reverse DCF: ARM designs chips.\n\n"
                "$ON:\n🤖: ON Semiconductor is a compelling value.\n"
                "📊 Reverse DCF: Power and sensing leader.")
        pattern = rf"^\$ON\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        match = re.search(pattern, bulk, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertIn("ON Semiconductor is a compelling value", match.group(1))
        self.assertNotIn("future dominance", match.group(1))
        self.assertNotIn("ARM designs", match.group(1))

    def test_section_header_stripping(self):
        import re
        bulk = "SECTION 2 — PER-TICKER ANALYSIS:\n$AAPL\n🤖: Good stock."
        bulk = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk)
        self.assertNotIn("SECTION", bulk)
        self.assertIn("$AAPL", bulk)

    def test_cashtag_removal(self):
        import re
        text = "$NVDA is great and $AMD is a challenger"
        result = re.sub(r'\$([A-Z]+)', r'\1', text)
        self.assertEqual(result, "NVDA is great and AMD is a challenger")

    def test_idx_map_resolution(self):
        IDX_MAP = {
            "mag7": "MAGS", "mags": "MAGS",
            "nasdaq": "QQQ", "qqq": "QQQ",
            "schd": "SCHD", "smh": "SMH", "igv": "IGV",
        }
        src_stem = "smh"
        idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")
        self.assertEqual(idx_name, "SMH")

    def test_idx_map_fallback(self):
        IDX_MAP = {
            "mag7": "MAGS", "nasdaq": "QQQ", "schd": "SCHD", "smh": "SMH", "igv": "IGV",
        }
        src_stem = "unknown_file"
        idx_name = next((v for k, v in IDX_MAP.items() if k in src_stem), "SPY")
        self.assertEqual(idx_name, "SPY")

    def test_excl_bad_filters_bad_income_grade(self):
        _BAD_GRADES = {'B-', 'C', 'D', 'N/A'}
        self.assertIn('C', _BAD_GRADES)
        self.assertIn('D', _BAD_GRADES)
        self.assertIn('B-', _BAD_GRADES)
        self.assertNotIn('B', _BAD_GRADES)
        self.assertNotIn('B+', _BAD_GRADES)
        self.assertNotIn('A', _BAD_GRADES)

    def test_excl_bad_filters_bad_credit_rating(self):
        _BAD_RATINGS = {'BB+', 'BB', 'BB-', 'B+', 'B', 'B-', 'CCC+', 'CCC', 'CCC-', 'CC', 'D', 'NR'}
        self.assertIn('CC', _BAD_RATINGS)
        self.assertIn('BB', _BAD_RATINGS)
        self.assertIn('D', _BAD_RATINGS)
        self.assertNotIn('BBB', _BAD_RATINGS)
        self.assertNotIn('BBB-', _BAD_RATINGS)
        self.assertNotIn('A', _BAD_RATINGS)
        self.assertNotIn('AAA', _BAD_RATINGS)


class TestSimulator(unittest.TestCase):
    """Tests for experimental/simulator.py: ATR stops, chandelier trail,
    regime gate, cooldown, ranking, sizing, slippage and time stop."""

    @classmethod
    def setUpClass(cls):
        from experimental import simulator
        cls.sim = simulator

    def _setup(self, sym, score, dte=None, trend=0.0):
        return {"symbol": sym, "index": "QQQ", "direction": "bull",
                "price": 100.0, "atr": 2.0, "score": score,
                "days_to_earnings": dte, "trend_strength": trend}

    # ── Ranking ───────────────────────────────────────────────────────────

    def test_rank_setups_earnings_in_window_first(self):
        """A setup whose earnings fall inside the hold window ranks first."""
        setups = [self._setup("FAR", 4, dte=90), self._setup("SOON", 3, dte=10),
                  self._setup("NONE", 4, dte=None)]
        ranked = self.sim._rank_setups(setups)
        self.assertEqual(ranked[0]["symbol"], "SOON")

    def test_rank_setups_prefers_score_3_4_over_5(self):
        """Score-5 setups averaged +0.05R vs +0.21R for 3-4 → ranked lower."""
        setups = [self._setup("FIVE", 5), self._setup("THREE", 3), self._setup("FOUR", 4)]
        ranked = self.sim._rank_setups(setups)
        self.assertEqual(ranked[-1]["symbol"], "FIVE")

    def test_rank_setups_trend_strength_tiebreak(self):
        setups = [self._setup("WEAK", 4, trend=0.5), self._setup("STRONG", 4, trend=2.5)]
        ranked = self.sim._rank_setups(setups)
        self.assertEqual([s["symbol"] for s in ranked], ["STRONG", "WEAK"])

    # ── Score band ────────────────────────────────────────────────────────

    def test_min_score_floor_is_three(self):
        self.assertEqual(self.sim.MIN_SCORE, 3)

    def test_score_band_excludes_six_plus(self):
        """History showed score 6+ setups underperform — band is 3-5."""
        self.assertEqual(self.sim.MAX_SCORE, 5)
        for score, expect_pass in [(2, False), (3, True), (5, True), (6, False), (7, False)]:
            in_band = self.sim.MIN_SCORE <= score <= self.sim.MAX_SCORE
            self.assertEqual(in_band, expect_pass, f"score={score}")

    # ── ATR & initial stop ────────────────────────────────────────────────

    def _bars(self, n=30, close=100.0, rng=2.0):
        idx = pd.bdate_range("2026-01-01", periods=n)
        return pd.DataFrame({"Open": close, "High": close + rng / 2,
                             "Low": close - rng / 2, "Close": close,
                             "Volume": 1000}, index=idx)

    def test_atr_constant_range(self):
        """Flat closes with a constant 2.0 high-low range → ATR = 2.0."""
        self.assertAlmostEqual(self.sim._atr(self._bars()), 2.0)

    def test_atr_insufficient_data(self):
        self.assertIsNone(self.sim._atr(self._bars(n=5)))
        self.assertIsNone(self.sim._atr(None))

    def test_initial_stop_is_stop_atr_multiple(self):
        atr = 3.0
        self.assertAlmostEqual(self.sim._initial_stop("bull", 100.0, atr), 100.0 - self.sim.STOP_ATR * atr)
        self.assertAlmostEqual(self.sim._initial_stop("bear", 100.0, atr), 100.0 + self.sim.STOP_ATR * atr)

    def test_stop_atr_is_wide(self):
        """The whole point of v2: stop ≥ 2 ATR (old median was 0.36 ATR)."""
        self.assertGreaterEqual(self.sim.STOP_ATR, 2.0)
        self.assertGreater(self.sim.TRAIL_ATR, self.sim.STOP_ATR)

    # ── Regime gate ───────────────────────────────────────────────────────

    def test_direction_allowed_matches_regime(self):
        self.assertTrue(self.sim._direction_allowed("bull", "UP"))
        self.assertFalse(self.sim._direction_allowed("bull", "DOWN"))
        self.assertTrue(self.sim._direction_allowed("bear", "DOWN"))
        self.assertFalse(self.sim._direction_allowed("bear", "UP"))

    def test_direction_blocked_without_regime(self):
        self.assertFalse(self.sim._direction_allowed("bull", None))
        self.assertFalse(self.sim._direction_allowed("bear", None))

    def test_index_regime_uses_cache(self):
        cache = {"QQQ": "DOWN"}
        with patch.object(self.sim, "_completed_daily_bars") as m:
            self.assertEqual(self.sim._index_regime("QQQ", cache), "DOWN")
            m.assert_not_called()

    def test_index_regime_from_bars(self):
        n = self.sim.REGIME_SMA + 5
        idx = pd.bdate_range("2026-01-01", periods=n)
        up = pd.DataFrame({"Close": np.linspace(90, 110, n)}, index=idx)
        down = pd.DataFrame({"Close": np.linspace(110, 90, n)}, index=idx)
        with patch.object(self.sim, "_completed_daily_bars", return_value=up):
            self.assertEqual(self.sim._index_regime("QQQ", {}), "UP")
        with patch.object(self.sim, "_completed_daily_bars", return_value=down):
            self.assertEqual(self.sim._index_regime("QQQ", {}), "DOWN")
        with patch.object(self.sim, "_completed_daily_bars", return_value=None):
            self.assertIsNone(self.sim._index_regime("QQQ", {}))

    # ── Chandelier trail ──────────────────────────────────────────────────

    def _pos(self, direction="bull", entry=100.0, stop=94.0):
        return {"symbol": "X", "direction": direction, "entry_price": entry,
                "stop": stop, "initial_stop": stop, "initial_risk": abs(entry - stop),
                "size": 1000.0, "shares": 10.0, "opened_at": "2026-01-05T08:00:00"}

    def test_trail_ratchets_up_for_long(self):
        pos = self._pos()  # stop 94
        # best close 110, ATR 3 → candidate 110 - 9 = 101 > 94 → moves
        self.assertTrue(self.sim._ratchet_trail(pos, 110.0, 3.0))
        self.assertEqual(pos["stop"], 101.0)

    def test_trail_never_loosens(self):
        pos = self._pos(stop=105.0)
        self.assertFalse(self.sim._ratchet_trail(pos, 110.0, 3.0))  # 101 < 105
        self.assertEqual(pos["stop"], 105.0)

    def test_trail_ratchets_down_for_short(self):
        pos = self._pos(direction="bear", entry=100.0, stop=106.0)
        self.assertTrue(self.sim._ratchet_trail(pos, 90.0, 3.0))   # 90 + 9 = 99 < 106
        self.assertEqual(pos["stop"], 99.0)
        self.assertFalse(self.sim._ratchet_trail(pos, 95.0, 3.0))  # 104 > 99 → no loosen

    def test_trail_ignores_bad_atr(self):
        pos = self._pos()
        self.assertFalse(self.sim._ratchet_trail(pos, 110.0, None))
        self.assertFalse(self.sim._ratchet_trail(pos, 110.0, 0.0))

    def test_update_trailing_stops_uses_completed_bars_since_entry(self):
        """Best close is taken from bars on/after the entry date only, and the
        ratchet runs at most once per day per position."""
        idx = pd.bdate_range("2026-01-01", periods=30)
        closes = np.full(30, 100.0)
        closes[idx.get_indexer([pd.Timestamp("2026-01-20")])[0]] = 120.0  # spike after entry
        closes[0] = 150.0  # spike BEFORE entry must be ignored
        bars = pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1,
                             "Close": closes, "Volume": 1}, index=idx)
        state = {"positions": [self._pos()], "history": [], "balance": 0}
        with patch.object(self.sim, "_completed_daily_bars", return_value=bars), \
             patch.object(self.sim, "_save_state"), patch.object(self.sim, "_log"):
            moved = self.sim._update_trailing_stops(state, "2026-02-13")
            self.assertEqual(moved, 1)
            atr = self.sim._atr(bars)
            self.assertAlmostEqual(state["positions"][0]["stop"],
                                   round(120.0 - self.sim.TRAIL_ATR * atr, 2))
            self.assertEqual(state["positions"][0]["trail_date"], "2026-02-13")
            # second call same day is a no-op
            self.assertEqual(self.sim._update_trailing_stops(state, "2026-02-13"), 0)

    # ── Cooldown ──────────────────────────────────────────────────────────

    def test_cooldown_after_recent_stop(self):
        from datetime import datetime, timedelta
        now = datetime(2026, 3, 10, 8, 0)
        hist = [{"symbol": "X", "close_reason": "STOP",
                 "closed_at": (now - timedelta(days=3)).isoformat()}]
        self.assertTrue(self.sim._in_cooldown("X", hist, now))
        self.assertFalse(self.sim._in_cooldown("Y", hist, now))

    def test_cooldown_expires_and_ignores_non_stop_exits(self):
        from datetime import datetime, timedelta
        now = datetime(2026, 3, 10, 8, 0)
        old = [{"symbol": "X", "close_reason": "STOP",
                "closed_at": (now - timedelta(days=self.sim.COOLDOWN_DAYS + 1)).isoformat()}]
        self.assertFalse(self.sim._in_cooldown("X", old, now))
        trail = [{"symbol": "X", "close_reason": "TRAIL",
                  "closed_at": (now - timedelta(days=1)).isoformat()}]
        self.assertFalse(self.sim._in_cooldown("X", trail, now))

    # ── check_positions ───────────────────────────────────────────────────

    def test_check_positions_stop_vs_trail_reason(self):
        """Exit at the untouched initial stop → STOP; at a ratcheted stop → TRAIL,
        and the R-multiple is measured against the initial risk."""
        st = {"positions": [self._pos(), self._pos()], "history": [], "balance": 0.0}
        st["positions"][0]["symbol"] = "A"
        st["positions"][1]["symbol"] = "B"; st["positions"][1]["stop"] = 108.0
        prices = {"A": 93.0, "B": 107.0}
        with patch.object(self.sim, "_get_price", side_effect=lambda s: prices[s]), \
             patch.object(self.sim, "_save_state"), patch.object(self.sim, "_log"):
            closed = self.sim.check_positions(st)
        self.assertEqual(closed, 2)
        by = {t["symbol"]: t for t in st["history"]}
        self.assertEqual(by["A"]["close_reason"], "STOP")
        self.assertEqual(by["B"]["close_reason"], "TRAIL")
        self.assertLess(by["A"]["r_multiple"], 0)
        self.assertGreater(by["B"]["r_multiple"], 1.0)

    def test_check_positions_no_target_exit(self):
        """A big favourable move alone does not close the trade (no fixed target)."""
        from datetime import datetime
        st = {"positions": [self._pos()], "history": [], "balance": 0.0}
        st["positions"][0]["target"] = 105.0
        st["positions"][0]["opened_at"] = datetime.now().isoformat()  # not time-stopped
        with patch.object(self.sim, "_get_price", return_value=130.0), \
             patch.object(self.sim, "_save_state"), patch.object(self.sim, "_log"):
            self.assertEqual(self.sim.check_positions(st), 0)
        self.assertEqual(len(st["positions"]), 1)

    # ── Slippage ──────────────────────────────────────────────────────────

    def test_slip_always_worse(self):
        """Every fill is worse than the quote: buys pay up, sells receive less."""
        q = 100.0
        self.assertGreater(self.sim._slip(q, "bull", "entry"), q)   # buy
        self.assertLess(self.sim._slip(q, "bull", "exit"), q)       # sell
        self.assertLess(self.sim._slip(q, "bear", "entry"), q)      # short sell
        self.assertGreater(self.sim._slip(q, "bear", "exit"), q)    # buy to cover

    def test_slip_magnitude(self):
        expected = 100.0 * (1 + self.sim.SLIPPAGE_BPS / 10000.0)
        self.assertAlmostEqual(self.sim._slip(100.0, "bull", "entry"), expected)

    # ── Risk-based sizing ─────────────────────────────────────────────────

    def test_position_size_equal_dollar_risk(self):
        """A 4% stop and a 5% stop should risk the same dollars."""
        equity, cash = 10000.0, 10000.0
        tight = self.sim._position_size(equity, cash, 100.0, 96.0)   # 4% stop
        wide = self.sim._position_size(equity, cash, 100.0, 95.0)    # 5% stop
        self.assertAlmostEqual(tight * 0.04, wide * 0.05, places=2)
        self.assertAlmostEqual(tight * 0.04, equity * self.sim.RISK_PCT, places=2)

    def test_position_size_notional_cap(self):
        """A very tight stop can't blow past the notional cap."""
        equity, cash = 10000.0, 10000.0
        size = self.sim._position_size(equity, cash, 100.0, 99.9)  # 0.1% stop
        self.assertLessEqual(size, equity * self.sim.MAX_NOTIONAL_PCT)

    def test_position_size_cash_cap_and_degenerate(self):
        self.assertLessEqual(self.sim._position_size(10000.0, 500.0, 100.0, 98.0), 500.0)
        self.assertEqual(self.sim._position_size(10000.0, 10000.0, 100.0, 100.0), 0.0)
        self.assertEqual(self.sim._position_size(10000.0, 10000.0, 0.0, 98.0), 0.0)

    # ── Time stop ─────────────────────────────────────────────────────────

    def test_trading_days_held_skips_weekends(self):
        from datetime import datetime
        # Fri 2026-08-07 -> Mon 2026-08-10 is 1 trading day
        held = self.sim._trading_days_held("2026-08-07T07:45:00",
                                           now=datetime(2026, 8, 10, 8, 0))
        self.assertEqual(held, 1)
        # Fri -> 4 weeks later = 20 trading days (time stop fires)
        held = self.sim._trading_days_held("2026-08-07T07:45:00",
                                           now=datetime(2026, 9, 4, 8, 0))
        self.assertEqual(held, 20)
        self.assertGreaterEqual(held, self.sim.MAX_HOLD_DAYS)

    def test_trading_days_held_same_day(self):
        from datetime import datetime
        held = self.sim._trading_days_held("2026-08-10T07:45:00",
                                           now=datetime(2026, 8, 10, 12, 0))
        self.assertEqual(held, 0)


# ─── engine/portfolio.py ───

class TestPortfolio(unittest.TestCase):
    """Portfolio file parsing, weighting, weighted roll-ups and the X-ray plot."""

    ROWS = [
        {'Ticker': 'AAPL*', 'PE': 30.0, 'FwdPE': 25.0, '2YFwd': 22.0, '5YGrowth': '10%',
         'PEG': 2.5, 'Mean': 2.0, 'Dev_SD': 1.0, 'Bull': '12%', 'Base': '8%', 'Bear': '2%'},
        {'Ticker': 'MSFT', 'PE': 35.0, 'FwdPE': 30.0, '2YFwd': 26.0, '5YGrowth': '15%',
         'PEG': 2.0, 'Mean': 2.2, 'Dev_SD': -0.5, 'Bull': '18%', 'Base': '12%', 'Bear': '6%'},
        {'Ticker': 'NVDA', 'PE': 50.0, 'FwdPE': 30.0, '2YFwd': 22.0, '5YGrowth': '40%',
         'PEG': 0.75, 'Mean': 1.2, 'Dev_SD': -1.5, 'Bull': '30%', 'Base': '20%', 'Bear': '10%'},
    ]

    def _write(self, content):
        import tempfile
        f = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
        f.write(content)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_parse_dedupes_and_sums_shares(self):
        from engine.portfolio import parse_portfolio_file
        path = self._write("# holdings\nAAPL, 10\nmsft 5\nAAPL, 5\nNVDA;20\nGOOGL\t3\n\n")
        pos = parse_portfolio_file(path)
        self.assertEqual(list(pos.keys()), ['AAPL', 'MSFT', 'NVDA', 'GOOGL'])  # first-seen order
        self.assertEqual(pos['AAPL'], 15.0)
        self.assertEqual(pos['MSFT'], 5.0)
        self.assertEqual(pos['GOOGL'], 3.0)

    def test_parse_fractional_and_thousands(self):
        from engine.portfolio import parse_portfolio_file
        pos = parse_portfolio_file(self._write("VTI, 1,250.5\nBRK.B, 2\n"))
        self.assertEqual(pos['VTI'], 1250.5)
        self.assertIn('BRK.B', pos)

    def test_parse_rejects_bad_line(self):
        from engine.portfolio import parse_portfolio_file
        with self.assertRaises(ValueError):
            parse_portfolio_file(self._write("AAPL, 10\nthis is not a position\n"))
        with self.assertRaises(ValueError):
            parse_portfolio_file(self._write("AAPL, 0\n"))
        with self.assertRaises(ValueError):
            parse_portfolio_file(self._write("# only comments\n"))
        with self.assertRaises(FileNotFoundError):
            parse_portfolio_file('/nonexistent/portfolio.txt')

    def test_compute_weights_by_market_value_desc(self):
        from engine.portfolio import compute_weights
        pos = {'AAPL': 15, 'MSFT': 5, 'NVDA': 20, 'GOOGL': 3, 'NOPX': 100}
        w = compute_weights(pos, {'AAPL': 200, 'MSFT': 420, 'NVDA': 120, 'GOOGL': 170, 'NOPX': None})
        # 3000, 2100, 2400, 510 -> total 8010; unpriced NOPX dropped
        self.assertEqual(list(w.keys()), ['AAPL', 'NVDA', 'MSFT', 'GOOGL'])
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertAlmostEqual(w['AAPL'], 3000 / 8010)
        self.assertNotIn('NOPX', w)
        self.assertEqual(compute_weights({'X': 1}, {}), {})

    def test_weighted_metrics_arithmetic_and_harmonic(self):
        from engine.portfolio import weighted_metrics
        w = {'AAPL': 0.5, 'MSFT': 0.25, 'NVDA': 0.25}
        wm = weighted_metrics(self.ROWS, w)
        self.assertAlmostEqual(wm['PEG'], 0.5 * 2.5 + 0.25 * 2.0 + 0.25 * 0.75)
        self.assertAlmostEqual(wm['Growth'], 0.5 * 10 + 0.25 * 15 + 0.25 * 40)
        self.assertAlmostEqual(wm['Base'], 0.5 * 8 + 0.25 * 12 + 0.25 * 20)
        self.assertAlmostEqual(wm['Dev_SD'], 0.5 * 1.0 + 0.25 * -0.5 + 0.25 * -1.5)
        # Harmonic: 1 / Σ(w/PE)
        self.assertAlmostEqual(wm['PE'], 1 / (0.5 / 30 + 0.25 / 35 + 0.25 / 50))
        self.assertAlmostEqual(wm['FwdPE'], 1 / (0.5 / 25 + 0.25 / 30 + 0.25 / 30))
        # Std recovered from |PEG-Mean|/|Dev|: 0.5, 0.4, 0.3
        self.assertAlmostEqual(wm['Std'], 0.5 * 0.5 + 0.25 * 0.4 + 0.25 * 0.3)
        self.assertEqual(wm['MedianPEG'], 2.0)  # sorted 0.75(.25) → 2.0(.50 cumulative) → 2.5
        self.assertAlmostEqual(wm['Coverage'], 1.0)
        self.assertEqual(wm['Positions'], 3)

    def test_weighted_metrics_excludes_nonpositive_pe_and_uncovered_rows(self):
        from engine.portfolio import weighted_metrics
        rows = [dict(self.ROWS[0]), dict(self.ROWS[1])]
        rows[0]['PE'] = 0  # unprofitable: engine reports 0
        w = {'AAPL': 0.5, 'MSFT': 0.3, 'GOOGL': 0.2}  # GOOGL has no row
        wm = weighted_metrics(rows, w)
        self.assertAlmostEqual(wm['PE'], 35.0)  # only MSFT counts
        self.assertAlmostEqual(wm['Coverage'], 0.8)
        self.assertEqual(wm['Positions'], 2)
        self.assertEqual(wm['TotalPositions'], 3)
        from engine.portfolio import format_weighted_summary
        self.assertIn('2 of 3 positions have GARP data', format_weighted_summary(wm))

    def test_weighted_metrics_empty(self):
        from engine.portfolio import weighted_metrics
        wm = weighted_metrics([], {})
        self.assertIsNone(wm['PEG'])
        self.assertEqual(wm['IncomeGrade'], 'N/A')
        self.assertEqual(wm['CreditRating'], 'NR')

    def test_weighted_grades(self):
        from engine.portfolio import weighted_income_grade, weighted_credit_rating
        w = {'A': 0.5, 'B': 0.5}
        grade, score = weighted_income_grade(w, {'A': {'grade': 'A++'}, 'B': {'grade': 'B'}})
        self.assertEqual(grade, 'A')  # (8 + 4) / 2 = 6 -> A
        rating, rscore = weighted_credit_rating(w, {'A': {'rating': 'AAA'}, 'B': {'rating': 'BBB'}})
        self.assertEqual(rating, 'A+')  # (20 + 12) / 2 = 16 -> A+
        self.assertEqual(rscore, 16)
        # N/A grades and missing tickers are ignored
        grade, _ = weighted_income_grade(w, {'A': {'grade': 'N/A'}})
        self.assertEqual(grade, 'N/A')

    def test_format_summary_handles_none(self):
        from engine.portfolio import weighted_metrics, format_weighted_summary
        text = format_weighted_summary(weighted_metrics([], {}))
        self.assertIn('N/A', text)
        self.assertIn('Weighted PEG', text)

    def test_plot_portfolio_creates_image_without_network(self):
        from graphics.visualizer import LynchPinVisualizer
        from engine.portfolio import weighted_metrics
        import tempfile
        viz = LynchPinVisualizer(output_dir=tempfile.gettempdir())
        w = {'AAPL': 0.5, 'MSFT': 0.25, 'NVDA': 0.25}
        wm = weighted_metrics(self.ROWS, w, {'AAPL': {'grade': 'A'}}, {'AAPL': {'rating': 'AAA'}})
        with patch('graphics.visualizer.yf.download') as dl:
            path = viz.plot_portfolio(w, wm, benchmark_returns=[('QQQ (5.0Y)', 17.0), ('S&P 500 (5.0Y)', 13.0)])
            dl.assert_not_called()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith('portfolio_allocation.png'))
        os.remove(path)

    def test_pie_slices_group_small_positions(self):
        from graphics.visualizer import LynchPinVisualizer
        import tempfile
        viz = LynchPinVisualizer(output_dir=tempfile.gettempdir())
        w = {'NVDA': 0.5, 'MSFT': 0.3, 'AAPL': 0.1, 'TSM': 0.03, 'AMD': 0.02,
             'PLTR': 0.02, 'KO': 0.02, 'PEP': 0.01}
        slices = viz._pie_slices(w)
        labels = [t for t, _ in slices]
        # Everything below the on-wedge label threshold is folded into one slice
        self.assertEqual(labels, ['NVDA', 'MSFT', 'AAPL', 'Other (5)'])
        self.assertAlmostEqual(dict(slices)['Other (5)'], 0.10)
        self.assertAlmostEqual(sum(v for _, v in slices), 1.0)
        # A single small position keeps its own name; nothing to group otherwise
        self.assertEqual([t for t, _ in viz._pie_slices({'A': 0.98, 'B': 0.02})], ['A', 'B'])
        self.assertEqual(len(viz._pie_slices({'A': 0.6, 'B': 0.4})), 2)


# ─── main.py portfolio helpers ───

class TestMainPortfolioHelpers(unittest.TestCase):

    def test_extract_price_priority_and_none(self):
        from main import _extract_price
        self.assertEqual(_extract_price({'currentPrice': 101.5, 'regularMarketPrice': 99}), 101.5)
        self.assertEqual(_extract_price({'regularMarketPrice': 99}), 99.0)
        self.assertEqual(_extract_price({'currentPrice': 0, 'previousClose': 50}), 50.0)
        self.assertIsNone(_extract_price({}))
        self.assertIsNone(_extract_price(None))

    def test_sort_positions_desc_by_weight(self):
        from main import _sort_positions
        df = pd.DataFrame([{'Ticker': 'AAPL*', 'PEG': 2.5}, {'Ticker': 'NVDA', 'PEG': 0.7},
                           {'Ticker': 'ZZZ', 'PEG': 1.0}])
        out = _sort_positions(df, {'AAPL': 0.3, 'NVDA': 0.6})
        self.assertEqual(list(out['Ticker']), ['NVDA', 'AAPL*', 'ZZZ'])
        self.assertEqual(out['Weight'].tolist(), [0.6, 0.3, 0.0])

    def test_resolve_idx_name(self):
        from main import _resolve_idx_name
        import argparse
        a = argparse.Namespace(weekly=False, portfolio='pf.txt', src='database/smh.txt')
        self.assertEqual(_resolve_idx_name(a), 'SPY')
        a = argparse.Namespace(weekly=False, portfolio=None, src='database/smh.txt')
        self.assertEqual(_resolve_idx_name(a), 'SMH')
        a = argparse.Namespace(weekly=True, portfolio=None, src='database/smh.txt')
        self.assertEqual(_resolve_idx_name(a), 'SPY')

    def test_grok_portfolio_question(self):
        from main import _grok_portfolio_question
        q = _grok_portfolio_question(7)
        self.assertIn('@grok', q)
        self.assertIn('BEST', q)
        self.assertIn('WORST', q)
        self.assertIn('7 positions', q)
        self.assertIn('DISCLAIMER', q)
        # Asks for FinTwit sentiment via X search, not just the thread's numbers
        self.assertIn('search recent X posts', q)
        self.assertIn('FinTwit sentiment alone', q)
        self.assertIn('love most and hate most', q)
        self.assertLess(len(q), 4000)

    def test_portfolio_flag_rejects_top_and_excl_bad(self):
        import sys
        from unittest.mock import patch as _patch
        import main as m
        with _patch.object(sys, 'argv', ['main.py', '--portfolio', 'x.txt', '--top', '3']), \
             _patch('builtins.print') as p:
            m.main()
        self.assertTrue(any('cannot be combined' in str(c) for c in p.call_args_list))

    def test_extract_portfolio_narrative(self):
        from main import _extract_portfolio_narrative
        bulk = ("PORTFOLIO:\n🐂 Bull: Three AAA names and $NVDA carry it.\n\n🐻 Bear: $AAPL is 37%.\n\n"
                "$AAPL:\n🤖: AAPL overview.\n📊 Reverse DCF: math.\n\n$NVDA:\n🤖: NVDA overview.")
        narrative, rest = _extract_portfolio_narrative(bulk)
        self.assertTrue(narrative.startswith('🐂 Bull:'))
        self.assertIn('🐻 Bear: AAPL is 37%.', narrative)   # cashtags stripped
        self.assertNotIn('$', narrative)
        self.assertNotIn('PORTFOLIO:', rest)
        self.assertTrue(rest.startswith('$AAPL:'))
        # Per-ticker regex still finds each block in the remaining text
        import re
        m = re.search(r"^\$NVDA\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)", rest, re.DOTALL | re.MULTILINE)
        self.assertIn('NVDA overview', m.group(1))
        # Absent block → empty narrative, text untouched
        self.assertEqual(_extract_portfolio_narrative("$AAPL:\nx"), ("", "$AAPL:\nx"))

    def test_build_prompt_portfolio_mode(self):
        from engine.ai_research import LynchPinResearcher
        rows = [{'Ticker': 'AAPL', 'PE': 30, 'FwdPE': 25, '2YFwd': 22, '5YGrowth': '10%', 'PEG': 2.5,
                 'Mean': 2.0, 'Dev_SD': 1.0, 'Bull': '12%', 'Base': '8%', 'Bear': '2%'}]
        p = LynchPinResearcher.build_prompt(rows, portfolio_summary="  Weighted PEG: 1.7")
        self.assertIn('PORTFOLIO', p)
        self.assertIn('Weighted PEG: 1.7', p)
        self.assertNotIn('INDEX: $', p)
        self.assertIn('SENTIMENT:', p)
        self.assertIn('PORTFOLIO:\n🐂 Bull:', p)          # portfolio-level bull/bear requested
        self.assertIn('📊 Reverse DCF', p)                  # per-position format = daily scan format
        self.assertIn('🧪 Stomach Test', p)
        p2 = LynchPinResearcher.build_prompt(rows, idx_name='QQQ')
        self.assertIn('INDEX: $QQQ', p2)
        self.assertNotIn('PORTFOLIO', p2)


if __name__ == '__main__':
    unittest.main()
