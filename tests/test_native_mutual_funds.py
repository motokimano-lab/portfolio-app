import io
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from datetime import datetime
import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import functions
import mutual_funds as mf

ASOF=datetime(2026,10,12)
FIXTURES = {
 mf.FUND_SPECS['9I312249']['history']: "基準日,基準価額,再投資価額,純資産,分配金\n2024/09/18,9999,9999,0,\n2026/10/08,12993,13689,0,\n2026/10/09,13207,13914,0,\n".encode('cp932'),
 mf.FUND_SPECS['9I312249']['latest']: b"code,date,nav,change,assets\n100105,2026/10/09,13207,214,0\n",
 mf.FUND_SPECS['9I312249']['distributions']: b"code,period,date,distribution,nav\n100105,7,2026/08/25,95,14018\n100105,6,2026/05/25,90,13048\n100105,5,2026/02/25,90,12329\n100105,4,2025/11/25,85,10581\n100105,3,2025/08/25,80,10301\n",
 mf.FUND_SPECS['03311187']['history']: b"fund name\ndate,nav,reinvested,distribution,assets\n2018/07/03,10038,10038,,0\n2026/04/27,41935,41935,0,0\n2026/10/08,45394,45394,,0\n2026/10/09,45246,45246,,0\n",
 mf.FUND_SPECS['03311187']['latest']: json.dumps({'result':{'status':200,'retcount':1},'datasets':[{'association_fund_cd':'03311187','base_date':'20261009','nav':45246,'cmp_prev_day':-148}]}).encode(),
 mf.FUND_SPECS['89311199']['history']: b"date,nav,assets\n20190926,10000,0\n20261008,41464,0\n20261009,41344,0\n",
 mf.FUND_SPECS['89311199']['distributions']: b'<morningstarXML><fund code="2019092601"><year><month><day value="14" year="2026" month="09" price="39584" volume="2974010" return_value="1.00194902" divident="0"/><day value="08" year="2026" month="10" price="41464"/></month></year></fund></morningstarXML>',
}
def response(url,**kwargs):
    r=requests.Response();r.status_code=200;r._content=FIXTURES[url];r.encoding='utf-8';return r

def rows():
    return pd.DataFrame([
      dict(ticker='ABBV',currency='USD',quantity=10,cost_price=80,price=200,sector='株'),
      dict(ticker='VOO',currency='USD',quantity=3,cost_price=500,price=700,sector='株'),
      dict(ticker='BTC-JPY',currency='JPY',quantity=.25,cost_price=8000000,price=10000000,sector='crypto'),
      dict(ticker='CASH',currency='JPY',quantity=123456,cost_price=1,price=1,sector='cash'),
    ])
def native(code='9I312249'):
    return pd.DataFrame([dict(instrument_type='mutual_fund',fund_code=code,
       ticker='MF:'+code,currency='JPY',quantity=100000,cost_price=10000,
       price=13207,price_unit=10000,sector='投信',display_name='SCHD')])

