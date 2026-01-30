#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:57 PM
@File       : debug_network.py
@Description: 
"""
import asyncio
import aiohttp
import os
import time

# 强制配置代理
PROXY = "http://127.0.0.1:7890"
os.environ["HTTP_PROXY"] = PROXY
os.environ["HTTPS_PROXY"] = PROXY

URLS = [
    ("Helius RPC", "https://mainnet.helius-rpc.com/"),
    ("DexScreener", "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"),
    ("Jupiter API",
     "https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=100000000&slippageBps=50")
]


async def test_url(session, name, url):
    print(f"Testing {name} ... ", end="", flush=True)
    start = time.time()
    try:
        # 模拟浏览器的 User-Agent
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}

        # 尝试 1: 自动代理 + SSL 开启
        async with session.get(url, headers=headers, timeout=10) as resp:
            print(f"✅ OK ({resp.status}) - {time.time() - start:.2f}s")
            return

    except Exception as e1:
        # 尝试 2: 强制代理 + SSL 关闭 (模拟您的 Trader 代码)
        try:
            print(f"\n   -> 重试 (强制代理+NoSSL)... ", end="", flush=True)
            async with session.get(url, headers=headers, proxy=PROXY, ssl=False, timeout=10) as resp:
                print(f"✅ OK ({resp.status}) - 救活了!")
        except Exception as e2:
            print(f"❌ 彻底失败: {e2}")


async def main():
    print(f"=== 网络连通性诊断 (代理: {PROXY}) ===")
    # trust_env=True 让 aiohttp 读取系统环境变量
    async with aiohttp.ClientSession(trust_env=True) as session:
        for name, url in URLS:
            await test_url(session, name, url)


if __name__ == "__main__":
    asyncio.run(main())