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
    """
    解析交易数据 (修复版：解决 WSOL 忽略问题 + 防止重复记账)
    """
    if not tx_data: return None

    token_transfers = tx_data.get('tokenTransfers', [])
    native_transfers = tx_data.get('nativeTransfers', [])

    trade_info = {
        "action": "UNKNOWN",
        "token_address": None,
        "amount": 0,
        "sol_spent": 0.0
    }

    # 定义常量
    WSOL_MINT = "So11111111111111111111111111111111111111112"
    # 你的 IGNORE_MINTS 列表需要在外部定义或这里引用
    # IGNORE_MINTS = [...]

    out_tokens = []
    in_tokens = []
    wsol_spent = 0.0  # 单独追踪 WSOL 花费

    # --- 1. 处理 Token 转账 ---
    for tx in token_transfers:
        mint = tx['mint']
        token_amount = tx.get('tokenAmount', 0)

        # 🛡️ 特殊处理 WSOL：计入成本，但不作为买卖目标
        if mint == WSOL_MINT:
            if tx['fromUserAccount'] == TARGET_WALLET:
                # Helius 的 tokenTransfers 通常已经是 Decimal 格式 (如 4.95)
                # 不需要除以 1e9，直接累加
                wsol_spent += float(token_amount)
            continue

        # 忽略黑名单代币 (USDC/USDT)
        if mint in IGNORE_MINTS:
            continue

        # 统计目标代币
        if tx['fromUserAccount'] == TARGET_WALLET:
            out_tokens.append((mint, token_amount))
        elif tx['toUserAccount'] == TARGET_WALLET:
            in_tokens.append((mint, token_amount))

    # --- 2. 处理 Native SOL 转账 ---
    native_sol_spent = 0.0
    sol_balance_change = 0

    for nt in native_transfers:
        amount = nt.get('amount', 0)  # 这是 lamports
        if nt['fromUserAccount'] == TARGET_WALLET:
            sol_balance_change -= amount
        elif nt['toUserAccount'] == TARGET_WALLET:
            sol_balance_change += amount

    # 只有当 SOL 净减少时，才计入花费
    # (如果是正数，说明可能是在卖出代币换回 SOL，或者是收到退款)
    if sol_balance_change < 0:
        native_sol_spent = abs(sol_balance_change) / 10 ** 9

    # --- 3. 🔥 核心计算逻辑：取最大值防止双重计算 ---
    # 场景 A (纯SOL买): Native花费 5, WSOL花费 0 -> Cost 5
    # 场景 B (Wrap+Swap): Native花费 5(去Wrap), WSOL花费 5(去Swap) -> Cost 5 (取 Max)
    # 场景 C (纯WSOL买): Native花费 0, WSOL花费 5 -> Cost 5
    trade_info['sol_spent'] = max(native_sol_spent, wsol_spent)

    # --- 4. 判定买卖方向 ---
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
    启动WebSocket监控，监听目标钱包的所有交易
    
    Args:
        process_callback: 处理交易的回调函数
        pm: PortfolioManager实例
    """
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                logger.info(f"🔗 连接 WebSocket: {TARGET_WALLET[:6]}...")
                # 🔥 关键修复：增加连接参数，提高连接稳定性
                # close_timeout=None: 不主动关闭连接
                # max_size=None: 不限制消息大小
                async with websockets.connect(
                    WSS_ENDPOINT, 
                    ping_interval=20,  # 每20秒发送ping（更频繁）
                    ping_timeout=10,   # 10秒内没收到pong就认为断开（更快检测）
                    close_timeout=None,  # 不主动关闭连接
                    max_size=None       # 不限制消息大小
                ) as ws:
                    # 发送订阅请求
                    subscribe_msg = {
                        "jsonrpc": "2.0", 
                        "id": 1, 
                        "method": "logsSubscribe",
                        "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info("📤 已发送订阅请求，等待确认...")
                    
                    # 🔥 关键修复：等待并验证订阅确认
                    subscription_confirmed = False
                    subscription_id = None
                    pending_notification = None  # 存储等待确认期间收到的通知
                    
                    # 等待订阅确认（最多等待5秒）
                    try:
                        for _ in range(10):  # 最多检查10次，每次0.5秒
                            msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                            data = json.loads(msg)
                            
                            # 记录所有收到的消息类型（用于调试）
                            msg_type = data.get("method", "response")
                            if "id" in data and data.get("id") == 1:
                                # 这是订阅响应
                                if "result" in data:
                                    subscription_id = data["result"]
                                    subscription_confirmed = True
                                    logger.info(f"✅ 订阅成功！订阅ID: {subscription_id}")
                                    break
                                elif "error" in data:
                                    logger.error(f"❌ 订阅失败: {data['error']}")
                                    raise Exception(f"订阅失败: {data['error']}")
                            elif msg_type == "logsNotification":
                                # 如果还没确认订阅就收到通知，说明订阅可能已经生效
                                if not subscription_confirmed:
                                    logger.info("✅ 收到交易通知，订阅已生效（跳过确认等待）")
                                    subscription_confirmed = True
                                # 保存这个通知，稍后处理
                                pending_notification = data
                                break
                            else:
                                # 记录其他类型的消息（用于调试）
                                logger.debug(f"📨 收到消息类型: {msg_type}, 内容: {str(data)[:200]}")
                    except asyncio.TimeoutError:
                        if not subscription_confirmed:
                            logger.warning("⚠️ 订阅确认超时，但继续监控（可能订阅已生效）")
                    
                    if not subscription_confirmed:
                        logger.warning("⚠️ 订阅未确认，但继续运行...")
                    else:
                        logger.info("👀 监控已就绪，等待大哥发车...")
                    
                    # 处理等待期间收到的通知
                    if pending_notification:
                        res = pending_notification['params']['result']
                        signature = res['value']['signature']
                        logger.info(f"⚡ 捕获链上动作: {signature[:8]}... (正在解析)")
                        
                        async def safe_process():
                            try:
                                await process_callback(session, signature, pm)
                            except Exception as e:
                                logger.error(f"💥 处理交易任务异常: {e}")
                                logger.error(traceback.format_exc())
                        
                        asyncio.create_task(safe_process())

                    # 🔥 关键修复：WebSocket连接状态监控（仅用于日志，不用于判断断开）
                    # WebSocket本身有ping_interval=30, ping_timeout=60，会自动检测连接状态
                    # 注意：websockets库的ping/pong在底层自动处理，不会触发应用层的ws.recv()
                    # 如果连接断开，websockets库会在ws.recv()时抛出异常
                    last_message_time = asyncio.get_event_loop().time()
                    STATUS_LOG_INTERVAL = 1800  # 每30分钟记录一次状态（长时间没消息是正常的）
                    last_status_log_time = asyncio.get_event_loop().time()
                    
                    # 🔥 新增：WebSocket状态监控任务（仅用于日志，不用于判断断开）
                    async def websocket_status_monitor():
                        """
                        监控WebSocket连接状态（仅用于日志记录）
                        - 依赖websockets库的ping/pong机制检测连接状态
                        - 如果连接断开，websockets库会在ws.recv()时抛出异常
                        - 长时间没有交易消息是正常的，不应该判断为断开
                        """
                        nonlocal last_message_time, last_status_log_time
                        
                        while True:
                            await asyncio.sleep(STATUS_LOG_INTERVAL)  # 每30分钟记录一次
                            current_time = asyncio.get_event_loop().time()
                            time_since_last_msg = current_time - last_message_time
                            
                            # 只记录状态，不判断断开（长时间没交易消息是正常的）
                            hours = time_since_last_msg / 3600
                            if hours >= 1:
                                logger.info(f"💓 WebSocket连接正常 | 订阅ID: {subscription_id} | 已 {hours:.1f} 小时未收到交易（正常，大哥可能还没交易）")
                            else:
                                logger.info(f"💓 WebSocket连接正常 | 订阅ID: {subscription_id} | 最后交易: {time_since_last_msg/60:.1f} 分钟前")
                            last_status_log_time = current_time
                    
                    # 启动状态监控任务
                    status_monitor_task = asyncio.create_task(websocket_status_monitor())
                    
                    
                    # 主循环：处理所有消息
                    # 🔥 关键修复：依赖WebSocket的ping/pong机制检测连接状态
                    # websockets库已设置ping_interval=30, ping_timeout=60，会自动检测连接断开
                    # 如果连接断开，websockets库会在ws.recv()时抛出ConnectionClosed异常
                    # 如果连接正常但没消息，这里会一直等待（这是正常的）
                    # 注意：websockets库的ping/pong在底层自动处理，不会触发应用层的ws.recv()
                    try:
                        while True:
                            # 直接接收消息，不设置超时
                            # 如果连接断开，websockets库会自动抛出ConnectionClosed异常
                            # 如果连接正常但没消息，这里会一直等待（这是正常的）
                            msg = await ws.recv()
                            data = json.loads(msg)
                            
                            # 更新最后收到消息的时间（仅应用层消息，ping/pong在底层处理）
                            current_time = asyncio.get_event_loop().time()
                            last_message_time = current_time

                            # 处理交易通知
                            if "method" in data and data["method"] == "logsNotification":
                                res = data['params']['result']
                                signature = res['value']['signature']

                                # 🔥 核心修改：移除 "Swap" 关键词过滤，捕获所有交易！
                                # 只要大哥动了，我们就去查，查回来发现不是 Swap 再扔掉
                                # 打印日志证明收到信号了
                                logger.info(f"⚡ 捕获链上动作: {signature} (开始处理)")

                                # 异步处理，防止阻塞 WebSocket 心跳
                                # 🔥 修复：添加异常处理，防止单个任务崩溃影响整体监控
                                async def safe_process():
                                    try:
                                        await process_callback(session, signature, pm)
                                    except Exception as e:
                                        logger.error(f"💥 处理交易任务异常: {signature[:16]}... | 错误: {e}")
                                        logger.error(traceback.format_exc())
                                
                                asyncio.create_task(safe_process())
                            else:
                                # 🔥 新增：记录所有其他消息类型，便于调试
                                msg_type = data.get("method", "unknown")
                                if msg_type not in ["ping", "pong"]:  # 忽略心跳消息
                                    logger.debug(f"📨 收到其他消息: {msg_type}, 内容: {str(data)[:200]}")
                    finally:
                        # 清理：取消状态监控任务
                        status_monitor_task.cancel()
                        try:
                            await status_monitor_task
                        except asyncio.CancelledError:
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                # 🔥 详细记录连接关闭信息，便于诊断
                close_code = e.code if hasattr(e, 'code') else 'unknown'
                close_reason = e.reason if hasattr(e, 'reason') else 'unknown'
                logger.error(f"❌ WebSocket 连接关闭: code={close_code}, reason={close_reason}, 3秒后重连...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"❌ WebSocket 异常: {e}, 3秒后重连...")
                logger.error(traceback.format_exc())
                await asyncio.sleep(3)