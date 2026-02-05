#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : test_jito_buy.py
@Description: Jito 买入测试脚本 - 测试 Jito Bundle 功能
@Usage      : python test_jito_buy.py
"""
import asyncio
import sys
import os
from pathlib import Path

# 添加项目根目录到路径
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from config.settings import RPC_URL, USE_JITO, JITO_TIP_AMOUNT, JITO_BLOCK_ENGINE_URL
from services.solana.trader import SolanaTrader
from utils.logger import logger


async def test_jito_buy():
    """
    测试 Jito 买入功能
    
    测试内容：
    1. 检查 Jito 配置
    2. 使用 0.01 SOL 买入指定代币
    3. 验证交易是否成功
    4. 检查余额变化
    """
    # 测试参数
    TARGET_TOKEN = "9XeizW4yMfUqfGmqF3niSL9zkWwGJQ8EY9EQopWQmM7S"
    BUY_AMOUNT_SOL = 0.01
    SLIPPAGE_BPS = 1000  # 10% 滑点（测试用）
    
    logger.info("=" * 80)
    logger.info("🧪 开始 Jito 买入测试")
    logger.info("=" * 80)
    
    # 1. 检查配置
    logger.info("\n📋 [步骤 1/5] 检查配置...")
    logger.info(f"   Jito 模式: {'✅ 已启用' if USE_JITO else '❌ 已禁用'}")
    if USE_JITO:
        logger.info(f"   Jito 小费: {JITO_TIP_AMOUNT} SOL")
        logger.info(f"   Jito 端点: {JITO_BLOCK_ENGINE_URL}")
    else:
        logger.warning("   ⚠️ 警告: Jito 模式未启用，将使用普通 RPC 模式")
    
    logger.info(f"   RPC 端点: {RPC_URL[:50]}...")
    logger.info(f"   目标代币: {TARGET_TOKEN}")
    logger.info(f"   买入金额: {BUY_AMOUNT_SOL} SOL")
    logger.info(f"   滑点设置: {SLIPPAGE_BPS} bps ({SLIPPAGE_BPS/100}%)")
    
    # 2. 初始化交易器
    logger.info("\n🔧 [步骤 2/5] 初始化交易器...")
    trader = None
    try:
        trader = SolanaTrader(RPC_URL)
        wallet_address = str(trader.payer.pubkey())
        logger.info(f"   ✅ 交易器初始化成功")
        logger.info(f"   钱包地址: {wallet_address}")
    except Exception as e:
        logger.error(f"   ❌ 交易器初始化失败: {e}")
        return False
    
    # 3. 检查初始余额
    logger.info("\n💰 [步骤 3/5] 检查初始余额...")
    try:
        # SOL 余额
        sol_balance = await trader.get_token_balance(wallet_address, trader.SOL_MINT)
        logger.info(f"   SOL 余额: {sol_balance:.4f} SOL")
        
        if sol_balance < BUY_AMOUNT_SOL + JITO_TIP_AMOUNT + 0.01:  # 预留一些 gas
            logger.error(f"   ❌ SOL 余额不足！需要至少 {BUY_AMOUNT_SOL + JITO_TIP_AMOUNT + 0.01:.4f} SOL")
            logger.error(f"   当前余额: {sol_balance:.4f} SOL")
            return False
        
        # 代币余额
        token_balance_before = await trader.get_token_balance(wallet_address, TARGET_TOKEN)
        logger.info(f"   代币余额 (买入前): {token_balance_before:.6f}")
        
    except Exception as e:
        logger.error(f"   ❌ 余额查询失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    # 4. 执行买入交易
    logger.info("\n🚀 [步骤 4/5] 执行买入交易...")
    logger.info(f"   交易模式: {'Jito Bundle' if USE_JITO else '普通 RPC'}")
    
    try:
        amount_lamports = int(BUY_AMOUNT_SOL * 10 ** 9)
        logger.info(f"   买入数量: {amount_lamports} lamports ({BUY_AMOUNT_SOL} SOL)")
        logger.info(f"   ⏳ 正在询价和构建交易（可能需要 10-30 秒）...")
        
        success, est_out = await trader.execute_swap(
            input_mint=trader.SOL_MINT,
            output_mint=TARGET_TOKEN,
            amount_lamports=amount_lamports,
            slippage_bps=SLIPPAGE_BPS
        )
        
        if success:
            logger.info(f"   ✅ 交易提交成功！")
            logger.info(f"   预计获得代币: {est_out} (原始单位)")
            
            # 等待交易确认
            logger.info(f"   ⏳ 等待交易确认...")
            await asyncio.sleep(5)  # 等待 5 秒让交易上链
            
        else:
            logger.error(f"   ❌ 交易提交失败！")
            return False
            
    except Exception as e:
        logger.error(f"   ❌ 交易执行异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    # 5. 验证交易结果
    logger.info("\n🔍 [步骤 5/5] 验证交易结果...")
    try:
        # 再次检查余额
        await asyncio.sleep(3)  # 再等 3 秒确保链上数据同步
        
        token_balance_after = await trader.get_token_balance(wallet_address, TARGET_TOKEN)
        logger.info(f"   代币余额 (买入后): {token_balance_after:.6f}")
        
        balance_change = token_balance_after - token_balance_before
        logger.info(f"   余额变化: {balance_change:+.6f}")
        
        if balance_change > 0:
            logger.info(f"   ✅ 买入成功！获得 {balance_change:.6f} 个代币")
            
            # 计算实际成本
            final_sol_balance = await trader.get_token_balance(wallet_address, trader.SOL_MINT)
            sol_spent = sol_balance - final_sol_balance
            logger.info(f"   实际花费: {sol_spent:.6f} SOL")
            
            if USE_JITO:
                logger.info(f"   (包含 Jito 小费: {JITO_TIP_AMOUNT} SOL)")
            
            return True
        else:
            logger.warning(f"   ⚠️ 余额未变化，交易可能未成功上链")
            logger.warning(f"   建议: 检查 Solscan 查看交易状态")
            return False
            
    except Exception as e:
        logger.error(f"   ❌ 余额验证失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False
    
    finally:
        # 清理资源
        if trader:
            await trader.close()
            logger.info("\n🔒 交易器已关闭")


async def main():
    """主函数"""
    try:
        success = await test_jito_buy()
        
        logger.info("\n" + "=" * 80)
        if success:
            logger.info("✅ 测试完成：交易成功！")
        else:
            logger.error("❌ 测试完成：交易失败或未确认")
        logger.info("=" * 80)
        
        return 0 if success else 1
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️ 测试被用户中断")
        return 1
    except Exception as e:
        logger.error(f"\n💥 测试异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
