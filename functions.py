import pandas as pd
import yfinance as yf
import gspread
import streamlit as st
import json
import os

from datetime import datetime
from zoneinfo import ZoneInfo
from oauth2client.service_account import ServiceAccountCredentials


def get_fx(symbol, default):
    try:
        fx = yf.Ticker(symbol)
        return fx.history(period="1d")["Close"].iloc[-1]
    except:
        return default


@st.cache_data(ttl=3600)
def get_price(ticker, cost_price, fallback_price=None):
    if ticker == "CASH":
        return 1, False

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")

        if not hist.empty:
            return hist["Close"].dropna().iloc[-1], False
    except:
        pass

    if fallback_price is not None:
        return fallback_price, True

    return cost_price, True
