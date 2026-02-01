#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/solana/trader.py
@Description: SOL 交易执行模块 (最终修复版：Solana RPC 强制关闭 SSL 验证)
"""
import base64
import os
import socket
import traceback

import aiohttp
import httpx  # 🔥 新增依赖
from dotenv import load_dotenv
# 引入 Solana 底层 Provider 以便注入自定义 Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.providers.async_http import AsyncHTTPProvider
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.instructions import close_account, CloseAccountParams
from spl.token.constants import TOKEN_PROGRAM_ID

from config.settings import PRIVATE_KEY, JUPITER_API_KEY
from utils.logger import logger

# 加载环境变量
load_dotenv()


class SolanaTrader:
    def __init__(self, rpc_endpoint):
        # 🔥 修复：移除未使用的 http_client，直接使用 rpc_client
        # 注意：httpx 的 SSL 验证已通过全局 patch_httpx_verify() 关闭
        self.rpc_client = AsyncClient(rpc_endpoint, timeout=30)

        if not PRIVATE_KEY:
            raise ValueError("❌ 未找到私钥，请在 .env 或 config/settings.py 中配置 PRIVATE_KEY")

        self.payer = Keypair.from_base58_string(PRIVATE_KEY)
        # 🔥 修复：使用官方新网关的正确路径 (/swap/v1/...)
        self.JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
        self.JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"
        self.SOL_MINT = "So11111111111111111111111111111111111111112"

        logger.info(f"💳 交易钱包已加载: {self.payer.pubkey()}")

    async def close(self):
        """ 关闭资源 """
        await self.rpc_client.close()

    async def get_token_balance(self, wallet_pubkey_str, token_mint_str):
        """ 查询指定钱包的代币余额 """
        try:
            if token_mint_str == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_pubkey_str))
                return resp.value / 10 ** 9

            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_pubkey_str), opts
            )
            if not resp.value: return 0

            account_pubkey = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_pubkey)
            return balance_resp.value.ui_amount if balance_resp.value.ui_amount else 0
        except Exception:
            return 0

    async def get_token_balance_raw(self, wallet_pubkey_str, token_mint_str):
        """ 🔥 新增：查询余额（返回原始整数，用于精确询价）"""
        try:
            if token_mint_str == self.SOL_MINT:
                resp = await self.rpc_client.get_balance(Pubkey.from_string(wallet_pubkey_str))
                return int(resp.value)

            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(
                Pubkey.from_string(wallet_pubkey_str), opts
            )
            if not resp.value: return 0

            account_pubkey = resp.value[0].pubkey
            balance_resp = await self.rpc_client.get_token_account_balance(account_pubkey)
            # 返回原始整数 (例如 1000000 而不是 1.0)
            return int(balance_resp.value.amount)
        except Exception:
            return None

    def _get_proxy(self):
        return os.environ.get("HTTP_PROXY")

    async def get_quote(self, session, input_mint, output_mint, amount, slippage_bps=50):
        """
        获取交易报价
        
        Args:
            session: aiohttp会话
            input_mint: 输入代币地址
            output_mint: 输出代币地址
            amount: 输入数量（lamports）
            slippage_bps: 滑点（basis points）
            
        Returns:
            quote响应数据，失败返回None
        """
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount)),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        # 🔥 关键修改：添加 x-api-key 请求头
        headers = {
            "Accept": "application/json",
            "x-api-key": JUPITER_API_KEY  # 身份凭证
        }

        try:
            # 这里的 session 依然会复用之前的代理/NoSSL设置，非常完美
            async with session.get(self.JUP_QUOTE_API, params=params, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ 询价API失败 [{response.status}]: {error_text[:500]}")
                    logger.error(f"   输入: {input_mint[:16]}... | 输出: {output_mint[:16]}... | 数量: {amount}")
                    return None
                quote_data = await response.json()
                logger.debug(f"✅ 询价API成功 | 输出数量: {quote_data.get('outAmount', 'N/A')}")
                return quote_data
        except Exception as e:
            logger.error(f"❌ 询价网络异常: {e}")
            logger.error(f"   输入: {input_mint[:16]}... | 输出: {output_mint[:16]}... | 数量: {amount}")
            return None

    async def get_swap_tx(self, session, quote_response):
        """
        构建交易数据
        
        Args:
            session: aiohttp会话
            quote_response: 询价响应数据
            
        Returns:
            swap交易数据，失败返回None
        """
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": str(self.payer.pubkey()),
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": "auto"
        }
        # 🔥 关键修改：同样添加 x-api-key
        headers = {
            "Content-Type": "application/json",
            "x-api-key": JUPITER_API_KEY
        }

        try:
            async with session.post(self.JUP_SWAP_API, json=payload, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ 构建交易API失败 [{response.status}]: {error_text[:500]}")
                    logger.error(f"   用户钱包: {str(self.payer.pubkey())[:16]}...")
                    return None
                swap_data = await response.json()
                logger.debug(f"✅ 构建交易API成功")
                return swap_data
        except Exception as e:
            logger.error(f"❌ Swap API网络异常: {e}")
            logger.error(f"   用户钱包: {str(self.payer.pubkey())[:16]}...")
            return None

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=100):
        """
        执行交易
        
        Args:
            input_mint: 输入代币地址
            output_mint: 输出代币地址
            amount_lamports: 输入数量（lamports）
            slippage_bps: 滑点（basis points）
            
        Returns:
            (success: bool, out_amount: int): 交易是否成功，预计输出数量
        """
        # 🔥🔥 核武器：强制 IPv4 + NoSSL 连接器 🔥🔥
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ssl=False,
            force_close=True
        )
        # trust_env=False 防止干扰，完全手动控制
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            # 步骤1: 询价
            logger.info(f"📊 [步骤1/3] 正在询价: {input_mint[:8]}... -> {output_mint[:8]}...")
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote:
                logger.error(f"❌ [步骤1失败] 询价失败，无法获取报价")
                return False, 0

            out_amount_est = int(quote['outAmount'])
            logger.info(f"✅ [步骤1完成] 询价成功 | 预计获得: {out_amount_est}")

            # 步骤2: 构建交易
            logger.info(f"🔨 [步骤2/3] 正在构建交易...")
            swap_res = await self.get_swap_tx(session, quote)
            if not swap_res:
                logger.error(f"❌ [步骤2失败] 构建交易失败，无法获取交易数据")
                return False, 0

            logger.info(f"✅ [步骤2完成] 交易构建成功")

            # 步骤3: 签名并发送交易
            try:
                logger.info(f"✍️ [步骤3/3] 正在签名交易...")
                tx_bytes = base64.b64decode(swap_res['swapTransaction'])
                transaction = VersionedTransaction.from_bytes(tx_bytes)
                message = transaction.message
                signature = self.payer.sign_message(to_bytes_versioned(message))
                signed_tx = VersionedTransaction.populate(message, [signature])

                logger.info("🚀 [步骤3] 发送交易上链...")
                opts = TxOpts(skip_preflight=True, max_retries=3)
                result = await self.rpc_client.send_transaction(signed_tx, opts=opts)

                tx_hash = str(result.value)
                logger.info(f"✅ [步骤3完成] 交易成功上链! Hash: https://solscan.io/tx/{tx_hash}")
                return True, out_amount_est

            except Exception as e:
                logger.error(f"❌ [步骤3失败] 交易执行异常: {e}")
                logger.error(traceback.format_exc())
                return False, 0

    async def close_token_account(self, token_mint_str):
        """ 🔥 回收租金：关闭空的代币账户，拿回 0.002 SOL """
        try:
            # 1. 查找该代币的 ATA (关联账户)
            opts = TokenAccountOpts(mint=Pubkey.from_string(token_mint_str))
            resp = await self.rpc_client.get_token_accounts_by_owner(self.payer.pubkey(), opts)

            if not resp.value:
                logger.info(f"⚠️ 账户不存在，无需关闭: {token_mint_str}")
                return False

            token_account_pubkey = resp.value[0].pubkey

            # 2. 构建关闭指令 (CloseAccount)
            close_ix = close_account(
                CloseAccountParams(
                    account=token_account_pubkey,
                    dest=self.payer.pubkey(),
                    owner=self.payer.pubkey(),
                    program_id=TOKEN_PROGRAM_ID
                )
            )

            # 3. 构建并发送交易 (Versioned Transaction)
            # 获取最新的 blockhash
            latest_blockhash = await self.rpc_client.get_latest_blockhash()

            # 直接使用 solders 构建 Versioned 交易 (这是 0.30+ 版本的正确写法)
            from solders.transaction import VersionedTransaction
            from solders.message import MessageV0

            msg = MessageV0.try_compile(
                self.payer.pubkey(),
                [close_ix],
                [],
                latest_blockhash.value.blockhash,
            )
            vtx = VersionedTransaction(msg, [self.payer])

            opts = TxOpts(skip_preflight=True)
            await self.rpc_client.send_transaction(vtx, opts=opts)

            logger.info(f"♻️ [房租回收] 成功关闭账户，回血 +0.002 SOL")
            return True

        except Exception as e:
            logger.warning(f"⚠️ 关闭账户失败 (可能由粉尘残留导致): {e}")
            return False


# 🔥 Monkey Patch: 强制修改 httpx 的默认行为，使其不验证 SSL
# 这一步是为了解决 Solana RPC (httpx) 在代理下的报错问题
def patch_httpx_verify():
    original_init = httpx.AsyncClient.__init__

    def new_init(self, *args, **kwargs):
        kwargs['verify'] = False  # 强制关闭验证
        original_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = new_init


patch_httpx_verify()
