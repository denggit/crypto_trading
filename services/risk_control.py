#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : risk_control.py
@Description: 
"""
# services/risk_control.py
from utils.logger import logger


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
