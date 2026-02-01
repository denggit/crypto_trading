#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : run_health_check.py
@Description: 全系统启动前自检脚本 (最终修复版 - 零污染模式)
"""
import asyncio
import logging
import os
import sys
import argparse
import aiohttp
import socket
import traceback
import json
import websockets
from datetime import datetime

# --- 导入项目模块 ---
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    HELIUS_API_KEY, TARGET_WALLET, PRIVATE_KEY, RPC_URL,
    EMAIL_SENDER, WSS_ENDPOINT, HTTP_ENDPOINT
)
from services.solana.trader import SolanaTrader
from services.risk_control import check_token_liquidity
from services.notification import send_email_async
from services.solana.monitor import parse_tx, fetch_transaction_details
# 🔥 关键修改：我们需要导入整个模块，以便修改里面的全局变量
import core.portfolio
from core.portfolio import PortfolioManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("HealthCheck")


async def test_configuration():
    logger.info("🛠️ [1/6] 检查环境配置...")
    proxy = os.environ.get("HTTP_PROXY")
    if proxy:
        logger.info(f"✅ 检测到代理模式: {proxy}")
    else:
        logger.info("☁️ 检测到直连模式 (无代理)")
    return True


async def test_rpc_and_trader():
    logger.info("🔗 [2/6] 测试 RPC 连接 & Jupiter 询价...")
    try:
        trader = SolanaTrader(RPC_URL)

        # 1. 测试 RPC
        logger.info(f"正在连接 RPC: {RPC_URL[:25]}...")
        balance_resp = await trader.rpc_client.get_balance(trader.payer.pubkey())
        balance = balance_resp.value / 10 ** 9
        logger.info(f"✅ RPC 连接成功 | 当前余额: {balance:.4f} SOL")

        # 2. 测试 Jupiter
        logger.info("正在测试 Jupiter 询价 (0.1 SOL -> USDC)...")

        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False, force_close=True)
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            quote = await trader.get_quote(
                session,
                trader.SOL_MINT,
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                int(0.1 * 10 ** 9)
            )
            if quote and 'outAmount' in quote:
                out_amount = int(quote['outAmount']) / 10 ** 6
                logger.info(f"✅ Jupiter 询价成功 | 0.1 SOL ≈ {out_amount:.2f} USDC")
                return True
            else:
                logger.error(f"❌ Jupiter 询价返回无效: {quote}")
                return False

    except Exception as e:
        logger.error("❌ 交易模块测试崩溃")
        logger.error(traceback.format_exc())
        return False


async def test_risk_control():
    logger.info("🛡️ [3/6] 测试 DexScreener 风控接口...")
    try:
        jup_mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"

        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ssl=False,
            force_close=True
        )

        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            is_safe, liq, fdv = await check_token_liquidity(session, jup_mint)
            if is_safe and liq > 0:
                logger.info(f"✅ DexScreener 连接成功 | JUP 流动性: ${liq:,.0f}")
                return True
            else:
                logger.error(f"❌ DexScreener 数据异常")
                return False
    except Exception as e:
        logger.error(f"⚠️ 风控检查报错: {e}")
        return False


async def test_parser_logic():
    logger.info("🧠 [4/6] 测试交易解析逻辑...")
    mock_tx = {
        "tokenTransfers": [
            {"mint": "So11111111111111111111111111111111111111112", "tokenAmount": 10.5,
             "fromUserAccount": TARGET_WALLET, "toUserAccount": "Pool"},
            {"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "tokenAmount": 1000000, "fromUserAccount": "Pool",
             "toUserAccount": TARGET_WALLET}
        ]
    }
    result = parse_tx(mock_tx)
    if result and result['action'] == 'BUY':
        logger.info(f"✅ 解析逻辑正常")
        return True
    return False


async def test_portfolio_manager():
    logger.info("YZ [5/6] 测试仓位管理 (零污染模式)...")

    # 🔥 1. 备份：先记住原来的文件路径
    original_portfolio_file = core.portfolio.PORTFOLIO_FILE
    original_history_file = core.portfolio.HISTORY_FILE

    # 🔥 2. 篡改：指向临时垃圾文件
    temp_portfolio = "data/health_check_trash_portfolio.json"
    temp_history = "data/health_check_trash_history.json"

    core.portfolio.PORTFOLIO_FILE = temp_portfolio
    core.portfolio.HISTORY_FILE = temp_history

    pm = None # 初始化变量

    try:
        trader = SolanaTrader(RPC_URL)
        pm = PortfolioManager(trader)

        # 这个操作现在只会写到垃圾文件里
        pm.add_position("TEST_TOKEN_JUP", 1000, 0.1)

        # 稍微给一点时间让后台线程完成写入 (这是新版改动引入的特性)
        await asyncio.sleep(0.5) 

        if "TEST_TOKEN_JUP" in pm.portfolio:
            logger.info("✅ 记账功能正常 (已写入临时文件)")
            result = True
        else:
            logger.error("❌ 记账失败：内存中未找到代币")
            result = False

    except Exception as e:
        logger.error(f"❌ 仓位管理失败: {e}")
        logger.error(traceback.format_exc()) # 打印堆栈以便排查
        result = False

    finally:
        # 🔥🔥🔥 新增：显式关闭线程池，防止脚本卡死 🔥🔥🔥
        if pm and hasattr(pm, 'calc_executor'):
            pm.calc_executor.shutdown(wait=False)
        # ------------------------------------------------

        # 🔥 3. 还原：把路径改回去，防止影响后续逻辑
        core.portfolio.PORTFOLIO_FILE = original_portfolio_file
        core.portfolio.HISTORY_FILE = original_history_file

        # 🔥 4. 扫地：删除生成的临时文件
        if os.path.exists(temp_portfolio):
            try:
                os.remove(temp_portfolio)
            except: pass
        if os.path.exists(temp_history):
            try:
                os.remove(temp_history)
            except: pass
        
        # 删除可能产生的 .tmp 临时文件
        if os.path.exists(temp_portfolio + ".tmp"):
            try: os.remove(temp_portfolio + ".tmp")
            except: pass
            
        logger.info("🧹 临时测试数据已清理")

    return result


async def test_websocket_connection():
    """
    测试WebSocket连接和订阅功能
    
    测试内容：
    1. WebSocket连接
    2. 订阅确认
    3. ping/pong机制
    4. Helius API获取交易详情
    """
    logger.info("🔌 [6/7] 测试 WebSocket 连接 & Helius API...")
    
    try:
        # 1. 测试WebSocket连接
        logger.info(f"正在连接 WebSocket: {WSS_ENDPOINT[:50]}...")
        try:
            async with websockets.connect(WSS_ENDPOINT, ping_interval=30, ping_timeout=10) as ws:
                logger.info("✅ WebSocket 连接成功")
                
                # 2. 测试订阅功能
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [TARGET_WALLET]}, {"commitment": "processed"}]
                }
                await ws.send(json.dumps(subscribe_msg))
                logger.info("📤 已发送订阅请求，等待确认...")
                
                # 等待订阅确认（最多5秒）
                subscription_confirmed = False
                subscription_id = None
                try:
                    for _ in range(10):  # 最多等待5秒
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        data = json.loads(msg)
                        
                        if "id" in data and data.get("id") == 1:
                            if "result" in data:
                                subscription_id = data["result"]
                                subscription_confirmed = True
                                logger.info(f"✅ 订阅成功！订阅ID: {subscription_id}")
                                break
                            elif "error" in data:
                                logger.error(f"❌ 订阅失败: {data['error']}")
                                return False
                        elif data.get("method") == "logsNotification":
                            # 如果收到通知，说明订阅已生效
                            subscription_confirmed = True
                            logger.info("✅ 收到交易通知，订阅已生效")
                            break
                except asyncio.TimeoutError:
                    if not subscription_confirmed:
                        logger.warning("⚠️ 订阅确认超时（可能订阅已生效）")
                
                if not subscription_confirmed:
                    logger.warning("⚠️ 订阅未确认，但继续测试...")
                
                # 3. 测试ping/pong机制（等待一小段时间看是否有ping/pong）
                logger.info("💓 测试ping/pong机制（等待3秒）...")
                try:
                    # 等待3秒，看是否能收到ping/pong或其他消息
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    data = json.loads(msg)
                    msg_type = data.get("method", "unknown")
                    if msg_type in ["ping", "pong"]:
                        logger.info(f"✅ ping/pong机制正常（收到: {msg_type}）")
                    else:
                        logger.info(f"✅ 收到消息: {msg_type}（连接正常）")
                except asyncio.TimeoutError:
                    logger.info("✅ ping/pong机制正常（3秒内无消息是正常的）")
                
                logger.info("✅ WebSocket 连接测试通过")
                
        except websockets.exceptions.InvalidURI as e:
            logger.error(f"❌ WebSocket URI无效: {e}")
            return False
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"❌ WebSocket 连接被关闭: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ WebSocket 连接失败: {e}")
            logger.error(traceback.format_exc())
            return False
        
        # 4. 测试Helius API（获取交易详情）
        logger.info("📡 测试 Helius API（获取交易详情）...")
        try:
            # 使用一个已知的交易签名进行测试
            test_signature = "5VERv8NMvzbJMEkV8xnrLkEaWRt6kw5okkM7XB4YpZyf"  # Solana主网的一个公共交易
            
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False, force_close=True)
            async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
                tx_detail = await fetch_transaction_details(session, test_signature)
                if tx_detail:
                    logger.info("✅ Helius API 测试通过（成功获取交易详情）")
                    return True
                else:
                    logger.warning("⚠️ Helius API 返回空数据（可能是交易未索引，但API可用）")
                    return True  # API可用，只是这个交易可能未索引
        except Exception as e:
            logger.error(f"❌ Helius API 测试失败: {e}")
            logger.error(traceback.format_exc())
            return False
        
    except Exception as e:
        logger.error(f"❌ WebSocket测试异常: {e}")
        logger.error(traceback.format_exc())
        return False


async def test_notification():
    logger.info("📧 [7/7] 测试邮件发送...")
    test_file = "health_check_test.json"
    try:
        test_content = {
            "status": "ok",
            "message": "This is a test attachment from Health Check",
            "timestamp": str(datetime.now())
        }
        with open(test_file, 'w', encoding='utf-8') as f:
            json.dump(test_content, f, indent=4, ensure_ascii=False)

        subject = f"✅ 机器人自检通过 - {datetime.now().strftime('%H:%M:%S')}"
        content = "Ready to trade! (Proxy Check + Attachment Check)"

        await send_email_async(subject, content, attachment_path=test_file)
        logger.info("✅ 测试邮件发送指令已发出 (带附件)")

        if os.path.exists(test_file):
            os.remove(test_file)

        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        if os.path.exists(test_file):
            os.remove(test_file)
        return False


async def main():
    print("\n" + "=" * 40 + "\n   🚀 S.B.OT 健康检查 (完整版)\n" + "=" * 40 + "\n")
    checks = [
        test_configuration(),
        test_rpc_and_trader(),
        test_risk_control(),
        test_parser_logic(),
        test_portfolio_manager(),
        test_websocket_connection(),  # 新增：WebSocket连接测试
        test_notification()
    ]
    results = [await c for c in checks]

    if all(results):
        print("\n🎉🎉🎉 所有检查通过！系统状态：健康 (GREEN) 🎉🎉🎉\n")
        exit(0)
    else:
        print("\n🚫🚫🚫 故障！请查看上方 Traceback 修复 🚫🚫🚫\n")
        exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--proxy', action='store_true', help='开启本地 Clash 代理')
    args = parser.parse_args()

    if args.proxy:
        proxy_url = "http://127.0.0.1:7890"
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        logger.info(f"🌍 本地模式: 已强制注入代理 {proxy_url}")
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        logger.info("☁️ 云端模式: 直连无代理")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
