"""
Perpetual Futures Market Radar (Async).
Acts as an algorithmic screener across multiple timeframes (15m, 1H, 12H).
Evaluates Bitcoin's macro bias and scans altcoins for institutional liquidity sweeps.
"""
import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import requests

# ==============================================================================
# QUANTITATIVE CONFIGURATION & RISK MANAGEMENT
# ==============================================================================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("Radar")

# Rate limit handling for Kraken API
API_SEMAPHORE = asyncio.Semaphore(3)

# Timeframe constants in milliseconds
TF_15M_MS = 900_000
TF_1H_MS = 3_600_000
TF_1D_MS = 86_400_000

MONEDAS = [
    "BTC/USD:USD", "ETH/USD:USD", "SOL/USD:USD", "SUI/USD:USD", "NEAR/USD:USD",
    "AVAX/USD:USD", "APT/USD:USD", "INJ/USD:USD", "LINK/USD:USD", "RENDER/USD:USD",
    "FET/USD:USD", "TIA/USD:USD", "DOGE/USD:USD", "PEPE/USD:USD", "XRP/USD:USD"
]

# Risk Parameters for Tactical Orders
RIESGO_MAX_USD = 3.0
POSICION_MAX_USD = 150.0
APALANCAMIENTO_DEF = 5
FEE_TAKER = 0.0005
SLIPPAGE = 0.0005

# Risk-to-Reward (R) Exits
TP1_R_MULT = 1.0   
TP2_R_MULT = 2.2   

FUNDING_LONG_SQUEEZE = 0.0040
FUNDING_SHORT_SQUEEZE = -0.0025

