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

from dotenv import load_dotenv

load_dotenv()

# --- 代理设置 (最优先) ---
PROXY_URL = "http://127.0.0.1:7890"
os.environ["HTTP_PROXY"] = PROXY_URL
os.environ["HTTPS_PROXY"] = PROXY_URL

# --- API Keys ---
API_KEY = os.getenv("API_KEY")
TARGET_WALLET = os.getenv("TARGET_WALLET")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# --- 基础配置 ---
WSS_ENDPOINT = f"wss://mainnet.helius-rpc.com/?api-key={API_KEY}"
HTTP_ENDPOINT = f"https://api.helius.xyz/v0/transactions/?api-key={API_KEY}"
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={API_KEY}"

# --- 策略配置 ---
COPY_AMOUNT_SOL = 0.1
SLIPPAGE_BUY = 1000
SLIPPAGE_SELL = 2000
TAKE_PROFIT_ROI = 10.0

# --- 风控配置 ---
MIN_LIQUIDITY_USD = 20000
MAX_FDV = 5000000
MIN_FDV = 200000

# --- 邮箱配置 ---
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465

# --- 添加 Jupiter API Key 配置 ---
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")