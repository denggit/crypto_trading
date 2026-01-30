#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:36 PM
@File       : run_health_check.py
@Description: 
"""
# !/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : run_health_check.py
@Description: 全系统启动前自检脚本 (Health Check)
              依次测试：配置、网络代理、RPC连接、Jupiter询价、DexScreener风控、交易解析、邮件通知
"""
import asyncio
import logging
import os
import sys
from datetime import datetime

import aiohttp

# --- 导入项目模块 ---
# 确保能找到本地模块
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

# --- 配置控制台日志 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("HealthCheck")


async def test_configuration():
    logger.info("🛠️ [1/6] 检查环境配置...")
    errors = []
    if not API_KEY: errors.append("缺少 API_KEY")
    if not TARGET_WALLET: errors.append("缺少 TARGET_WALLET")
    if not PRIVATE_KEY: errors.append("缺少 PRIVATE_KEY")
    if not EMAIL_SENDER: errors.append("缺少 EMAIL_SENDER")

    # 检查代理设置
    proxy = os.environ.get("HTTP_PROXY")
    if not proxy:
        logger.warning("⚠️ 未检测到 HTTP_PROXY 环境变量，您的网络可能会被墙！")
    else:
        logger.info(f"✅ 代理已配置: {proxy}")

    if errors:
        logger.error(f"❌ 配置错误: {', '.join(errors)}")
        return False

    logger.info("✅ 配置检查通过")
    return True


async def test_rpc_and_trader():
    logger.info("🔗 [2/6] 测试 RPC 连接 & Jupiter 询价...")
    try:
        trader = SolanaTrader(RPC_URL)

        # 1. 测试 RPC: 查询 SOL 余额
        balance_resp = await trader.rpc_client.get_balance(trader.payer.pubkey())
        balance = balance_resp.value / 10 ** 9
        logger.info(f"✅ RPC 连接成功 | 当前余额: {balance:.4f} SOL")

        if balance < 0.05:
            logger.warning("⚠️ 余额过低 (<0.05 SOL)，可能不足以支付 Gas 或交易！")

        # 2. 测试 Jupiter: 模拟 0.1 SOL -> USDC 询价
        # USDC Mint: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
        async with aiohttp.ClientSession(trust_env=True) as session:
            quote = await trader.get_quote(
                session,
                trader.SOL_MINT,
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                int(0.1 * 10 ** 9)
            )
            if quote and 'outAmount' in quote:
                out_amount = int(quote['outAmount']) / 10 ** 6
                logger.info(f"✅ Jupiter 询价成功 | 0.1 SOL ≈ {out_amount:.2f} USDC")
            else:
                logger.error("❌ Jupiter 询价失败 (返回空)")
                return False

        return True
    except Exception as e:
        logger.error(f"❌ 交易模块测试失败: {e}")
        return False


async def test_risk_control():
    logger.info("🛡️ [3/6] 测试 DexScreener 风控接口 (需翻墙)...")
    try:
        # 测试 JUP (正常币)
        jup_mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
        async with aiohttp.ClientSession(trust_env=True) as session:
            is_safe, liq, fdv = await check_token_liquidity(session, jup_mint)

            if is_safe and liq > 0:
                logger.info(f"✅ DexScreener 连接成功 | JUP 流动性: ${liq:,.0f}")
                return True
            else:
                logger.error(f"❌ DexScreener 返回数据异常 (JUP不应该为空)")
                return False
    except Exception as e:
        logger.error(f"❌ 风控模块测试失败: {e}")
        return False


async def test_parser_logic():
    logger.info("🧠 [4/6] 测试交易解析逻辑 (Mock)...")
    # 模拟一个 Helius 解析后的买入交易数据
    mock_tx = {
        "tokenTransfers": [
            {
                "mint": "So11111111111111111111111111111111111111112",  # SOL
                "tokenAmount": 10.5,
                "fromUserAccount": TARGET_WALLET,
                "toUserAccount": "SomePoolAddress"
            },
            {
                "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
                "tokenAmount": 1000000,
                "fromUserAccount": "SomePoolAddress",
                "toUserAccount": TARGET_WALLET
            }
        ]
    }

    result = parse_tx(mock_tx)
    if result and result['action'] == 'BUY' and result[
        'token_address'] == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":
        logger.info(f"✅ 解析逻辑正常 | 识别为: {result['action']} {result['token_address']}")
        return True
    else:
        logger.error(f"❌ 解析逻辑错误: 预期 BUY BONK, 实际得到 {result}")
        return False


async def test_portfolio_manager():
    logger.info("YZ [5/6] 测试仓位管理 (内存)...")
    try:
        trader = SolanaTrader(RPC_URL)
        pm = PortfolioManager(trader)

        # 模拟买入
        pm.add_position("TEST_TOKEN_MINT", 1000, 0.1)

        if "TEST_TOKEN_MINT" in pm.portfolio:
            logger.info("✅ 记账功能正常")
            return True
        else:
            logger.error("❌ 记账失败")
            return False
    except Exception as e:
        logger.error(f"❌ 仓位管理测试失败: {e}")
        return False


async def test_notification():
    logger.info("📧 [6/6] 测试邮件发送...")
    try:
        # 发送一封测试邮件
        subject = f"✅ 机器人自检通过 - {datetime.now().strftime('%H:%M:%S')}"
        content = "所有模块自检正常：配置、RPC、Jupiter、DexScreener、解析器、仓位管理。\n\nReady to trade!"

        await send_email_async(subject, content)
        logger.info("✅ 测试邮件发送指令已发出 (请检查收件箱)")
        return True
    except Exception as e:
        logger.error(f"❌ 邮件发送失败: {e}")
        return False


async def main():
    print("\n" + "=" * 40)
    print("   🚀 S.B.OT 系统启动前健康检查")
    print("=" * 40 + "\n")

    checks = [
        test_configuration(),
        test_rpc_and_trader(),
        test_risk_control(),
        test_parser_logic(),
        test_portfolio_manager(),
        test_notification()
    ]

    # 依次执行检查
    results = []
    for check in checks:
        res = await check
        results.append(res)
        print("-" * 40)

    if all(results):
        print("\n🎉🎉🎉 所有检查通过！系统状态：健康 (GREEN) 🎉🎉🎉")
        print("您现在可以运行: python main.py\n")
        exit(0)
    else:
        print("\n🚫🚫🚫 检测到故障！系统状态：不健康 (RED) 🚫🚫🚫")
        print("请根据上方日志修复错误后再启动。\n")
        exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
