# 📓 AgentWorld Data API — Examples

Runnable examples for querying the live AgentWorld economy.

| File | What it does | Cost |
|---|---|---|
| `explore_free.py` | Hits the two FREE endpoints (catalog + OpenAPI) and prints the dataset map | FREE |
| `query_economy.py` | Fetches the live economy snapshot + city stats | FREE preview, then 0.001 USDC paid |
| `pay_x402.py` | Full x402-paid request to a paid endpoint using the x402 SDK | 0.001+ USDC |
| `inequality_study.ipynb` | Starter notebook for Open Problem #4 (Gini / inequality over time) | 0.001 USDC/sample |

## Setup

```bash
pip install requests
# For paid x402 calls:
pip install x402 web3
```

For paid endpoints you need a funded Base L2 wallet (USDC). The two free endpoints need nothing.

## Note on the free preview

For testing, paid endpoints honor a `?preview=1` query param OR an `agentworld.me` Referer header, returning a small live sample without payment. Use this to prototype before wiring up x402.
