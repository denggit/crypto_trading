#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : run_health_check.py
@Description: 全系统启动前自检脚本 (最终修复版 - 支持附件测试)
"""
import asyncio
import logging
import os
import sys
import argparse
import aiohttp
import socket
import traceback
import json  # 🔥 新增 import
from datetime import datetime

# --- 导入项目模块 ---
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.settings import (
    API_KEY, TARGET_WALLET, PRIVATE_KEY, RPC_URL,
    EMAIL_SENDER
)
from services.solana.trader import SolanaTrader
from services.risk_control import check_token_liquidity
from services.notification import send_email_async
from services.solana.monitor import parse_tx
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
    logger.info("YZ [5/6] 测试仓位管理...")
    try:
        trader = SolanaTrader(RPC_URL)
        pm = PortfolioManager(trader)
        pm.add_position("TEST_TOKEN_JUP", 1000, 0.1)
        if "TEST_TOKEN_JUP" in pm.portfolio:
            logger.info("✅ 记账功能正常")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ 仓位管理失败: {e}")
        return False


async def test_notification():
    logger.info("📧 [6/6] 测试邮件发送...")
    test_file = "health_check_test.json"
    try:
        # 🔥 1. 创建一个临时的测试文件
        test_content = {
            "status": "ok",
            "message": "This is a test attachment from Health Check",
            "timestamp": str(datetime.now())
        }
        with open(test_file, 'w', encoding='utf-8') as f:
            json.dump(test_content, f, indent=4, ensure_ascii=False)

        # 🔥 2. 发送邮件带附件
        subject = f"✅ 机器人自检通过 - {datetime.now().strftime('%H:%M:%S')}"
        content = "Ready to trade! (Proxy Check + Attachment Check)"

        await send_email_async(subject, content, attachment_path=test_file)
        # await send_email_async(subject, content)
        logger.info("✅ 测试邮件发送指令已发出 (带附件)")

        # 🔥 3. 发完后清理垃圾文件
        if os.path.exists(test_file):
            os.remove(test_file)

        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        # 出错也要尝试清理文件
        if os.path.exists(test_file):
            os.remove(test_file)
        return False


async def main():
    print("\n" + "=" * 40 + "\n   🚀 S.B.OT 健康检查 (双模版)\n" + "=" * 40 + "\n")
    checks = [
        test_configuration(),
        test_rpc_and_trader(),
        test_risk_control(),
        test_parser_logic(),
        test_portfolio_manager(),
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