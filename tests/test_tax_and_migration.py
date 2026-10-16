import unittest
from unittest.mock import patch
from datetime import date

import pandas as pd

from dividend_tax import calc_after_tax_dividend, normalize_account_type
from functions import (
    normalize_positions, compare_portfolio, compare_latest_logs,
    save_daily_log_detail, save_daily_log_v2, DAILY_LOG_V2_HEADERS,
    analyze_attribution_period, summarize_attribution, calculate_estimated_twr
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

    def test_cash_cost_basis_is_not_balance_squared(self):
        frame = pd.DataFrame([dict(
            ticker='CASH', currency='USD', quantity=5000, cost_price=5000,
            price=1, price_unit=1, sector='CASH'
        )])
        result = normalize_positions(frame)
        result['price'] = 1
        from functions import prepare_base_dataframe
        prepared = prepare_base_dataframe(result, 150, 0.006)
        self.assertEqual(prepared.iloc[0].value_jpy, 750000)
        self.assertEqual(prepared.iloc[0].cost_basis_jpy, 750000)

    def test_attribution_exactly_splits_market_fx_and_flow(self):
        columns=DAILY_LOG_V2_HEADERS
        rows=[
            ['2026-10-15','x','元気','特定','AAA','AAA','米国株','Tech','USD','','','',1,10,100,150,150000,100000,'成長','',0,'',''],
            ['2026-10-15','x','元気','VCB','CASH','USD','現金・債券','CASH','USD','','','',1,1000,1,150,150000,150000,'現金','',0,'',''],
            ['2026-10-16','x','元気','特定','AAA','AAA','米国株','Tech','USD','','','',1,12,110,155,204600,120000,'成長','',10,'',''],
            ['2026-10-16','x','元気','VCB','CASH','USD','現金・債券','CASH','USD','','','',1,1200,1,155,186000,186000,'現金','',0,'',''],
        ]
        detail,daily,summary=analyze_attribution_period(pd.DataFrame(rows,columns=columns))
        self.assertAlmostEqual(summary['total_change'],90600)
        self.assertAlmostEqual(
            summary['flow_effect']+summary['market_effect']+summary['fx_effect'],
            summary['total_change'],
            places=6,
        )
        cash=detail.loc[detail.ticker.eq('CASH')].iloc[0]
        self.assertAlmostEqual(cash.market_effect,0)
        self.assertGreater(cash.flow_effect,0)
        self.assertGreater(cash.fx_effect,0)
        self.assertEqual(cash.market_group,'現金')
        self.assertEqual(cash.fx_driver_currency,'USD')
        stock=detail.loc[detail.ticker.eq('AAA')].iloc[0]
        self.assertEqual(stock.market_group,'Tech')
        self.assertEqual(stock.fx_driver_currency,'USD')
        self.assertGreater(stock.flow_effect,0)
        self.assertGreater(stock.market_effect,0)
        self.assertGreater(stock.fx_effect,0)
        self.assertEqual(len(daily),1)

    def test_new_position_is_flow_not_fake_market_gain(self):
        columns=DAILY_LOG_V2_HEADERS
        rows=[
            ['2026-10-15','x','元気','楽天銀行','CASH','JPY','現金・債券','CASH','JPY','','','',1,1000000,1,1,1000000,1000000,'現金','',0,'',''],
            ['2026-10-16','x','元気','楽天銀行','CASH','JPY','現金・債券','CASH','JPY','','','',1,500000,1,1,500000,500000,'現金','',0,'',''],
            ['2026-10-16','x','元気','特定','NEW','NEW','日本株','機械','JPY','','','',1,100,5000,1,500000,500000,'成長','',0,'',''],
        ]
        detail,_,summary=analyze_attribution_period(pd.DataFrame(rows,columns=columns))
        new=detail.loc[detail.ticker.eq('NEW')].iloc[0]
        self.assertAlmostEqual(new.flow_effect,500000)
        self.assertAlmostEqual(new.market_effect,0)
        self.assertAlmostEqual(new.fx_effect,0)
        self.assertAlmostEqual(summary['total_change'],0,places=6)

    def test_us_fund_attribution_separates_proxy_fx_and_residual(self):
        columns=DAILY_LOG_V2_HEADERS
        nav0=10000.0
        nav1=nav0 * 1.02 * 1.01 * 0.999
        rows=[
            ['2026-10-15','x','元気','NISA','MF:03311187','SP500','米国株','',
             'JPY','mutual_fund','03311187','eMAXIS Slim',10000,10000,nav0,1,
             nav0,nav0,'成長','',0,'2026-10-14','VOO'],
            ['2026-10-16','x','元気','NISA','MF:03311187','SP500','米国株','',
             'JPY','mutual_fund','03311187','eMAXIS Slim',10000,10000,nav1,1,
             nav1,nav1,'成長','',0,'2026-10-15','VOO'],
        ]
        references={
            ('VOO','2026-10-14'):{'proxy_price':100.0,'usd_jpy':150.0},
            ('VOO','2026-10-15'):{'proxy_price':102.0,'usd_jpy':151.5},
        }
        detail,_,summary=analyze_attribution_period(
            pd.DataFrame(rows,columns=columns),
            fund_reference=references,
        )
        item=detail.iloc[0]
        self.assertTrue(item.fund_fx_estimated)
        self.assertEqual(item.attribution_method,'fund_proxy_estimate')
        self.assertEqual(item.market_group,'米国株投信')
        self.assertEqual(item.fx_driver_currency,'USD')
        self.assertGreater(item.market_effect,0)
        self.assertGreater(item.fx_effect,0)
        self.assertLess(item.residual_effect,0)
        self.assertAlmostEqual(
            item.flow_effect+item.market_effect+item.fx_effect+item.residual_effect,
            item.total_change,
            places=8,
        )
        self.assertAlmostEqual(
            summary['flow_effect']+summary['market_effect']+
            summary['fx_effect']+summary['residual_effect'],
            summary['total_change'],
            places=8,
        )
        self.assertTrue(summary['fund_fx_estimated'])
        self.assertAlmostEqual(
            summary['investment_effect'],
            summary['market_effect']+summary['fx_effect']+summary['residual_effect'],
            places=8,
        )

    def test_us_fund_without_reference_falls_back_safely(self):
        columns=DAILY_LOG_V2_HEADERS
        rows=[
            ['2026-10-15','x','元気','NISA','MF:03311187','SP500','米国株','',
             'JPY','mutual_fund','03311187','eMAXIS Slim',10000,10000,10000,1,
             10000,10000,'成長','',0,'',''],
            ['2026-10-16','x','元気','NISA','MF:03311187','SP500','米国株','',
             'JPY','mutual_fund','03311187','eMAXIS Slim',10000,10000,10100,1,
             10100,10100,'成長','',0,'',''],
        ]
        detail,_,summary=analyze_attribution_period(pd.DataFrame(rows,columns=columns))
        item=detail.iloc[0]
        self.assertFalse(item.fund_fx_estimated)
        self.assertAlmostEqual(item.fx_effect,0)
        self.assertAlmostEqual(item.residual_effect,0)
        self.assertAlmostEqual(item.market_effect,item.total_change)
        self.assertFalse(summary['fund_fx_estimated'])

    def test_period_attribution_sums_daily_and_groups_sector(self):
        columns=DAILY_LOG_V2_HEADERS
        def row(day,ticker,name,sector,q,p,fx,value):
            return [day,'x','元気','特定',ticker,name,'米国株',sector,'USD','','','',1,q,p,fx,value,0,'成長','',0,'','']
        frame=pd.DataFrame([
            row('2026-10-15','AAA','AAA','Tech',10,100,150,150000),
            row('2026-10-15','BBB','BBB','Bank',10,100,150,150000),
            row('2026-10-16','AAA','AAA','Tech',10,105,150,157500),
            row('2026-10-16','BBB','BBB','Bank',10,98,150,147000),
            row('2026-10-17','AAA','AAA','Tech',10,110,152,167200),
            row('2026-10-17','BBB','BBB','Bank',10,99,152,150480),
        ],columns=columns)
        detail,daily,summary=analyze_attribution_period(frame)
        self.assertEqual(len(daily),2)
        self.assertEqual(summary['actual_start'].isoformat(),'2026-10-15')
        self.assertEqual(summary['actual_end'].isoformat(),'2026-10-17')
        grouped=summarize_attribution(detail,'sector')
        self.assertEqual(set(grouped.sector),{'Tech','Bank'})
        self.assertAlmostEqual(
            grouped.total_change.sum(),
            summary['total_change'],
            places=6,
        )

    def test_estimated_twr_chain_links_daily_returns(self):
        detail = pd.DataFrame([
            dict(
                start_date=date(2026,10,15), end_date=date(2026,10,16),
                value_start=100.0, value_end=110.0, total_change=10.0,
                flow_effect=0.0, investment_effect=10.0,
            ),
            dict(
                start_date=date(2026,10,16), end_date=date(2026,10,17),
                value_start=110.0, value_end=115.5, total_change=5.5,
                flow_effect=0.0, investment_effect=5.5,
            ),
        ])
        daily, summary = calculate_estimated_twr(detail)
        self.assertTrue(summary['valid'])
        self.assertAlmostEqual(daily.iloc[0].period_return, 0.10)
        self.assertAlmostEqual(daily.iloc[1].period_return, 0.05)
        self.assertAlmostEqual(summary['twr'], 0.155)
        self.assertAlmostEqual(summary['investment_effect'], 15.5)
        self.assertEqual(summary['periods'], 2)

    def test_estimated_twr_uses_midpoint_flow_adjustment(self):
        detail = pd.DataFrame([
            dict(
                start_date=date(2026,10,15), end_date=date(2026,10,16),
                value_start=100.0, value_end=215.0, total_change=115.0,
                flow_effect=100.0, investment_effect=15.0,
            ),
        ])
        daily, summary = calculate_estimated_twr(detail)
        self.assertTrue(summary['valid'])
        self.assertAlmostEqual(daily.iloc[0].capital_base, 150.0)
        self.assertAlmostEqual(daily.iloc[0].period_return, 0.10)
        self.assertAlmostEqual(summary['twr'], 0.10)
        self.assertAlmostEqual(summary['simple_asset_change'], 1.15)

    def test_estimated_twr_rejects_nonpositive_capital_base(self):
        detail = pd.DataFrame([
            dict(
                start_date=date(2026,10,15), end_date=date(2026,10,16),
                value_start=10.0, value_end=0.0, total_change=-10.0,
                flow_effect=-30.0, investment_effect=20.0,
            ),
        ])
        daily, summary = calculate_estimated_twr(detail)
        self.assertFalse(summary['valid'])
        self.assertEqual(summary['invalid_periods'], 1)
        self.assertTrue(pd.isna(daily.iloc[0].period_return))

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
