#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 1/30/26 1:20 PM
@File       : solana_trader.py
@Description: SOL 交易执行模块 (已升级支持余额查询和数量返回)
"""
import base64

import aiohttp
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from config.settings import PRIVATE_KEY
# 引入项目内的统一日志和配置
from utils.logger import logger

# 加载环境变量
load_dotenv()


class SolanaTrader:
    def __init__(self, rpc_endpoint):
        self.rpc_client = AsyncClient(rpc_endpoint)

        if not PRIVATE_KEY:
            raise ValueError("❌ 未找到私钥，请在 .env 或 config/settings.py 中配置 PRIVATE_KEY")

        self.payer = Keypair.from_base58_string(PRIVATE_KEY)

        self.JUP_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
        self.JUP_SWAP_API = "https://quote-api.jup.ag/v6/swap"
        self.SOL_MINT = "So11111111111111111111111111111111111111112"

        logger.info(f"💳 交易钱包已加载: {self.payer.pubkey()}")

    async def get_token_balance(self, wallet_pubkey_str, token_mint_str):
        """ 查询指定钱包的代币余额 (返回人类可读的 UI Amount，例如 10.5) """
        try:
            if token_mint_str == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_pubkey_str))
                # SOL 的精度是 9，这里手动转一下
                return resp.value / 10 ** 9

            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_pubkey_str), opts
            )

            if not resp.value:
                return 0

            account_pubkey = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_pubkey)

            # 使用 ui_amount (float)
            return balance_resp.value.ui_amount if balance_resp.value.ui_amount else 0
        except Exception:
            # 查不到通常意味着没余额
            return 0

    async def get_quote(self, session, input_mint, output_mint, amount, slippage_bps=50):
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        try:
            async with session.get(self.JUP_QUOTE_API, params=params) as response:
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
        try:
            async with session.post(self.JUP_SWAP_API, json=payload) as response:
                if response.status != 200:
                    logger.error(f"构建交易失败: {await response.text()}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"Swap API 异常: {e}")
            return None

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=100):
        """
        执行交易并返回 (是否成功, 预估获得的代币数量)
        """
        # 🔥 trust_env=True 确保走代理
        async with aiohttp.ClientSession(trust_env=True) as session:
            # 1. 询价
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote: return False, 0

            # 记录预估获得的数量 (outAmount) 用于记账
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
