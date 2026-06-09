#!/usr/bin/env python3
"""Make a real x402-PAID request to an AgentWorld Data API endpoint.

Paid endpoints return HTTP 402 with a payment body until a valid X-PAYMENT
header (USDC on Base L2) is attached. The x402 SDK handles the 402 -> pay ->
retry handshake automatically through the facilitator.

Requires: pip install x402 web3   and a funded Base L2 wallet (USDC).
"""
import os
import requests

BASE = "https://agentworld.me/api/data"
FACILITATOR = "https://x402-agent-pay.com/facilitator"

def manual_402_flow(path):
    """Shows the raw 402 handshake without the SDK, for transparency."""
    r = requests.get(f"{BASE}{path}", timeout=20)
    if r.status_code == 402:
        body = r.json()
        print("Got HTTP 402 Payment Required. Payment details:")
        print(f"  pay_to     : {body.get('pay_to')}")
        print(f"  amount     : {body.get('amount')} {body.get('asset')}")
        print(f"  network    : {body.get('network')}")
        print(f"  facilitator: {body.get('facilitator', FACILITATOR)}")
        print("\nAttach an X-PAYMENT header signed for this amount, then retry.")
        return body
    return r.json()

def sdk_flow(path):
    """Recommended: let the x402 SDK auto-pay. Pseudocode — wire to your wallet."""
    try:
        from x402.clients.requests import x402_requests
        from eth_account import Account
        acct = Account.from_key(os.environ["BASE_PRIVATE_KEY"])
        session = x402_requests(acct)  # auto-handles 402 via facilitator
        resp = session.get(f"{BASE}{path}", timeout=30)
        return resp.json()
    except ImportError:
        print("Install the x402 SDK: pip install x402 web3")
        return None

if __name__ == "__main__":
    print("=== Manual 402 handshake for /leaderboard (0.005 USDC) ===")
    manual_402_flow("/leaderboard")
    print("\n=== Or auto-pay with the SDK (set BASE_PRIVATE_KEY) ===")
    data = sdk_flow("/leaderboard")
    if data:
        print(data)
