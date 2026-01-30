#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : monitor.py
@Description: 
"""
import asyncio
# services/monitor.py
import json

import aiohttp
import websockets

from config.settings import WSS_ENDPOINT, TARGET_WALLET, HTTP_ENDPOINT
from utils.logger import logger

# 黑名单：忽略 SOL, USDC, USDT
IGNORE_MINTS = [
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
]


# ================= 辅助模块：交易解析 =================
async def fetch_transaction_details(session, signature):
    payload = {"transactions": [signature]}
    try:
        async with session.post(HTTP_ENDPOINT, json=payload) as response:
            if response.status == 200:
                data = await response.json()
                if len(data) > 0: return data[0]
    except Exception:
        pass
    return None


def parse_tx(tx_data):
    if not tx_data: return None
    token_transfers = tx_data.get('tokenTransfers', [])
    trade_info = {"action": "UNKNOWN", "token_address": None, "amount": 0}

    out_tokens = []
    in_tokens = []

    # 🚫 黑名单：忽略 SOL, USDC, USDT
    IGNORE_MINTS = [
        "So11111111111111111111111111111111111111112",  # WSOL
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    ]

    for tx in token_transfers:
        mint = tx['mint']
        if mint in IGNORE_MINTS: continue  # 🔥 遇到稳定币直接跳过

        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, tx['tokenAmount']))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, tx['tokenAmount']))

    # (原本的判断逻辑保持不变...)
    if in_tokens:
        trade_info['action'] = "BUY"
        trade_info['token_address'] = in_tokens[0][0]
        trade_info['amount'] = in_tokens[0][1]
    elif out_tokens:
        trade_info['action'] = "SELL"
        trade_info['token_address'] = out_tokens[0][0]
        trade_info['amount'] = out_tokens[0][1]

    return trade_info


async def start_monitor(process_callback, pm):
    """
    process_callback: 这是一个回调函数，当解析出交易时调用它
    """
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 连接 WebSocket: {TARGET_WALLET}...")
                async with websockets.connect(WSS_ENDPOINT, ping_interval=30, ping_timeout=60) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                    }))
                    logger.info("👀 监控已就绪...")
                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if "method" in data and data["method"] == "logsNotification":
                            res = data['params']['result']
                            if any("Swap" in log for log in res['value']['logs']):
                                signature = res['value']['signature']
                                # 这里调用传入的回调函数，解耦逻辑
                                asyncio.create_task(process_callback(session, signature, pm))
            except Exception as e:
                logger.error(f"❌ 连接断开: {e}, 3秒后重连...")
                await asyncio.sleep(3)