class Tests(unittest.TestCase):
    @patch('mutual_funds.requests.get',side_effect=response)
    def test_provider_formats(self,get):
        for code,expected in [('9I312249',(13207,360,380)),('03311187',(45246,0,0)),('89311199',(41344,0,0))]:
            r=mf.get_mutual_fund_data(code,ASOF)
            self.assertTrue(r['success'],r['error'])
            self.assertEqual((r['price'],r['ttm_distribution'],r['forecast_distribution']),expected)
            self.assertEqual(r['base_date'],'2026-10-09')
            self.assertTrue(r['stale']);self.assertTrue(r['warnings'])
    def test_unit_legacy_values_unchanged(self):
        source=rows();result=functions.prepare_base_dataframe(source,150,.006)
        expected=source['price']*source['quantity']*pd.Series([150,150,1,1])
        pd.testing.assert_series_equal(result['value_jpy'],expected,check_names=False)
        self.assertTrue((result['price_unit']==1).all())
        self.assertNotIn('instrument_type',source)
    def test_native_units_and_cost(self):
        r=functions.prepare_base_dataframe(native(),150,.006).iloc[0]
        self.assertEqual(r.value_jpy,132070)
        self.assertEqual(r.cost_basis_jpy,100000)
        self.assertEqual(r.fx_to_jpy,1)
    def test_blank_instrument_keeps_legacy(self):
        r=rows();r['instrument_type']='';r['price_unit']=10000
        self.assertTrue((functions.normalize_positions(r)['price_unit']==1).all())
    def test_schema_leading_zero_and_names(self):
        d=pd.read_csv(io.StringIO('ticker,currency,quantity,cost_price,sector,instrument_type,fund_code\nx,JPY,100,10000,株,mutual_fund,03311187\n'),dtype={'fund_code':'string'})
        r=functions.normalize_positions(d).iloc[0]
        self.assertEqual(r.ticker,'MF:03311187');self.assertEqual(r.display_name,'SP500')
        self.assertEqual(r.price_unit,10000)
    def test_reject_wrong_native_unit_and_currency(self):
        for field,value in [('price_unit',1),('currency','USD')]:
            d=native();d[field]=value
            with self.assertRaises(ValueError):functions.normalize_positions(d)
    @patch('mutual_funds.requests.get',side_effect=response)
    @patch('functions.yf.download',side_effect=AssertionError('MF sent to Yahoo'))
    def test_mf_only_no_etf_or_yahoo(self,yahoo,get):
        r,status=functions.get_position_assets_data(native(),True,ASOF)
        self.assertFalse(r[3]);self.assertEqual(r[4]['MF:9I312249'],360)
        self.assertEqual(r[5]['MF:9I312249'],380)
        self.assertEqual(100000/10000*r[4]['MF:9I312249'],3600)
        yahoo.assert_not_called()
    @patch('mutual_funds.requests.get',side_effect=requests.Timeout())
    @patch('functions.yf.download',side_effect=AssertionError('ETF fallback'))
    def test_failure_explicit_no_etf_fallback(self,yahoo,get):
        r,status=functions.get_position_assets_data(native(),True,ASOF)
        self.assertEqual(r[3],['MF:9I312249']);self.assertTrue(math.isnan(r[0]['MF:9I312249']))
        self.assertFalse(status['MF:9I312249']['success']);yahoo.assert_not_called()
    def test_future_distribution_mufg(self):
        url=mf.FUND_SPECS['03311187']['history'];old=FIXTURES[url]
        text=old.decode('cp932').replace('2026/04/27,41935,41935,0,','2026/04/27,41935,41935,12,')
        FIXTURES[url]=text.encode('cp932')
        try:
            with patch('mutual_funds.requests.get',side_effect=response):r=mf.get_mutual_fund_data('03311187',ASOF)
            self.assertEqual((r['ttm_distribution'],r['forecast_distribution']),(12,12))
        finally:FIXTURES[url]=old
    def test_future_distribution_sbi(self):
        url=mf.FUND_SPECS['89311199']['distributions'];old=FIXTURES[url]
        FIXTURES[url]=old.replace(b'year="2026" month="09" price="39584" volume="2974010" return_value="1.00194902" divident="0"',b'year="2026" month="09" price="39584" volume="2974010" return_value="1.00194902" divident="8"')
        try:
            self.assertNotEqual(old,FIXTURES[url])
            with patch('mutual_funds.requests.get',side_effect=response):r=mf.get_mutual_fund_data('89311199',ASOF)
            self.assertEqual((r['ttm_distribution'],r['forecast_distribution']),(8,8))
        finally:FIXTURES[url]=old
    def test_latest_zero_not_previous_positive(self):
        url=mf.FUND_SPECS['9I312249']['distributions'];old=FIXTURES[url]
        FIXTURES[url]=old.replace(b'2026/08/25,95,',b'2026/08/25,0,')
        try:
            with patch('mutual_funds.requests.get',side_effect=response):r=mf.get_mutual_fund_data('9I312249',ASOF)
            self.assertEqual(r['forecast_distribution'],0);self.assertEqual(r['ttm_distribution'],265)
        finally:FIXTURES[url]=old
    def test_ttm_cutoff(self):
        url=mf.FUND_SPECS['9I312249']['distributions'];old=FIXTURES[url]
        FIXTURES[url]=old+b'100105,0,2025/10/12,50,10000\r\n'
        try:
            with patch('mutual_funds.requests.get',side_effect=response):r=mf.get_mutual_fund_data('9I312249',ASOF)
            self.assertEqual(r['ttm_distribution'],360)
        finally:FIXTURES[url]=old
    def test_disagreement_failure(self):
        url=mf.FUND_SPECS['03311187']['latest'];old=FIXTURES[url]
        data=json.loads(old);data['datasets'][0]['nav']=1;FIXTURES[url]=json.dumps(data).encode()
        try:
            with patch('mutual_funds.requests.get',side_effect=response):r=mf.get_mutual_fund_data('03311187',ASOF)
            self.assertFalse(r['success'])
        finally:FIXTURES[url]=old

if __name__=='__main__':unittest.main(verbosity=2)
