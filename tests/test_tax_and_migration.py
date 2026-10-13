import unittest
from unittest.mock import patch
from datetime import date

import pandas as pd

from dividend_tax import calc_after_tax_dividend, normalize_account_type
from functions import normalize_positions, compare_portfolio, compare_latest_logs, save_daily_log_detail


class TaxAndMigrationTests(unittest.TestCase):
    def test_schd_taxable_standard_model(self):
        row = normalize_positions(pd.DataFrame([dict(
            ticker='x', currency='JPY', instrument_type='mutual_fund',
            fund_code='9I312249', account_type='特定', annual_div_jpy=900
        )])).iloc[0]
        self.assertAlmostEqual(calc_after_tax_dividend(row), 796.85)

    def test_fund_tax_free_accounts(self):
        for account in ['NISA', '旧NISA', 'Jr.NISA', 'ジュニアNISA', ' ＮＩＳＡ ']:
            row = dict(instrument_type='mutual_fund', account_type=account,
                       annual_div_jpy=360, foreign_tax_adjustment=True,
                       underlying_foreign_tax_rate=.10)
            self.assertEqual(calc_after_tax_dividend(row), 360)

    def test_legacy_and_direct_foreign_equities(self):
        for ticker, currency, account, expected in [
            ('VOO', 'USD', '特定', 796.85), ('SCHD', 'USD', 'NISA', 1000),
            ('ABBV', 'USD', '特定', 717.165), ('ABBV', 'USD', '旧NISA', 900),
            ('1928.T', 'JPY', 'Jr.NISA', 1000), ('1928.T', 'JPY', '特定', 796.85),
        ]:
            self.assertAlmostEqual(calc_after_tax_dividend(dict(
                ticker=ticker, currency=currency, account_type=account, annual_div_jpy=1000)), expected)

    def test_unknown_fund_no_guessed_adjustment(self):
        row = normalize_positions(pd.DataFrame([dict(
            ticker='MF:unknown', currency='JPY', instrument_type='mutual_fund',
            fund_code='unknown', account_type='特定', annual_div_jpy=1000
        )])).iloc[0]
        self.assertFalse(row.foreign_tax_adjustment)
        self.assertAlmostEqual(calc_after_tax_dividend(row), 796.85)

    def test_accounts_stay_distinct(self):
        self.assertEqual([normalize_account_type(a) for a in ['NISA', '旧NISA', 'Jr.NISA']],
                         ['NISA', '旧NISA', 'Jr.NISA'])

    def logs(self):
        return pd.DataFrame([
            ['2026-10-09','VOO','SP500','米国株','投信',100],
            ['2026-10-09','ABBV','ABBV','米国株','医薬',100],
            ['2026-10-12','MF:03311187','SP500','米国株','投信',120],
            ['2026-10-12','ABBV','ABBV','米国株','医薬',105],
            ['2026-10-13','MF:03311187','SP500','米国株','投信',125],
            ['2026-10-13','ABBV','ABBV','米国株','医薬',110],
        ], columns=['date','ticker','display_name','asset_class','sector','value_jpy'])

    def test_migration_continuity_and_rankings(self):
        log=self.logs()
        merged, summary=compare_portfolio(log,date(2026,10,9),date(2026,10,13))
        self.assertEqual(summary['migration_names'], ['SP500'])
        self.assertEqual(len(merged),2)
        self.assertEqual((summary['total_start'],summary['total_end'],summary['total_diff']),(200,235,35))
        self.assertTrue(pd.isna(merged.loc[merged.display_name.eq('SP500'),'growth_pct'].iloc[0]))
        for key in ['top_gainers','top_losers','top_diff_up','top_diff_down']:
            self.assertNotIn('SP500',summary[key].display_name.tolist())
        latest=compare_latest_logs(log.loc[log.date.le('2026-10-12')])
        self.assertEqual(latest['migration_names'],['SP500'])
        self.assertEqual(latest['top_gainers'].display_name.tolist(),['ABBV'])

    def test_after_migration_returns_normal(self):
        merged,summary=compare_portfolio(self.logs(),date(2026,10,12),date(2026,10,13))
        self.assertEqual(summary['migration_names'],[])
        self.assertFalse(merged['valuation_method_change'].any())
        self.assertAlmostEqual(merged.loc[merged.display_name.eq('SP500'),'growth_pct'].iloc[0],4.17)
        self.assertEqual(compare_latest_logs(self.logs())['migration_names'],[])

    def test_partial_migration_is_detected(self):
        log=self.logs()
        log.loc[len(log)] = ['2026-10-09','MF:89311199','SP500','米国株','投信',50]
        log.loc[len(log)] = ['2026-10-12','MF:89311199','SP500','米国株','投信',55]
        _,summary=compare_portfolio(log,date(2026,10,9),date(2026,10,12))
        self.assertEqual(summary['migration_names'],['SP500'])

    def test_market_movers_ignore_cash_and_trade_size(self):
        frame = pd.DataFrame([
            dict(ticker='CASH', display_name='USD', day_diff_pct=0, value_jpy=2000000),
            dict(ticker='AAA', display_name='AAA', day_diff_pct=2.0, value_jpy=100000),
            dict(ticker='AAA', display_name='AAA', day_diff_pct=2.0, value_jpy=900000),
            dict(ticker='BBB', display_name='BBB', day_diff_pct=-1.5, value_jpy=500000),
            dict(ticker='MF:03311187', display_name='SP500', day_diff_pct=-0.4, value_jpy=3000000),
            dict(ticker='MF:89311199', display_name='SP500', day_diff_pct=-0.2, value_jpy=1000000),
        ])
        from functions import build_current_market_movers
        gainers, losers = build_current_market_movers(frame)
        self.assertNotIn('USD', gainers.display_name.tolist() + losers.display_name.tolist())
        self.assertEqual(gainers.iloc[0].display_name, 'AAA')
        self.assertAlmostEqual(gainers.iloc[0].market_pct, 2.0)
        sp = gainers.loc[gainers.display_name.eq('SP500')].iloc[0].market_pct
        self.assertAlmostEqual(sp, -0.35)
        self.assertEqual(losers.iloc[0].display_name, 'BBB')

    def test_writer_six_columns_and_today_replacement(self):
        # Local double only: no real client and no Google Sheets mutation.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')
        header=['date','ticker','display_name','asset_class','sector','value_jpy']
        old=['2026-01-01','VOO','SP500','米国株','投信','100']
        class Sheet:
            cleared=False
            saved=None
            def get_all_values(self):return [header,old,[today,'VOO','SP500','米国株','投信','110']]
            def clear(self):self.cleared=True
            def update(self,values):self.saved=values
        sheet=Sheet()
        class Client:
            def open(self,name):return self
            def worksheet(self,name):return sheet
        frame=pd.DataFrame([dict(ticker='MF:03311187',display_name='SP500',
                                 asset_class='米国株',sector='投信',value_jpy=120)])
        with patch('functions.ServiceAccountCredentials.from_json_keyfile_dict',return_value=object()), \
             patch('functions.gspread.authorize',return_value=Client()):
            save_daily_log_detail(frame,{})
        self.assertTrue(sheet.cleared)
        self.assertEqual(sheet.saved[:2],[header,old])
        self.assertEqual(sheet.saved[2],[today,'MF:03311187','SP500','米国株','投信',120.0])
        self.assertEqual(len(sheet.saved),3)
        self.assertTrue(all(len(row)==6 for row in sheet.saved))


if __name__ == '__main__':
    unittest.main()
