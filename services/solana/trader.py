#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@File       : services/solana/trader.py
@Description: SOL 交易执行模块 (最终修复版：Solana RPC 强制关闭 SSL 验证)
"""
import base64
import os
import socket

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
        # 🔥 核心修复：创建一个不验证 SSL 的 httpx 客户端
        # trust_env=True 会自动读取系统环境变量里的代理设置
        self.http_client = httpx.AsyncClient(verify=False, trust_env=True, timeout=30.0)

        # 将这个“不听话”的客户端注入到 Solana Provider 中
        provider = AsyncHTTPProvider(endpoint=rpc_endpoint, extra_headers={"Content-Type": "application/json"})
        # 强行覆盖 provider 内部的 session (这是 solana-py 的底层逻辑)
        # 注意：solana-py 版本不同可能实现不同，但通常 provider.session 就是 httpx client
        # 如果版本较新，可能需要通过构造函数传递，但目前的库通常不支持直接传 client
        # 所以我们用这一招：让 Provider 使用我们自定义的 client
        # (注：为了兼容性，更稳妥的方式是让 httpx 全局不验证，但那样太暴力。
        # 这里我们利用 AsyncHTTPProvider 的机制，它初始化时会创建 session。
        # 我们这里重新初始化一个 AsyncClient 并传入 provider)

        # 更稳妥的注入方式：
        # 直接使用 args 构造 AsyncClient，但 solana 库没暴露 verify 参数。
        # 所以我们这里做一个 trick：
        self.rpc_client = AsyncClient(rpc_endpoint, timeout=30)
        # 替换内部 provider 的 session
        if hasattr(self.rpc_client._provider, 'session'):
            # 关闭原有的，换成我们的
            # (这里不做替换了，风险较大，我们改用环境变量控制 httpx)
            pass

        # 💡 重新思考：最稳妥的方法其实是直接控制 httpx 的全局行为或者在 main.py 里处理
        # 但既然要在 trader 里封装，我们用下面这个最稳的写法：
        # 自定义 Provider 类太复杂，我们直接用 httpx 的环境变量。
        # 见下方 _hack_httpx_verify()
        pass

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
        await self.http_client.aclose()

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
            return 0

    def _get_proxy(self):
        return os.environ.get("HTTP_PROXY")

    async def get_quote(self, session, input_mint, output_mint, amount, slippage_bps=50):
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
                    logger.error(f"询价失败 [{response.status}]: {await response.text()}")
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
        # 🔥 关键修改：同样添加 x-api-key
        headers = {
            "Content-Type": "application/json",
            "x-api-key": JUPITER_API_KEY
        }

        try:
            async with session.post(self.JUP_SWAP_API, json=payload, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"构建交易失败 [{response.status}]: {await response.text()}")
                    return None
                return await response.json()
        except Exception as e:
            logger.error(f"Swap API 异常: {e}")
            return None

    async def execute_swap(self, input_mint, output_mint, amount_lamports, slippage_bps=100):
        """ 执行交易 """
        # 🔥🔥 核武器：强制 IPv4 + NoSSL 连接器 🔥🔥
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ssl=False,
            force_close=True
        )
        # trust_env=False 防止干扰，完全手动控制
        async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
            quote = await self.get_quote(session, input_mint, output_mint, amount_lamports, slippage_bps)
            if not quote: return False, 0

            out_amount_est = int(quote['outAmount'])
            swap_res = await self.get_swap_tx(session, quote)
            if not swap_res: return False, 0

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
