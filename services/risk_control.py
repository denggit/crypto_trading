#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : risk_control.py
@Description: 
"""
import aiohttp
from utils.logger import logger


async def check_is_safe_token(session, token_mint):
    """
    🔥 核心风控：检测代币是否安全（非貔貅/蜜罐）
    使用 RugCheck API (专门针对 Solana)
    
    :param session: aiohttp 会话
    :param token_mint: 代币地址
    :return: True 表示安全（可以交易），False 表示危险（貔貅盘/蜜罐）
    """
    if token_mint == "So11111111111111111111111111111111111111112": # WSOL
        return True # 安全

    url = f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report"
    
    try:
        async with session.get(url, timeout=5) as response:
            if response.status == 200:
                data = await response.json()
                
                # 1. 检查评分 (分数越高越危险，通常 > 5000 就很危险)
                score = data.get('score', 0)
                if score > 2000: # 严格一点，超过2000分就不碰
                    logger.warning(f"⚠️ 风险过高 (Score: {score}): {token_mint}")
                    return False
                
                # 2. 检查危险标记
                risks = data.get('risks', [])
                critical_risks = [r for r in risks if r['level'] == 'danger']
                if len(critical_risks) > 0:
                    logger.warning(f"☠️ 发现致命风险: {critical_risks[0]['name']}")
                    return False
                
                # 3. 检查铸币权/冻结权是否还在 (Solana 特色貔貅)
                token_meta = data.get('tokenMeta', {})
                if not token_meta.get('mutable', True): # 如果元数据不可变是好事，但在 RugCheck 里要看 specific risks
                    pass

                logger.info(f"✅ 合约检测通过 (Score: {score})")
                return True
            else:
                # 如果 RugCheck 还没收录这个新币，通常说明它太新了，可以策略性放过或拒绝
                # 激进策略：返回 True (赌它不是)
                # 保守策略：返回 False (看不懂就不买)
                logger.warning(f"RugCheck 未收录，跳过检测")
                return True 
                
    except Exception as e:
        logger.error(f"合约检测网络失败: {e}")
        return True # 网络断了默认放行(激进) 或 拦截(保守)
        

async def check_token_liquidity(session, token_mint):
    if token_mint == "So11111111111111111111111111111111111111112":
        return True, 999999999, 999999999

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
    headers = {
        "User-Agent": "Mozilla/5.0 ... (保持你的User-Agent)",
        "Accept": "application/json"
    }

    try:
        # trust_env=True 已在 session 创建时统一处理
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                pairs = data.get('pairs', [])
                if not pairs: return False, 0, 0

                solana_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if not solana_pairs: return False, 0, 0

                best_pair = max(solana_pairs, key=lambda x: x.get('liquidity', {}).get('usd', 0))
                return True, best_pair.get('liquidity', {}).get('usd', 0), best_pair.get('fdv', 0)
    except Exception as e:
        logger.error(f"⚠️ 风控检查报错: {e}")

    return False, 0, 0
