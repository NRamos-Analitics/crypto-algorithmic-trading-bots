"""
Data Ingestion Module for Kraken Futures API.
Handles market mapping, time-series OHLCV fetching, and funding rate extraction.
"""
import logging
import pandas as pd

logger = logging.getLogger("sniper.ingestion")

TF_MS = 3_600_000
TEMPORALIDAD = "1h"
LIMITE_VELAS = 300

MONEDAS_SNIPER = [
    "BTC", "ETH", "SOL", "BNB", "AVAX",
    "ADA", "LINK", "XRP", "POL", "ARB",
    "OP", "DOGE", "BCH", "LTC", "UNI",
    "INJ", "TIA", "APT", "FET"
]

def ultimo_cierre_ts(ahora_s: float) -> int:
    ahora_ms = int(ahora_s * 1000)
    return (ahora_ms // TF_MS - 1) * TF_MS

def velas_cerradas(ohlcv: list, ahora_s: float) -> pd.DataFrame:
    cols = ["timestamp", "open", "high", "low", "close", "volume"]
    if not ohlcv:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(ohlcv, columns=cols)
    corte = ultimo_cierre_ts(ahora_s)
    return df[df["timestamp"] <= corte].reset_index(drop=True)

def mapear_mercados(exchange, monedas: list) -> list:
    try:
        mercados = exchange.load_markets()
    except Exception as e:
        logger.error("Error loading Kraken markets: %s", e)
        return []

    seleccionados = []
    for m in monedas:
        encontrado = None
        for sym, info in mercados.items():
            if (
                info.get("base") == m
                and info.get("quote") == "USD"
                and info.get("type") == "swap"
                and info.get("linear") is True
                and info.get("active") is True
            ):
                encontrado = sym
                break
        if encontrado:
            seleccionados.append(encontrado)
        else:
            logger.warning("Symbol skipped (no active linear USD perpetual): %s", m)
    return seleccionados

def funding_pct_hora(exchange, simbolos: list) -> dict:
    try:
        raw = exchange.fetch_funding_rates(simbolos)
    except Exception as e:
        logger.warning("Could not fetch funding rates: %s", e)
        return {}

    res = {}
    for sym in simbolos:
        info = raw.get(sym) if isinstance(raw, dict) else None
        if info and info.get("fundingRate") is not None:
            res[sym] = float(info["fundingRate"]) * 100.0
    return res