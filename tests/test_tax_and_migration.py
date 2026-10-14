import unittest
from unittest.mock import patch
from datetime import date

import pandas as pd

from dividend_tax import calc_after_tax_dividend, normalize_account_type
from functions import (
    normalize_positions, compare_portfolio, compare_latest_logs,
    save_daily_log_detail, save_daily_log_v2, DAILY_LOG_V2_HEADERS
)


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

    def test_writer_replaces_only_today_without_clearing_history(self):
        # Local double only: no real client and no Google Sheets mutation.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')
        header=['date','ticker','display_name','asset_class','sector','value_jpy']
        old=['2026-01-01','VOO','SP500','米国株','投信','100']
        current=[today,'VOO','SP500','米国株','投信','110']

        class Sheet:
            def __init__(self):
                self.rows=[header,old,current]
                self.cleared=False
            def col_values(self,column):
                return [row[0] for row in self.rows if row]
            def append_rows(self,rows,value_input_option=None):
                self.rows.extend(rows)
            def delete_rows(self,start,end=None):
                end=start if end is None else end
                del self.rows[start-1:end]
            def clear(self):
                self.cleared=True
                raise AssertionError('historical sheet must never be cleared')

        sheet=Sheet()
        class Client:
            def open(self,name):return self
            def worksheet(self,name):return sheet

        frame=pd.DataFrame([dict(ticker='MF:03311187',display_name='SP500',
                                 asset_class='米国株',sector='投信',value_jpy=120)])
        with patch('functions.ServiceAccountCredentials.from_json_keyfile_dict',return_value=object()), \
             patch('functions.gspread.authorize',return_value=Client()):
            result=save_daily_log_detail(frame,{})

        self.assertFalse(sheet.cleared)
        self.assertEqual(sheet.rows[0],header)
        self.assertEqual(sheet.rows[1],old)
        self.assertEqual(sheet.rows[2],[today,'MF:03311187','SP500','米国株','投信',120.0])
        self.assertEqual(len(sheet.rows),3)
        self.assertIn('replaced 1 rows',result)

    def test_writer_append_failure_preserves_existing_today(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')
        header=['date','ticker','display_name','asset_class','sector','value_jpy']
        current=[today,'VOO','SP500','米国株','投信','110']

        class Sheet:
            def __init__(self):self.rows=[header,current]
            def col_values(self,column):return [row[0] for row in self.rows]
            def append_rows(self,rows,value_input_option=None):raise RuntimeError('network')
            def delete_rows(self,start,end=None):raise AssertionError('must not delete after failed append')

        sheet=Sheet()
        class Client:
            def open(self,name):return self
            def worksheet(self,name):return sheet

        frame=pd.DataFrame([dict(ticker='VOO',display_name='SP500',
                                 asset_class='米国株',sector='投信',value_jpy=120)])
        with patch('functions.ServiceAccountCredentials.from_json_keyfile_dict',return_value=object()), \
             patch('functions.gspread.authorize',return_value=Client()):
            with self.assertRaises(RuntimeError):
                save_daily_log_detail(frame,{})
        self.assertEqual(sheet.rows,[header,current])

    def test_v2_snapshot_keeps_legacy_comparison_separate(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')

        class Sheet:
            def __init__(self):self.rows=[DAILY_LOG_V2_HEADERS]
            def col_values(self,column):return [row[0] for row in self.rows]
            def append_rows(self,rows,value_input_option=None):self.rows.extend(rows)
            def delete_rows(self,start,end=None):
                end=start if end is None else end
                del self.rows[start-1:end]

        sheet=Sheet()
        class Client:
            def open(self,name):return self
            def worksheet(self,name):
                self.requested=name
                return sheet

        frame=pd.DataFrame([dict(
            owner='元気',account_type='NISA',ticker='ABBV',display_name='ABBV',
            asset_class='米国株',sector='医薬',currency='USD',instrument_type='',
            fund_code='',product_name='',price_unit=1,quantity=10,price=200,
            fx_to_jpy=150,value_jpy=300000,cost_basis_jpy=120000,
            purpose='配当',role='core',day_diff_pct=1.25
        )])
        client=Client()
        with patch('functions.ServiceAccountCredentials.from_json_keyfile_dict',return_value=object()), \
             patch('functions.gspread.authorize',return_value=client):
            result=save_daily_log_v2(frame,{})

        self.assertEqual(client.requested,'Daily_Log_V2')
        self.assertEqual(len(sheet.rows),2)
        self.assertEqual(len(sheet.rows[1]),len(DAILY_LOG_V2_HEADERS))
        self.assertEqual(sheet.rows[1][0],today)
        self.assertEqual(sheet.rows[1][2:6],['元気','NISA','ABBV','ABBV'])
        self.assertEqual(sheet.rows[1][15],150.0)
        self.assertIn('Daily_Log_V2',result)


if __name__ == '__main__':
    unittest.main()
