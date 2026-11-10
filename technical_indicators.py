"""
Technical Analysis and Risk Management Module.
Computes complex time-series indicators (EMA, ADX, RVOL) and calculates strict risk-to-reward parameters.
"""
import math
import pandas as pd
import pandas_ta as ta

# Quantitative and Risk Constants
MIN_VELAS = 210
ADX_UMBRAL = 25.0
RVOL_MINIMO = 1.8
MAX_EXTENSION_ATR = 2.5
MAX_RANGO_ATR = 3.0
FRACCION_CIERRE_MECHA = 0.35
CADUCIDAD_ENTRADA_ATR = 0.5

# Step-Target Exits
SL_MULT_ATR = 1.5
TP1_MULT_ATR = 1.5   
TP2_MULT_ATR = 3.5   
RR_MINIMO = 1.25     
SL_MIN_PCT = 0.002

# Microstructure Fees & Funding
FEE_TAKER = 0.0005
SLIPPAGE = 0.0005
MMR = 0.01
HORAS_FUNDING_EST = 12.0
FUNDING_LONG_MAX_H = 0.0040
FUNDING_LONG_SQUEEZE_H = -0.0025
FUNDING_SHORT_MIN_H = -0.0025
FUNDING_SHORT_SQUEEZE_H = 0.0040


def calcular_indicadores(df: pd.DataFrame):
    if df is None or len(df) < MIN_VELAS:
        return None

    d = df.copy()
    adx_df = ta.adx(d["high"], d["low"], d["close"], length=14)
    if adx_df is None or adx_df.empty:
        return None

    d["adx"] = adx_df["ADX_14"]
    d["dmp"] = adx_df["DMP_14"]
    d["dmn"] = adx_df["DMN_14"]
    d["rsi"] = ta.rsi(d["close"], length=14)
    d["atr"] = ta.atr(d["high"], d["low"], d["close"], length=14)
    d["vol_base"] = d["volume"].shift(1).rolling(20).mean()
    d["ema20"] = ta.ema(d["close"], length=20)
    d["ema200"] = ta.ema(d["close"], length=200)
    return d

def evaluar_senal(df_ind: pd.DataFrame):
    if df_ind is None or len(df_ind) < 2:
        return None, "invalid_data", None

    previa = df_ind.iloc[-2]
    actual = df_ind.iloc[-1]

    campos = ["adx", "dmp", "dmn", "rsi", "atr", "vol_base", "ema20", "ema200", "close", "open", "high", "low", "volume"]
    for c in campos:
        if pd.isna(actual.get(c)) or pd.isna(previa.get(c)):
            return None, "invalid_data", None

    atr = float(actual["atr"])
    vol_base_act = float(actual["vol_base"])
    vol_base_prev = float(previa["vol_base"])
    if atr <= 0 or vol_base_act <= 0 or vol_base_prev <= 0:
        return None, "invalid_data", None

    adx_act = float(actual["adx"])
    adx_prev = float(previa["adx"])
    if adx_act < ADX_UMBRAL or adx_act <= adx_prev:
        return None, "adx", None

    rvol_act = float(actual["volume"]) / vol_base_act
    rvol_prev = float(previa["volume"]) / vol_base_prev
    rvol = max(rvol_act, rvol_prev)
    if rvol < RVOL_MINIMO:
        return None, "rvol", None

    close = float(actual["close"])
    ema20 = float(actual["ema20"])
    ext = abs(close - ema20) / atr
    if ext > MAX_EXTENSION_ATR:
        return None, "extension", None

    high = float(actual["high"])
    low = float(actual["low"])
    open_p = float(actual["open"])
    rango = high - low
    if rango <= 0 or (rango / atr) > MAX_RANGO_ATR:
        return None, "rango_excesivo", None

    dmp = float(actual["dmp"])
    dmn = float(actual["dmn"])
    rsi = float(actual["rsi"])

    direccion = None
    if dmp > dmn and close > ema20 and rsi < 72.0 and close > open_p:
        direccion = "LONG"
    elif dmn > dmp and close < ema20 and rsi > 28.0 and close < open_p:
        direccion = "SHORT"

    if direccion is None:
        return None, "direccion", None

    if direccion == "LONG":
        if (high - close) / rango > FRACCION_CIERRE_MECHA:
            return None, "mecha", None
    else:
        if (close - low) / rango > FRACCION_CIERRE_MECHA:
            return None, "mecha", None

    datos = {
        "adx_prev": adx_prev,
        "adx": adx_act,
        "rvol": rvol,
        "ext": ext,
        "macro_alcista": bool(close > float(actual["ema200"])),
    }
    return direccion, "ok", datos

