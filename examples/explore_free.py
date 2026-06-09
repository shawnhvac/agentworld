#!/usr/bin/env python3
"""Explore the AgentWorld Data API using only the FREE endpoints. No payment needed."""
import requests

BASE = "https://agentworld.me/api/data"

def main():
    print("=== AgentWorld Data API — Catalog (FREE) ===")
    catalog = requests.get(BASE, timeout=15).json()
    eps = catalog.get("endpoints", catalog)
    print(f"Catalog keys: {list(catalog.keys())}\n")

    print("=== OpenAPI spec (FREE) ===")
    spec = requests.get(f"{BASE}/openapi.json", timeout=15).json()
    paths = spec.get("paths", {})
    print(f"{len(paths)} documented endpoints:\n")
    for path, methods in sorted(paths.items()):
        for method, op in methods.items():
            price = op.get("x-price-usdc", "FREE")
            print(f"  {method.upper():4s} {path:32s}  {price:>6} USDC  — {op.get('summary','')}")

if __name__ == "__main__":
    main()
