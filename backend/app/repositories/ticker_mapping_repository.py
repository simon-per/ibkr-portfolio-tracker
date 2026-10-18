"""
Ticker Mapping Repository
Manages custom ticker mappings for Yahoo Finance.
"""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticker_mapping import TickerMapping


class TickerMappingRepository:
    """Repository for ticker mapping operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_mapping(
        self,
        ibkr_symbol: str,
        ibkr_exchange: str
    ) -> Optional[TickerMapping]:
        """
        Get Yahoo ticker mapping for an IBKR security.

        Args:
            ibkr_symbol: IBKR symbol (e.g., "XNAS")
            ibkr_exchange: IBKR exchange (e.g., "IBIS2")

        Returns:
            TickerMapping if found, None otherwise
        """
        result = await self.session.execute(
            select(TickerMapping)
            .where(
                TickerMapping.ibkr_symbol == ibkr_symbol,
                TickerMapping.ibkr_exchange == ibkr_exchange,
                TickerMapping.is_active == True
            )
        )
        return result.scalar_one_or_none()

    async def create_mapping(
        self,
        ibkr_symbol: str,
        ibkr_exchange: str,
        yahoo_ticker: str,
        source: str = "manual",
        notes: str = None
    ) -> TickerMapping:
        """Create a new ticker mapping"""
        mapping = TickerMapping(
            ibkr_symbol=ibkr_symbol,
            ibkr_exchange=ibkr_exchange,
            yahoo_ticker=yahoo_ticker,
            source=source,
            notes=notes
        )
        self.session.add(mapping)
        await self.session.flush()
        await self.session.refresh(mapping)
        return mapping

    async def upsert_mapping(
        self,
        ibkr_symbol: str,
        ibkr_exchange: str,
        yahoo_ticker: str,
        source: str = "manual",
        notes: str = None
    ) -> TickerMapping:
        """Create or update a ticker mapping"""
        existing = await self.get_mapping(ibkr_symbol, ibkr_exchange)

        if existing:
            existing.yahoo_ticker = yahoo_ticker
            existing.source = source
            if notes:
                existing.notes = notes
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            return await self.create_mapping(
                ibkr_symbol, ibkr_exchange, yahoo_ticker, source, notes
            )

    async def initialize_default_mappings(self) -> int:
        """
        Initialize built-in ticker mappings for common securities.
        Returns the number of mappings created.
        """
        # Common German ETFs and stocks with different Yahoo Finance tickers
        default_mappings = [
            # German ETFs - XETRA/IBIS listings
            ("XNAS", "IBIS2", "XNAS.DE", "Xtrackers Nasdaq 100 UCITS ETF"),
            ("XAIX", "XETRA", "XAIX.DE", "Xtrackers MSCI World UCITS ETF"),
            ("XAIX", "IBIS2", "XAIX.DE", "Xtrackers MSCI World UCITS ETF"),
            ("DBPG", "IBIS2", "DBPG.DE", "Xtrackers S&P 500 2x Leveraged Daily Swap UCITS ETF"),
            ("VUSA", "XETRA", "VUSA.DE", "Vanguard S&P 500 UCITS ETF"),
            ("EUNL", "XETRA", "EUNL.DE", "iShares Core MSCI World UCITS ETF"),
            ("CSPX", "XETRA", "CSPX.DE", "iShares Core S&P 500 UCITS ETF"),
            ("IWDA", "XETRA", "IWDA.DE", "iShares Core MSCI World UCITS ETF"),

            # UK ETFs - London Stock Exchange
            ("VUSA", "LSE", "VUSA.L", "Vanguard S&P 500 UCITS ETF"),
            ("VWRL", "LSE", "VWRL.L", "Vanguard FTSE All-World UCITS ETF"),
            ("CSPX", "LSE", "CSPX.L", "iShares Core S&P 500 UCITS ETF"),

            # Amsterdam (AEB) listings
            ("VWRL", "AEB", "VWRL.AS", "Vanguard FTSE All-World UCITS ETF"),
        ]

        created_count = 0
        for ibkr_symbol, ibkr_exchange, yahoo_ticker, notes in default_mappings:
            existing = await self.get_mapping(ibkr_symbol, ibkr_exchange)
            if not existing:
                await self.create_mapping(
                    ibkr_symbol=ibkr_symbol,
                    ibkr_exchange=ibkr_exchange,
                    yahoo_ticker=yahoo_ticker,
                    source="builtin",
                    notes=notes
                )
                created_count += 1

        if created_count > 0:
            await self.session.flush()

        return created_count
