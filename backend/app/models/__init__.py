"""
Database models for the IBKR Portfolio Analyzer.
All models are imported here to ensure they're discovered by Alembic for migrations.
"""

from app.models.security import Security
from app.models.taxlot import TaxLot
from app.models.exchange_rate import ExchangeRate
from app.models.market_price import MarketPrice
from app.models.analyst_rating import AnalystRating
from app.models.benchmark_price import BenchmarkPrice
from app.models.fundamental_metrics import FundamentalMetrics
from app.models.earnings_event import EarningsEvent
from app.models.watchlist_item import WatchlistItem
from app.models.dividend_payment import DividendPayment
from app.models.app_settings import AppSetting
from app.models.trade import Trade
from app.models.corporate_action import CorporateAction
from app.models.cash_flow import CashFlow
from app.models.sync_run import SyncRun
# These two were missing, so `import app.models` left Base.metadata incomplete — Alembic
# discovery and any create_all() silently skipped them.
from app.models.ticker_mapping import TickerMapping
from app.models.benchmark_timeline_cache import BenchmarkTimelineCache
from app.models.etf_basket import EtfBasket, EtfHolding
from app.models.isin_identity import IsinIdentity

__all__ = [
    "Security",
    "TaxLot",
    "ExchangeRate",
    "MarketPrice",
    "AnalystRating",
    "BenchmarkPrice",
    "FundamentalMetrics",
    "EarningsEvent",
    "WatchlistItem",
    "DividendPayment",
    "AppSetting",
    "Trade",
    "CorporateAction",
    "CashFlow",
    "SyncRun",
    "TickerMapping",
    "BenchmarkTimelineCache",
    "EtfBasket",
    "EtfHolding",
    "IsinIdentity",
]
