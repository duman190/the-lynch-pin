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
        # Mean PEG 3.0 should be capped to 1.5 in prompt
        data = [{
            'Ticker': 'TEST', 'PE': 30.0, 'FwdPE': 25.0, '2YFwd': 20.0,
            '5YGrowth': '20.0%', 'PEG': 1.5, 'Mean': 3.0, 'Dev_SD': -1.0,
            'Bull': '25.0%', 'Base': '18.0%', 'Bear': '10.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data)
        # target PEG = min(1.5, 3.0) = 1.5, implied PE = 1.5 * 20 = 30
        self.assertIn('target PEG 1.50', prompt)
        self.assertIn('30x PE', prompt)

    def test_build_prompt_target_peg_uses_mean_when_lower(self):
        from engine.ai_research import LynchPinResearcher
        # Mean PEG 1.0 < 1.5, so target PEG = 1.0
        data = [{
            'Ticker': 'TEST', 'PE': 20.0, 'FwdPE': 15.0, '2YFwd': 12.0,
            '5YGrowth': '20.0%', 'PEG': 0.8, 'Mean': 1.0, 'Dev_SD': -0.5,
            'Bull': '30.0%', 'Base': '20.0%', 'Bear': '12.0%'
        }]
        prompt = LynchPinResearcher.build_prompt(data)
        # target PEG = min(1.5, 1.0) = 1.0, implied PE = 1.0 * 20 = 20
        self.assertIn('target PEG 1.00', prompt)

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
        pattern = rf"\$?\bMSFT\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        match = re.search(pattern, bulk, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("Great company", match.group(1))

    def test_ticker_regex_without_colon(self):
        import re
        bulk = "$NVDA\n🤖: Monster growth.\n📊 Reverse DCF: AI dominance.\n\n$AMD\n🤖: Challenger."
        bulk = re.sub(r'SECTION \d+[^\n]*\n*', '', bulk)
        pattern = rf"\$?\bNVDA\b:?\s*\n?(.*?)(?=\n\$[A-Z]|\Z)"
        match = re.search(pattern, bulk, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(match)
        self.assertIn("Monster growth", match.group(1))

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


if __name__ == '__main__':
    unittest.main()
