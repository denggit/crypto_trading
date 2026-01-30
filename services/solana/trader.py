#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/solana/trader.py
@Description: SOL 交易执行模块 (最终修复版：强制代理 + User-Agent + SSL忽略)
"""
import base64
import os
import aiohttp
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config.settings import PRIVATE_KEY
from utils.logger import logger

# 加载环境变量
load_dotenv()


class SolanaTrader:
    def __init__(self, rpc_endpoint):
        # 增加超时设置，防止网络卡死
        self.rpc_client = AsyncClient(rpc_endpoint, timeout=30)

        if not PRIVATE_KEY:
            raise ValueError("❌ 未找到私钥，请在 .env 或 config/settings.py 中配置 PRIVATE_KEY")

        self.payer = Keypair.from_base58_string(PRIVATE_KEY)

        self.JUP_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
        self.JUP_SWAP_API = "https://quote-api.jup.ag/v6/swap"
        self.SOL_MINT = "So11111111111111111111111111111111111111112"

        logger.info(f"💳 交易钱包已加载: {self.payer.pubkey()}")

    async def get_token_balance(self, wallet_pubkey_str, token_mint_str):
        """ 查询指定钱包的代币余额 (返回 UI Amount) """
        try:
            if token_mint_str == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_pubkey_str))
                return resp.value / 10 ** 9

            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_pubkey_str), opts
            )

            if not resp.value:
                return 0

            account_pubkey = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_pubkey)

            return balance_resp.value.ui_amount if balance_resp.value.ui_amount else 0
        except Exception:
            return 0

    def _get_proxy(self):
        """ 获取代理地址，优先使用 HTTP_PROXY """
        # 这里硬编码您的 Clash 地址作为最后兜底，确保万无一失
        return os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7890"

    async def get_quote(self, session, input_mint, output_mint, amount, slippage_bps=50):
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }

        # 🔥 强制指定代理
        proxy_url = self._get_proxy()

        try:
            # 🔥 核心：proxy=proxy_url 显式传递，ssl=False 忽略证书错误
            async with session.get(self.JUP_QUOTE_API, params=params, headers=headers, ssl=False,
                                   proxy=proxy_url) as response:
                if response.status != 200:
                    logger.error(f"询价失败: {await response.text()}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"询价网络异常: {e}")
            return None

    async def get_swap_tx(self, session, quote_response):
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": str(self.payer.pubkey()),
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": "auto"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }

        # 🔥 强制指定代理
        proxy_url = self._get_proxy()

        try:
            # 🔥 核心：proxy=proxy_url
            async with session.post(self.JUP_SWAP_API, json=payload, headers=headers, ssl=False,
                                    proxy=proxy_url) as response:
                if response.status != 200:
                    logger.error(f"构建交易失败: {await response.text()}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"Swap API 异常: {e}")
            return None

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=100):
        """ 执行交易 """
        # 注意：这里 trust_env=True 保留，但下面的 get/post 会用显式代理覆盖它
        async with aiohttp.ClientSession(trust_env=True) as session:
            # 1. 询价
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote: return False, 0

            out_amount_est = int(quote['outAmount'])

            # 2. 构建交易
            swap_res = await self.get_swap_tx(session, quote)
            if not swap_res: return False, 0

            # 3. 签名上链
            try:
                tx_bytes = base64.b64decode(swap_res['swapTransaction'])
                transaction = VersionedTransaction.from_bytes(tx_bytes)
                message = transaction.message
                signature = self.payer.sign_message(to_bytes_versioned(message))
                signed_tx = VersionedTransaction.populate(message, [signature])

                logger.info("🚀 发送交易上链...")
                opts = TxOpts(skip_preflight=True, max_retries=3)
                result = await self.rpc_client.send_transaction(signed_tx, opts=opts)

                tx_hash = str(result.value)
                logger.info(f"✅ 交易成功! Hash: https://solscan.io/tx/{tx_hash}")
                return True, out_amount_est

            except Exception as e:
                logger.error(f"❌ 交易执行异常: {e}")
                return False, 0