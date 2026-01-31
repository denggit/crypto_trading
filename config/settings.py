#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:19 PM
@File       : settings.py
@Description: 
"""
# config/settings.py
import os
from pathlib import Path

# 获取项目根目录 (假设 settings.py 在 config/ 下，往上找两层)
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

from dotenv import load_dotenv
load_dotenv(dotenv_path=ENV_PATH) # 🔥 强制指定绝对路径

# --- API Keys ---
API_KEY = os.getenv("API_KEY")
TARGET_WALLET = os.getenv("TARGET_WALLET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# --- 基础配置 ---
WSS_ENDPOINT = f"wss://mainnet.helius-rpc.com/?api-key={API_KEY}"
HTTP_ENDPOINT = f"https://api.helius.xyz/v0/transactions/?api-key={API_KEY}"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={API_KEY}"
BOT_NAME = os.getenv("BOT_NAME", "NONAME")

# --- 策略配置 ---
COPY_AMOUNT_SOL = 0.1
SLIPPAGE_BUY = 1000
SLIPPAGE_SELL = 2000
TAKE_PROFIT_ROI = 10.0

# --- 风控配置 (跟单模式 - 激进版) ---
MIN_LIQUIDITY_USD = 3000   # 原来是 20000 -> 改为 3000
MAX_FDV = 5000000          # 保持不变
MIN_FDV = 0                # 原来是 200000 -> 改为 0 (只要有池子就跟)
MIN_SMART_MONEY_COST = 1.0 # 设定门槛：少于 1.0 SOL 视为试盘/杂音，不跟

# --- 邮箱配置 ---
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465

# --- 添加 Jupiter API Key 配置 ---
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")

# --- 📅 日报发送时间配置 (错峰运行) ---
# 默认值为 "09:00"
_daily_time_str = os.getenv("DAILY_REPORT_TIME", "09:00")

try:
    # 尝试解析 "HH:MM" 格式
    REPORT_HOUR, REPORT_MINUTE = map(int, _daily_time_str.split(":"))
    
    # 简单的范围检查
    if not (0 <= REPORT_HOUR <= 23 and 0 <= REPORT_MINUTE <= 59):
        raise ValueError("时间超出范围")
        
except ValueError:
    print(f"⚠️ [配置警告] DAILY_REPORT_TIME 格式错误 ({_daily_time_str})，已重置为默认 09:00")
    REPORT_HOUR = 9
    REPORT_MINUTE = 0
