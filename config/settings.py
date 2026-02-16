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
# 本币为 USDC：每次跟单买入金额（单位：USDC）
COPY_AMOUNT_USDC = float(os.getenv("COPY_AMOUNT_USDC", 10.0))
SLIPPAGE_BUY = int(os.getenv("SLIPPAGE_BUY", 1000))
SLIPPAGE_SELL = int(os.getenv("SLIPPAGE_SELL", 2000))
TAKE_PROFIT_ROI = float(os.getenv("TAKE_PROFIT_ROI", 10.0))

# 🔥 新增：止盈卖出比例 (默认 0.5 即 50%)
TAKE_PROFIT_SELL_PCT = float(os.getenv("TAKE_PROFIT_SELL_PCT", 0.5))

# 🛡️ 止损百分比 (默认 0.5 即 50%，当亏损达到此比例时触发止损)
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", 0.5))

# --- 风控配置 ---
MIN_LIQUIDITY_USD = int(os.getenv("MIN_LIQUIDITY_USD", 3000))
MAX_FDV = int(os.getenv("MAX_FDV", 5000000))
MIN_FDV = int(os.getenv("MIN_FDV", 0))
MIN_SMART_MONEY_COST = float(os.getenv("MIN_SMART_MONEY_COST", 1.0))

# 🛡️ V4 Pro 双重熔断风控机制
# 1. 【核心风控】单币最大持仓成本 (USDC，本币)
# 只要在这个币上总共花的钱没超过这个值，就会一直跟单
# 只有在完全清仓后，成本才会归零，可以重新买入
MAX_POSITION_USDC = float(os.getenv("MAX_POSITION_USDC", 200.0))

# 2. 【频次风控】单币最大买入次数硬限制
# 给一个宽松的上限（如 20 次），仅用于防止 API 被刷爆或恶意脚本
# 买入次数不会在清仓后清零，是累计的
MAX_BUY_COUNTS_HARD_LIMIT = int(os.getenv("MAX_BUY_COUNTS_HARD_LIMIT", 20))

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

# --- 币地址与精度 ---
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# USDC 精度（用于原始数量与显示数量换算）
USDC_DECIMALS = 6

# --- Jito MEV 防护配置 ---
# 是否开启 Jito 模式 (默认开启，.env 可覆盖)
# 开启后，交易将通过 Jito Bundle 发送，防夹且加速
USE_JITO = os.getenv("USE_JITO", "true").lower() == "true"

# Jito 小费金额 (SOL)
# 默认 0.0001 SOL，这是一个能保证上链的基础值
# 如果想抢得更猛，可以在 .env 里改成 0.001 或更高
JITO_TIP_AMOUNT = float(os.getenv("JITO_TIP_AMOUNT", 0.0001))

# Jito Block Engine URL
# 针对美西服务器 (硅谷/加州)，推荐使用 SLC (盐湖城) 节点，延迟最低
# 如果连接不稳定，可以改回主网通用节点: https://mainnet.block-engine.jito.wtf
JITO_BLOCK_ENGINE_URL = os.getenv("JITO_BLOCK_ENGINE_URL", "https://slc.mainnet.block-engine.jito.wtf/api/v1/bundles")

# Jito 官方小费账户列表 (这些是写死的，全球通用)
# 交易时会随机选一个转账
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopXSjb3uJC66ewJR605UwYJ7r3n",
    "DfXygSm4jCyNCybVYYK6DwvWqjKkNEqGdQI15a5Q1jvI",
    "ADuUkR4ykGytmnb5LHydo2iamqrpobyRGmurdZG5iDkD",
    "DttWaMuVvTiduZRNguLF8983agHzztVXiMVB3yKDhKS5",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnIzKZ6jJ"
]
