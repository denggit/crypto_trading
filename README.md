Web3 Solana 跟单机器人，跟单聪明钱。

## 本币说明
- **本币为 USDC**：买入、卖出、成本与盈亏均以 USDC 计价与结算。
- 钱包需持有足够 **USDC** 用于跟单买入，以及少量 **SOL** 用于 Gas 与 Jito 小费。

## 关键配置 (.env / config/settings.py)
- `COPY_AMOUNT_USDC`: 每次跟单买入金额（USDC），默认 10
- `MAX_POSITION_USDC`: 单币最大持仓成本（USDC），默认 200