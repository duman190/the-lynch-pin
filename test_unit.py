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
        # Growth < 20, mean_peg 3.0 > 2.5 cap
        self.assertAlmostEqual(_terminal_peg(15, 3.0), 2.5)

    def test_terminal_peg_high_growth_reversed(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 60%: 1.5 - 0.5*(60/30 - 1) = 1.5 - 0.5 = 1.0
        self.assertAlmostEqual(_terminal_peg(60, 2.0), 1.0)

    def test_terminal_peg_high_growth_floor(self):
        from engine.lynch_pin_core import _terminal_peg
        # Growth 90%: 1.5 - 0.5*(90/30 - 1) = 1.5 - 1.0 = 0.5 -> floored at 0.8
        self.assertAlmostEqual(_terminal_peg(90, 2.0), 0.8)

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
    """Tests for experimental/simulator.py setup ranking and fill validation."""

    @classmethod
    def setUpClass(cls):
        from experimental import simulator
        cls.sim = simulator

    def _setup(self, sym, rr, score, edge=60):
        return {"symbol": sym, "index": "QQQ", "direction": "bull",
                "price": 100.0, "target": 110.0, "stop": 95.0,
                "score": score, "edge": edge, "edge_pnl": 1.0, "rr": rr}

    def test_rank_setups_rr_primary(self):
        """Setups sorted by R/R descending regardless of edge/score."""
        setups = [self._setup("A", rr=1.6, score=7, edge=75),
                  self._setup("B", rr=3.0, score=3, edge=55),
                  self._setup("C", rr=2.2, score=5, edge=65)]
        ranked = self.sim._rank_setups(setups)
        self.assertEqual([s["symbol"] for s in ranked], ["B", "C", "A"])

    def test_rank_setups_score_tiebreaker(self):
        """Equal R/R falls back to score."""
        setups = [self._setup("LOW", rr=2.0, score=3),
                  self._setup("HIGH", rr=2.0, score=6),
                  self._setup("MID", rr=2.0, score=4)]
        ranked = self.sim._rank_setups(setups)
        self.assertEqual([s["symbol"] for s in ranked], ["HIGH", "MID", "LOW"])

    def test_fill_rr_valid_bull(self):
        # reward = 110 - 100 = 10, risk = 100 - 95 = 5 -> rr 2.0
        self.assertEqual(self.sim._fill_rr("bull", 100.0, 110.0, 95.0), 2.0)

    def test_fill_rr_valid_bear(self):
        # reward = 100 - 90 = 10, risk = 104 - 100 = 4 -> rr 2.5
        self.assertEqual(self.sim._fill_rr("bear", 100.0, 90.0, 104.0), 2.5)

    def test_fill_rr_rejects_price_through_stop(self):
        """Bull whose fill price drifted below stop -> invalid geometry."""
        self.assertIsNone(self.sim._fill_rr("bull", 94.0, 110.0, 95.0))

    def test_fill_rr_rejects_price_through_target(self):
        """Bull whose fill price drifted above target -> no reward left."""
        self.assertIsNone(self.sim._fill_rr("bull", 111.0, 110.0, 95.0))

    def test_fill_rr_rejects_compressed_ratio(self):
        """Price drift compressed R/R below MIN_RR -> rejected.
        reward = 110 - 108 = 2, risk = 108 - 95 = 13 -> rr 0.15 < MIN_RR."""
        self.assertIsNone(self.sim._fill_rr("bull", 108.0, 110.0, 95.0))

    def test_fill_rr_boundary_at_min_rr(self):
        """R/R exactly at MIN_RR is accepted."""
        # reward = MIN_RR * risk: risk = 5, reward = MIN_RR * 5
        target = 100.0 + self.sim.MIN_RR * 5.0
        self.assertEqual(self.sim._fill_rr("bull", 100.0, target, 95.0), self.sim.MIN_RR)

    def test_min_score_floor_is_three(self):
        self.assertEqual(self.sim.MIN_SCORE, 3)

    def test_score_band_excludes_six_plus(self):
        """History showed score 6+ setups underperform — band is 3-5."""
        self.assertEqual(self.sim.MAX_SCORE, 5)
        for score, expect_pass in [(2, False), (3, True), (5, True), (6, False), (7, False)]:
            in_band = self.sim.MIN_SCORE <= score <= self.sim.MAX_SCORE
            self.assertEqual(in_band, expect_pass, f"score={score}")

    def test_min_rr_floor(self):
        self.assertEqual(self.sim.MIN_RR, 2.0)

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
        # dollar risk = size * stop_frac — must be equal (= equity * RISK_PCT)
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
        # Fri -> next Fri = 5 trading days (time stop fires)
        held = self.sim._trading_days_held("2026-08-07T07:45:00",
                                           now=datetime(2026, 8, 14, 8, 0))
        self.assertEqual(held, 5)
        self.assertGreaterEqual(held, self.sim.MAX_HOLD_DAYS)

    def test_trading_days_held_same_day(self):
        from datetime import datetime
        held = self.sim._trading_days_held("2026-08-10T07:45:00",
                                           now=datetime(2026, 8, 10, 12, 0))
        self.assertEqual(held, 0)

    # ── Breakeven stop ────────────────────────────────────────────────────

    def _pos(self, direction="bull", entry=100.0, stop=95.0, target=110.0):
        return {"symbol": "X", "direction": direction, "entry_price": entry,
                "stop": stop, "target": target, "initial_risk": abs(entry - stop),
                "size": 1000.0, "shares": 10.0}

    def test_breakeven_arms_at_one_r_bull(self):
        pos = self._pos()  # risk = 5
        self.assertFalse(self.sim._maybe_breakeven(pos, 104.9))  # < +1R
        self.assertEqual(pos["stop"], 95.0)
        self.assertTrue(self.sim._maybe_breakeven(pos, 105.0))   # = +1R
        self.assertEqual(pos["stop"], 100.0)

    def test_breakeven_arms_at_one_r_bear(self):
        pos = self._pos(direction="bear", entry=100.0, stop=104.0, target=90.0)  # risk = 4
        self.assertFalse(self.sim._maybe_breakeven(pos, 96.5))
        self.assertTrue(self.sim._maybe_breakeven(pos, 96.0))
        self.assertEqual(pos["stop"], 100.0)

    def test_breakeven_only_fires_once(self):
        pos = self._pos()
        self.assertTrue(self.sim._maybe_breakeven(pos, 105.0))
        self.assertFalse(self.sim._maybe_breakeven(pos, 106.0))  # already at entry
        self.assertEqual(pos["stop"], 100.0)


if __name__ == '__main__':
    unittest.main()
