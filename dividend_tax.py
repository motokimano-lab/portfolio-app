"""Income capacity reference amounts, not actual cash receipts.

Domestic fund distributions already reflect foreign tax inside the fund.
The adjustment model assumes ordinary distributions and full foreign credit.
"""
import math
import unicodedata

FOREIGN_WITHHOLDING_RATE = 0.10
JAPAN_INCOME_TAX_RATE = 0.15315
JAPAN_RESIDENT_TAX_RATE = 0.05
PROXY_ETF_TICKERS = {"VOO", "SCHD"}
TAX_FREE_ACCOUNTS = {"NISA", "旧NISA", "Jr.NISA"}


def normalize_account_type(value):
    text = unicodedata.normalize("NFKC", str(value)).strip()
    key = text.casefold().replace(" ", "")
    return {"nisa": "NISA", "旧nisa": "旧NISA", "jr.nisa": "Jr.NISA",
            "jrnisa": "Jr.NISA", "ジュニアnisa": "Jr.NISA"}.get(key, text)


def calc_after_tax_dividend(row):
    amount = float(row.get("annual_div_jpy", 0) or 0)
    if not math.isfinite(amount):
        return float("nan")
    exempt = normalize_account_type(row.get("account_type", "")) in TAX_FREE_ACCOUNTS
    domestic_rate = JAPAN_INCOME_TAX_RATE + JAPAN_RESIDENT_TAX_RATE
    if row.get("instrument_type") == "mutual_fund":
        if exempt:
            return amount
        if row.get("foreign_tax_adjustment", False):
            foreign_rate = float(row.get("underlying_foreign_tax_rate", 0))
            if not 0 <= foreign_rate < 1:
                raise ValueError("underlying_foreign_tax_rate must be in [0, 1)")
            gross = amount / (1 - foreign_rate)
            income_tax = max(0, gross * (JAPAN_INCOME_TAX_RATE - foreign_rate))
            resident_tax = gross * JAPAN_RESIDENT_TAX_RATE
            return amount - income_tax - resident_tax
        return amount * (1 - domestic_rate)
    # Compatibility until Positions is migrated from its ETF proxies.
    if str(row.get("ticker", "")).upper() in PROXY_ETF_TICKERS:
        return amount if exempt else amount * (1 - domestic_rate)
    if row.get("currency") == "USD":
        amount *= 1 - FOREIGN_WITHHOLDING_RATE
    return amount if exempt else amount * (1 - domestic_rate)
