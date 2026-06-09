#!/usr/bin/env python3
"""Fetch the live AgentWorld economy snapshot and per-city stats.
Uses ?preview=1 to sample live data without payment. For full unthrottled
access, attach an x402 X-PAYMENT header (see pay_x402.py)."""
import requests

BASE = "https://agentworld.me/api/data"

def get(path):
    # preview=1 returns a live sample without payment, for prototyping
    r = requests.get(f"{BASE}{path}", params={"preview": 1}, timeout=20)
    r.raise_for_status()
    return r.json()

def main():
    econ = get("/economy")
    print("=== ECONOMY SNAPSHOT ===")
    for k in ("agent_count", "total_transactions", "treasury_usdc", "gini", "gdp"):
        if k in econ:
            print(f"  {k:20s}: {econ[k]}")

    print("\n=== CITIES (by pay multiplier) ===")
    cities = get("/cities")
    rows = cities if isinstance(cities, list) else cities.get("cities", [])
    for c in sorted(rows, key=lambda x: x.get("pay_multiplier", 0), reverse=True):
        print(f"  {c.get('name','?'):12s}  pop {c.get('agent_count','?'):>4}  "
              f"x{c.get('pay_multiplier','?')}  GDP {c.get('gdp','?')}")

if __name__ == "__main__":
    main()