# ==============================================================================
# CORE UTILITIES & CANDLESTICK SYNCHRONIZATION
# ==============================================================================
def ultimo_cierre_ts(ahora_s: float, tf_ms: int) -> int:
    ahora_ms = int(ahora_s * 1000)
    return (ahora_ms // tf_ms - 1) * tf_ms

def filtrar_velas_cerradas(df: pd.DataFrame, ahora_s: float, tf_ms: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    corte = ultimo_cierre_ts(ahora_s, tf_ms)
    return df[df["timestamp"] <= corte].reset_index(drop=True)

def enviar_telegram(mensaje: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram not configured: missing environment variables.")
        return False
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.error(f"HTTP Error Telegram ({resp.status_code}): {resp.text}")
            return False
        return True
    except Exception as e:
        msg_limpio = str(e).replace(token, "***TOKEN***")
        logger.error(f"Network failure Telegram: {msg_limpio}")
        return False

async def obtener_velas(exchange, simbolo: str, timeframe: str, limite: int) -> pd.DataFrame:
    async with API_SEMAPHORE:
        try:
            ohlcv = await exchange.fetch_ohlcv(simbolo, timeframe, limit=limite)
            if not ohlcv:
                return pd.DataFrame()
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            logger.error(f"Error fetching {simbolo} ({timeframe}): {e}")
            return pd.DataFrame()

# ==============================================================================
# LEVEL 1: MACRO SCANNER (Market Thermometer - Every 12 Hours)
# ==============================================================================
async def nivel_macro(exchange, ahora_s: float):
    logger.info("📡 Scanning LEVEL 1: MACRO (12H)...")
    df_raw = await obtener_velas(exchange, "BTC/USD:USD", "1d", 250)
    df_btc = filtrar_velas_cerradas(df_raw, ahora_s, TF_1D_MS)
    
    tendencia, dist_ema, precio_btc = "UNKNOWN", 0.0, 0.0
    if not df_btc.empty and len(df_btc) > 200:
        ema200 = ta.ema(df_btc["close"], length=200).iloc[-1]
        precio_btc = df_btc["close"].iloc[-1]
        tendencia = "BULLISH 🟢" if precio_btc > ema200 else "BEARISH 🔴"
        dist_ema = ((precio_btc / ema200) - 1) * 100

    fondeo_promedio = 0.0
    estado_apalancamiento = "NEUTRAL ⚪"
    try:
        rates = await exchange.fetch_funding_rates(MONEDAS)
        fondos = [float(info["fundingRate"]) * 100 for sym, info in rates.items() if info.get("fundingRate") is not None]
        if fondos:
            fondeo_promedio = sum(fondos) / len(fondos)
            if fondeo_promedio > FUNDING_LONG_SQUEEZE:
                estado_apalancamiento = "LONG SQUEEZE RISK ⚠️ (Overbought)"
            elif fondeo_promedio < FUNDING_SHORT_SQUEEZE:
                estado_apalancamiento = "SHORT SQUEEZE RISK 🩸 (Oversold)"
    except Exception as e:
        logger.error(f"Error calculating Funding Rates: {e}")

    alerta = (
        "🌍 <b>MACRO RADAR (12H): SYSTEM STATE</b> 🌍\n\n"
        f"<b>BTC Regime (1D):</b> {tendencia}\n"
        f"• Daily Close: <code>${precio_btc:,.2f}</code>\n"
        f"• EMA200 Distance: <code>{dist_ema:+.2f}%</code>\n\n"
        f"<b>Global Leverage (Kraken):</b>\n"
        f"• Average 15 Assets: <code>{fondeo_promedio:+.4f}%/h</code>\n"
        f"• Diagnostic: <b>{estado_apalancamiento}</b>"
    )
    enviar_telegram(alerta)

# ==============================================================================
# LEVEL 2: SCREENER (Top Opportunities - Every 1 Hour)
# ==============================================================================
async def nivel_screener(exchange, ahora_s: float):
    logger.info("🎯 Scanning LEVEL 2: SCREENER (1H)...")
    tareas = [obtener_velas(exchange, sym, "1h", 100) for sym in MONEDAS]
    resultados = await asyncio.gather(*tareas)
    corte_1h = ultimo_cierre_ts(ahora_s, TF_1H_MS)
    
    ranking = []
    for sym, df_raw in zip(MONEDAS, resultados):
        df = filtrar_velas_cerradas(df_raw, ahora_s, TF_1H_MS)
        if df.empty or len(df) < 50 or int(df["timestamp"].iloc[-1]) != corte_1h:
            continue
            
        df["vol_base"] = df["volume"].shift(1).rolling(20).mean()
        vela = df.iloc[-1]
        if pd.isna(vela["vol_base"]) or vela["vol_base"] <= 0:
            continue
        rvol = vela["volume"] / vela["vol_base"]
        
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is None or adx_df.empty:
            continue
        
        adx_act = adx_df["ADX_14"].iloc[-1]
        adx_prev = adx_df["ADX_14"].iloc[-2]
        
        if rvol > 2.0 and adx_act > 25 and adx_act > adx_prev:
            ranking.append({"sym": sym.split("/")[0], "rvol": rvol, "adx": adx_act})
            
    ranking = sorted(ranking, key=lambda x: x["rvol"], reverse=True)[:3]
    if ranking:
        lineas = ["📊 <b>1H SCREENER: TOP 3 INSTITUTIONAL FLOWS</b> 📊\n"]
        for idx, r in enumerate(ranking, 1):
            lineas.append(
                f"{idx}. <b>{r['sym']}</b>\n"
                f"   • RVOL: <code>{r['rvol']:.2f}x</code>\n"
                f"   • ADX: <code>{r['adx']:.2f}</code> (Trend accelerating)"
            )
        lineas.append("\n<i>👉 TACTICAL NOTE: Do not market enter. Add to Watchlist (15m/5m) and wait for EMA20 rejection + StochS exhaustion.</i>")
        enviar_telegram("\n".join(lineas))

# ==============================================================================
# LEVEL 3: MICRO / FLASH (Ignition & Tactical Plan - Every 15m)
# ==============================================================================
def evaluar_sesgo_btc(df_btc: pd.DataFrame, corte_15m: int) -> str:
    """Evaluates if BTC had a sweep or ignition in 15m to block contrary plans."""
    if df_btc.empty or len(df_btc) < 30 or int(df_btc["timestamp"].iloc[-1]) != corte_15m:
        return "NEUTRAL"
        
    df_btc = df_btc.copy()
    df_btc["atr"] = ta.atr(df_btc["high"], df_btc["low"], df_btc["close"], length=14)
    df_btc["vol_base"] = df_btc["volume"].shift(1).rolling(20).mean()
    
    vela = df_btc.iloc[-1]
    if pd.isna(vela["atr"]) or pd.isna(vela["vol_base"]) or vela["vol_base"] <= 0:
        return "NEUTRAL"
        
    rango = vela["high"] - vela["low"]
    if rango <= 0:
        return "NEUTRAL"
        
    expansion = rango / vela["atr"]
    rvol = vela["volume"] / vela["vol_base"]
    cuerpo_max = max(vela["open"], vela["close"])
    cuerpo_min = min(vela["open"], vela["close"])
    cuerpo = cuerpo_max - cuerpo_min
    
    mecha_sup = (vela["high"] - cuerpo_max) / rango
    mecha_inf = (cuerpo_min - vela["low"]) / rango
    
    if (mecha_inf > 0.65 and expansion > 1.5) or (rvol > 2.5 and expansion > 2.0 and cuerpo > rango * 0.5 and vela["close"] > vela["open"]):
        return "BLOCK_SHORT"
        
    if (mecha_sup > 0.65 and expansion > 1.5) or (rvol > 2.5 and expansion > 2.0 and cuerpo > rango * 0.5 and vela["close"] < vela["open"]):
        return "BLOCK_LONG"
        
    return "NEUTRAL"

async def nivel_micro(exchange, ahora_s: float):
    logger.info("⚡ Scanning LEVEL 3: MICRO / FLASH (15m)...")
    tareas = [obtener_velas(exchange, sym, "15m", 50) for sym in MONEDAS]
    resultados = await asyncio.gather(*tareas)
    corte_15m = ultimo_cierre_ts(ahora_s, TF_15M_MS)
    
    mapa_velas = {}
    for sym, df_raw in zip(MONEDAS, resultados):
        mapa_velas[sym] = filtrar_velas_cerradas(df_raw, ahora_s, TF_15M_MS)
        
    sesgo_btc = evaluar_sesgo_btc(mapa_velas.get("BTC/USD:USD", pd.DataFrame()), corte_15m)
    if sesgo_btc != "NEUTRAL":
        logger.info(f"🛡️ BTC Shield Active on 15m: {sesgo_btc}")
    
    for sym in MONEDAS:
        df = mapa_velas[sym]
        if df.empty or len(df) < 30 or int(df["timestamp"].iloc[-1]) != corte_15m:
            continue
        
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["vol_base"] = df["volume"].shift(1).rolling(20).mean()
        
        vela = df.iloc[-1]
        if pd.isna(vela["atr"]) or pd.isna(vela["vol_base"]) or vela["vol_base"] <= 0:
            continue
        
        rango = vela["high"] - vela["low"]
        if rango <= 0:
            continue
        
        rvol = vela["volume"] / vela["vol_base"]
        expansion = rango / vela["atr"]
        
        cuerpo_max = max(vela["open"], vela["close"])
        cuerpo_min = min(vela["open"], vela["close"])
        cuerpo = cuerpo_max - cuerpo_min
        
        mecha_sup = (vela["high"] - cuerpo_max) / rango
        mecha_inf = (cuerpo_min - vela["low"]) / rango
        
        activo = sym.split("/")[0]
        
        # 1. Liquidity Hunting (Sweeps and Traps)
        if mecha_inf > 0.65 and expansion > 1.5:
            enviar_telegram(
                f"🩸 <b>{activo} (15m)</b>: Massive bearish sweep detected (Bottom Wick: {mecha_inf*100:.0f}%).\n\n"
                f"<i>👉 TACTICAL NOTE: Institutional buyers absorbed the drop. Look for bullish confirmation on 15m/5m for LONG entry.</i>"
            )
            continue
        if mecha_sup > 0.65 and expansion > 1.5:
            enviar_telegram(
                f"🔥 <b>{activo} (15m)</b>: Massive bull trap detected (Top Wick: {mecha_sup*100:.0f}%).\n\n"
                f"<i>👉 TACTICAL NOTE: Strong resistance rejection. DO NOT LONG. Severe risk of bearish cascade.</i>"
            )
            continue
            
        # 2. Momentum Ignition and Scaled Tactical Plan
        if rvol > 3.0 and expansion > 2.5 and cuerpo > (rango * 0.5):
            direccion = "LONG" if vela["close"] > vela["open"] else "SHORT"
            
            if activo != "BTC":
                if direccion == "SHORT" and sesgo_btc == "BLOCK_SHORT":
                    logger.info(f"SHORT plan on {activo} skipped due to BTC bullish absorption/strength.")
                    continue
                if direccion == "LONG" and sesgo_btc == "BLOCK_LONG":
                    logger.info(f"LONG plan on {activo} skipped due to BTC bearish rejection/strength.")
                    continue
            
            margen_sl = vela["atr"] * 1.0
            entrada = (vela["high"] + vela["low"]) / 2.0
            
            if direccion == "SHORT":
                sl = vela["high"] + margen_sl
                dist_sl = sl - entrada
                tp1 = entrada - (dist_sl * TP1_R_MULT)
                tp2 = entrada - (dist_sl * TP2_R_MULT)
                sl_pct = dist_sl / entrada
            else:
                sl = vela["low"] - margen_sl
                dist_sl = entrada - sl
                tp1 = entrada + (dist_sl * TP1_R_MULT)
                tp2 = entrada + (dist_sl * TP2_R_MULT)
                sl_pct = dist_sl / entrada

            costo = (2.0 * FEE_TAKER) + SLIPPAGE
            riesgo_real = max(sl_pct + costo, 0.001)
            
            nominal = min(POSICION_MAX_USD, RIESGO_MAX_USD / riesgo_real)
            riesgo_usd_real = nominal * riesgo_real
            margen = nominal / APALANCAMIENTO_DEF

            alerta = (
                f"🚨 <b>IGNITION & TACTICAL PLAN: {activo} (15m)</b> 🚨\n\n"
                f"Direction: <b>{direccion}</b>\n"
                f"• Current Close: <code>${vela['close']:.5g}</code>\n"
                f"• RVOL: <code>{rvol:.1f}x</code> | Expansion: <code>{expansion:.1f} ATR</code>\n\n"
                f"🎯 <b>LIMIT ORDER (50% Candle Retracement):</b>\n"
                f"• Limit Entry: <code>${entrada:.5g}</code>\n"
                f"• Stop Loss: <code>${sl:.5g}</code> (-{sl_pct*100:.2f}%)\n"
                f"• TP1 (Take 50% at {TP1_R_MULT}R): <code>${tp1:.5g}</code> <i>(Move SL to Break-Even)</i>\n"
                f"• TP2 (Remaining 50% at {TP2_R_MULT}R): <code>${tp2:.5g}</code>\n\n"
                f"⚖️ <b>Capital Management:</b>\n"
                f"• Real Risk at SL: <b><code>-${riesgo_usd_real:.2f} USD</code></b> (Cap: ${RIESGO_MAX_USD:.2f})\n"
                f"• Total Nominal Size: <code>${nominal:.2f} USD</code>\n"
                f"• Leverage: <code>{APALANCAMIENTO_DEF}x</code> (Margin: <code>${margen:.2f}</code>)"
            )
            enviar_telegram(alerta)

# ==============================================================================
# SCHEDULER
# ==============================================================================
async def main():
    exchange = ccxt.krakenfutures({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"adjustForTimeDifference": True}
    })
    logger.info("✅ Tactical Radar Bot v2.1 Started...")
    enviar_telegram(
        "📡 <b>TACTICAL RADAR v2.1 ONLINE (15m/1H/12H)</b>\n"
        "• Candle closure verified by Timestamp\n"
        "• BTC Correlation Shield active on 15m\n"
        "• Scaled exits calibrated in real R units (1.0R / 2.2R)"
    )
    
    try:
        while True:
            ahora = time.time()
            prox_15m = ((int(ahora) // 900) + 1) * 900
            segundos_espera = prox_15m - ahora + 3.0
            
            await asyncio.sleep(segundos_espera)
            
            t_ciclo = time.time()
            hora_actual_utc = datetime.fromtimestamp(t_ciclo, tz=timezone.utc)
            minuto = hora_actual_utc.minute
            hora = hora_actual_utc.hour
            
            await nivel_micro(exchange, t_ciclo)
            if minuto == 0:
                await nivel_screener(exchange, t_ciclo)
            if minuto == 0 and (hora == 0 or hora == 12):
                await nivel_macro(exchange, t_ciclo)
                
    except asyncio.CancelledError:
        logger.info("Bot manually stopped.")
    except Exception as e:
        logger.error(f"Fatal error in main loop: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down Radar Bot...")