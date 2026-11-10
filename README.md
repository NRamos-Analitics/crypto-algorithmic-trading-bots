# Algorithmic Trading Ecosystem: Scanners & Alert Systems

## 🎯 Project Overview
This repository contains the architecture for a dual-bot real-time cryptocurrency trading system (`Perps_Radar` and `SNIPER BOT`). Designed to operate in tandem, the system scans the Kraken Pro futures market across multiple timeframes using asynchronous API calls, computes technical indicators, and dispatches automated tactical trade alerts via Telegram.

## ⚙️ Core Architecture & Features

### 1. Perps_Radar (Market Thermometer & Screener)
* **Asynchronous Polling (`asyncio`):** Efficiently fetches market data concurrently without rate-limiting blocks.
* **Multi-Timeframe Analysis:** 
  * **12H Macro:** Evaluates Bitcoin's regime vs EMA200 and scans global funding rates for squeeze risks.
  * **1H Screener:** Identifies the top 3 altcoins with accelerating momentum (ADX > 25) and high Relative Volume (RVOL).
  * **15m Micro:** Detects liquidity sweeps and momentum ignition, utilizing a "BTC Correlation Shield" to prevent trading against dominant market trends.

### 2. SNIPER BOT (Tactical Execution & Position Monitoring)
* **Modular Design:** Strict separation of concerns between data ingestion, technical indicators, and the main orchestration loop.
* **Algorithmic Signal Generation:** Computes Exponential Moving Averages (EMA 20/200), Stochastic Oscillator, ADX, and RVOL using Pandas and NumPy.
* **Dynamic Risk Management:** Calculates position sizing dynamically based on a strict USD risk limit, incorporating exchange taker fees, slippage, and projected funding costs.
* **State Management:** Tracks active plans and alerts the user to take partial profits (TP1, TP2) and move stop-losses to break-even.

## 🛠️ Technology Stack
* **Python:** Core application logic, synchronous and asynchronous (`asyncio`) processing.
* **Pandas & NumPy:** Time-series data manipulation and mathematical indicator calculations.
* **REST APIs (ccxt):** Robust integration with the Kraken Pro Financial API.
* **Telegram API:** Automated messaging pipeline for instant push notifications.
