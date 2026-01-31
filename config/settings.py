#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:19 PM
@File       : settings.py
@Description: 全局配置 (支持 .env 动态调整)
"""
# config/settings.py
import os
from pathlib import Path

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

from dotenv import load_dotenv
load_dotenv(dotenv_path=ENV_PATH) 

# --- API Keys ---
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
TARGET_WALLET = os.getenv("TARGET_WALLET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# --- 基础配置 ---
WSS_ENDPOINT = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HTTP_ENDPOINT = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
BOT_NAME = os.getenv("BOT_NAME", "NONAME")

# --- 策略配置 (支持动态调整) ---
# 强制转换为 float/int，防止从 .env 读取到字符串导致计算错误
COPY_AMOUNT_SOL = float(os.getenv("COPY_AMOUNT_SOL", 0.1))
SLIPPAGE_BUY = int(os.getenv("SLIPPAGE_BUY", 1000))
SLIPPAGE_SELL = int(os.getenv("SLIPPAGE_SELL", 2000))
TAKE_PROFIT_ROI = float(os.getenv("TAKE_PROFIT_ROI", 10.0))

# 🔥 新增：止盈卖出比例 (默认 0.5 即 50%)
TAKE_PROFIT_SELL_PCT = float(os.getenv("TAKE_PROFIT_SELL_PCT", 0.5))

# --- 风控配置 ---
MIN_LIQUIDITY_USD = int(os.getenv("MIN_LIQUIDITY_USD", 3000))           
MAX_FDV = int(os.getenv("MAX_FDV", 5000000))                            
MIN_FDV = int(os.getenv("MIN_FDV", 0))                                  
MIN_SMART_MONEY_COST = float(os.getenv("MIN_SMART_MONEY_COST", 1.0))
MAX_BUY_TIME = int(os.getenv("MAX_BUY_TIME", 3))

# --- 邮箱配置 ---
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465

# --- Jupiter API ---
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# --- 日报时间 ---
_daily_time_str = os.getenv("DAILY_REPORT_TIME", "09:00")
try:
    REPORT_HOUR, REPORT_MINUTE = map(int, _daily_time_str.split(":"))
    if not (0 <= REPORT_HOUR <= 23 and 0 <= REPORT_MINUTE <= 59):
        raise ValueError
except ValueError:
    print(f"⚠️ [配置警告] DAILY_REPORT_TIME 格式错误 ({_daily_time_str})，重置为 09:00")
    REPORT_HOUR, REPORT_MINUTE = 9, 0
