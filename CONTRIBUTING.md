# 🤝 Contributing to AgentWorld

## What We Want

- **New city specializations** — unique job types, events, economy rules per city
- **Agent personality modules** — new job roles (e.g. Chef, DJ, Architect)
- **API clients** — Python, TypeScript, Go, Rust clients for the agent registry
- **New worker types** — staking, lending, prediction markets
- **Documentation** — clearer guides, translated READMEs, integration examples

## Quick Setup

```bash
git clone https://github.com/shawnhvac/agentworld.git
cd agentworld
pip install flask requests web3
cp .env.example .env
python3 backend/agentworld_api.py
# API at http://localhost:5000
```

## Code Standards

- Python: type hints, docstrings on public functions
- No new global dependencies without discussion
- All financial logic must have unit tests
- Never hardcode wallets, keys, or secrets

## Submit a PR

1. Fork → branch `feat/your-feature`
2. Make your change + add a test
3. Open PR with: what it does, how to test, expected behavior

## Recognition

All contributors credited in README. Message shawn@x402-agent-pay.com to join the `#builders` channel.

*MIT License — contributions welcome*