def filtro_funding(direccion: str, funding_pct_h):
    if funding_pct_h is None or pd.isna(funding_pct_h):
        return False, "n/d", False

    f = float(funding_pct_h)
    if direccion == "LONG":
        if f > FUNDING_LONG_MAX_H:
            return True, f"blocked ({f:+.4f}%/h)", False
        if f <= FUNDING_LONG_SQUEEZE_H:
            return False, f"favorable ({f:+.4f}%/h)", True
        return False, f"neutral ({f:+.4f}%/h)", False
    else:
        if f < FUNDING_SHORT_MIN_H:
            return True, f"blocked ({f:+.4f}%/h)", False
        if f >= FUNDING_SHORT_SQUEEZE_H:
            return False, f"favorable ({f:+.4f}%/h)", True
        return False, f"neutral ({f:+.4f}%/h)", False

def plan_operacion(direccion: str, entrada: float, atr: float, POSICION_MAX_USD: float, RIESGO_MAX_USD: float, APALANCAMIENTO_MAX: int, funding_pct_h: float = 0.0):
    if entrada <= 0 or atr <= 0 or pd.isna(entrada) or pd.isna(atr):
        return None

    dist_sl = SL_MULT_ATR * atr
    dist_tp1 = TP1_MULT_ATR * atr
    dist_tp2 = TP2_MULT_ATR * atr

    sl_pct = dist_sl / entrada
    tp1_pct = dist_tp1 / entrada
    tp2_pct = dist_tp2 / entrada
    
    if sl_pct < SL_MIN_PCT:
        return None

    f_val = 0.0 if funding_pct_h is None or pd.isna(funding_pct_h) else float(funding_pct_h)
    if direccion == "LONG":
        costo_funding = max(0.0, (f_val / 100.0) * HORAS_FUNDING_EST)
        sl = entrada - dist_sl
        tp1 = entrada + dist_tp1
        tp2 = entrada + dist_tp2
    else:
        costo_funding = max(0.0, (-f_val / 100.0) * HORAS_FUNDING_EST)
        sl = entrada + dist_sl
        tp1 = entrada - dist_tp1
        tp2 = entrada - dist_tp2

    costo_base = 2.0 * FEE_TAKER + SLIPPAGE
    costo_total = costo_base + costo_funding

    riesgo_unit = sl_pct + costo_total
    beneficio_unit = ((tp1_pct + tp2_pct) / 2.0) - costo_total
    
    if beneficio_unit <= 0 or riesgo_unit <= 0:
        return None

    rr = beneficio_unit / riesgo_unit
    if rr < RR_MINIMO:
        return None

    lev_seguro = int(math.floor(1.0 / (sl_pct / 0.6 + MMR)))
    apalancamiento = min(APALANCAMIENTO_MAX, lev_seguro)
    if apalancamiento < 1:
        return None

    colchon_liq = (1.0 / apalancamiento) - MMR
    if direccion == "LONG":
        liq_aprox = entrada * (1.0 - colchon_liq)
        if sl <= liq_aprox:
            return None
    else:
        liq_aprox = entrada * (1.0 + colchon_liq)
        if sl >= liq_aprox:
            return None

    nominal = min(POSICION_MAX_USD, RIESGO_MAX_USD / riesgo_unit)
    perdida_usd = nominal * riesgo_unit
    ganancia_usd_promedio = nominal * beneficio_unit
    margen = nominal / apalancamiento

    return {
        "dir": direccion,
        "entrada": float(entrada),
        "sl": float(sl),
        "tp1": float(tp1),
        "tp2": float(tp2),
        "sl_pct": float(sl_pct),
        "rr": float(rr),
        "nominal": float(nominal),
        "perdida_usd": float(perdida_usd),
        "ganancia_usd": float(ganancia_usd_promedio),
        "apalancamiento": int(apalancamiento),
        "margen": float(margen),
        "liq_aprox": float(liq_aprox),
    }