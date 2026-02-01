#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : monitor.py
@Description: 智能监控核心 (修复版: 增加重试机制 + 移除Log过滤 + 增强调试)
"""
import asyncio
import json
import traceback
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


async def fetch_transaction_details(session, signature):
    """
    带重试机制的交易详情抓取
    解决：WebSocket推送太快，Helius API 还没索引到的问题
    """
    payload = {"transactions": [signature]}
    max_retries = 3

    for i in range(max_retries):
        try:
            async with session.post(HTTP_ENDPOINT, json=payload, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and len(data) > 0:
                        return data[0]
                    else:
                        logger.debug(f"⚠️ [Attempt {i + 1}] Helius 返回空数据，等待索引...")
                elif response.status == 429:
                    logger.warning(f"⚠️ [Attempt {i + 1}] API 限流 (429)，等待中...")
                else:
                    logger.error(f"❌ [Attempt {i + 1}] API 请求失败: {response.status}")
        except Exception as e:
            logger.error(f"❌ [Attempt {i + 1}] 网络异常: {e}")

        # 指数退避：第一次等2秒，第二次等3秒...
        await asyncio.sleep(2 + i)

    logger.error(f"💀 最终放弃：交易 {signature} 经过 {max_retries} 次重试仍无法获取详情")
    return None


def parse_tx(tx_data):
    if not tx_data: return None

    token_transfers = tx_data.get('tokenTransfers', [])
    native_transfers = tx_data.get('nativeTransfers', [])

    trade_info = {
        "action": "UNKNOWN",
        "token_address": None,
        "amount": 0,
        "sol_spent": 0.0
    }

    out_tokens = []
    in_tokens = []

    for tx in token_transfers:
        mint = tx['mint']
        if mint in IGNORE_MINTS: continue

        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, tx['tokenAmount']))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, tx['tokenAmount']))

    # 计算 SOL 变动
    sol_change = 0
    for nt in native_transfers:
        if nt['fromUserAccount'] == TARGET_WALLET:
            sol_change -= nt['amount']
        elif nt['toUserAccount'] == TARGET_WALLET:
            sol_change += nt['amount']

    if sol_change < 0:
        trade_info['sol_spent'] = abs(sol_change) / 10 ** 9

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
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 连接 WebSocket: {TARGET_WALLET[:6]}...")
                async with websockets.connect(WSS_ENDPOINT, ping_interval=30, ping_timeout=60) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                        "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                    }))
                    logger.info("👀 监控已就绪，等待大哥发车...")

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)

                        if "method" in data and data["method"] == "logsNotification":
                            res = data['params']['result']
                            signature = res['value']['signature']

                            # 🔥 核心修改：移除 "Swap" 关键词过滤，捕获所有交易！
                            # 只要大哥动了，我们就去查，查回来发现不是 Swap 再扔掉
                            # 打印日志证明收到信号了
                            logger.info(f"⚡ 捕获链上动作: {signature[:8]}... (正在解析)")

                            # 异步处理，防止阻塞 WebSocket 心跳
                            # 🔥 修复：添加异常处理，防止单个任务崩溃影响整体监控
                            async def safe_process():
                                try:
                                    await process_callback(session, signature, pm)
                                except Exception as e:
                                    logger.error(f"💥 处理交易任务异常: {e}")
                                    logger.error(traceback.format_exc())
                            
                            asyncio.create_task(safe_process())

            except Exception as e:
                logger.error(f"❌ WebSocket 断开: {e}, 3秒后重连...")
                await asyncio.sleep(3)