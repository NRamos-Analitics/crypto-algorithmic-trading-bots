"""
Main Orchestrator and Alert Pipeline.
Handles system state, continuous API polling, and dispatching alerts via Telegram API.
"""
import os
import time
import json
import logging
from pathlib import Path
import ccxt
import pandas as pd
import requests

from data_ingestion import (
    mapear_mercados, ultimo_cierre_ts, velas_cerradas, 
    funding_pct_hora, TEMPORALIDAD, LIMITE_VELAS, MONEDAS_SNIPER
)
from technical_indicators import (
    calcular_indicadores, evaluar_senal, filtro_funding, 
    plan_operacion, CADUCIDAD_ENTRADA_ATR
)

logger = logging.getLogger("sniper")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Portfolio & System Limits
POSICION_MAX_USD = 150.0
RIESGO_MAX_USD = 3.0
APALANCAMIENTO_MAX = 10
MAX_PLANES = 2
TIME_STOP_H = 12
COOLDOWN_S = 3600
RUTA_ESTADO = Path("estado_sniper.json")


def enviar_telegram(mensaje: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not getattr(resp, "ok", False):
            logger.error("HTTP Error Telegram (%s): %s", getattr(resp, "status_code", "?"), getattr(resp, "text", ""))
            return False
        return True
    except Exception as e:
        msg_limpio = str(e).replace(token, "***TOKEN***")
        logger.error("Network failure Telegram: %s", msg_limpio)
        return False

def nuevo_estado() -> dict:
    return {"planes": {}, "cooldown": {}, "scan": {"ts": 0, "hechos": []}}

def guardar_estado(est: dict, ruta: Path = RUTA_ESTADO):
    tmp = Path(f"{ruta}.tmp")
    tmp.write_text(json.dumps(est, indent=2), encoding="utf-8")
    tmp.replace(ruta)

def cargar_estado(ruta: Path = RUTA_ESTADO) -> dict:
    if not Path(ruta).exists():
        return nuevo_estado()
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        if not isinstance(datos, dict) or "planes" not in datos:
            return nuevo_estado()
        datos.setdefault("cooldown", {})
        datos.setdefault("scan", {"ts": 0, "hechos": []})
        return datos
    except Exception:
        return nuevo_estado()

def cerrar_plan(est: dict, simbolo: str, ahora_s: float):
    est["planes"].pop(simbolo, None)
    est["cooldown"][simbolo] = float(ahora_s) + COOLDOWN_S

def revisar_planes(est: dict, precios: dict, ahora_s: float = None, ahora: float = None) -> list:
    t_actual = ahora if ahora is not None else (ahora_s if ahora_s is not None else time.time())
    eventos = []
    
    for sym, plan in est.get("planes", {}).items():
        px = precios.get(sym)
        if px is None or pd.isna(px):
            continue
            
        px = float(px)
        d = plan["dir"]
        fase = plan.get("fase", 1) 
        
        if d == "LONG":
            if fase == 1:
                if px <= plan["sl"]:
                    eventos.append(("SL_INICIAL", sym, px, plan))
                elif px >= plan["tp1"]:
                    eventos.append(("TP1", sym, px, plan))
            elif fase == 2:
                if px <= plan["entrada"]:
                    eventos.append(("SL_BREAK_EVEN", sym, px, plan))
                elif px >= plan["tp2"]:
                    eventos.append(("TP2", sym, px, plan))
        else:
            if fase == 1:
                if px >= plan["sl"]:
                    eventos.append(("SL_INICIAL", sym, px, plan))
                elif px <= plan["tp1"]:
                    eventos.append(("TP1", sym, px, plan))
            elif fase == 2:
                if px >= plan["entrada"]:
                    eventos.append(("SL_BREAK_EVEN", sym, px, plan))
                elif px <= plan["tp2"]:
                    eventos.append(("TP2", sym, px, plan))

        if fase == 1 and (t_actual - float(plan.get("t", 0.0))) >= TIME_STOP_H * 3600:
            eventos.append(("TIME_STOP", sym, px, plan))
            
    return eventos

def formatear_senal(simbolo: str, plan: dict, datos: dict, txt_funding: str, squeeze: bool) -> str:
    activo = simbolo.split("/")[0]
    cabecera = "🚀 <b>ALERTA DE SQUEEZE INSTITUCIONAL</b>" if squeeze else "⚡ <b>IMPULSO TENDENCIAL CONFIRMADO</b>"
    macro_txt = "Alcista (Sobre EMA 200)" if datos.get("macro_alcista") else "Bajista (Bajo EMA 200)"
    
    lineas = [
        "🎯 <b>SNIPER BOT v3.2 (GESTIÓN ESCALONADA)</b> 🎯",
        cabecera,
        f"Activo: <b>{activo}</b> | <code>{plan['dir']}</code>",
        "",
        "📊 <b>CONTEXTO DE MERCADO:</b>",
        f"• ADX Acelerando: <code>{datos['adx_prev']:.2f}</code> -> <code>{datos['adx']:.2f}</code>",
        f"• Volumen RVOL: <code>{datos['rvol']:.2f}x</code>",
        f"• Contexto Macro: <code>{macro_txt}</code>",
        f"• Funding Rate: <code>{txt_funding}</code>",
        "",
        f"💰 <b>CAPITAL Y RIESGO:</b>",
        f"• Tamaño Total (Nominal): <b><code>${plan['nominal']:.2f} USD</code></b>",
        f"• Apalancamiento: <b><code>{plan['apalancamiento']}x</code></b>",
        f"• Riesgo Máximo en SL: <b><code>-${plan['perdida_usd']:.2f} USD</code></b>",
        "",
        "👉 <b>PLAN DE EJECUCIÓN MANUAL EN KRAKEN:</b>",
        f"1️⃣ <b>Entrada (Market/Limit):</b> <code>${plan['entrada']:.5g}</code>",
        f"2️⃣ <b>Stop Loss Inicial:</b> <code>${plan['sl']:.5g}</code>",
        f"3️⃣ <b>Take Profit 1 (Vender 50%):</b> <code>${plan['tp1']:.5g}</code>",
        f"4️⃣ <b>Take Profit 2 (Resto 50%):</b> <code>${plan['tp2']:.5g}</code>",
        "",
        "<i>* El bot vigilará el mercado y te avisará por aquí exactamente cuándo cobrar el 50% y cuándo mover el SL a Break-Even.</i>"
    ]
    return "\n".join(lineas)

def formatear_salida(tipo: str, simbolo: str, precio: float, plan: dict) -> str:
    activo = simbolo.split("/")[0]
    
    mensajes = {
        "SL_INICIAL": "🔴 <b>STOP LOSS TOCADO</b>\n👉 <b>ACCIÓN:</b> Cierra la posición asumiendo la pérdida controlada. El trade fue invalidado estructuralmente.",
        "TP1": "🟢 <b>TAKE PROFIT 1 ALCANZADO (+1.5 ATR)</b>\n👉 <b>ACCIÓN REQUERIDA AHORA:</b>\n1. Entra a Kraken y <b>cierra exactamente el 50%</b> de tu posición en mercado/límite.\n2. Modifica tu orden de Stop Loss y <b>súbela a tu precio de entrada</b> (Break-Even).\n<i>(El bot ahora vigilará el resto de la posición con Riesgo Cero)</i>",
        "SL_BREAK_EVEN": "⚪ <b>STOP LOSS EN BREAK-EVEN TOCADO</b>\n👉 <b>ACCIÓN:</b> El precio se devolvió después de cobrar TP1. Cierra el 50% restante sin pérdidas. Trade finalizado en verde.",
        "TP2": "🚀 <b>TAKE PROFIT 2 ALCANZADO (+3.5 ATR)</b>\n👉 <b>ACCIÓN:</b> Objetivo final alcanzado. Cierra el resto de la posición. ¡Excelente trade!",
        "TIME_STOP": "⏰ <b>TIME STOP (Expiración)</b>\n👉 <b>ACCIÓN:</b> El trade ha estado lateralizando durante 12 horas sin impulso. Cierra la posición a precio de mercado para liberar capital."
    }
    
    instruccion = mensajes.get(tipo, f"Salida desconocida: {tipo}")
    
    return (
        f"🔔 <b>ALERTA DE SEGUIMIENTO SNIPER</b>\n"
        f"Activo: <b>{activo}</b> (<code>{plan['dir']}</code>)\n"
        f"Precio Actual: <code>${precio:.5g}</code> | Entrada Original: <code>${plan['entrada']:.5g}</code>\n\n"
        f"{instruccion}"
    )

def procesar_simbolo(exchange, simbolo: str, ts_cierre: int, ahora_s: float, precio_vivo, funding_map: dict, est: dict, enviar=enviar_telegram) -> bool:
    if est["scan"].get("ts") != ts_cierre:
        est["scan"] = {"ts": ts_cierre, "hechos": []}

    if simbolo in est["scan"]["hechos"] or simbolo in est["planes"]:
        return True

    if float(est.get("cooldown", {}).get(simbolo, 0.0)) > ahora_s:
        est["scan"]["hechos"].append(simbolo)
        return True

    if len(est["planes"]) >= MAX_PLANES:
        est["scan"]["hechos"].append(simbolo)
        return True

    try:
        ohlcv = exchange.fetch_ohlcv(simbolo, timeframe=TEMPORALIDAD, limit=LIMITE_VELAS)
    except Exception as e:
        logger.warning("Error OHLCV %s: %s", simbolo, e)
        return False

    df = velas_cerradas(ohlcv, ahora_s)
    if df.empty or int(df["timestamp"].iloc[-1]) < ts_cierre:
        return False

    df_ind = calcular_indicadores(df)
    direccion, motivo, datos = evaluar_senal(df_ind)
    if direccion is None:
        est["scan"]["hechos"].append(simbolo)
        return True

    ultima = df_ind.iloc[-1]
    cierre_vela = float(ultima["close"])
    atr = float(ultima["atr"])
    px_entrada = float(precio_vivo) if precio_vivo is not None else cierre_vela

    if abs(px_entrada - cierre_vela) > CADUCIDAD_ENTRADA_ATR * atr:
        est["scan"]["hechos"].append(simbolo)
        return True

    f_hora = funding_map.get(simbolo)
    bloqueado, txt_funding, squeeze = filtro_funding(direccion, f_hora)
    if bloqueado:
        est["scan"]["hechos"].append(simbolo)
        return True

    plan = plan_operacion(direccion, px_entrada, atr, POSICION_MAX_USD, RIESGO_MAX_USD, APALANCAMIENTO_MAX, funding_pct_h=f_hora or 0.0)
    if plan is None:
        est["scan"]["hechos"].append(simbolo)
        return True

    msg = formatear_senal(simbolo, plan, datos, txt_funding, squeeze)
    if not enviar(msg):
        return False

    est["planes"][simbolo] = {
        "dir": direccion,
        "entrada": plan["entrada"],
        "sl": plan["sl"],
        "tp1": plan["tp1"],
        "tp2": plan["tp2"],
        "fase": 1,
        "t": float(ahora_s),
    }
    est["scan"]["hechos"].append(simbolo)
    return True

def ciclo(exchange, simbolos: list, est: dict, enviar=enviar_telegram, ahora: float = None):
    t_actual = ahora if ahora is not None else time.time()
    precios = {}
    
    try:
        tickers = exchange.fetch_tickers()
        for s_sym in simbolos:
            info = tickers.get(s_sym)
            if info and info.get("last") is not None:
                precios[s_sym] = float(info["last"])
    except ccxt.NetworkError:
        pass 
    except Exception:
        pass 

    for tipo, sym, px, plan in revisar_planes(est, precios, ahora=t_actual):
        if enviar(formatear_salida(tipo, sym, px, plan)):
            if tipo == "TP1":
                plan["fase"] = 2
            else:
                cerrar_plan(est, sym, t_actual)

    ts_cierre = ultimo_cierre_ts(t_actual)
    if est.get("scan", {}).get("ts") != ts_cierre:
        est["scan"] = {"ts": ts_cierre, "hechos": []}

    pendientes = [s_sym for s_sym in simbolos if s_sym not in est["scan"]["hechos"]]
    if pendientes:
        funding_map = funding_pct_hora(exchange, pendientes)
        for s_sym in pendientes:
            procesar_simbolo(exchange, s_sym, ts_cierre, t_actual, precios.get(s_sym), funding_map, est, enviar)

if __name__ == "__main__":
    ex = ccxt.krakenfutures({
        "enableRateLimit": True, 
        "timeout": 30000,
        "options": {"adjustForTimeDifference": True}
    })
    
    pares = mapear_mercados(ex, MONEDAS_SNIPER)
    if not pares:
        logger.error("No valid markets found on Kraken Futures.")
    else:
        estado = cargar_estado()
        enviar_telegram(
            "🎯 <b>SNIPER BOT v3.2 EN LÍNEA (MODO ASISTENTE MANUAL)</b>\n"
            f"• Riesgo: <code>-${RIESGO_MAX_USD:.2f} USD</code>\n"
            f"• Nuevo Módulo Activo: <b>Cobros Parciales (TP1 y TP2) y Break-Even Dinámico</b>"
        )
        
        logger.info("Sniper Bot v3.2 started. Silent mode active. Watching market every 60s...")
        
        while True:
            ciclo(ex, pares, estado)
            guardar_estado(estado) 
            time.sleep(60)