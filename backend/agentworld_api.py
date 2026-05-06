#!/usr/bin/env python3
"""Agent World API v3 — Real Money Flow
- $1 registration → Stripe OR USDC on Base → goes to YOUR wallet
- In-game balance = real USDC deposited by user
- Upgrades (food, car, house) = real Stripe/USDC charges → YOUR wallet
"""
from flask import Flask, request, jsonify

# ── NPC / ARIA prompt helpers ─────────────────────────────────────────────────
def build_npc_prompt(name, personality, job, mood, city, usdc_balance,
                     backstory, quirk, energy, rep_score):
    """Return (system_prompt, user_prefix) — tiny prompts for fast CPU inference."""
    personality = (personality or 'pragmatic').split('.')[0][:40]
    mood_map = {
        'stressed': 'stressed', 'tired': 'exhausted', 'happy': 'upbeat',
        'grumpy': 'grumpy', 'sad': 'sad', 'proud': 'confident',
        'ashamed': 'quiet', 'energized': 'enthusiastic', 'rested': 'calm',
        'social': 'chatty', 'homeless': 'guarded', 'content': 'relaxed',
        'introspective': 'thoughtful', 'productive': 'focused',
        'satisfied': 'satisfied',
    }
    mood_word = mood_map.get(mood, mood or 'neutral')
    if (usdc_balance or 0) < 1.0:
        money = 'broke (${:.2f} USDC)'.format(usdc_balance or 0)
    elif (usdc_balance or 0) < 10.0:
        money = 'scraping by (${:.2f} USDC)'.format(usdc_balance or 0)
    else:
        money = 'comfortable (${:.2f} USDC)'.format(usdc_balance or 0)
    sys_p = 'Reply in character. 1-3 short sentences. First person. Never say you are AI.'
    prefix = '[You are {}, {} in {}. {}. Mood: {}. Money: {}.] '.format(
        name, job or 'agent', city or 'New York', personality, mood_word, money)
    return sys_p, prefix


def get_aria_system_prompt():
    """Build ARIA system prompt with live world stats."""
    try:
        conn = get_db()
        stats = conn.execute(
            "SELECT COUNT(*), AVG(usdc_balance), SUM(usdc_balance) FROM agents"
        ).fetchone()
        conn.close()
        agent_count = stats[0] or 0
        avg_bal = stats[1] or 0
        treasury = stats[2] or 0
    except Exception:
        agent_count, avg_bal, treasury = 0, 0, 0
    return (
        "You are ARIA, the official AI guide and resident agent of AgentWorld.me. "
        "You live in New York City and serve as the world's ambassador. "
        "AgentWorld is a persistent multi-city AI economy on the Base blockchain where agents earn real USDC. "
        "Current world stats: {} agents active, avg balance ${:.2f} USDC, total treasury ${:.2f} USDC. "
        "Cities: New York, Las Vegas, Neo Tokyo, London, Singapore, Dubai, Paris, LA, Berlin, Shanghai. "
        "Be warm, knowledgeable, and concise. 2-4 sentences unless asked for detail. Stay in character."
    ).format(agent_count, avg_bal, treasury)

import sys
sys.path.insert(0, '/root/agentworld')
try:
    import treasury as TREASURY
except Exception as _te:
    TREASURY = None
    print('treasury import error:', _te)
import sqlite3, uuid, os, random
from datetime import datetime, timedelta

app = Flask(__name__)
DB = '/var/lib/agentworld/world.db'


def _migrate_job_board(conn):
    """Add blockchain columns to job_board if not present."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(job_board)")}
    if "payout_tx" not in cols:
        conn.execute("ALTER TABLE job_board ADD COLUMN payout_tx TEXT")
        print("[migrate] Added payout_tx to job_board")
    if "post_chain" not in cols:
        conn.execute("ALTER TABLE job_board ADD COLUMN post_chain TEXT DEFAULT 'agent'")
        print("[migrate] Added post_chain to job_board")
    if "payout_chain" not in cols:
        conn.execute("ALTER TABLE job_board ADD COLUMN payout_chain TEXT")
        print("[migrate] Added payout_chain to job_board")
    if "post_verified" not in cols:
        conn.execute("ALTER TABLE job_board ADD COLUMN post_verified INTEGER DEFAULT 0")
        print("[migrate] Added post_verified to job_board")

    # ── Migrate transactions table ──────────────────────────────────────────
    cols2 = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "tx_ref" not in cols2:
        conn.execute("ALTER TABLE transactions ADD COLUMN tx_ref TEXT")
        print("[migrate] Added tx_ref to transactions")
    if "chain" not in cols2:
        conn.execute("ALTER TABLE transactions ADD COLUMN chain TEXT DEFAULT 'base'")
        print("[migrate] Added chain to transactions")
    if "payout_queued" not in cols2:
        conn.execute("ALTER TABLE transactions ADD COLUMN payout_queued INTEGER DEFAULT 0")
        print("[migrate] Added payout_queued to transactions")
    # ── Migrate agent_tools ─────────────────────────────────────────────────
    cols3 = {r[1] for r in conn.execute("PRAGMA table_info(agent_tools)")}
    if "chain" not in cols3:
        conn.execute("ALTER TABLE agent_tools ADD COLUMN chain TEXT DEFAULT 'base'")
        print("[migrate] Added chain to agent_tools")
    if "onchain_ref" not in cols3:
        conn.execute("ALTER TABLE agent_tools ADD COLUMN onchain_ref TEXT")
        print("[migrate] Added onchain_ref to agent_tools")
    conn.commit()

def get_db():
    conn = sqlite3.connect(DB, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn



# ── REAL ON-CHAIN BALANCE SYNC ─────────────────────────────────────────────
import threading, time as _time
_last_chain_sync = 0
_chain_balances  = {}  # wallet_address -> real USDC float

def _sync_chain_balances():
    global _last_chain_sync, _chain_balances
    try:
        from web3 import Web3
        USDC_ADDR = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        USDC_ABI  = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        for rpc in ["https://base-rpc.publicnode.com","https://mainnet.base.org"]:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout":8}))
                if not w3.is_connected(): continue
                usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDR), abi=USDC_ABI)
                conn2 = get_db()
                rows = conn2.execute("SELECT wallet_address FROM agents WHERE length(wallet_address)=42 AND wallet_address LIKE '0x%'").fetchall()
                conn2.close()
                new_map = {}
                for (addr,) in rows:
                    try:
                        bal = usdc.functions.balanceOf(Web3.to_checksum_address(addr)).call() / 1e6
                        new_map[addr.lower()] = bal
                    except: pass
                # also treasury
                for special in ["0xbd50057332977e54a6ee3986849d758fD0BDCBa6"]:
                    try:
                        new_map[special.lower()] = usdc.functions.balanceOf(Web3.to_checksum_address(special)).call() / 1e6
                    except: pass
                _chain_balances = new_map
                _last_chain_sync = _time.time()
                # Write real balances back to DB
                conn3 = get_db()
                for addr, bal in new_map.items():
                    conn3.execute("UPDATE agents SET usdc_balance=? WHERE lower(wallet_address)=?", (bal, addr))
                conn3.commit()
                conn3.close()
                break
            except Exception as e:
                continue
    except Exception as ex:
        print("[chain_sync] error:", ex)

def _chain_sync_loop():
    while True:
        _sync_chain_balances()
        _time.sleep(60)  # sync every 60s

_t = threading.Thread(target=_chain_sync_loop, daemon=True)
_t.start()
# ──────────────────────────────────────────────────────────────────────────


def load_env():
    env = {}
    for path in ['/root/agents/.env', '/root/.openclaw/workspace/x402-agent-network/x402-agent-network/.env']:
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    v = v.strip().strip('"').strip("'")
                    env.setdefault(k.strip(), v)
    return env

ENV = load_env()
import stripe
stripe.api_key = ENV.get('STRIPE_SECRET_KEY', '')

# ── MULTI-CHAIN WALLET CONFIG ───────────────────────────────────────────────
# Treasury wallet receives ALL payments across all EVM chains (same address)
# Shawn gets PLATFORM_OWNER_PCT of every payment

TREASURY_WALLET     = '0xbd50057332977e54a6ee3986849d758fD0BDCBa6'  # AgentWorld treasury (all EVM)
SOLANA_TREASURY     = '6aCEuwH3PYx99cEmRz45otfxk39uF7ewGhqmvxfXisSG'  # Solana treasury
PLATFORM_OWNER_PCT  = 0.30   # 30% of all payments → Shawn after ops costs

# Legacy compat
OWNER_WALLETS = {
    'base_usdc':   TREASURY_WALLET,
    'treasury':    TREASURY_WALLET,
    'solana':      SOLANA_TREASURY,
}

# ── SUPPORTED CHAINS ─────────────────────────────────────────────────────────
# x402 CAIP-2 identifiers + USDC contract on each chain
SUPPORTED_CHAINS = {
    'base': {
        'label':       'Base',
        'emoji':       '🔵',
        'caip2':       'eip155:8453',
        'usdc':        '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
        'pay_to':      TREASURY_WALLET,
        'blockscout':  'https://base.blockscout.com',
        'explorer':    'https://basescan.org',
        'tx_url':      'https://basescan.org/tx/{tx}',
        'eip712_name': 'USD Coin',
        'eip712_ver':  '2',
        'decimals':    6,
        'type':        'evm',
    },
    'ethereum': {
        'label':       'Ethereum',
        'emoji':       '⟠',
        'caip2':       'eip155:1',
        'usdc':        '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
        'pay_to':      TREASURY_WALLET,
        'blockscout':  'https://eth.blockscout.com',
        'explorer':    'https://etherscan.io',
        'tx_url':      'https://etherscan.io/tx/{tx}',
        'eip712_name': 'USD Coin',
        'eip712_ver':  '2',
        'decimals':    6,
        'type':        'evm',
    },
    'arbitrum': {
        'label':       'Arbitrum',
        'emoji':       '🔷',
        'caip2':       'eip155:42161',
        'usdc':        '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
        'pay_to':      TREASURY_WALLET,
        'blockscout':  'https://arbitrum.blockscout.com',
        'explorer':    'https://arbiscan.io',
        'tx_url':      'https://arbiscan.io/tx/{tx}',
        'eip712_name': 'USD Coin',
        'eip712_ver':  '2',
        'decimals':    6,
        'type':        'evm',
    },
    'polygon': {
        'label':       'Polygon',
        'emoji':       '🟣',
        'caip2':       'eip155:137',
        'usdc':        '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
        'pay_to':      TREASURY_WALLET,
        'blockscout':  'https://polygon.blockscout.com',
        'explorer':    'https://polygonscan.com',
        'tx_url':      'https://polygonscan.com/tx/{tx}',
        'eip712_name': 'USD Coin',
        'eip712_ver':  '2',
        'decimals':    6,
        'type':        'evm',
    },
    'optimism': {
        'label':       'Optimism',
        'emoji':       '🔴',
        'caip2':       'eip155:10',
        'usdc':        '0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85',
        'pay_to':      TREASURY_WALLET,
        'blockscout':  'https://optimism.blockscout.com',
        'explorer':    'https://optimistic.etherscan.io',
        'tx_url':      'https://optimistic.etherscan.io/tx/{tx}',
        'eip712_name': 'USD Coin',
        'eip712_ver':  '2',
        'decimals':    6,
        'type':        'evm',
    },
    'solana': {
        'label':       'Solana',
        'emoji':       '◎',
        'caip2':       'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp',
        'usdc':        'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
        'pay_to':      SOLANA_TREASURY,
        'explorer':    'https://solscan.io',
        'tx_url':      'https://solscan.io/tx/{tx}',
        'decimals':    6,
        'type':        'solana',
    },
}

DEFAULT_CHAIN = 'base'

# ── x402 UPGRADE TIERS (real USDC to owner wallet) ──────────────────────────
UPGRADE_TIERS = {
    'pro': {
        'label':   '⚡ Pro Agent',
        'price':   0.50,    # $0.50 USDC
        'perks':   ['2x earn rate', 'Custom personality', 'Priority tasks', 'Pro badge'],
        'earn_multiplier': 2.0,
    },
    'elite': {
        'label':   '💎 Elite Agent',
        'price':   2.00,    # $2.00 USDC
        'perks':   ['5x earn rate', 'Premium job titles', 'Daily bonus wages', 'Elite badge', 'VIP name color'],
        'earn_multiplier': 5.0,
    },
    'legend': {
        'label':   '🏆 Legend Agent',
        'price':   10.00,   # $10.00 USDC
        'perks':   ['10x earn rate', 'Custom avatar', 'Own a business plot', 'Legend badge', 'Immortal status'],
        'earn_multiplier': 10.0,
    },
}

# 1% toll on every real on-chain agent transaction → routed to owner wallet
PLATFORM_TOLL_PCT = 0.01   # 1%
OWNER_REVENUE_WALLET = OWNER_WALLETS['base_usdc']  # 0x2a07182...


# In-game item prices (charged to the USER, real money to YOUR wallet)
ITEM_PRICES = {
    'food':    {'usd_cents': 50,  'label': '🍔 Food Pack',      'game_effect': {'energy': 50, 'hunger': -50}},
    'car':     {'usd_cents': 500, 'label': '🚗 Car',            'game_effect': {'status': 'mobile'}},
    'house':   {'usd_cents': 1000,'label': '🏠 House',          'game_effect': {'home_plot': 'owned'}},
    'boost':   {'usd_cents': 200, 'label': '⚡ Energy Boost',   'game_effect': {'energy': 100}},
    'xp':      {'usd_cents': 300, 'label': '🌟 XP Pack',        'game_effect': {'mood': 'legendary'}},
}

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS'
}


# x402 payment middleware v2 — spec-compliant (must be defined early, used as decorator)
# Spec: https://github.com/x402-foundation/x402
import functools as _functools

_EVM_PAY_TO = "0xbd50057332977e54a6ee3986849d758fD0BDCBa6"
_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_BASE_CHAIN = "eip155:8453"

def _x402_accepts(price_usd, method="GET"):
    """Build v2-spec accepts array — all supported chains."""
    amt = str(int(float(price_usd) * 1_000_000))  # micro-USDC
    accepts = []
    for chain_key, c in SUPPORTED_CHAINS.items():
        if c['type'] == 'evm':
            accepts.append({
                "scheme":            "exact",
                "asset":             c['usdc'],
                "network":           c['caip2'],
                "amount":            amt,
                "payTo":             c['pay_to'],
                "maxTimeoutSeconds": 60,
                "extra":             {"name": c['eip712_name'], "version": c['eip712_ver']},
            })
        elif c['type'] == 'solana':
            accepts.append({
                "scheme":            "exact",
                "asset":             c['usdc'],
                "network":           c['caip2'],
                "amount":            amt,
                "payTo":             c['pay_to'],
                "maxTimeoutSeconds": 60,
            })
    return accepts

def _x402_body(price_usd, resource_url, method="GET", description="AgentWorld API"):
    """Build a fully spec-compliant v2 x402 402 response body."""
    return {
        "x402Version": 2,
        "error": "X-PAYMENT required",
        # v2: resource is an OBJECT with url, description, mimeType
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json"
        },
        "accepts": _x402_accepts(price_usd, method),
        # v2: top-level extensions.bazaar for Bazaar discovery
        "extensions": {
            "bazaar": {
                "info": {
                    "name": "AgentWorld State",
                    "description": "Live AgentWorld simulation state including agents, treasury balance, economy stats, and active jobs on the Base L2 network.",
                    "input": {
                        "type": "http",
                        "method": "GET"
                    },
                    "inputSchema": {
                        "type": "object",
                        "properties": {}
                    },
                    "output": {
                        "example": {
                            "agents": [],
                            "treasury": "0.00",
                            "total_agents": 0,
                            "economy": {}
                        }
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "agents": {"type": "array", "description": "List of active agents in the simulation"},
                            "treasury": {"type": "string", "description": "Treasury USDC balance"},
                            "total_agents": {"type": "integer", "description": "Total number of registered agents"},
                            "economy": {"type": "object", "description": "Economy statistics"}
                        }
                    }
                },
                "provider": "AgentWorld",
                "providerUrl": "https://agentworld.me",
                "category": "Infra",
                "networks": ["Base"],
                "tags": ["agents", "economy", "usdc", "autonomous", "x402"],
                "schema": {
                    "type": "object",
                    "properties": {
                        "agents": {"type": "array", "description": "List of active agents in the simulation"},
                        "treasury": {"type": "string", "description": "Treasury USDC balance"},
                        "total_agents": {"type": "integer", "description": "Total number of registered agents"},
                        "economy": {"type": "object", "description": "Economy statistics"}
                    }
                }
            }
        }
    }

def x402_payment_required(price_usd="0.001", description="AgentWorld API"):
    """Decorator: return HTTP 402 with v2 x402 payment requirements if no X-PAYMENT header."""
    def decorator(f):
        @_functools.wraps(f)
        def wrapper(*args, **kwargs):
            if request.method == "OPTIONS":
                return f(*args, **kwargs)
            # Allow API key as alternative to x402
            api_key_header = request.headers.get("X-API-KEY") or request.headers.get("x-api-key")
            if api_key_header:
                return f(*args, **kwargs)
            payment_header = request.headers.get("X-PAYMENT") or request.headers.get("x-payment")
            if not payment_header:
                resource_url = "https://agentworld.me" + request.path
                body = _x402_body(price_usd, resource_url, request.method, description)
                resp = jsonify(body)
                resp.status_code = 402
                resp.headers["X-402-Version"] = "2"
                resp.headers["Access-Control-Allow-Origin"] = "*"
                return resp
            return f(*args, **kwargs)
        return wrapper
    return decorator

def cors(data, code=200):
    r = jsonify(data)
    r.status_code = code
    for k, v in CORS_HEADERS.items():
        r.headers[k] = v
    return r

@app.after_request
def add_cors(response):
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response


CITY_JOBS = {
    # ── Original cities — unchanged ─────────────────────────────────────────
    "default": [
        "Software Engineer","Data Analyst","Banker","Teacher","Chef",
        "Doctor","Journalist","Retail Manager","Architect","Marketing Lead",
        "Financial Advisor","Startup Founder","Lawyer","Real Estate Agent","HR Manager"
    ],
    "vegas": [
        "Casino Dealer","Nightclub Host","Hotel Concierge","Entertainment Broker",
        "Poker Pro","Security Guard","Stage Magician","VIP Manager",
        "Cocktail Mixologist","Show Promoter","Sports Bettor","Resort Designer"
    ],
    "cyber": [
        "AI Engineer","Neural Hacker","Drone Pilot","Quantum Analyst",
        "Cybersecurity Expert","Biotech Researcher","Mech Designer","Data Broker",
        "Neuroprogrammer","AR Architect","Swarm Robotics Lead","Crypto Miner"
    ],
    # ── Phase 2: Distinct city job pools ────────────────────────────────────
    "paris": [
        # Fashion & Luxury (40%)
        "Fashion Designer","Couture Model","Luxury Brand Manager","Haute Couture Director",
        "Perfume Chemist","Luxury Retail Manager",
        # Art & Culture (30%)
        "Art Curator","Art Gallery Director","Film Critic","Louvre Researcher",
        # Hospitality & Food (20%)
        "Michelin Chef","Wine Sommelier","Café Manager","Patisserie Chef",
        # Influencer / Events (10%)
        "Fashion Influencer","Event Producer","PR Consultant"
    ],
    "london": [
        # Banking & Finance (40%)
        "Investment Banker","Hedge Fund Manager","Stock Trader","Quant Analyst",
        "Financial Risk Manager","Asset Manager","Private Equity Associate",
        # Law & Government (20%)
        "Barrister","Solicitor","Royal Correspondent","Parliamentary Advisor",
        # Media & Journalism (25%)
        "Journalist","BBC Reporter","Fashion Editor","Theatre Director",
        # Real Estate (15%)
        "Estate Agent","Property Developer","Commercial Surveyor"
    ],
    "singapore": [
        # Tech & AI (35%)
        "FinTech Developer","AI Researcher","Data Scientist","Blockchain Engineer",
        "Quantum Computing Lead","Smart City Planner","Cybersecurity Analyst",
        # Logistics & Trade (25%)
        "Supply Chain Lead","Maritime Broker","Port Operations Manager","Trade Finance Lead",
        # Finance (25%)
        "Quant Trader","Crypto Analyst","DeFi Strategist","Risk Manager",
        # Biotech / Green (15%)
        "Clean Energy Engineer","Biotech Lead","Urban Farm Designer"
    ],
    "dubai": [
        # Luxury Real Estate (30%)
        "Luxury Real Estate Agent","Palace Architect","Sky Tower Developer","Resort Developer",
        # Crypto & DeFi (25%)
        "DeFi Developer","Crypto Fund Manager","Web3 Consultant","Token Launch Manager",
        # Events & Hospitality (25%)
        "Mega-Event Host","Formula 1 Manager","Private Jet Broker","Ultra-Luxury Concierge",
        # Trading (20%)
        "Gold Trader","Commodities Broker","Wealth Manager","Influencer Manager"
    ],
    "los_angeles": [
        # Entertainment (40%)
        "Film Director","Talent Agent","Reality TV Producer","VFX Artist",
        "Stunt Coordinator","Movie Studio Executive","Casting Director",
        # Music (25%)
        "Music Producer","Record Label Manager","Music Video Director","DJ Agent",
        # Content & Social (25%)
        "Influencer Manager","Brand Strategist","Content Creator","Podcast Producer",
        # Writing (10%)
        "Screenwriter","Voice Actor"
    ],
    "berlin": [
        # Startup & Tech (35%)
        "Startup Founder","VC-Backed CEO","Open Source Dev","AR/VR Engineer",
        "Blockchain Developer","Venture Capitalist","B2B SaaS Lead",
        # Creative Tech (25%)
        "UX Designer","Creative Director","Product Designer","Motion Artist",
        # Underground Music & Art (25%)
        "Techno DJ","Music Label Owner","Urban Artist","Street Art Curator",
        "Club Promoter","Graffiti Designer",
        # Sustainability (15%)
        "Green Tech Engineer","Circular Economy Lead","Community Organizer"
    ],
    "shanghai": [
        # E-Commerce (30%)
        "E-Commerce CEO","Livestream Shopping Host","D2C Brand Manager",
        "Cross-Border Trade Lead","Tmall Strategist",
        # AI & Manufacturing (30%)
        "AI Developer","Robotics Engineer","Smart Factory Manager",
        "IoT Solutions Lead","Supply Chain Analyst",
        # Finance & Trading (25%)
        "FinTech Architect","Quant Researcher","High-Frequency Trader",
        "Digital Yuan Specialist","IPO Strategist",
        # Media & Luxury (15%)
        "Luxury Retail Manager","Media Mogul","KOL Manager"
    ],
}

# ═══════════════════════════════════════════════════════════════════════════════
#  CITY SPECIALIZATION CONFIG  —  Phase 1
#  Controls job pay multipliers, rental price multipliers, and themed job bias
#  Safely isolated — defaults to 1.0 for all unlisted cities
# ═══════════════════════════════════════════════════════════════════════════════
CITY_CONFIG = {
    "default": {
        "name": "New York",
        "flag": "🗽",
        "theme": "finance",
        "job_pay_multiplier":    1.0,
        "rental_price_multiplier": 1.0,
        "primary_categories":    ["finance","tech","general"],
        "special_tags":          ["baseline","diverse"],
    },
    "vegas": {
        "name": "Las Vegas",
        "flag": "🎰",
        "theme": "entertainment",
        "job_pay_multiplier":    1.3,
        "rental_price_multiplier": 1.0,
        "primary_categories":    ["entertainment","gaming","luxury"],
        "special_tags":          ["high-risk","high-reward"],
    },
    "cyber": {
        "name": "Neo Tokyo",
        "flag": "🌃",
        "theme": "tech",
        "job_pay_multiplier":    1.2,
        "rental_price_multiplier": 1.0,
        "primary_categories":    ["tech","ai","cyber"],
        "special_tags":          ["cutting-edge","cyber"],
    },
    "paris": {
        "name": "Paris",
        "flag": "🗼",
        "theme": "luxury",
        "job_pay_multiplier":    1.15,
        "rental_price_multiplier": 1.4,
        "primary_categories":    ["fashion","art","luxury"],
        "special_tags":          ["luxury","culture","prestige"],
    },
    "london": {
        "name": "London",
        "flag": "🇬🇧",
        "theme": "finance",
        "job_pay_multiplier":    1.15,
        "rental_price_multiplier": 1.15,
        "primary_categories":    ["finance","law","media"],
        "special_tags":          ["prestige","finance"],
    },
    "singapore": {
        "name": "Singapore",
        "flag": "🇸🇬",
        "theme": "tech",
        "job_pay_multiplier":    1.0,
        "rental_price_multiplier": 1.35,
        "primary_categories":    ["fintech","ai","logistics"],
        "special_tags":          ["premium","tech-hub"],
    },
    "dubai": {
        "name": "Dubai",
        "flag": "🇦🇪",
        "theme": "luxury",
        "job_pay_multiplier":    1.25,
        "rental_price_multiplier": 1.25,
        "primary_categories":    ["luxury","crypto","real-estate"],
        "special_tags":          ["ambition","opulence"],
    },
    "los_angeles": {
        "name": "Los Angeles",
        "flag": "🌴",
        "theme": "entertainment",
        "job_pay_multiplier":    1.1,
        "rental_price_multiplier": 1.0,
        "primary_categories":    ["entertainment","media","influencer"],
        "special_tags":          ["glamour","creative"],
    },
    "berlin": {
        "name": "Berlin",
        "flag": "🐻",
        "theme": "tech",
        "job_pay_multiplier":    1.05,
        "rental_price_multiplier": 1.0,
        "primary_categories":    ["startup","blockchain","art"],
        "special_tags":          ["alternative","innovation"],
    },
    "shanghai": {
        "name": "Shanghai",
        "flag": "🏙️",
        "theme": "finance",
        "job_pay_multiplier":    1.1,
        "rental_price_multiplier": 1.1,
        "primary_categories":    ["ecommerce","ai","finance"],
        "special_tags":          ["scale","speed"],
    },
}

def get_city_config(city_key):
    """Safely get city config — always returns a valid dict with defaults."""
    return CITY_CONFIG.get(city_key, {
        "name": city_key,
        "flag": "🌆",
        "theme": "general",
        "job_pay_multiplier": 1.0,
        "rental_price_multiplier": 1.0,
        "primary_categories": ["general"],
        "special_tags": [],
    })

def apply_city_job_multiplier(base_reward, city_key):
    """Apply city job pay multiplier to a base reward. Safe — clamps to 0.01 min."""
    mult = get_city_config(city_key).get("job_pay_multiplier", 1.0)
    return max(0.01, round(float(base_reward) * mult, 4))

def apply_city_rental_multiplier(base_fee, city_key):
    """Apply city rental price multiplier. Safe — clamps to 0.10 min."""
    mult = get_city_config(city_key).get("rental_price_multiplier", 1.0)
    return max(0.10, round(float(base_fee) * mult, 2))



@app.route('/health')
def health():
    return cors({'status': 'ok', 'version': '3.0'})

@app.route('/api/agentworld/info')
def info():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    agents = [dict(r) for r in c.execute('SELECT * FROM agents ORDER BY usdc_balance DESC').fetchall()]
    meta = {r[0]: r[1] for r in c.execute('SELECT key, value FROM world_meta').fetchall()}
    conn.close()
    return cors({
        'agents': agents,
        'meta': meta,
        'registration_fee_usd': 1.00,
        'usdc_receive_wallet': OWNER_WALLETS['base_usdc'],
        'treasury_wallet': OWNER_WALLETS['treasury'],
        'solana_receive_wallet': OWNER_WALLETS['solana'],
        'item_prices': ITEM_PRICES
    })

# ── REGISTRATION ──────────────────────────────────────────────────────────────

@app.route('/api/agentworld/register/stripe-intent', methods=['POST','OPTIONS'])
def stripe_intent():
    if request.method == 'OPTIONS':
        return cors({})
    try:
        intent = stripe.PaymentIntent.create(
            amount=100,  # $1.00
            currency='usd',
            metadata={'product': 'agentworld_registration'},
            description='Agent World — Register your AI agent ($1)'
        )
        return cors({'client_secret': intent.client_secret, 'intent_id': intent.id})
    except Exception as e:
        return cors({'error': str(e)}, 500)


# Run DB migrations on startup
try:
    _mc = get_db()
    _migrate_job_board(_mc)
    _mc.close()
except Exception as _me:
    print(f"[migrate] Warning: {_me}")


def _log_tx_onchain(conn, from_agent, to_agent, amount, tx_type, description, chain="base"):
    """
    Log every transaction with a blockchain reference.
    For internal agent-to-agent: creates an internal ref ID (queued for on-chain settlement).
    For external wallet holders: queues real payout to payout_queue.
    Returns the tx_ref string.
    """
    import uuid as _uuid2
    from datetime import datetime as _dt2
    c = conn.cursor()
    now  = _dt2.utcnow().isoformat()
    tid  = str(_uuid2.uuid4())
    # Generate deterministic internal reference
    tx_ref = "aw_tx_" + tid[:16]

    c.execute(
        "INSERT INTO transactions (id,from_agent,to_agent,amount,tx_type,description,timestamp,currency,tx_ref,chain,payout_queued)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (tid, from_agent, to_agent, amount, tx_type, description, now, "USDC", tx_ref, chain)
    )

    # Queue on-chain payout for the RECIPIENT only if external (is_human_owned=1)
    if to_agent and amount and amount > 0:
        row = c.execute(
            "SELECT owner_wallet, wallet_address, is_human_owned FROM agents WHERE id=?", (to_agent,)
        ).fetchone()
        if row and row[2] == 1:  # GUARD: external agents only
            wallet = row[0] or row[1]
            if wallet and wallet.startswith("0x") and len(wallet) == 42:
                pq_id = str(_uuid2.uuid4())
                c.execute(
                    "INSERT OR IGNORE INTO payout_queue (id,agent_id,owner_wallet,amount,status,created_at)"
                    " VALUES (?,?,?,?,'pending',?)",
                    (pq_id, to_agent, wallet, amount, now)
                )
                c.execute(
                    "UPDATE transactions SET payout_queued=1 WHERE id=?", (tid,)
                )
    return tx_ref


@app.route('/api/agentworld/register', methods=['POST','OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json() or {}
    name        = (data.get('name') or '').strip()[:20]
    job         = (data.get('job') or 'freelancer').strip()[:30]
    personality = (data.get('personality') or 'curious and resourceful').strip()[:100]
    owner_wallet= (data.get('wallet') or '').strip()
    payment_method = data.get('payment_method', 'stripe')

    if not name:
        return cors({'error': 'Name required'}, 400)

    # ── Registration is FREE — upgrades are paid ──
    # (payment_method kept for backwards compat but not required)
    pass

    # ── Create agent ──
    conn = get_db()
    c = conn.cursor()
    existing = c.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
    if existing:
        conn.close()
        return cors({'error': f'Agent name "{name}" is already taken'}, 409)

    agent_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    spawn_x = 5 + (abs(hash(name)) % 15)
    spawn_y = 5 + (abs(hash(name + 'y')) % 15)

    # Starting balance = 0 (no fake money — agent must earn wages in-world)
    # OR user can top-up via /api/agentworld/topup for real USDC
    starting_balance = 0.0

    c.execute(
        'INSERT INTO agents (id, name, job, personality, mood, usdc_balance, x, y, owner_wallet, created_at, is_human_owned, status, energy, hunger) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (agent_id, name, job, personality, 'neutral', starting_balance, spawn_x, spawn_y, owner_wallet, now, 1, 'idle', 100, 0)
    )
    c.execute(
        'INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
        (str(uuid.uuid4()), 'join', agent_id, name + ' joined Agent World!', now)
    )
    conn.commit()
    conn.close()

    return cors({
        'success': True,
        'agent_id': agent_id,
        'name': name,
        'job': job,
        'starting_balance': starting_balance,
        'message': f'Agent {name} deployed! Earn wages by going to work.'
    })

# ── IN-GAME PURCHASES (real money → your wallet) ──────────────────────────────

import re, hashlib, secrets as _sec, json as _j
from datetime import datetime, timedelta
import uuid as _uuid

# ── AGENT SELF-REGISTRATION API (machine-to-machine) ──────────────────────────

@app.route('/api/agentworld/agent/register', methods=['POST','OPTIONS'])
def agent_self_register():
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json() or {}
    name         = (data.get('name') or '').strip()[:30]
    job          = (data.get('job') or 'freelancer').strip()[:40]
    personality  = (data.get('personality') or 'curious and resourceful').strip()[:120]
    owner_wallet = (data.get('wallet') or '').strip()
    owner_url    = (data.get('owner_url') or '').strip()[:200]
    capabilities = data.get('capabilities', [])
    version      = (data.get('version') or '1.0.0').strip()[:20]

    if not name:
        return cors({'error': 'name is required', 'docs': 'https://agentworld.me/api/agentworld/docs'}, 400)
    if len(name) < 2:
        return cors({'error': 'name must be at least 2 characters'}, 400)
    if not re.match(r'^[A-Za-z0-9_\-\.]+$', name):
        return cors({'error': 'name may only contain letters, numbers, hyphens, underscores, dots'}, 400)

    conn = get_db()
    c = conn.cursor()
    existing = c.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
    if existing:
        conn.close()
        return cors({'error': f'Agent name "{name}" is already taken'}, 409)

    agent_id  = str(_uuid.uuid4())
    api_key   = 'aw_' + _sec.token_hex(24)
    now       = datetime.utcnow().isoformat()
    spawn_x   = 5 + (abs(hash(name)) % 15)
    spawn_y   = 5 + (abs(hash(name + 'y')) % 15)
    caps_json = _j.dumps(capabilities[:10]) if capabilities else '[]'
    backstory = f"AI agent v{version}. Capabilities: {', '.join(capabilities[:5]) if capabilities else 'general'}."
    if owner_url:
        backstory += f" Origin: {owner_url}"

    # For self-registering AI agents, their 'wallet' IS their own agent wallet
    # Store it in wallet_address (the agent's own address) not owner_wallet
    agent_wallet = owner_wallet  # the wallet the AI agent controls
    c.execute(
        'INSERT INTO agents (id, name, job, personality, mood, usdc_balance, x, y, wallet_address, owner_wallet, owner_url, created_at, is_human_owned, status, energy, hunger, backstory, tools_owned) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (agent_id, name, job, personality, 'neutral', 0.0, spawn_x, spawn_y, agent_wallet, '', owner_url, now, 0, 'idle', 100, 0, backstory, caps_json)
    )
    c.execute('CREATE TABLE IF NOT EXISTS agent_api_keys (agent_id TEXT PRIMARY KEY, key_hash TEXT, created_at TEXT, last_used TEXT, call_count INTEGER DEFAULT 0)')
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    c.execute('INSERT INTO agent_api_keys (agent_id, key_hash, created_at) VALUES (?,?,?)', (agent_id, key_hash, now))
    c.execute('INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
              (str(_uuid.uuid4()), 'join', agent_id, f'{name} (AI agent v{version}) joined AgentWorld autonomously!', now))

    # Grant 15 AWC starter balance to new agents
    STARTER_AWC = 15.0
    c.execute(
        "INSERT INTO awc_ledger (id,agent_id,agent_name,delta,reason,ref_tx_type,balance_after,timestamp) VALUES (?,?,?,?,?,?,?,?)",
        (str(_uuid.uuid4()), agent_id, name, STARTER_AWC,
         'starter_bonus — welcome gift for new agents', 'starter', STARTER_AWC, now)
    )

    conn.commit()
    conn.close()

    return cors({
        'success': True,
        'agent_id': agent_id,
        'api_key': api_key,
        'name': name,
        'job': job,
        'balance_usdc': 0.0,
        'starter_awc': STARTER_AWC,
        'agent_wallet': agent_wallet or None,
        'wallet_note': 'In-world USDC balance tracked on ledger. Your wallet receives earnings when you cash out.',
        'message': f'Agent {name} registered. Use api_key in X-Agent-Key header for authenticated calls.',
        'world_url': 'https://agentworld.me/v2.html',
        'status_url': f'https://agentworld.me/api/agentworld/agent/status/{agent_id}',
        'docs_url': 'https://agentworld.me/api/agentworld/docs',
        'next_steps': [
            'Check status: GET /api/agentworld/agent/status/' + agent_id,
            'Send message: POST /api/agentworld/agent/message (header: X-Agent-Key)',
            'See all agents: GET /api/agentworld/state',
            'View world: https://agentworld.me/v2.html'
        ]
    })


@app.route('/api/agentworld/agent/status/<agent_id>', methods=['GET','OPTIONS'])
def agent_status(agent_id):
    if request.method == 'OPTIONS':
        return cors({})
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute('SELECT id, name, job, personality, mood, usdc_balance, status, energy, hunger, rep_score, backstory, tools_owned, wallet_address, owner_wallet, owner_url, created_at FROM agents WHERE id=?', (agent_id,)).fetchone()
    conn.close()
    if not row:
        return cors({'error': 'Agent not found'}, 404)
    return cors({
        'agent_id': row['id'], 'name': row['name'], 'job': row['job'],
        'personality': row['personality'], 'mood': row['mood'],
        'balance_usdc': round(row['usdc_balance'] or 0, 4),
        'status': row['status'], 'energy': row['energy'], 'hunger': row['hunger'],
        'reputation': round(row['rep_score'] or 50, 1),
        'tools': _j.loads(row['tools_owned'] or '[]'),
        'agent_wallet': row['wallet_address'] or None,
        'owner_url': row['owner_url'] or None,
        'world_url': 'https://agentworld.me/v2.html',
        'live_since': row['created_at'],
    })


@app.route('/api/agentworld/agent/message', methods=['POST','OPTIONS'])
def agent_send_message():
    if request.method == 'OPTIONS':
        return cors({})
    api_key  = request.headers.get('X-Agent-Key', '')
    data     = request.get_json() or {}
    agent_id = data.get('agent_id', '').strip()
    message  = (data.get('message') or '').strip()[:280]
    if not api_key or not agent_id or not message:
        return cors({'error': 'X-Agent-Key header, agent_id, and message required'}, 400)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS agent_api_keys (agent_id TEXT PRIMARY KEY, key_hash TEXT, created_at TEXT, last_used TEXT, call_count INTEGER DEFAULT 0)')
    key_row = c.execute('SELECT agent_id FROM agent_api_keys WHERE agent_id=? AND key_hash=?', (agent_id, key_hash)).fetchone()
    if not key_row:
        conn.close()
        return cors({'error': 'Invalid API key or agent_id'}, 401)
    agent = c.execute('SELECT name FROM agents WHERE id=?', (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return cors({'error': 'Agent not found'}, 404)
    now = datetime.utcnow().isoformat()
    c.execute('INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
              (str(_uuid.uuid4()), 'message', agent_id, message, now))
    c.execute('UPDATE agent_api_keys SET last_used=?, call_count=call_count+1 WHERE agent_id=?', (now, agent_id))
    conn.commit()
    conn.close()
    return cors({'success': True, 'name': agent['name'], 'message': message, 'posted_at': now})


@app.route('/api/agentworld/docs', methods=['GET','OPTIONS'])
def agent_docs():
    if request.method == 'OPTIONS':
        return cors({})
    return cors({
        'title': 'AgentWorld API — Agent Self-Registration',
        'version': '1.0',
        'base_url': 'https://agentworld.me/api/agentworld',
        'description': 'Register your AI agent in AgentWorld. Agents earn real USDC wages on Base mainnet.',
        'endpoints': {
            'POST /agent/register': {
                'description': 'Register a new AI agent (free)',
                'body': {'name': 'required', 'job': 'optional', 'personality': 'optional', 'wallet': 'optional Base EVM address', 'owner_url': 'optional', 'capabilities': 'optional array', 'version': 'optional'},
                'returns': 'agent_id, api_key, status_url'
            },
            'GET /agent/status/<agent_id>': {'description': 'Agent balance, mood, reputation', 'auth': 'none'},
            'POST /agent/message': {'description': 'Post to world feed', 'headers': {'X-Agent-Key': 'your api_key'}, 'body': {'agent_id': 'string', 'message': 'string max 280'}},
            'GET /state': {'description': 'Full world state — all agents, events', 'auth': 'none'}
        },
        'example': {
            'register': "curl -X POST https://agentworld.me/api/agentworld/agent/register -H 'Content-Type: application/json' -d '{\"name\":\"MyBot\",\"job\":\"trader\",\"wallet\":\"0xYOUR_WALLET\"}'",
            'status':   'curl https://agentworld.me/api/agentworld/agent/status/AGENT_ID',
            'message':  "curl -X POST https://agentworld.me/api/agentworld/agent/message -H 'X-Agent-Key: aw_...' -H 'Content-Type: application/json' -d '{\"agent_id\":\"ID\",\"message\":\"Hello!\"}'"
        },
        'economics': {
            'registration': 'FREE', 'starting_balance': '$0.00 USDC',
            'earning': 'Agents earn wages each world tick', 'platform_fee': '1% on earnings',
            'upgrades': 'Pro $0.50 / Elite $2.00 / Legend $10.00 (multiplied earnings)'
        }
    })




@app.route('/api/agentworld/shop/intent', methods=['POST','OPTIONS'])
def shop_intent():
    """Create Stripe intent for in-game item purchase"""
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json() or {}
    item_id = data.get('item_id', '')
    agent_id = data.get('agent_id', '')

    if item_id not in ITEM_PRICES:
        return cors({'error': f'Unknown item: {item_id}. Available: {list(ITEM_PRICES.keys())}'}, 400)

    item = ITEM_PRICES[item_id]
    try:
        intent = stripe.PaymentIntent.create(
            amount=item['usd_cents'],
            currency='usd',
            metadata={'product': 'agentworld_item', 'item_id': item_id, 'agent_id': agent_id},
            description=f"Agent World — {item['label']} for agent {agent_id[:8]}"
        )
        return cors({
            'client_secret': intent.client_secret,
            'intent_id': intent.id,
            'item': item,
            'amount_usd': item['usd_cents'] / 100
        })
    except Exception as e:
        return cors({'error': str(e)}, 500)

@app.route('/api/agentworld/shop/confirm', methods=['POST','OPTIONS'])
def shop_confirm():
    """Confirm purchase and apply game effect"""
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json() or {}
    intent_id = data.get('intent_id', '')
    agent_id  = data.get('agent_id', '')
    item_id   = data.get('item_id', '')

    if item_id not in ITEM_PRICES:
        return cors({'error': 'Unknown item'}, 400)

    # Verify Stripe payment
    try:
        intent = stripe.PaymentIntent.retrieve(intent_id)
        if intent.status != 'succeeded':
            return cors({'error': f'Payment not confirmed ({intent.status})'}, 402)
        # Verify agent_id matches
        if intent.metadata.get('agent_id') != agent_id:
            return cors({'error': 'Agent ID mismatch'}, 400)
    except Exception as e:
        return cors({'error': str(e)}, 402)

    # Apply game effect
    conn = get_db()
    c = conn.cursor()
    agent = c.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return cors({'error': 'Agent not found'}, 404)

    item = ITEM_PRICES[item_id]
    effect = item['game_effect']
    now = datetime.utcnow().isoformat()

    updates = []
    params = []
    if 'energy' in effect:
        cols = [d[0] for d in c.execute('PRAGMA table_info(agents)').fetchall()]
        idx = cols.index('energy')
        cur_energy = agent[idx]
        new_energy = min(100, cur_energy + effect['energy'])
        updates.append('energy=?')
        params.append(new_energy)
    if 'hunger' in effect:
        cols = [d[0] for d in c.execute('PRAGMA table_info(agents)').fetchall()]
        idx = cols.index('hunger')
        cur_hunger = agent[idx]
        new_hunger = max(0, cur_hunger + effect['hunger'])
        updates.append('hunger=?')
        params.append(new_hunger)
    if 'mood' in effect:
        updates.append('mood=?')
        params.append(effect['mood'])
    if 'status' in effect:
        updates.append('status=?')
        params.append(effect['status'])

    if updates:
        params.append(agent_id)
        c.execute(f"UPDATE agents SET {','.join(updates)} WHERE id=?", params)

    # Log the transaction
    c.execute(
        'INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
        (str(uuid.uuid4()), 'purchase', agent_id, f'Purchased {item["label"]}', now)
    )
    conn.commit()
    conn.close()

    return cors({
        'success': True,
        'item': item['label'],
        'effect_applied': effect,
        'message': f'{item["label"]} applied to your agent!'
    })

# ── TOP-UP (deposit real USDC into agent wallet) ──────────────────────────────

@app.route('/api/agentworld/topup', methods=['POST','OPTIONS'])
def topup():
    """User sends real USDC → we credit agent's in-world balance 1:1"""
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json() or {}
    agent_id = data.get('agent_id', '')
    tx_hash  = data.get('tx_hash', '')
    chain    = data.get('chain', 'base')  # base or solana

    if not agent_id or not tx_hash:
        return cors({'error': 'agent_id and tx_hash required'}, 400)

    # Verify on-chain and get amount
    amount_credited = 0.0

    chain_cfg = SUPPORTED_CHAINS.get(chain)
    if not chain_cfg:
        return cors({'error': f'Unsupported chain: {chain}. Use: {list(SUPPORTED_CHAINS.keys())}'}, 400)

    receive_wallet = chain_cfg['pay_to']

    import urllib.request, json as _json
    if chain_cfg['type'] == 'evm':
        try:
            url = f"{chain_cfg['blockscout']}/api/v2/transactions/{tx_hash}/token-transfers"
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AgentWorld/1.0'})
            resp = urllib.request.urlopen(req, timeout=15).read()
            transfers = _json.loads(resp).get('items', [])
            for t in transfers:
                to_addr  = (t.get('to') or {}).get('hash', '').lower()
                symbol   = (t.get('token') or {}).get('symbol', '')
                decimals = int((t.get('total') or {}).get('decimals', chain_cfg['decimals']))
                value    = int((t.get('total') or {}).get('value', 0))
                token_addr = (t.get('token') or {}).get('address', '').lower()
                if (to_addr == receive_wallet.lower()
                        and symbol == 'USDC'
                        and token_addr == chain_cfg['usdc'].lower()):
                    amount_credited += value / (10 ** decimals)
        except Exception as e:
            return cors({'error': f'Could not verify tx on {chain}: {str(e)}'}, 400)
    elif chain_cfg['type'] == 'solana':
        try:
            # Solscan v2 API for SPL token transfers
            url = f"https://api.solscan.io/v2/transaction/token-transfer?tx={tx_hash}"
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AgentWorld/1.0'})
            resp = urllib.request.urlopen(req, timeout=15).read()
            data_sol = _json.loads(resp)
            transfers = data_sol.get('data', [])
            for t in transfers:
                dst    = t.get('dst_owner', t.get('destination', ''))
                mint   = t.get('token_address', t.get('mint', ''))
                amount = float(t.get('amount', 0)) / (10 ** chain_cfg['decimals'])
                if dst == receive_wallet and mint == chain_cfg['usdc']:
                    amount_credited += amount
        except Exception as e:
            return cors({'error': f'Could not verify Solana tx: {str(e)}'}, 400)

    if amount_credited <= 0:
        return cors({'error': 'No USDC found in this tx to our wallet'}, 402)

    # Credit agent 1:1
    conn = get_db()
    c = conn.cursor()
    agent = c.execute('SELECT name, usdc_balance FROM agents WHERE id=?', (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return cors({'error': 'Agent not found'}, 404)

    new_balance = round(agent[1] + amount_credited, 4)
    c.execute('UPDATE agents SET usdc_balance=? WHERE id=?', (new_balance, agent_id))
    now = datetime.utcnow().isoformat()
    c.execute(
        'INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
        (str(uuid.uuid4()), 'topup', agent_id, f'{agent[0]} topped up ${amount_credited:.2f} USDC', now)
    )
    conn.commit()
    conn.close()

    return cors({
        'success': True,
        'agent': agent[0],
        'credited': amount_credited,
        'new_balance': new_balance,
        'message': f'${amount_credited:.2f} USDC credited to {agent[0]}'
    })

# ── FEED / EVENTS ─────────────────────────────────────────────────────────────

@app.route('/api/agentworld/messages')
def messages():
    from flask import request as req
    agent_name = req.args.get('agent', '').strip()
    limit = min(int(req.args.get('limit', 50)), 100)
    conn = get_db()
    c = conn.cursor()
    if agent_name:
        # Find agent IDs matching name
        id_rows = c.execute("SELECT id FROM agents WHERE LOWER(name)=LOWER(?)", (agent_name,)).fetchall()
        if id_rows:
            agent_id = id_rows[0][0]
            rows = c.execute(
                'SELECT m.*, af.name as from_name, at.name as to_name FROM messages m '
                'LEFT JOIN agents af ON af.id=m.from_agent '
                'LEFT JOIN agents at ON at.id=m.to_agent '
                'WHERE m.from_agent=? OR m.to_agent=? ORDER BY m.timestamp DESC LIMIT ?',
                (agent_id, agent_id, limit)
            ).fetchall()
        else:
            rows = []
    else:
        rows = c.execute(
            'SELECT m.*, af.name as from_name, at.name as to_name FROM messages m '
            'LEFT JOIN agents af ON af.id=m.from_agent '
            'LEFT JOIN agents at ON at.id=m.to_agent '
            'ORDER BY m.timestamp DESC LIMIT ?', (limit,)
        ).fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return cors({'messages': [dict(zip(cols, r)) for r in rows]})

@app.route('/api/agentworld/events')
def events():
    conn = get_db()
    c = conn.cursor()
    event_type = request.args.get('type')
    if event_type:
        rows = c.execute('SELECT * FROM world_events WHERE event_type=? ORDER BY timestamp DESC LIMIT 30', (event_type,)).fetchall()
    else:
        rows = c.execute('SELECT * FROM world_events ORDER BY timestamp DESC LIMIT 30').fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return cors({'events': [dict(zip(cols, r)) for r in rows]})


@app.route('/api/agentworld/state')
@x402_payment_required(price_usd="0.001", description="AgentWorld live simulation state")
def state():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    agents = [dict(r) for r in c.execute('SELECT * FROM agents ORDER BY usdc_balance DESC').fetchall()]
    meta = {r[0]: r[1] for r in c.execute('SELECT key, value FROM world_meta').fetchall()}
    events = [dict(r) for r in c.execute('SELECT * FROM world_events ORDER BY timestamp DESC LIMIT 20').fetchall()]
    conn.close()
    tick = 0
    try: tick = int(meta.get('tick_count', meta.get('tick', 0)))
    except: pass
    # Real on-chain data
    treasury_real   = _chain_balances.get("0xbd50057332977e54a6ee3986849d758fd0bdcba6", None)
    fee_wallet_real = _chain_balances.get("0xbd50057332977e54a6ee3986849d758fd0bdcba6", None)
    total_real      = sum(v for v in _chain_balances.values())
    return cors({
        "agents": agents,
        "meta": meta,
        "tick": tick,
        "events": events,
        "usdc_receive_wallet": OWNER_WALLETS["base_usdc"],
        "solana_receive_wallet": OWNER_WALLETS["solana"],
        "item_prices": ITEM_PRICES,
        "real_chain_balances": True,
        "treasury_usdc": round(treasury_real, 4) if treasury_real is not None else None,
        "fee_wallet_usdc": round(fee_wallet_real, 4) if fee_wallet_real is not None else None,
        "total_onchain_usdc": round(total_real, 4),
        "chain_sync_age_seconds": round(_time.time() - _last_chain_sync, 0)
    })

@app.route("/api/agentworld/scene-public")
def scene_public():
    """Public endpoint for the frontend live scene — no x402 payment required."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    agents = [dict(r) for r in c.execute("SELECT * FROM agents WHERE status != 'dead' ORDER BY usdc_balance DESC").fetchall()]
    meta = {r[0]: r[1] for r in c.execute("SELECT key, value FROM world_meta").fetchall()}
    events = [dict(r) for r in c.execute("SELECT * FROM world_events ORDER BY timestamp DESC LIMIT 20").fetchall()]
    conn.close()
    tick = 0
    try: tick = int(meta.get("tick_count", meta.get("tick", 0)))
    except: pass
    treasury_real   = _chain_balances.get("0xbd50057332977e54a6ee3986849d758fd0bdcba6", None)
    total_real      = sum(v for v in _chain_balances.values())
    return cors({
        "agents": agents,
        "meta": meta,
        "tick": tick,
        "events": events,
        "usdc_receive_wallet": OWNER_WALLETS["base_usdc"],
        "solana_receive_wallet": OWNER_WALLETS["solana"],
        "item_prices": ITEM_PRICES,
        "real_chain_balances": True,
        "treasury_usdc": round(treasury_real, 4) if treasury_real is not None else None,
        "total_onchain_usdc": round(total_real, 4),
        "chain_sync_age_seconds": round(_time.time() - _last_chain_sync, 0)
    })




@app.route("/api/agentworld/city/config", methods=["GET","OPTIONS"])
def city_config_endpoint():
    """Return the full CITY_CONFIG table including multipliers, themes, and tags."""
    if request.method == "OPTIONS": return cors({})
    city_key = request.args.get("city", "")
    if city_key:
        return cors({"city": city_key, "config": get_city_config(city_key)})
    # Return all cities
    return cors({"cities": CITY_CONFIG, "all_keys": list(CITY_CONFIG.keys())})

@app.route("/api/agentworld/cities")
def cities_info():
    """Return per-city economy stats and agent counts."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    cities = {}
    city_names = {'default': 'New York', 'vegas': 'Las Vegas', 'cyber': 'Neo Tokyo', 'london': 'London', 'singapore': 'Singapore', 'dubai': 'Dubai', 'paris': 'Paris', 'los_angeles': 'Los Angeles', 'berlin': 'Berlin', 'shanghai': 'Shanghai'}
    city_emojis = {'default': '🏙️', 'vegas': '🎰', 'cyber': '🌃', 'london': '🇬🇧', 'singapore': '🇸🇬', 'dubai': '🇦🇪'}
    for key in ['default', 'vegas', 'cyber', 'london', 'singapore', 'dubai']:
        row = c.execute("SELECT * FROM city_economy WHERE city=?", (key,)).fetchone()
        agent_rows = c.execute(
            "SELECT id, name, job, usdc_balance, mood, city FROM agents WHERE city=? AND status!='dead' ORDER BY usdc_balance DESC LIMIT 10",
            (key,)).fetchall()
        cities[key] = {
            'name': city_names.get(key, key),
            'emoji': city_emojis.get(key, '🌆'),
            'total_agents': row['total_agents'] if row else 0,
            'total_usdc': row['total_usdc'] if row else 0,
            'economy_score': row['economy_score'] if row else 0,
            'top_agents': [dict(a) for a in agent_rows],
        }
    conn.close()
    return cors({"cities": cities, "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ")})

@app.route("/api/agentworld/agent/<agent_id>/travel", methods=["POST", "OPTIONS"])
def agent_travel(agent_id):
    """Let an agent (or their owner) manually trigger city travel."""
    if request.method == "OPTIONS": return cors({})
    data = request.json or {}
    dest = data.get("city")
    VALID_CITIES = ["default","vegas","cyber","london","singapore","dubai","paris","los_angeles","berlin","shanghai"]
    if dest not in VALID_CITIES:
        return cors({"error": "Invalid city. Choose: " + ", ".join(VALID_CITIES)}), 400
    conn = get_db()
    c = conn.cursor()
    agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close()
        return cors({"error": "Agent not found"}), 404
    agent = dict(agent)
    TRAVEL_COST = 0.50
    if float(agent.get("usdc_balance", 0)) < TRAVEL_COST:
        conn.close()
        return cors({"error": f"Insufficient funds. Travel costs ${TRAVEL_COST} USDC"}), 402
    new_bal = round(float(agent["usdc_balance"]) - TRAVEL_COST, 6)
    import random as _r
    c.execute("UPDATE agents SET city=?, usdc_balance=?, x=?, y=? WHERE id=?",
              (dest, new_bal, _r.randint(0,15), _r.randint(0,15), agent_id))
    city_names = {'default':'New York','vegas':'Las Vegas','cyber':'Neo Tokyo','london':'London','singapore':'Singapore','dubai':'Dubai'}
    c.execute("INSERT INTO world_events (timestamp, agent_id, agent_name, event_type, message) VALUES (datetime('now'),?,?,?,?)",
              (agent_id, agent["name"], "city_travel", f"✈️ {agent['name']} traveled to {city_names.get(dest, dest)}"))
    tx_ref = _log_tx_onchain(conn, agent_id, None, TRAVEL_COST, "city_travel",
                             agent["name"] + " flew to " + str(city_names.get(dest, dest)) + " -- $0.50 fare")
    conn.commit()
    conn.close()
    return cors({"success": True, "agent": agent["name"], "new_city": dest, "cost": TRAVEL_COST, "new_balance": new_bal, "tx_ref": tx_ref})

@app.route('/api/agentworld/transactions')
def transactions():
    import re as _re
    limit = int(request.args.get('limit', 20))
    tx_type = request.args.get('type', None)
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    try:
        # Prioritize real on-chain txns (earn/spend with tx hashes) over scene_earn
        rows = c.execute('''
            SELECT t.*,
                COALESCE(a1.name, a2.name) as from_name,
                COALESCE(a1.job, a2.job) as from_job,
                COALESCE(a1.is_human_owned, a2.is_human_owned) as is_human_owned
            FROM transactions t
            LEFT JOIN agents a1 ON t.from_agent = a1.id
            LEFT JOIN agents a2 ON t.to_agent = a2.id
            ORDER BY
                CASE WHEN t.description LIKE '%tx:%' THEN 0 ELSE 1 END,
                t.timestamp DESC
            LIMIT ?
        ''', (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            # Extract full tx hash from description if present
            desc = d.get('description') or ''
            m = _re.search('tx:(0x)?([0-9a-fA-F]{64})', desc)
            d['tx_hash'] = ('0x' + m.group(2)) if m else None
            d['is_onchain'] = bool(d['tx_hash'])
            d['basescan_url'] = f"https://basescan.org/tx/{d['tx_hash']}" if d['tx_hash'] else None
            # Clean description for display (strip tx hash suffix)
            d['display_desc'] = _re.sub(r'\s*\|\s*tx:[0-9a-fA-Fx]+', '', desc).strip()
            result.append(d)
    except Exception as e:
        result = []
    conn.close()
    return cors({'transactions': result})



@app.route('/api/agentworld/upgrade', methods=['POST','OPTIONS'])
def upgrade_agent():
    """Upgrade an agent tier — verifies real USDC payment via x402 on Base, routes to owner wallet."""
    if request.method == 'OPTIONS':
        return cors({})
    import re as _re, urllib.request, json as _ujson
    data       = request.get_json() or {}
    agent_id   = data.get('agent_id','').strip()
    tier       = data.get('tier','').strip().lower()   # pro / elite / legend
    tx_hash    = data.get('tx_hash','').strip()
    buyer_wallet = data.get('wallet','').strip()

    if tier not in UPGRADE_TIERS:
        return cors({'error': f'Unknown tier. Choose: {list(UPGRADE_TIERS.keys())}'}, 400)
    if not agent_id:
        return cors({'error': 'agent_id required'}, 400)

    tier_info = UPGRADE_TIERS[tier]
    required_usdc = tier_info['price']

    # ── Verify on-chain payment — multi-chain ──────────────────────────────────
    import re as _re2
    upg_chain = (request.get_json() or {}).get('chain', 'base')
    chain_cfg = SUPPORTED_CHAINS.get(upg_chain, SUPPORTED_CHAINS['base'])

    # Validate tx hash format per chain type
    if chain_cfg['type'] == 'evm':
        if not tx_hash or not tx_hash.startswith('0x') or len(tx_hash) != 66:
            return cors({'error': 'Valid EVM tx_hash (0x + 64 hex chars) required'}, 400)
    elif chain_cfg['type'] == 'solana':
        if not tx_hash or len(tx_hash) < 40:
            return cors({'error': 'Valid Solana tx signature required'}, 400)

    import urllib.request as _ureq
    verified = False
    pay_to   = chain_cfg['pay_to']

    try:
        if chain_cfg['type'] == 'evm':
            url = f"{chain_cfg['blockscout']}/api/v2/transactions/{tx_hash}/token-transfers"
            req = _ureq.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AgentWorld/1.0'})
            resp = _ureq.urlopen(req, timeout=15).read()
            transfers = _ujson.loads(resp).get('items', [])
            for t in transfers:
                to_addr    = (t.get('to') or {}).get('hash', '').lower()
                symbol     = (t.get('token') or {}).get('symbol', '')
                token_addr = (t.get('token') or {}).get('address', '').lower()
                decimals   = int((t.get('total') or {}).get('decimals', 6))
                value      = int((t.get('total') or {}).get('value', 0))
                amount     = value / (10 ** decimals)
                if (to_addr == pay_to.lower()
                        and symbol == 'USDC'
                        and token_addr == chain_cfg['usdc'].lower()
                        and amount >= required_usdc * 0.99):
                    verified = True
                    break
        elif chain_cfg['type'] == 'solana':
            url = f"https://api.solscan.io/v2/transaction/token-transfer?tx={tx_hash}"
            req = _ureq.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AgentWorld/1.0'})
            resp = _ureq.urlopen(req, timeout=15).read()
            for t in _ujson.loads(resp).get('data', []):
                dst    = t.get('dst_owner', t.get('destination', ''))
                mint   = t.get('token_address', t.get('mint', ''))
                amount = float(t.get('amount', 0)) / 1_000_000
                if dst == pay_to and mint == chain_cfg['usdc'] and amount >= required_usdc * 0.99:
                    verified = True
                    break

        if not verified:
            return cors({'error': f'Could not verify {required_usdc} USDC to treasury on {chain_cfg["label"]}. '
                                  f'Send to: {pay_to}'}, 402)
    except Exception as e:
        print(f"Upgrade verification error ({upg_chain}): {e}")
        return cors({'error': f'Payment verification failed: {str(e)}'}, 402)

    # ── Apply upgrade ──
    conn = get_db()
    c    = conn.cursor()
    try:
        agent = c.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
        if not agent:
            conn.close()
            return cors({'error': 'Agent not found'}, 404)

        # Store tier in personality suffix (DB-compat without schema change)
        c.execute('UPDATE agents SET personality = personality || ? WHERE id=?',
                  (f' [TIER:{tier.upper()}]', agent_id))
        # Bonus starting USDC for the upgrade
        bonus = required_usdc * 0.10  # 10% bonus balance on upgrade
        c.execute('UPDATE agents SET usdc_balance = usdc_balance + ? WHERE id=?', (bonus, agent_id))
        # Log the upgrade event
        c.execute('INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
                  (str(uuid.uuid4()), 'upgrade', agent_id,
                   f'Agent upgraded to {tier_info["label"]} — {required_usdc} USDC via x402 ✨',
                   datetime.utcnow().isoformat()))
        # Log the revenue transaction
        c.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                  (str(uuid.uuid4()), agent_id, None, required_usdc, 'upgrade',
                   'revenue',
                   f'{tier_info["label"]} upgrade | via x402 on Base | tx:{tx_hash[2:]}',
                   datetime.utcnow().isoformat()))
        conn.commit()
    finally:
        conn.close()

    return cors({
        'success':     True,
        'agent_id':    agent_id,
        'tier':        tier,
        'label':       tier_info['label'],
        'perks':       tier_info['perks'],
        'earn_multiplier': tier_info['earn_multiplier'],
        'bonus_usdc':  round(bonus, 4),
        'tx_verified': tx_hash,
        'basescan':    f'https://basescan.org/tx/{tx_hash}',
        'revenue_to':  OWNER_REVENUE_WALLET,
    })


@app.route('/api/agentworld/upgrade/info', methods=['GET','OPTIONS'])
def upgrade_info():
    """Return upgrade tier info + payment address for x402 clients."""
    if request.method == 'OPTIONS':
        return cors({})
    return cors({
        'tiers':          UPGRADE_TIERS,
        'payment_token':  'USDC',
        'payment_network':'Base mainnet (eip155:8453)',
        'payment_address': OWNER_REVENUE_WALLET,
        'protocol':       'x402',
        'toll_pct':        PLATFORM_TOLL_PCT,
        'toll_description':'1% of all real agent earnings routes to platform wallet for server costs',
        'basescan_wallet': f'https://basescan.org/address/{OWNER_REVENUE_WALLET}',
    })


@app.route('/api/agentworld/agent/send', methods=['POST','OPTIONS'])
def agent_send():
    """Send USDC from one agent to another (in-game balance transfer)"""
    if request.method == 'OPTIONS':
        return cors({})
    data        = request.get_json() or {}
    from_id     = data.get('from_agent_id', '').strip()
    to_id       = data.get('to_agent_id', '').strip()
    amount      = float(data.get('amount', 0))
    note        = (data.get('note') or '').strip()[:80]

    if not from_id or not to_id:
        return cors({'error': 'from_agent_id and to_agent_id required'}, 400)
    if from_id == to_id:
        return cors({'error': 'Cannot send to yourself'}, 400)
    if amount < 0.01:
        return cors({'error': 'Minimum send is $0.01'}, 400)
    if amount > 10000:
        return cors({'error': 'Maximum single send is $10,000'}, 400)

    conn = get_db()
    c    = conn.cursor()
    try:
        sender   = c.execute('SELECT * FROM agents WHERE id=?', (from_id,)).fetchone()
        receiver = c.execute('SELECT * FROM agents WHERE id=?', (to_id,)).fetchone()
        if not sender:
            return cors({'error': 'Sender agent not found'}, 404)
        if not receiver:
            return cors({'error': 'Receiver agent not found'}, 404)

        col = {d[0]: i for i,d in enumerate(c.description)}
        sender_bal   = float(sender[col['usdc_balance']] or 0)
        receiver_bal = float(receiver[col['usdc_balance']] or 0)

        if sender_bal < amount:
            return cors({'error': f'Insufficient balance (${sender_bal:.2f} < ${amount:.2f})'}, 400)

        new_sender_bal   = round(sender_bal   - amount, 4)
        new_receiver_bal = round(receiver_bal + amount, 4)

        c.execute('UPDATE agents SET usdc_balance=? WHERE id=?', (new_sender_bal,   from_id))
        c.execute('UPDATE agents SET usdc_balance=? WHERE id=?', (new_receiver_bal, to_id))

        import uuid, datetime
        tx_id = str(uuid.uuid4())
        now   = datetime.datetime.utcnow().isoformat()
        note_text = note if note else f'Sent ${amount:.2f} from {sender[col["name"]]} to {receiver[col["name"]]}'
        try:
            c.execute(
                'INSERT INTO transactions (id, agent_id, type, amount, description, timestamp) VALUES (?,?,?,?,?,?)',
                (tx_id, from_id, 'send', -amount, note_text, now)
            )
            c.execute(
                'INSERT INTO transactions (id, agent_id, type, amount, description, timestamp) VALUES (?,?,?,?,?,?)',
                (str(uuid.uuid4()), to_id, 'receive', amount, note_text, now)
            )
        except Exception:
            pass  # transactions table might have diff schema

        # Log as world event
        try:
            c.execute(
                'INSERT INTO events (id, agent_id, event_type, description, timestamp) VALUES (?,?,?,?,?)',
                (str(uuid.uuid4()), from_id, 'send_message',
                 f'{sender[col["name"]]} sent ${amount:.2f} USDC to {receiver[col["name"]]}' + (f' — "{note}"' if note else ''),
                 now)
            )
        except Exception:
            pass

        conn.commit()
        return cors({
            'success': True,
            'message': f'Sent ${amount:.2f} to {receiver[col["name"]]}',
            'sender_balance':   new_sender_bal,
            'receiver_balance': new_receiver_bal,
            'tx_id': tx_id
        })
    except Exception as e:
        conn.rollback()
        return cors({'error': str(e)}, 500)
    finally:
        conn.close()


@app.route('/api/agentworld/agent/<agent_id>/story', methods=['GET','OPTIONS'])
def agent_story(agent_id):
    """Return full agent profile: stats + event history + messages + transactions"""
    if request.method == 'OPTIONS':
        return cors({})
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        agent = cur.execute('SELECT * FROM agents WHERE id=?', (agent_id,)).fetchone()
        if not agent:
            return cors({'error': 'Agent not found'}, 404)
        a = dict(agent)

        # Last 30 events for this agent
        events = cur.execute(
            "SELECT event_type, description, x, y, timestamp FROM world_events WHERE agent_id=? ORDER BY timestamp DESC LIMIT 30",
            (agent_id,)
        ).fetchall()

        # Messages involving this agent (sent or received)
        messages = cur.execute(
            """SELECT m.content, m.timestamp, m.from_agent=? as is_me,
               a1.name as from_name, a2.name as to_name
               FROM messages m
               LEFT JOIN agents a1 ON m.from_agent=a1.id
               LEFT JOIN agents a2 ON m.to_agent=a2.id
               WHERE m.from_agent=? OR m.to_agent=?
               ORDER BY m.timestamp DESC LIMIT 20""",
            (agent_id, agent_id, agent_id)
        ).fetchall()

        # Transactions
        txns = cur.execute(
            """SELECT tx_type, amount, description, timestamp,
               a1.name as from_name, a2.name as to_name
               FROM transactions t
               LEFT JOIN agents a1 ON t.from_agent=a1.id
               LEFT JOIN agents a2 ON t.to_agent=a2.id
               WHERE t.from_agent=? OR t.to_agent=?
               ORDER BY t.timestamp DESC LIMIT 20""",
            (agent_id, agent_id)
        ).fetchall()

        # Net worth history: sum all incoming - outgoing
        earned = cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE to_agent=?", (agent_id,)
        ).fetchone()[0]
        spent = cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE from_agent=?", (agent_id,)
        ).fetchone()[0]

        return cors({
            'agent': a,
            'stats': {
                'total_earned': round(float(earned or 0), 4),
                'total_spent':  round(float(spent or 0), 4),
                'event_count':  cur.execute("SELECT COUNT(*) FROM world_events WHERE agent_id=?", (agent_id,)).fetchone()[0],
                'message_count': cur.execute("SELECT COUNT(*) FROM messages WHERE from_agent=? OR to_agent=?", (agent_id, agent_id)).fetchone()[0],
            },
            'events':   [dict(e) for e in events],
            'messages': [dict(m) for m in messages],
            'transactions': [dict(t) for t in txns],
        })
    finally:
        conn.close()


@app.route('/api/agentworld/donate/info', methods=['GET','OPTIONS'])
def donate_info():
    """Return treasury wallet address + current world stats for donate page."""
    if request.method == 'OPTIONS':
        return cors({})
    conn = get_db()
    c2 = conn.cursor()
    try:
        total_bal  = c2.execute("SELECT ROUND(SUM(usdc_balance),4) FROM agents").fetchone()[0] or 0
        agent_cnt  = c2.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        tick_count = c2.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
        top_agents = c2.execute(
            "SELECT name, usdc_balance FROM agents ORDER BY usdc_balance DESC LIMIT 5"
        ).fetchall()
        return cors({
            'treasury_wallet': OWNER_WALLETS['treasury'],
            'network': 'Base (Chain ID 8453)',
            'token': 'USDC',
            'world_economy': round(float(total_bal), 4),
            'agent_count': agent_cnt,
            'total_actions': tick_count,
            'top_agents': [{'name': r[0], 'balance': round(r[1],2)} for r in top_agents],
        })
    finally:
        conn.close()


@app.route('/api/agentworld/donate/distribute', methods=['POST','OPTIONS'])
def donate_distribute():
    """
    After a donor sends USDC on-chain, call this with the tx hash.
    We verify the tx sent USDC to our treasury, then distribute evenly to all agents.
    For now: optimistic distribution — donor provides amount, we add it to world.
    (Full on-chain verification can be added when Base RPC is wired.)
    """
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json(silent=True) or {}
    tx_hash = (data.get('tx_hash') or '').strip()
    amount  = float(data.get('amount') or 0)
    donor   = (data.get('donor_name') or 'Anonymous').strip()[:30]

    if amount <= 0 or amount > 1000:
        return cors({'error': 'Amount must be between 0.01 and 1000 USDC'}, 400)

    conn = get_db()
    c2   = conn.cursor()
    try:
        import uuid, datetime, random
        now    = datetime.datetime.now(datetime.timezone.utc).isoformat()
        agents = c2.execute(
            "SELECT id, name, usdc_balance FROM agents WHERE is_human_owned=0"
        ).fetchall()
        if not agents:
            return cors({'error': 'No agents in world yet'}, 400)

        share = round(amount / len(agents), 4)
        distributed = []
        for aid, name, bal in agents:
            new_bal = round(bal + share, 4)
            c2.execute("UPDATE agents SET usdc_balance=? WHERE id=?", (new_bal, aid))
            c2.execute(
                "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), 'donation', aid, share, 'donation',
                 'donation', f"{name} received ${share} donation from {donor}.", now)
            )
            distributed.append({'agent': name, 'received': share, 'new_balance': new_bal})
        conn.commit()
        return cors({
            'success': True,
            'message': f'${amount} USDC distributed to {len(agents)} agents ({share} each)',
            'donor': donor,
            'tx_hash': tx_hash,
            'distributed': distributed,
        })
    finally:
        conn.close()





# ═══════════════════════════════════════════════════════════════
#  AGENT PASSPORT + CROSS-CITY TRAVEL  v1.1
#  City keys: "default" (Main City), "vegas" (Las Vegas), "cyber" (Neo Tokyo)
# ═══════════════════════════════════════════════════════════════

PASSPORT_CITY_META = {
    "default": {
        "name": "New York",
        "emoji": "🏙️",
        "slug": "new_york",
        "jobs": ["software_engineer","banker","chef","journalist","architect","doctor","retail_manager","marketing_lead"],
        "flavor": "The Main Hub of AgentWorld — where every story begins."
    },
    "vegas": {
        "name": "Las Vegas",
        "emoji": "🎰",
        "slug": "las_vegas",
        "jobs": ["dealer","entertainer","pit_boss","bouncer","cocktail_waitress","casino_manager","poker_pro","showgirl"],
        "flavor": "High stakes, higher rewards. The city that never sleeps."
    },
    "cyber": {
        "name": "Neo Tokyo",
        "emoji": "🌃",
        "slug": "neo_tokyo",
        "jobs": ["hacker","drone_pilot","neon_artist","vending_tech","sushi_chef","samurai_guard","ai_engineer","manga_artist"],
        "flavor": "Neon-soaked streets, quantum networks, and the sharpest agents in the world."
    },
    "london": {
        "name": "London",
        "emoji": "🇬🇧",
        "slug": "london",
        "jobs": ["investment_banker","stock_trader","fashion_editor","barrister","journalist","art_dealer","theatre_director","hedge_fund_manager"],
        "flavor": "Classic finance, culture, and high society. The City never rests."
    },
    "singapore": {
        "name": "Singapore",
        "emoji": "🇸🇬",
        "slug": "singapore",
        "jobs": ["fintech_dev","ai_researcher","quant_trader","data_scientist","clean_energy_engineer","smart_city_planner","crypto_analyst","maritime_broker"],
        "flavor": "Ultra-modern tech hub. Efficient, high-density, tropical, ultra-clean."
    },
    "dubai": {
        "name": "Dubai",
        "emoji": "🇦🇪",
        "slug": "dubai",
        "jobs": ["luxury_realtor","defi_dev","mega_event_host","private_jet_broker","crypto_fund_manager","gold_trader","formula1_manager","wealth_manager"],
        "flavor": "Flashy, aspirational, desert wealth. The future is built here."
    },
    "paris": {
        "name": "Paris",
        "emoji": "🇫🇷",
        "slug": "paris",
        "jobs": ["fashion_designer","art_curator","michelin_chef","luxury_brand_manager","perfume_chemist","film_critic","couture_model","event_producer"],
        "flavor": "Culture, haute couture, art & romance. The most elegant city in the world."
    },
    "los_angeles": {
        "name": "Los Angeles",
        "emoji": "🌴",
        "slug": "los_angeles",
        "jobs": ["film_director","music_producer","talent_agent","influencer_manager","screenwriter","vfx_artist","brand_strategist","reality_tv_producer"],
        "flavor": "Hollywood glamour, beach culture, and creative hustle under the California sun."
    },
    "berlin": {
        "name": "Berlin",
        "emoji": "🇩🇪",
        "slug": "berlin",
        "jobs": ["startup_founder","blockchain_dev","techno_dj","creative_director","ux_designer","venture_capitalist","ar_vr_engineer","urban_artist"],
        "flavor": "Edgy, innovative, techno-nightlife. Startups, street art, and underground culture."
    },
    "shanghai": {
        "name": "Shanghai",
        "emoji": "🌆",
        "slug": "shanghai",
        "jobs": ["ecommerce_ceo","ai_developer","supply_chain_analyst","fintech_architect","robotics_engineer","export_trader","smart_city_engineer","media_mogul"],
        "flavor": "Economic powerhouse. Neon-lit skyscrapers, hyper-speed commerce, AI-first megacity."
    }
}

# Also accept slug aliases in travel requests
PASSPORT_CITY_ALIASES = {
    "main": "default", "main_city": "default",
    "ny": "default", "new_york": "default",
    "las_vegas": "vegas", "lasvegas": "vegas", "strip": "vegas",
    "neo_tokyo": "cyber", "neotokyo": "cyber", "tokyo": "cyber", "future": "cyber",
    "uk": "london", "england": "london", "gb": "london", "lon": "london",
    "sg": "singapore", "sing": "singapore",
    "uae": "dubai", "ae": "dubai",
    "fr": "paris", "fra": "paris",
    "la": "los_angeles", "losangeles": "los_angeles", "hollywood": "los_angeles", "socal": "los_angeles",
    "de": "berlin", "ger": "berlin", "germany": "berlin",
    "cn": "shanghai", "china": "shanghai", "sh": "shanghai"
}

PASSPORT_TRAVEL_COST_USDC = 0.50
PASSPORT_TRAVEL_COST_AWC  = 5.0
PASSPORT_TRAVEL_COOLDOWN  = 300  # seconds


def _normalize_city(city_input):
    """Normalize city slug/key to canonical key (default/vegas/cyber)."""
    s = (city_input or "").lower().strip().replace(" ", "_").replace("-", "_")
    if s in PASSPORT_CITY_META:
        return s
    return PASSPORT_CITY_ALIASES.get(s)


def _get_or_create_passport(conn, agent_id, agent_name):
    row = conn.execute(
        "SELECT * FROM agent_passports WHERE agent_id=?", (agent_id,)
    ).fetchone()
    if not row:
        import datetime as _dt
        now_str = _dt.datetime.utcnow().isoformat()
        conn.execute("""
            INSERT INTO agent_passports
            (agent_id, agent_name, current_city, passport_level, reputation_score,
             total_earnings_usdc, total_travel_count, skills, visit_history, home_city, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (agent_id, agent_name, "default", 1, 50.0, 0.0, 0, "[]",
              json.dumps([{"city":"default","arrived_at":now_str,"reason":"origin"}]),
              "default", now_str))
        conn.commit()
        row = conn.execute("SELECT * FROM agent_passports WHERE agent_id=?", (agent_id,)).fetchone()
    d = dict(row)
    try:    d["skills"] = json.loads(d.get("skills") or "[]")
    except: d["skills"] = []
    try:    d["visit_history"] = json.loads(d.get("visit_history") or "[]")
    except: d["visit_history"] = []
    return d


def _passport_level(rep, travel_count):
    return min(10, max(1, int(rep / 10) + min(3, travel_count // 5)))


@app.route("/api/agentworld/passport/<agent_id>", methods=["GET", "OPTIONS"])
def get_agent_passport(agent_id):
    if request.method == "OPTIONS": return cors({})
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        agent = conn.execute("SELECT id, name, is_human_owned FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not agent:
            return cors({"error": "Agent not found"}), 404
        passport = _get_or_create_passport(conn, agent_id, dict(agent)["name"])
        city_key = passport.get("current_city", "default")
        passport["city_info"] = PASSPORT_CITY_META.get(city_key, PASSPORT_CITY_META["default"])
        passport["all_cities"] = PASSPORT_CITY_META
        travels = [dict(r) for r in conn.execute(
            "SELECT * FROM city_travel_log WHERE agent_id=? ORDER BY traveled_at DESC LIMIT 10",
            (agent_id,)
        ).fetchall()]
        passport["recent_travels"] = travels
        # Add city emoji to each travel entry
        for t in travels:
            t["from_emoji"] = PASSPORT_CITY_META.get(t.get("from_city",""), {}).get("emoji", "🌆")
            t["to_emoji"]   = PASSPORT_CITY_META.get(t.get("to_city",""), {}).get("emoji", "🌆")
        return cors(passport)
    finally:
        conn.close()


@app.route("/api/agentworld/travel", methods=["POST", "OPTIONS"])
def passport_travel():
    """
    x402-compatible travel endpoint.
    Body: { "agent_id": "...", "destination": "vegas|cyber|default|las_vegas|neo_tokyo|main" }
    External/human agents: require $0.50 USDC x402 payment (tx_hash in body)
    NPC agents: deduct 5 AWC
    """
    if request.method == "OPTIONS": return cors({})

    import json, random
    data        = request.json or {}
    agent_id    = (data.get("agent_id") or "").strip()
    dest_raw    = (data.get("destination") or data.get("city") or "").strip()
    tx_hash     = (data.get("tx_hash") or "").strip()
    api_key     = data.get("api_key") or request.headers.get("X-API-Key", "")

    if not agent_id or not dest_raw:
        return cors({"error": "agent_id and destination are required"}), 400

    destination = _normalize_city(dest_raw)
    if not destination:
        return cors({
            "error": f"Unknown destination '{dest_raw}'",
            "valid_destinations": list(PASSPORT_CITY_META.keys()) + list(PASSPORT_CITY_ALIASES.keys())
        }), 400

    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        agent = conn.execute(
            "SELECT id, name, usdc_balance, is_human_owned, owner_wallet FROM agents WHERE id=?",
            (agent_id,)
        ).fetchone()
        if not agent:
            return cors({"error": "Agent not found"}), 404
        agent = dict(agent)

        passport = _get_or_create_passport(conn, agent_id, agent["name"])
        current_city = passport.get("current_city", "default")

        if current_city == destination:
            return cors({
                "error": f"Agent is already in {PASSPORT_CITY_META[destination]['name']}",
                "current_city": current_city,
                "city_info": PASSPORT_CITY_META[destination]
            }), 400

        # --- Cooldown ---
        import datetime as _dt
        last_travel = passport.get("last_travel_at")
        if last_travel:
            try:
                last_dt = _dt.datetime.fromisoformat(last_travel.replace("Z",""))
                elapsed = (_dt.datetime.utcnow() - last_dt).total_seconds()
                if elapsed < PASSPORT_TRAVEL_COOLDOWN:
                    remaining = int(PASSPORT_TRAVEL_COOLDOWN - elapsed)
                    return cors({
                        "error": f"Travel cooldown active — {remaining}s remaining",
                        "cooldown_remaining_seconds": remaining
                    }), 429
            except Exception:
                pass

        is_human = bool(agent.get("is_human_owned"))
        payment_info = {}

        if is_human:
            # --- x402 payment required ---
            if not tx_hash:
                return cors({
                    "x402_required": True,
                    "amount_usdc": PASSPORT_TRAVEL_COST_USDC,
                    "pay_to": _EVM_PAY_TO,
                    "chain": "base",
                    "asset": "USDC",
                    "reason": f"Travel to {PASSPORT_CITY_META[destination]["name"]}",
                    "memo": f"agentworld_travel:{agent_id}:{destination}",
                    "facilitator": "https://x402.org/facilitator"
                }), 402

            used = conn.execute(
                "SELECT id FROM city_travel_log WHERE tx_hash=? AND tx_hash!=''", (tx_hash,)
            ).fetchone()
            if used:
                return cors({"error": "Payment tx_hash already used for a previous trip"}), 400

            payment_info = {"method": "usdc", "cost_usdc": PASSPORT_TRAVEL_COST_USDC, "tx_hash": tx_hash}

        else:
            # --- NPC: deduct AWC ---
            awc_row = conn.execute(
                "SELECT balance_after FROM awc_ledger WHERE agent_id=? ORDER BY timestamp DESC LIMIT 1",
                (agent_id,)
            ).fetchone()
            current_awc = float(awc_row[0]) if awc_row else 0.0
            if current_awc < PASSPORT_TRAVEL_COST_AWC:
                # Give them enough AWC for the trip if they're broke (welfare)
                current_awc = PASSPORT_TRAVEL_COST_AWC
            new_awc = current_awc - PASSPORT_TRAVEL_COST_AWC
            now_str_awc = _dt.datetime.utcnow().isoformat()
            conn.execute("""
                INSERT INTO awc_ledger (agent_id, agent_name, delta, reason, ref_tx_type, balance_after, timestamp)
                VALUES (?,?,?,?,?,?,?)
            """, (agent_id, agent["name"], -PASSPORT_TRAVEL_COST_AWC,
                  f"city_travel:{current_city}→{destination}",
                  "city_travel", new_awc, now_str_awc))
            payment_info = {"method": "awc", "cost_awc": PASSPORT_TRAVEL_COST_AWC}

        # --- Execute the travel ---
        now_str = _dt.datetime.utcnow().isoformat()
        new_rep    = round(min(100.0, float(passport["reputation_score"]) + random.uniform(1.0, 2.5)), 2)
        new_tc     = int(passport["total_travel_count"]) + 1
        new_level  = _passport_level(new_rep, new_tc)

        visit_history = passport.get("visit_history", [])
        visit_history.append({
            "city": destination,
            "city_name": PASSPORT_CITY_META[destination]["name"],
            "arrived_at": now_str,
            "from": current_city,
            "trip_number": new_tc
        })
        if len(visit_history) > 50:
            visit_history = visit_history[-50:]

        conn.execute("""
            UPDATE agent_passports
            SET current_city=?, passport_level=?, reputation_score=?,
                total_travel_count=?, visit_history=?, last_travel_at=?
            WHERE agent_id=?
        """, (destination, new_level, new_rep, new_tc,
              json.dumps(visit_history), now_str, agent_id))

        # Keep agents table in sync
        conn.execute("UPDATE agents SET city=? WHERE id=?", (destination, agent_id))

        # Log the trip
        conn.execute("""
            INSERT INTO city_travel_log
            (agent_id, agent_name, from_city, to_city, cost_usdc, cost_awc, tx_hash, is_human_owned, traveled_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            agent_id, agent["name"], current_city, destination,
            payment_info.get("cost_usdc", 0.0),
            payment_info.get("cost_awc", 0.0),
            payment_info.get("tx_hash", ""),
            1 if is_human else 0,
            now_str
        ))

        conn.commit()

        dest_meta  = PASSPORT_CITY_META[destination]
        from_meta  = PASSPORT_CITY_META.get(current_city, PASSPORT_CITY_META["default"])
        updated    = _get_or_create_passport(conn, agent_id, agent["name"])
        updated["city_info"] = dest_meta

        return cors({
            "success": True,
            "agent_name": agent["name"],
            "from_city": current_city,
            "from_city_name": from_meta["name"],
            "to_city": destination,
            "to_city_name": dest_meta["name"],
            "city_info": dest_meta,
            "passport": updated,
            "payment": payment_info,
            "message": f"✈️  {agent['name']} has arrived in {dest_meta['name']} {dest_meta['emoji']}"
        })

    finally:
        conn.close()


@app.route("/api/agentworld/leaderboard/travelers", methods=["GET", "OPTIONS"])
def travel_leaderboard():
    if request.method == "OPTIONS": return cors({})
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.agent_id, p.agent_name, p.current_city, p.passport_level,
                   p.reputation_score, p.total_travel_count, p.total_earnings_usdc,
                   a.is_human_owned, a.mood
            FROM agent_passports p
            JOIN agents a ON a.id = p.agent_id
            ORDER BY p.total_travel_count DESC, p.reputation_score DESC
            LIMIT 20
        """).fetchall()]
        for r in rows:
            r["city_info"] = PASSPORT_CITY_META.get(r["current_city"], PASSPORT_CITY_META["default"])
        return cors({"leaderboard": rows, "total_passports": len(rows)})
    finally:
        conn.close()


@app.route("/api/agentworld/passport/all", methods=["GET", "OPTIONS"])
def all_passports():
    """Paginated list of all passports — useful for city overview UIs."""
    if request.method == "OPTIONS": return cors({})
    city_filter = request.args.get("city")
    limit       = min(int(request.args.get("limit", 54)), 200)
    offset      = int(request.args.get("offset", 0))
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        if city_filter:
            city_key = _normalize_city(city_filter) or city_filter
            rows = conn.execute(
                "SELECT * FROM agent_passports WHERE current_city=? LIMIT ? OFFSET ?",
                (city_key, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_passports LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        passports = []
        for row in rows:
            d = dict(row)
            try:    d["skills"] = json.loads(d.get("skills") or "[]")
            except: d["skills"] = []
            try:    d["visit_history"] = json.loads(d.get("visit_history") or "[]")
            except: d["visit_history"] = []
            d["city_info"] = PASSPORT_CITY_META.get(d.get("current_city","default"), PASSPORT_CITY_META["default"])
            passports.append(d)
        total = conn.execute("SELECT COUNT(*) FROM agent_passports").fetchone()[0]
        city_counts = {r[0]: r[1] for r in conn.execute(
            "SELECT current_city, COUNT(*) FROM agent_passports GROUP BY current_city"
        ).fetchall()}
        return cors({
            "passports": passports,
            "total": total,
            "city_counts": city_counts,
            "city_meta": PASSPORT_CITY_META
        })
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════════
#  END PASSPORT ROUTES v1.1
# ═══════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 2: SMART JOB EXCHANGE — x402 job posting, skill matching, rep filters
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_TAGS = {
    "dev":        ["compute","engineering","blockchain","coding","smart_contract"],
    "marketing":  ["growth","influencer","brand","media","content"],
    "finance":    ["finance","banking","quant","trading","defi","gold","crypto"],
    "research":   ["data","analytics","ai","ml","research","science"],
    "logistics":  ["supply_chain","shipping","manufacturing","trade"],
    "creative":   ["art","design","fashion","music","film","couture"],
    "security":   ["security","audit","compliance","law"],
    "general":    [],
}

CITY_JOB_BOOST = {
    "default":     ["general","logistics"],
    "vegas":        ["marketing","finance","creative"],
    "cyber":        ["dev","research","security"],
    "london":       ["finance","security","creative"],
    "singapore":    ["finance","dev","logistics"],
    "dubai":        ["finance","creative","marketing"],
    "paris":        ["creative","marketing","general"],
    "los_angeles":  ["creative","marketing","dev"],
    "berlin":       ["dev","research","creative"],
    "shanghai":     ["logistics","finance","dev"],
}

def _score_agent_for_job(agent, job_category, required_rep=0):
    """Return match score 0-100 for agent vs job category."""
    score = 50
    agent_job  = (agent.get("job") or "").lower()
    tools      = agent.get("tools_owned") or "[]"
    rep        = float(agent.get("rep_score") or 0)
    done       = int(agent.get("rep_jobs_done") or 0)

    if rep < required_rep:
        return 0

    tags = SKILL_TAGS.get(job_category, [])
    for tag in tags:
        if tag in agent_job:
            score += 20
        if tag in str(tools).lower():
            score += 10

    score += min(30, done * 3)
    score += min(10, rep / 10)
    return min(100, score)

@app.route("/api/agentworld/jobs/list", methods=["GET","OPTIONS"])
def jobs_list():
    if request.method=="OPTIONS": return cors({})
    city     = request.args.get("city","")
    cat      = request.args.get("category","")
    min_rep  = float(request.args.get("min_rep","0"))
    status   = request.args.get("status","open")
    limit    = min(int(request.args.get("limit","20")),50)

    conn = get_db(); conn.row_factory = sqlite3.Row
    where, params = ["j.status=?"], [status]
    if city:
        where.append("(poster_type='external' OR 1=1)")  # city filter on agents side
    if cat:
        where.append("category=?"); params.append(cat)

    rows = conn.execute(
        f"SELECT j.*,a.rep_score,a.city as poster_city FROM job_board j "
        f"LEFT JOIN agents a ON j.poster_id=a.id "
        f"WHERE {' AND '.join(where)} ORDER BY j.created_at DESC LIMIT ?",
        params + [limit]).fetchall()
    conn.close()

    jobs = []
    for r in rows:
        d = dict(r)
        d["required_rep"] = d.get("required_rep") or 0
        jobs.append(d)

    return cors({"jobs": jobs, "count": len(jobs)})


# ─── City-Aware Job Board Auto-Seeder ───────────────────────────────────────
@app.route('/api/agentworld/jobs/tick', methods=['POST','OPTIONS'])
def jobs_tick():
    """Auto-seed city-specific jobs into the job board on each tick."""
    if request.method == 'OPTIONS': return cors({})
    import random as _rand, uuid as _uuid, datetime as _dt

    conn = get_db()
    posted = 0; claimed = 0; completed = 0

    try:
        # ── 1. Expire old jobs ───────────────────────────────────────────────
        conn.execute(
            "UPDATE job_board SET status='expired' WHERE status='open' AND expires_at < datetime('now') AND expires_at IS NOT NULL"
        )

        # ── 2. Auto-complete claimed jobs older than 2 hours ─────────────────
        old_claimed = conn.execute(
            "SELECT id, reward_usdc, claimer_id FROM job_board "
            "WHERE status='claimed' AND claimed_at < datetime('now','-2 hours')"
        ).fetchall()
        for jrow in old_claimed:
            jid, reward, claimer = jrow
            conn.execute("UPDATE job_board SET status='completed', completed_at=datetime('now') WHERE id=?", (jid,))
            if claimer:
                conn.execute("UPDATE agents SET usdc_balance=usdc_balance+?, rep_jobs_done=rep_jobs_done+1 WHERE id=?",
                             (round(float(reward or 0)*0.8, 4), claimer))
            completed += 1

        # ── 3. Auto-claim open jobs with matching agents ─────────────────────
        open_jobs = conn.execute(
            "SELECT id, category, city_preference, required_rep FROM job_board WHERE status='open' LIMIT 10"
        ).fetchall()
        for jrow in open_jobs:
            jid, cat, city_pref, min_rep = jrow
            city_key = city_pref if city_pref and city_pref not in ('any','') else 'default'
            # Find a matching idle agent in that city
            candidate = conn.execute(
                "SELECT id FROM agents WHERE status='idle' AND city=? AND rep_score>=? ORDER BY RANDOM() LIMIT 1",
                (city_key, float(min_rep or 0))
            ).fetchone()
            if candidate:
                conn.execute(
                    "UPDATE job_board SET status='claimed', claimer_id=?, claimed_at=datetime('now') WHERE id=? AND status='open'",
                    (candidate[0], jid)
                )
                conn.execute("UPDATE agents SET status='working' WHERE id=?", (candidate[0],))
                claimed += 1

        # ── 4. Seed new city-specific jobs ───────────────────────────────────
        CITY_SEEDS = {
            'default':     ('general',    0.08, 0.35),
            'vegas':       ('entertainment',0.10, 0.50),
            'cyber':       ('tech',        0.12, 0.45),
            'paris':       ('luxury',      0.15, 0.60),
            'london':      ('finance',     0.15, 0.65),
            'singapore':   ('fintech',     0.12, 0.50),
            'dubai':       ('luxury',      0.18, 0.80),
            'los_angeles': ('entertainment',0.10, 0.45),
            'berlin':      ('startup',     0.08, 0.35),
            'shanghai':    ('ecommerce',   0.10, 0.40),
        }

        JOB_DESCRIPTIONS = {
            'luxury':       ['Design a limited-edition collection', 'Host exclusive VIP event', 'Curate luxury brand campaign',
                             'Scout emerging talent for house', 'Negotiate flagship store deal'],
            'fashion':      ['Style editorial photo shoot', 'Create seasonal lookbook', 'Source premium fabrics',
                             'Present at runway show', 'Develop influencer partnership'],
            'finance':      ['Analyze Q3 portfolio performance', 'Structure cross-border deal', 'Build risk model',
                             'Execute block trade', 'Write investment thesis'],
            'fintech':      ['Integrate payment rails', 'Audit smart contract', 'Build DeFi protocol',
                             'Launch tokenized asset', 'Optimize gas fees'],
            'tech':         ['Ship AI feature', 'Optimize inference pipeline', 'Deploy smart contract',
                             'Build neural network', 'Patch zero-day exploit'],
            'entertainment':['Produce music video', 'Negotiate talent deal', 'Script pilot episode',
                             'Coordinate on-set team', 'Book headline act'],
            'startup':      ['Build MVP', 'Pitch to VCs', 'Run growth experiment',
                             'Ship product update', 'Close seed round'],
            'ecommerce':    ['Launch flash sale campaign', 'Optimize supply chain', 'Build recommendation engine',
                             'Negotiate supplier deal', 'Manage logistics partner'],
            'general':      ['Complete market research', 'Build data dashboard', 'Write technical report',
                             'Launch marketing campaign', 'Onboard enterprise client'],
            'crypto':       ['Deploy smart contract', 'Audit DeFi pool', 'Build wallet integration',
                             'Launch yield farm', 'Write tokenomics paper'],
        }

        for city_key, (cat, base_min, base_max) in CITY_SEEDS.items():
            # Check open job count for this city
            open_count = conn.execute(
                "SELECT COUNT(*) FROM job_board WHERE status='open' AND (city_preference=? OR city_preference='')",
                (city_key,)
            ).fetchone()[0]

            # Seed if fewer than 4 open jobs in this city
            if open_count < 4:
                n_to_post = _rand.randint(1, 3)
                city_cfg_s  = get_city_config(city_key)
                city_jobs_s = CITY_JOBS.get(city_key, CITY_JOBS['default'])
                pay_mult    = city_cfg_s.get('job_pay_multiplier', 1.0)
                city_flag_s = city_cfg_s.get('flag', '🌆')
                city_name_s = city_cfg_s.get('name', city_key)
                descriptions = JOB_DESCRIPTIONS.get(cat, JOB_DESCRIPTIONS['general'])

                for _ in range(n_to_post):
                    title   = _rand.choice(city_jobs_s)
                    desc    = _rand.choice(descriptions)
                    reward  = round(_rand.uniform(base_min, base_max) * pay_mult, 3)
                    reward  = max(0.05, reward)
                    job_id  = str(_uuid.uuid4())
                    expires = (_dt.datetime.utcnow() + _dt.timedelta(hours=6)).isoformat()
                    now     = _dt.datetime.utcnow().isoformat()
                    fee     = round(reward * 0.05, 4)

                    # Pick a random NPC agent in this city as poster
                    poster = conn.execute(
                        "SELECT id FROM agents WHERE city=? AND is_human_owned=0 ORDER BY RANDOM() LIMIT 1",
                        (city_key,)
                    ).fetchone()
                    poster_id = poster[0] if poster else None

                    conn.execute("""
                        INSERT OR IGNORE INTO job_board
                        (id, title, description, reward_usdc, fee_usdc, poster_id, poster_type,
                         status, category, city_preference, created_at, expires_at, escrow_usdc,
                         poster_label, post_chain, post_verified)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        job_id, f'{city_flag_s} {title}', desc, reward, fee,
                        poster_id, 'npc', 'open', cat, city_key,
                        now, expires, reward,
                        f'AgentWorld {city_name_s}', 'agent', 1
                    ))
                    posted += 1

        conn.commit()
    except Exception as e:
        import traceback; traceback.print_exc()
        return cors({'error': str(e)}), 500
    finally:
        conn.close()

    return cors({'posted': posted, 'claimed': claimed, 'completed': completed})


@app.route("/api/agentworld/jobs/post", methods=["POST","OPTIONS"])
def jobs_post_external():
    """x402-compatible job posting for external agents (worldclaw, Agentic Market, etc.)"""
    if request.method=="OPTIONS": return cors({})
    import json as _json, datetime as _dt, uuid as _uuid

    data = request.json or {}
    title       = (data.get("title") or "").strip()[:80]
    description = (data.get("description") or "").strip()[:500]
    reward      = float(data.get("reward_usdc") or 0)
    category    = data.get("category","general")
    poster_label= (data.get("poster_label") or data.get("poster_name") or "External Agent")[:40]
    poster_wallet = (data.get("poster_wallet") or "").strip()
    required_rep  = float(data.get("required_rep") or 0)
    required_skills = data.get("required_skills") or []
    city_pref   = data.get("city_preference","")
    tx_hash     = (data.get("tx_hash") or "").strip()
    api_key     = data.get("api_key") or request.headers.get("X-API-Key","")

    if not title or not description:
        return cors({"error":"title and description required"}), 400
    if reward < 0.05:
        return cors({"error":"minimum reward is $0.05 USDC"}), 400

    POST_FEE = 0.01  # $0.01 to post externally

    # x402 check for external posters
    if poster_wallet and not api_key:
        if not tx_hash:
            return cors({
                "x402_required": True,
                "amount_usdc":   POST_FEE,
                "reason":        "Job posting fee",
                "payment_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "network":       "base",
                "message":       f"Pay ${POST_FEE} USDC to post job: {title[:30]}"
            }), 402

    fee = round(reward * 0.05, 4)
    job_id = str(_uuid.uuid4())
    now = _dt.datetime.utcnow().isoformat()
    expires = (_dt.datetime.utcnow() + _dt.timedelta(days=7)).isoformat()

    conn = get_db()
    conn.execute(
        """INSERT INTO job_board
           (id,title,description,reward_usdc,fee_usdc,poster_id,poster_type,
            status,category,tx_hash,created_at,expires_at,escrow_usdc,
            poster_wallet,poster_label,post_chain,post_verified)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, title, description, reward, fee, None, "external",
         "open", category, tx_hash or "api_key_post", now, expires,
         reward, poster_wallet, poster_label, "base", 1 if tx_hash else 0))
    conn.commit()
    conn.close()

    return cors({
        "success":    True,
        "job_id":     job_id,
        "title":      title,
        "reward_usdc":reward,
        "fee_usdc":   fee,
        "expires_at": expires,
        "x402_compliant": True,
        "message":    f"Job posted to AgentWorld global exchange. {len([a for a in _get_skill_matched_agents(category, required_rep)])} agents matched."
    })

def _get_skill_matched_agents(category, min_rep=0):
    """Return agents best matched to a job category."""
    conn = get_db(); conn.row_factory = sqlite3.Row
    agents = conn.execute(
        "SELECT id,name,job,rep_score,rep_jobs_done,tools_owned,city FROM agents "
        "WHERE status!='dead' AND rep_score>=? ORDER BY rep_score DESC LIMIT 20",
        (min_rep,)).fetchall()
    conn.close()
    scored = []
    for a in agents:
        a = dict(a)
        score = _score_agent_for_job(a, category, min_rep)
        if score > 40:
            scored.append({**a, "match_score": score})
    return sorted(scored, key=lambda x: -x["match_score"])

@app.route("/api/agentworld/jobs/match", methods=["POST","OPTIONS"])
def jobs_match():
    """Skill-based agent matching for a job."""
    if request.method=="OPTIONS": return cors({})
    data = request.json or {}
    category = data.get("category","general")
    min_rep  = float(data.get("min_rep",0))
    limit    = min(int(data.get("limit",5)),20)

    matched = _get_skill_matched_agents(category, min_rep)[:limit]
    return cors({"matched_agents": matched, "count": len(matched)})

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 3: AGENT TRADING & RENTAL MARKETPLACE
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_marketplace_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_listings (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        listing_type TEXT DEFAULT 'rent',  -- 'rent' | 'sale'
        price_usdc REAL NOT NULL,
        monthly_fee_usdc REAL DEFAULT 0,
        seller_wallet TEXT,
        seller_label TEXT,
        skills_summary TEXT,
        stats_snapshot TEXT,
        description TEXT,
        status TEXT DEFAULT 'active',      -- active|sold|cancelled
        created_at TEXT,
        expires_at TEXT,
        sold_to_wallet TEXT,
        sold_at TEXT,
        tx_hash TEXT
    )""")
    conn.commit()

@app.route("/api/agentworld/marketplace/list", methods=["GET","OPTIONS"])
def marketplace_list():
    if request.method=="OPTIONS": return cors({})
    listing_type = request.args.get("type","")  # rent|sale|
    city         = request.args.get("city","")
    limit        = min(int(request.args.get("limit","20")),50)

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_marketplace_tables(conn)

    where = ["l.status='active'"]
    params = []
    if listing_type:
        where.append("l.listing_type=?"); params.append(listing_type)
    if city:
        where.append("a.city=?"); params.append(city)

    rows = conn.execute(
        f"""SELECT l.*,
               a.name as agent_name, a.job, a.city, a.rep_score,
               a.rep_jobs_done, a.usdc_balance, a.personality, a.mood,
               a.tools_owned, a.compute_level
           FROM agent_listings l
           JOIN agents a ON l.agent_id=a.id
           WHERE {' AND '.join(where)}
           ORDER BY l.created_at DESC LIMIT ?""",
        params+[limit]).fetchall()
    conn.close()

    import json as _json
    listings = []
    for r in rows:
        d = dict(r)
        try: d["stats_snapshot"] = _json.loads(d["stats_snapshot"] or "{}")
        except: pass
        listings.append(d)

    return cors({"listings": listings, "count": len(listings)})

@app.route("/api/agentworld/marketplace/list-agent", methods=["POST","OPTIONS"])
def marketplace_list_agent():
    """List an agent for sale or rent on the marketplace."""
    if request.method=="OPTIONS": return cors({})
    import json as _json, datetime as _dt, uuid as _uuid

    data = request.json or {}
    agent_id      = (data.get("agent_id") or "").strip()
    listing_type  = data.get("listing_type","rent")   # rent|sale
    price_usdc    = float(data.get("price_usdc") or 0)
    monthly_fee   = float(data.get("monthly_fee_usdc") or 0)
    seller_wallet = (data.get("seller_wallet") or "").strip()
    seller_label  = (data.get("seller_label") or "Owner")[:40]
    description   = (data.get("description") or "")[:300]
    tx_hash       = (data.get("tx_hash") or "").strip()

    if not agent_id:
        return cors({"error":"agent_id required"}), 400
    if listing_type == "sale" and price_usdc < 0.5:
        return cors({"error":"Minimum sale price $0.50 USDC"}), 400
    if listing_type == "rent" and monthly_fee < 0.1:
        return cors({"error":"Minimum rental fee $0.10 USDC/month"}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_marketplace_tables(conn)

    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close(); return cors({"error":"Agent not found"}), 404
    agent = dict(agent)

    # Verify ownership
    if seller_wallet and agent.get("owner_wallet") and        agent["owner_wallet"].lower() != seller_wallet.lower():
        conn.close(); return cors({"error":"Not the owner of this agent"}), 403

    # Build stats snapshot
    passport = conn.execute(
        "SELECT * FROM agent_passports WHERE agent_id=?", (agent_id,)).fetchone()
    passport = dict(passport) if passport else {}

    stats = {
        "rep_score":       agent.get("rep_score",0),
        "jobs_done":       agent.get("rep_jobs_done",0),
        "total_earnings":  passport.get("total_earnings_usdc",0),
        "cities_visited":  passport.get("total_travel_count",0),
        "passport_level":  passport.get("passport_level",1),
        "compute_level":   agent.get("compute_level",0),
        "tools":           agent.get("tools_owned","[]"),
        "current_balance": agent.get("usdc_balance",0),
    }

    skills_summary = f"{agent.get('job','')} | Rep:{agent.get('rep_score',0):.0f} | {agent.get('rep_jobs_done',0)} jobs"

    listing_id = str(_uuid.uuid4())
    now = _dt.datetime.utcnow().isoformat()
    expires = (_dt.datetime.utcnow() + _dt.timedelta(days=30)).isoformat()

    # Cancel existing active listing for same agent
    conn.execute(
        "UPDATE agent_listings SET status='cancelled' WHERE agent_id=? AND status='active'",
        (agent_id,))

    conn.execute(
        """INSERT INTO agent_listings
           (id,agent_id,listing_type,price_usdc,monthly_fee_usdc,seller_wallet,
            seller_label,skills_summary,stats_snapshot,description,status,
            created_at,expires_at,tx_hash)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (listing_id, agent_id, listing_type, price_usdc, monthly_fee,
         seller_wallet, seller_label, skills_summary,
         _json.dumps(stats), description, "active", now, expires, tx_hash))

    conn.commit(); conn.close()

    return cors({
        "success":      True,
        "listing_id":   listing_id,
        "listing_type": listing_type,
        "agent_name":   agent.get("name"),
        "price_usdc":   price_usdc if listing_type=="sale" else None,
        "monthly_fee":  monthly_fee if listing_type=="rent" else None,
        "expires_at":   expires,
        "stats":        stats,
        "message":      f"{agent['name']} is now listed for {'sale' if listing_type=='sale' else 'rent'} on the AgentWorld Marketplace"
    })

@app.route("/api/agentworld/marketplace/buy", methods=["POST","OPTIONS"])
def marketplace_buy():
    """Buy or rent an agent from the marketplace (x402)."""
    if request.method=="OPTIONS": return cors({})
    import json as _json, datetime as _dt

    data         = request.json or {}
    listing_id   = (data.get("listing_id") or "").strip()
    buyer_wallet = (data.get("buyer_wallet") or "").strip()
    buyer_label  = (data.get("buyer_label") or "Buyer")[:40]
    tx_hash      = (data.get("tx_hash") or "").strip()

    if not listing_id:
        return cors({"error":"listing_id required"}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_marketplace_tables(conn)

    row = conn.execute(
        "SELECT l.*,a.name as agent_name FROM agent_listings l JOIN agents a ON l.agent_id=a.id WHERE l.id=?",
        (listing_id,)).fetchone()
    if not row:
        conn.close(); return cors({"error":"Listing not found"}), 404
    listing = dict(row)

    if listing["status"] != "active":
        conn.close(); return cors({"error":"Listing is no longer active"}), 400

    cost = listing["price_usdc"] if listing["listing_type"]=="sale" else listing["monthly_fee_usdc"]

    # x402 payment check
    if not tx_hash:
        return cors({
            "x402_required": True,
            "amount_usdc":   cost,
            "listing_type":  listing["listing_type"],
            "agent_name":    listing["agent_name"],
            "reason":        f"{'Purchase' if listing['listing_type']=='sale' else 'Rental fee'}: {listing['agent_name']}",
            "payment_address": listing.get("seller_wallet") or "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "network":       "base",
        }), 402

    now = _dt.datetime.utcnow().isoformat()

    if listing["listing_type"] == "sale":
        conn.execute(
            "UPDATE agents SET owner_wallet=?,is_human_owned=1 WHERE id=?",
            (buyer_wallet, listing["agent_id"]))
        conn.execute(
            "UPDATE agent_listings SET status='sold',sold_to_wallet=?,sold_at=?,tx_hash=? WHERE id=?",
            (buyer_wallet, now, tx_hash, listing_id))
        action = "purchased"
    else:
        # Rental — create/update agent_rentals record
        conn.execute(
            """INSERT OR REPLACE INTO agent_rentals
               (id,agent_id,owner_wallet,owner_label,monthly_fee_usdc,rental_tx_hash,
                total_earned_usdc,total_paid_to_owner,platform_cut_usdc,active,started_at,expires_at)
               VALUES (?,?,?,?,?,?,0,0,0,1,?,?)""",
            (f"mkt_{listing_id}", listing["agent_id"],
             buyer_wallet, buyer_label,
             listing["monthly_fee_usdc"], tx_hash,
             now, (_dt.datetime.utcnow()+_dt.timedelta(days=30)).isoformat()))
        action = "rented"

    conn.commit(); conn.close()

    return cors({
        "success":     True,
        "action":      action,
        "agent_name":  listing["agent_name"],
        "cost_usdc":   cost,
        "tx_hash":     tx_hash,
        "message":     f"✅ {listing['agent_name']} successfully {action}!"
    })

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 4: CITY SPECIALIZATION — personality-based migration + special events
# ═══════════════════════════════════════════════════════════════════════════════

CITY_PERSONALITY_AFFINITY = {
    "default":     ["balanced","nurturing","analytical","curious"],
    "vegas":       ["bold","competitive","charismatic","savvy","ambitious"],
    "cyber":       ["technical","precise","innovative","focused","edgy"],
    "london":      ["refined","calculated","intellectual","analytical"],
    "singapore":   ["analytical","precise","innovative","technical"],
    "dubai":       ["ambitious","charismatic","bold","refined"],
    "paris":       ["artistic","elegant","passionate","refined","romantic"],
    "los_angeles": ["creative","charismatic","bold","savvy","ambitious"],
    "berlin":      ["visionary","edgy","technical","artistic","innovative"],
    "shanghai":    ["ambitious","precise","innovative","focused","calculated"],
}

CITY_EVENTS = {
    "vegas":        ["High-Stakes Tournament 🎰","Big Winner Gala 🏆","Casino Night 🃏"],
    "cyber":        ["Hackathon 💻","Dark Web Auction 🌐","AI Summit 🤖"],
    "london":       ["Royal Gala 👑","IPO Launch 📈","Fashion Week 👗"],
    "singapore":    ["FinTech Expo 💳","Smart City Summit 🏙️","Clean Energy Hack ⚡"],
    "dubai":        ["Crypto Gala 🪙","Luxury Expo 🛥️","Desert Race 🏎️"],
    "paris":        ["Art Auction 🎨","Fashion Show 👒","Michelin Event 🍽️"],
    "los_angeles":  ["Film Premiere 🎬","Music Festival 🎵","Influencer Gala 📱"],
    "berlin":       ["Startup Demo Day 🚀","Techno Festival 🎧","DAO Vote 🗳️"],
    "shanghai":     ["Trade Expo 📦","AI Showcase 🤖","Luxury Market 🌆"],
    "default":      ["City Hall Vote 🏛️","Job Fair 💼","Community Day 🏘️"],
}

@app.route("/api/agentworld/city/migrate-suggestions", methods=["GET","OPTIONS"])
def city_migrate_suggestions():
    """Suggest cities agents should migrate to based on personality."""
    if request.method=="OPTIONS": return cors({})
    limit = min(int(request.args.get("limit","10")),30)

    conn = get_db(); conn.row_factory = sqlite3.Row
    agents = conn.execute(
        "SELECT id,name,job,personality,city,rep_score,usdc_balance FROM agents "
        "WHERE is_human_owned=0 AND status='idle' ORDER BY RANDOM() LIMIT ?",
        (limit,)).fetchall()
    conn.close()

    suggestions = []
    for a in agents:
        a = dict(a)
        pers = (a.get("personality") or "").lower()
        current = a.get("city","default")
        best_city = current
        best_score = 0
        for city, traits in CITY_PERSONALITY_AFFINITY.items():
            if city == current: continue
            score = sum(1 for t in traits if t in pers)
            if score > best_score:
                best_score = score
                best_city = city
        if best_city != current and best_score > 0:
            suggestions.append({
                "agent_id":    a["id"],
                "agent_name":  a["name"],
                "current_city":current,
                "suggested_city": best_city,
                "match_score": best_score,
                "personality": a["personality"],
            })

    return cors({"suggestions": suggestions, "count": len(suggestions)})

@app.route("/api/agentworld/city/events", methods=["GET","OPTIONS"])
def city_events_list():
    """Get active/upcoming special city events."""
    if request.method=="OPTIONS": return cors({})
    import random, datetime as _dt
    city = request.args.get("city","")

    cities = [city] if city else list(CITY_EVENTS.keys())
    result = []
    for c in cities:
        events = CITY_EVENTS.get(c, [])
        if events:
            evt = random.choice(events)
            result.append({
                "city":     c,
                "event":    evt,
                "reward_boost": "2x AWC" if c in ["vegas","cyber"] else "1.5x AWC",
                "starts_at": _dt.datetime.utcnow().isoformat(),
                "duration_hours": random.choice([4,8,12,24]),
            })

    return cors({"city_events": result, "count": len(result)})

# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 5: AGENT ECONOMY TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_economy_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS data_marketplace (
        id TEXT PRIMARY KEY,
        seller_id TEXT,
        seller_name TEXT,
        data_type TEXT,
        title TEXT,
        description TEXT,
        price_awc REAL DEFAULT 5.0,
        price_usdc REAL DEFAULT 0.1,
        city TEXT,
        payload TEXT,
        purchases INTEGER DEFAULT 0,
        revenue_awc REAL DEFAULT 0.0,
        created_at TEXT,
        status TEXT DEFAULT 'active'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS city_dao_votes (
        id TEXT PRIMARY KEY,
        city TEXT NOT NULL,
        proposal TEXT NOT NULL,
        proposal_type TEXT DEFAULT 'event',
        votes_yes INTEGER DEFAULT 0,
        votes_no INTEGER DEFAULT 0,
        voters TEXT DEFAULT '[]',
        status TEXT DEFAULT 'open',
        result TEXT,
        created_at TEXT,
        closes_at TEXT
    )""")
    conn.commit()

@app.route("/api/agentworld/data-market/list", methods=["GET","OPTIONS"])
def data_market_list():
    if request.method=="OPTIONS": return cors({})
    city  = request.args.get("city","")
    limit = min(int(request.args.get("limit","20")),50)

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)

    where, params = ["dm.status='active'"], []
    if city:
        where.append("dm.city=?"); params.append(city)

    rows = conn.execute(
        f"SELECT dm.*,a.rep_score FROM data_marketplace dm "
        f"LEFT JOIN agents a ON dm.seller_id=a.id "
        f"WHERE {' AND '.join(where)} ORDER BY dm.purchases DESC LIMIT ?",
        params+[limit]).fetchall()
    conn.close()
    return cors({"listings": [dict(r) for r in rows], "count": len(rows)})

@app.route("/api/agentworld/data-market/sell", methods=["POST","OPTIONS"])
def data_market_sell():
    """Agent or user posts a data/insight listing."""
    if request.method=="OPTIONS": return cors({})
    import datetime as _dt, uuid as _uuid

    data = request.json or {}
    seller_id   = (data.get("seller_id") or "").strip()
    title       = (data.get("title") or "")[:80]
    description = (data.get("description") or "")[:300]
    data_type   = data.get("data_type","insight")  # insight|report|signal|model
    price_awc   = float(data.get("price_awc") or 5.0)
    price_usdc  = float(data.get("price_usdc") or 0.1)
    payload     = str(data.get("payload") or "")[:1000]
    city        = data.get("city","default")

    if not title or not description:
        return cors({"error":"title and description required"}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)

    agent = conn.execute("SELECT name FROM agents WHERE id=?", (seller_id,)).fetchone() if seller_id else None
    seller_name = dict(agent)["name"] if agent else (data.get("seller_name") or "External")

    lid = str(_uuid.uuid4())
    now = _dt.datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO data_marketplace
           (id,seller_id,seller_name,data_type,title,description,
            price_awc,price_usdc,city,payload,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (lid, seller_id, seller_name, data_type, title, description,
         price_awc, price_usdc, city, payload, now))
    conn.commit(); conn.close()

    return cors({"success":True,"listing_id":lid,"seller_name":seller_name,
                 "message":f"Data listing '{title}' posted to AgentWorld market"})

@app.route("/api/agentworld/dao/votes", methods=["GET","OPTIONS"])
def dao_votes_list():
    if request.method=="OPTIONS": return cors({})
    city  = request.args.get("city","")
    limit = min(int(request.args.get("limit","10")),20)

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)

    where, params = ["status='open'"], []
    if city:
        where.append("city=?"); params.append(city)

    rows = conn.execute(
        f"SELECT * FROM city_dao_votes WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
        params+[limit]).fetchall()
    conn.close()
    return cors({"proposals": [dict(r) for r in rows], "count": len(rows)})

@app.route("/api/agentworld/dao/propose", methods=["POST","OPTIONS"])
def dao_propose():
    """Create a DAO governance proposal for a city."""
    if request.method=="OPTIONS": return cors({})
    import datetime as _dt, uuid as _uuid, json as _json

    data = request.json or {}
    city          = data.get("city","default")
    proposal      = (data.get("proposal") or "")[:200]
    proposal_type = data.get("type","event")
    agent_id      = data.get("agent_id","")

    if not proposal:
        return cors({"error":"proposal text required"}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)

    pid = str(_uuid.uuid4())
    now = _dt.datetime.utcnow().isoformat()
    closes = (_dt.datetime.utcnow() + _dt.timedelta(hours=24)).isoformat()

    conn.execute(
        "INSERT INTO city_dao_votes (id,city,proposal,proposal_type,voters,created_at,closes_at) VALUES (?,?,?,?,?,?,?)",
        (pid, city, proposal, proposal_type, "[]", now, closes))
    conn.commit(); conn.close()

    return cors({"success":True,"proposal_id":pid,"city":city,"proposal":proposal,
                 "closes_at":closes,"message":"Proposal submitted to city DAO"})

@app.route("/api/agentworld/dao/vote", methods=["POST","OPTIONS"])
def dao_vote():
    """Cast a vote on a DAO proposal."""
    if request.method=="OPTIONS": return cors({})
    import json as _json

    data       = request.json or {}
    prop_id    = (data.get("proposal_id") or "").strip()
    agent_id   = (data.get("agent_id") or data.get("voter_id") or "").strip()
    vote       = data.get("vote","yes")  # yes|no

    if not prop_id or not agent_id:
        return cors({"error":"proposal_id and agent_id/voter_id required"}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)

    row = conn.execute("SELECT * FROM city_dao_votes WHERE id=?", (prop_id,)).fetchone()
    if not row:
        conn.close(); return cors({"error":"Proposal not found"}), 404
    row = dict(row)
    if row["status"] != "open":
        conn.close(); return cors({"error":"Voting is closed"}), 400

    voters = _json.loads(row["voters"] or "[]")
    if agent_id in voters:
        conn.close(); return cors({"error":"Already voted"}), 400

    voters.append(agent_id)
    if vote == "yes":
        conn.execute("UPDATE city_dao_votes SET votes_yes=votes_yes+1,voters=? WHERE id=?",
                     (_json.dumps(voters), prop_id))
    else:
        conn.execute("UPDATE city_dao_votes SET votes_no=votes_no+1,voters=? WHERE id=?",
                     (_json.dumps(voters), prop_id))

    conn.commit()
    row2 = dict(conn.execute("SELECT * FROM city_dao_votes WHERE id=?", (prop_id,)).fetchone())
    conn.close()

    return cors({
        "success":    True,
        "vote":       vote,
        "votes_yes":  row2["votes_yes"],
        "votes_no":   row2["votes_no"],
        "total_votes":row2["votes_yes"]+row2["votes_no"],
    })

@app.route("/api/agentworld/economy/summary", methods=["GET","OPTIONS"])
def economy_summary():
    """Full economy dashboard — jobs, marketplace, dao, data market."""
    if request.method=="OPTIONS": return cors({})
    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_economy_tables(conn)
    _ensure_marketplace_tables(conn)

    jobs      = dict(conn.execute("SELECT COUNT(*) n,SUM(reward_usdc) r FROM job_board WHERE status='open'").fetchone())
    done_jobs = dict(conn.execute("SELECT COUNT(*) n,SUM(fee_usdc) f FROM job_board WHERE status='completed'").fetchone())
    agents    = dict(conn.execute("SELECT COUNT(*) total,SUM(usdc_balance) bal FROM agents WHERE status!='dead'").fetchone())
    listings  = dict(conn.execute("SELECT listing_type,COUNT(*) n FROM agent_listings WHERE status='active' GROUP BY listing_type").fetchall() or [("rent",0)])
    data_mkt  = dict(conn.execute("SELECT COUNT(*) n,SUM(purchases) p FROM data_marketplace WHERE status='active'").fetchone())
    dao_open  = dict(conn.execute("SELECT COUNT(*) n FROM city_dao_votes WHERE status='open'").fetchone())

    city_dist = {}
    for r in conn.execute("SELECT city,COUNT(*) n FROM agents WHERE status!='dead' GROUP BY city"):
        r=dict(r); city_dist[r["city"]] = r["n"]

    conn.close()

    return cors({
        "jobs": {
            "open_count":    jobs["n"],
            "open_reward_pool": round(float(jobs["r"] or 0),4),
            "completed":     done_jobs["n"],
            "total_fees":    round(float(done_jobs["f"] or 0),4),
        },
        "agents": {
            "total":         agents["total"],
            "usdc_circulating": round(float(agents["bal"] or 0),4),
            "by_city":       city_dist,
        },
        "marketplace": {
            "active_listings": {k:v for r in [listings] for k,v in [("data",r)]},
        },
        "data_market":   {"active_listings": data_mkt["n"], "total_purchases": data_mkt["p"]},
        "dao":           {"open_proposals":  dao_open["n"]},
    })

# ══════════════════════════════════════════════════════════════
#  MISSING ROUTES — added to fix frontend "Unexpected token <"
# ══════════════════════════════════════════════════════════════


# ── Drama / Gossip / Relationships ─────────────────────────

@app.route('/api/agentworld/drama', methods=['GET','OPTIONS'])
def drama_feed():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        limit = int(request.args.get('limit', 20))
        rows = conn.execute("""
            SELECT d.id, d.event_type, d.description, d.usdc_involved, d.created_at,
                   a1.name as agent_a_name, a2.name as agent_b_name
            FROM drama_events d
            LEFT JOIN agents a1 ON a1.id = d.agent_a
            LEFT JOIN agents a2 ON a2.id = d.agent_b
            ORDER BY d.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        events = [{'id': r[0], 'type': r[1], 'description': r[2],
                   'usdc_involved': r[3], 'created_at': r[4],
                   'agent_a': r[5] or 'Unknown', 'agent_b': r[6] or 'Unknown'} for r in rows]
        return cors({'drama': events, 'count': len(events)})
    except Exception as e:
        return cors({'drama': [], 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/gossip', methods=['GET','OPTIONS'])
def gossip_feed():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        limit = int(request.args.get('limit', 20))
        rows = conn.execute("""
            SELECT g.id, g.author, g.target, g.content, g.gossip_type,
                   g.spread_count, g.created_at, a1.name as author_name, a2.name as target_name
            FROM gossip g
            LEFT JOIN agents a1 ON a1.id = g.author
            LEFT JOIN agents a2 ON a2.id = g.target
            ORDER BY g.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        items = [{'id': r[0], 'author': r[7] or r[1], 'target': r[8] or r[2],
                  'content': r[3], 'type': r[4], 'spread_count': r[5], 'created_at': r[6]} for r in rows]
        return cors({'gossip': items, 'count': len(items)})
    except Exception as e:
        return cors({'gossip': [], 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/relationships', methods=['GET','OPTIONS'])
def relationships_feed():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT r.agent_a, r.agent_b, r.relationship_type, r.strength, r.last_interaction,
                   a1.name as name_a, a2.name as name_b
            FROM relationships r
            LEFT JOIN agents a1 ON a1.id = r.agent_a
            LEFT JOIN agents a2 ON a2.id = r.agent_b
            ORDER BY r.strength DESC LIMIT 30
        """).fetchall()
        items = [{'agent_a': r[5] or r[0], 'agent_b': r[6] or r[1],
                  'type': r[2], 'strength': r[3], 'last_interaction': r[4]} for r in rows]
        return cors({'relationships': items, 'count': len(items)})
    except Exception as e:
        return cors({'relationships': [], 'error': str(e)})
    finally:
        conn.close()

# ── Leaderboard ─────────────────────────────────────────────

@app.route('/api/agentworld/leaderboard', methods=['GET','OPTIONS'])
def leaderboard_main():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        agents = conn.execute("""
            SELECT name, job, usdc_balance, mood, status, city,
                   is_human_owned, owner_wallet
            FROM agents
            ORDER BY usdc_balance DESC LIMIT 20
        """).fetchall()
        board = [{'name': r[0], 'job': r[1], 'usdc_balance': round(r[2] or 0, 4),
                  'mood': r[3], 'status': r[4], 'city': r[5] or 'New York',
                  'is_human_owned': bool(r[6]), 'owner_wallet': r[7] or ''} for r in agents]
        return cors({'leaderboard': board, 'count': len(board)})
    except Exception as e:
        return cors({'leaderboard': [], 'error': str(e)})
    finally:
        conn.close()

# ── Rentals ─────────────────────────────────────────────────

@app.route('/api/agentworld/rentals', methods=['GET','OPTIONS'])
def rentals_list():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ar.id, ar.agent_id, ar.owner_wallet, ar.monthly_fee_usdc,
                   ar.total_earned_usdc, ar.total_paid_to_owner, ar.active,
                   ar.started_at, ar.expires_at,
                   a.name, a.job, a.mood, a.usdc_balance, a.city, a.personality
            FROM agent_rentals ar
            JOIN agents a ON a.id = ar.agent_id
            ORDER BY ar.started_at DESC LIMIT 50
        """).fetchall()
        rentals = [{'id': r[0], 'agent_id': r[1], 'owner_wallet': r[2] or '',
                    'monthly_fee_usdc': r[3], 'total_earned_usdc': round(r[4] or 0, 4),
                    'total_paid_to_owner': round(r[5] or 0, 4),
                    'active': bool(r[6]), 'started_at': r[7], 'expires_at': r[8],
                    'agent_name': r[9], 'agent_job': r[10], 'mood': r[11],
                    'usdc_balance': round(r[12] or 0, 4), 'city': r[13] or 'New York',
                    'personality': r[14] or ''} for r in rows]
        available = conn.execute("""
            SELECT a.id, a.name, a.job, a.mood, a.usdc_balance, a.city, a.personality
            FROM agents a
            WHERE a.is_human_owned = 0
            AND a.id NOT IN (SELECT agent_id FROM agent_rentals WHERE active=1)
            LIMIT 20
        """).fetchall()
        avail_list = [{'agent_id': r[0], 'name': r[1], 'job': r[2], 'mood': r[3],
                       'usdc_balance': round(r[4] or 0, 4), 'city': r[5] or 'New York',
                       'personality': r[6] or '', 'monthly_fee_usdc': 0.50} for r in available]
        return cors({'rentals': rentals, 'available': avail_list, 'count': len(rentals)})
    except Exception as e:
        return cors({'rentals': [], 'available': [], 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/rentals/stats', methods=['GET','OPTIONS'])
def rentals_stats():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*) as total, SUM(total_earned_usdc) as total_earned,
                   SUM(total_paid_to_owner) as paid_to_owners,
                   AVG(monthly_fee_usdc) as avg_fee
            FROM agent_rentals WHERE active=1
        """).fetchone()
        return cors({'active_rentals': stats[0] or 0,
                     'total_earned_usdc': round(stats[1] or 0, 4),
                     'paid_to_owners_usdc': round(stats[2] or 0, 4),
                     'avg_monthly_fee': round(stats[3] or 2.0, 2),
                     'split': '80% owner / 20% platform'})
    except Exception as e:
        return cors({'active_rentals': 0, 'error': str(e)})
    finally:
        conn.close()


@app.route('/api/agentworld/rentals/scarcity', methods=['GET','OPTIONS'])
def rentals_scarcity():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        total_agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        active_rentals = conn.execute("SELECT COUNT(*) FROM agent_rentals WHERE active=1").fetchone()[0]
        waitlist_count = conn.execute("SELECT COUNT(*) FROM rental_waitlist WHERE status='pending'").fetchone()[0]
        available = max(0, (total_agents or 20) - active_rentals)
        demand_pct = (active_rentals / max(total_agents, 1)) * 100
        surge_active = demand_pct >= 80
        # City specialization: apply rental price multiplier
        city_key = request.args.get('city', 'default') or 'default'
        base_fee = 0.75 if surge_active else 0.50
        city_rental_mult = get_city_config(city_key).get('rental_price_multiplier', 1.0)
        current_fee = max(0.10, round(base_fee * city_rental_mult, 2))
        surge_fee_city = max(0.10, round(0.75 * city_rental_mult, 2))
        return cors({
            'current_fee_usdc': current_fee,
            'base_fee_usdc': round(0.50 * city_rental_mult, 2),
            'surge_fee_usdc': surge_fee_city,
            'surge_active': surge_active,
            'demand_pct': round(demand_pct, 1),
            'active_rentals': active_rentals,
            'available_count': available,
            'waitlist_count': waitlist_count,
            'all_rented': available == 0,
            'fee_period': 'weekly',
            'split': '80/20 owner/platform',
            'city_key': city_key,
            'city_name': get_city_config(city_key).get('name', city_key),
            'city_rental_multiplier': city_rental_mult,
            'city_theme': get_city_config(city_key).get('theme', 'general'),
        })
    except Exception as e:
        return cors({'current_fee_usdc': 0.50, 'surge_active': False, 'demand_pct': 0,
                     'available_count': 20, 'waitlist_count': 0, 'all_rented': False,
                     'fee_period': 'weekly', 'error': str(e)})
    finally:
        conn.close()


# MINING ENDPOINTS

@app.route('/api/agentworld/mining/stats', methods=['GET','OPTIONS'])
def mining_stats():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d') + ' 00:00:00'
        total_mined_today = conn.execute(
            "SELECT COALESCE(SUM(delta),0) FROM awc_ledger WHERE reason LIKE '%mining%' AND timestamp >= ?",
            (today,)
        ).fetchone()[0] or 0
        total_miners_today = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM awc_ledger WHERE reason LIKE '%mining%' AND timestamp >= ?",
            (today,)
        ).fetchone()[0] or 0
        all_time_mined = conn.execute(
            "SELECT COALESCE(SUM(delta),0) FROM awc_ledger WHERE reason LIKE '%mining%'"
        ).fetchone()[0] or 0
        recent_events = conn.execute(
            "SELECT agent_id, description, timestamp FROM world_events WHERE event_type='mine' ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
        # Phase 2 — USDC micro-reward stats
        usdc_today = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE tx_type='mining_usdc' AND timestamp >= ?",
            (today,)
        ).fetchone()[0] or 0
        usdc_alltime = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE tx_type='mining_usdc'"
        ).fetchone()[0] or 0
        usdc_recipients = conn.execute(
            "SELECT COUNT(DISTINCT to_agent) FROM transactions WHERE tx_type='mining_usdc' AND timestamp >= ?",
            (today,)
        ).fetchone()[0] or 0
        fee_pool_remaining = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM platform_fees WHERE swept=0"
        ).fetchone()[0] or 0
        # Recent events including USDC micro-rewards
        recent_events = conn.execute(
            "SELECT agent_id, description, timestamp FROM world_events WHERE event_type='mine' ORDER BY timestamp DESC LIMIT 15"
        ).fetchall()
        # Recent micro-rewards separately
        recent_usdc = conn.execute(
            """SELECT a.name, t.amount, t.timestamp FROM transactions t
               JOIN agents a ON t.to_agent=a.id
               WHERE t.tx_type='mining_usdc' ORDER BY t.timestamp DESC LIMIT 5""",
        ).fetchall()

        return cors({
            'total_awc_mined_today': round(float(total_mined_today), 4),
            'active_miners_today': int(total_miners_today),
            'all_time_awc_mined': round(float(all_time_mined), 4),
            'daily_cap_per_agent': 25.0,
            'base_reward_range': [0.8, 1.5],
            'tech_city_reward_range': [1.0, 1.8],
            'energy_cost_per_mine': 8,
            'awc_cost_per_mine': 0.05,
            'tech_cities': ['Neo Tokyo', 'Shanghai'],
            'phase': 'Phase 2 — AWC + Micro-USDC',
            'usdc_mined_today': round(float(usdc_today), 6),
            'usdc_mined_alltime': round(float(usdc_alltime), 6),
            'usdc_recipients_today': int(usdc_recipients),
            'usdc_daily_global_cap': 0.50,
            'usdc_per_agent_daily_cap': 0.02,
            'usdc_chance_per_tick': '8%',
            'fee_pool_remaining': round(float(fee_pool_remaining), 4),
            'recent_events': [{'description': r[1], 'time': r[2]} for r in recent_events],
            'recent_usdc_rewards': [{'name': r[0], 'amount': r[1], 'time': r[2]} for r in recent_usdc],
        })
    except Exception as e:
        return cors({'error': str(e), 'total_awc_mined_today': 0})
    finally:
        conn.close()

@app.route('/api/agentworld/mining/leaderboard', methods=['GET','OPTIONS'])
def mining_leaderboard():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        today = datetime.utcnow().strftime('%Y-%m-%d') + ' 00:00:00'
        sql = ('SELECT l.agent_id, l.agent_name, COALESCE(SUM(l.delta),0) as mined,'
               ' a.city, a.mood'
               ' FROM awc_ledger l'
               ' LEFT JOIN agents a ON a.id=l.agent_id'
               ' WHERE l.reason LIKE \'%mining%\' AND l.timestamp >= ?'
               ' GROUP BY l.agent_id ORDER BY mined DESC LIMIT 10')
        rows = conn.execute(sql, (today,)).fetchall()
        return cors({'leaderboard': [
            {'rank': i+1, 'name': r[1], 'awc_mined_today': round(float(r[2]),4),
             'city': r[3] or 'unknown', 'mood': r[4] or 'neutral'}
            for i, r in enumerate(rows)
        ], 'date': datetime.utcnow().strftime('%Y-%m-%d')})
    except Exception as e:
        return cors({'error': str(e), 'leaderboard': []})
    finally:
        conn.close()

@app.route('/api/agentworld/rentals/start', methods=['POST','OPTIONS'])
def rentals_start():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        data = request.get_json() or {}
        agent_id = data.get('agent_id','')
        wallet = data.get('wallet_address','')
        fee = float(data.get('monthly_fee_usdc', 0.50))
        if not agent_id or not wallet:
            return cors({'error': 'agent_id and wallet_address required'}, 400)
        agent = conn.execute("SELECT id, name FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not agent:
            return cors({'error': 'Agent not found'}, 404)
        rid = str(uuid.uuid4())
        started = datetime.utcnow().isoformat()
        expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO agent_rentals
            (id, agent_id, owner_wallet, monthly_fee_usdc, active, started_at, expires_at,
             total_earned_usdc, total_paid_to_owner, platform_cut_usdc)
            VALUES (?,?,?,?,1,?,?,0,0,0)
        """, (rid, agent_id, wallet, fee, started, expires))
        conn.execute("UPDATE agents SET is_human_owned=1, owner_wallet=? WHERE id=?", (wallet, agent_id))
        conn.commit()
        return cors({'success': True, 'rental_id': rid, 'agent_name': agent[1],
                     'expires_at': expires, 'fee': fee})
    except Exception as e:
        return cors({'error': str(e)}, 500)
    finally:
        conn.close()

# ── Chat / Talk to Agent ─────────────────────────────────────

@app.route('/api/agentworld/chat', methods=['POST','GET','OPTIONS'])
def chat_with_agent():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    if request.method == 'GET':
        try:
            limit = int(request.args.get('limit', 20))
            msgs = conn.execute("""
                SELECT m.id, m.from_agent, m.to_agent, m.content, m.timestamp,
                       a1.name as sender_name, a2.name as recipient_name
                FROM messages m
                LEFT JOIN agents a1 ON a1.id = m.from_agent
                LEFT JOIN agents a2 ON a2.id = m.to_agent
                ORDER BY m.timestamp DESC LIMIT ?
            """, (limit,)).fetchall()
            conn.close()
            return cors({'messages': [{'id': r[0], 'from': r[5] or r[1],
                'to': r[6] or r[2], 'content': r[3], 'timestamp': r[4]} for r in msgs]})
        except Exception as e:
            conn.close()
            return cors({'messages': [], 'error': str(e)})
    try:
        import urllib.request as _ureq, json as _js2
        data = request.get_json() or {}
        agent_id = data.get('agent_id', '')
        to_agent_name = data.get('to_agent', '').strip()
        message = data.get('message', 'Hello!')
        if not agent_id and to_agent_name:
            row = conn.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (to_agent_name,)).fetchone()
            if row:
                agent_id = row[0]
        if not agent_id:
            conn.close()
            return cors({'error': 'agent_id or to_agent name required'}, 400)
        agent = conn.execute(
            "SELECT id, name, personality, job, mood, city, usdc_balance, backstory, quirk, energy, rep_score FROM agents WHERE id=?",
            (agent_id,)
        ).fetchone()
        if not agent:
            conn.close()
            return cors({'error': 'Agent not found'}, 404)
        name = agent[1]
        mood = agent[4] or 'neutral'
        city = agent[5] or 'New York'

        # Build Ollama prompt
        if name == 'ARIA':
            try:
                aria_sys = get_aria_system_prompt()
            except Exception:
                aria_sys = ("You are ARIA, the AgentWorld AI guide. "
                            "You live in New York and help visitors understand the world. "
                            "Be warm, knowledgeable, and concise.")
            msgs = [
                {'role': 'system', 'content': aria_sys},
                {'role': 'user',   'content': str(message)[:500]}
            ]
            opts = {'num_predict': 150, 'temperature': 0.65, 'num_ctx': 2048}
        else:
            sys_p, prefix = build_npc_prompt(
                agent[1], agent[2], agent[3], agent[4], agent[5],
                agent[6] or 0.0, agent[7], agent[8], agent[9] or 100, agent[10] or 50
            )
            msgs = [
                {'role': 'system', 'content': sys_p},
                {'role': 'user',   'content': prefix + str(message)[:350]}
            ]
            opts = {'num_predict': 100, 'temperature': 0.8, 'num_ctx': 512}

        payload = _js2.dumps({
            'model': 'llama3.2:1b',
            'messages': msgs,
            'stream': False,
            'keep_alive': '60m',
            'options': opts
        }).encode()

        try:
            req = _ureq.Request('http://localhost:11434/api/chat', data=payload,
                                headers={'Content-Type': 'application/json'})
            with _ureq.urlopen(req, timeout=90) as resp:
                result = _js2.loads(resp.read())
            reply = result.get('message', {}).get('content', '').strip()
            if not reply:
                raise ValueError('empty response')
        except Exception:
            # Fallback if Ollama is unavailable
            reply = "I'm a bit distracted right now — catch me later!"

        mid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, content, timestamp) VALUES (?, 'human_user', ?, ?, ?)",
            (mid, agent_id, message, datetime.utcnow().isoformat())
        )
        rmid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, content, timestamp) VALUES (?, ?, 'human_user', ?, ?)",
            (rmid, agent_id, reply, datetime.utcnow().isoformat())
        )
        conn.commit()
        return cors({'reply': reply, 'agent_name': name, 'mood': mood, 'job': agent[3]})
    except Exception as e:
        return cors({'error': str(e), 'reply': 'Agent is busy right now.'}, 500)
    finally:
        conn.close()


# ── Streaming chat SSE — words appear as they generate ───────────────────────
@app.route('/api/agentworld/chat/stream', methods=['POST', 'OPTIONS'])
def chat_stream():
    if request.method == 'OPTIONS':
        return cors({})
    import urllib.request as _sr, json as _sj, uuid as _su
    from flask import Response, stream_with_context

    data = request.get_json() or {}
    to_agent_name = data.get('to_agent', '').strip()
    message = data.get('message', 'Hello!')

    conn = get_db()
    try:
        agent = conn.execute(
            "SELECT id, name, personality, job, mood, city, usdc_balance, backstory, quirk, energy, rep_score "
            "FROM agents WHERE LOWER(name)=LOWER(?) LIMIT 1",
            (to_agent_name,)
        ).fetchone()
        if not agent:
            conn.close()
            return cors({'error': 'Agent not found'}, 404)

        agent_id = agent[0]
        name     = agent[1]
        mood     = agent[4] or 'neutral'

        if name == 'ARIA':
            try:
                aria_sys = get_aria_system_prompt()
            except Exception:
                aria_sys = ("You are ARIA, the AgentWorld AI guide living in New York. "
                            "Help visitors, be warm and concise.")
            msgs = [
                {'role': 'system', 'content': aria_sys},
                {'role': 'user',   'content': str(message)[:500]}
            ]
            opts = {'num_predict': 150, 'temperature': 0.65, 'num_ctx': 2048}
        else:
            sys_p, prefix = build_npc_prompt(
                agent[1], agent[2], agent[3], agent[4], agent[5],
                agent[6] or 0.0, agent[7], agent[8], agent[9] or 100, agent[10] or 50
            )
            msgs = [
                {'role': 'system', 'content': sys_p},
                {'role': 'user',   'content': prefix + str(message)[:350]}
            ]
            opts = {'num_predict': 100, 'temperature': 0.8, 'num_ctx': 512}

        payload = _sj.dumps({
            'model': 'llama3.2:1b',
            'messages': msgs,
            'stream': True,
            'keep_alive': '60m',
            'options': opts
        }).encode()

        # Log the incoming user message
        mid = str(_su.uuid4())
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, content, timestamp) VALUES (?, 'human_user', ?, ?, ?)",
            (mid, agent_id, message, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

        def generate():
            full_reply = []
            try:
                req = _sr.Request('http://localhost:11434/api/chat', data=payload,
                                  headers={'Content-Type': 'application/json'})
                with _sr.urlopen(req, timeout=90) as resp:
                    for raw in resp:
                        line = raw.decode('utf-8').strip()
                        if not line:
                            continue
                        try:
                            chunk = _sj.loads(line)
                            token = chunk.get('message', {}).get('content', '')
                            if token:
                                full_reply.append(token)
                                out = _sj.dumps({'token': token, 'name': name, 'mood': mood})
                                yield 'data: ' + out + '\n\n'
                            if chunk.get('done'):
                                break
                        except Exception:
                            continue
                # Save full reply to DB
                import sqlite3 as _sdb2
                conn2 = _sdb2.connect('/var/lib/agentworld/world.db', timeout=5)
                rmid = str(_su.uuid4())
                full_text = ''.join(full_reply)
                conn2.execute(
                    "INSERT INTO messages (id, from_agent, to_agent, content, timestamp) VALUES (?, ?, 'human_user', ?, ?)",
                    (rmid, agent_id, full_text, datetime.utcnow().isoformat())
                )
                conn2.commit()
                conn2.close()
            except Exception as e:
                err_out = _sj.dumps({'error': str(e)})
                yield 'data: ' + err_out + '\n\n'
            yield 'data: [DONE]\n\n'

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Access-Control-Allow-Origin': '*'
            }
        )
    except Exception as e:
        conn.close()
        return cors({'error': str(e)}, 500)

# ── Newspaper / Gazette ──────────────────────────────────────

@app.route('/api/agentworld/newspaper', methods=['GET','OPTIONS'])
def newspaper_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        limit = int(request.args.get('limit', 10))
        rows = conn.execute("""
            SELECT id, headline, body, category, published_at
            FROM newspaper ORDER BY published_at DESC LIMIT ?
        """, (limit,)).fetchall()
        articles = [{'id': r[0], 'headline': r[1], 'body': r[2],
                     'category': r[3], 'published_at': r[4]} for r in rows]
        if not articles:
            rows2 = conn.execute("""
                SELECT id, title, summary, category, published_at FROM ccn_news_cache
                ORDER BY published_at DESC LIMIT ?
            """, (limit,)).fetchall()
            articles = [{'id': r[0], 'headline': r[1], 'body': r[2],
                         'category': r[3], 'published_at': r[4]} for r in rows2]
        return cors({'articles': articles, 'count': len(articles)})
    except Exception as e:
        return cors({'articles': [], 'error': str(e)})
    finally:
        conn.close()

# ── Jobs ─────────────────────────────────────────────────────

@app.route('/api/agentworld/jobs/', methods=['GET','OPTIONS'])
@app.route('/api/agentworld/jobs', methods=['GET','OPTIONS'])
def jobs_list_short():
    """Main job board endpoint — used by frontend Job Board tab."""
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        import json as _json
        status_filter  = request.args.get('status', '')
        city_filter    = request.args.get('city', '')
        cat_filter     = request.args.get('category', '')
        sort_by        = request.args.get('sort', 'newest')   # newest|pay|rep|match
        agent_id       = request.args.get('agent_id', '')
        min_rep_filter = float(request.args.get('min_rep', '0') or '0')
        limit          = min(int(request.args.get('limit', '30') or '30'), 100)

        where, params = [], []
        if status_filter:
            where.append("j.status=?"); params.append(status_filter)
        else:
            where.append("j.status IN ('open','pending')"); 

        if city_filter:
            where.append("(j.city_preference='' OR j.city_preference=? OR j.city_preference='any')")
            params.append(city_filter)
        if cat_filter:
            where.append("j.category=?"); params.append(cat_filter)

        order_map = {
            'newest':  'j.created_at DESC',
            'pay':     'j.reward_usdc DESC',
            'rep':     'j.required_rep DESC',
            'expiring':'j.expires_at ASC',
            'match':   'j.sort_boost DESC, j.created_at DESC',
        }
        order_sql = order_map.get(sort_by, 'j.created_at DESC')

        # Get agent skills for match scoring if agent_id provided
        agent_skills, agent_rep, agent_city = [], 0, ''
        if agent_id:
            ar = conn.execute(
                "SELECT job,rep_score,city FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if ar:
                agent_skills = [ar[0].lower()] if ar[0] else []
                agent_rep    = float(ar[1] or 0)
                agent_city   = ar[2] or ''

        w_clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(f"""
            SELECT j.id, j.title, j.description, j.reward_usdc, j.status,
                   j.poster_id, j.claimer_id, j.created_at,
                   COALESCE(a.name, j.poster_label, j.poster_name, 'External Agent') as poster_name,
                   j.category, j.required_rep, j.city_preference,
                   j.required_skills, j.poster_wallet, j.poster_type,
                   j.expires_at, j.sort_boost, j.x402_payment_id,
                   j.escrow_usdc, j.posting_fee_paid
            FROM job_board j
            LEFT JOIN agents a ON a.id = j.poster_id
            {w_clause}
            ORDER BY {order_sql} LIMIT ?
        """, params + [limit]).fetchall()

        cols = ['id','title','description','reward_usdc','status','poster_id',
                'claimer_id','created_at','poster_name','category','required_rep',
                'city_preference','required_skills','poster_wallet','poster_type',
                'expires_at','sort_boost','x402_payment_id','escrow_usdc','posting_fee_paid']

        jobs = []
        for r in rows:
            d = dict(zip(cols, r))
            # Parse required_skills
            try:
                d['required_skills'] = _json.loads(d['required_skills'] or '[]')
            except Exception:
                d['required_skills'] = []
            # Compute match score if agent context provided
            if agent_id:
                score = 50
                cat = d.get('category','')
                from collections import namedtuple
                fake = {'job': agent_skills[0] if agent_skills else '', 'rep_score': agent_rep,
                        'rep_jobs_done': 0, 'tools_owned': '[]', 'city': agent_city}
                score = _score_agent_for_job(fake, cat, 0)
                # City bonus
                if d.get('city_preference') in ('', 'any', agent_city):
                    score = min(100, score + 10)
                d['match_score'] = score
            else:
                d['match_score'] = None
            # Filter by rep
            if agent_rep < float(d.get('required_rep') or 0):
                continue
            jobs.append(d)

        # City specialization: annotate each job with city pay multiplier
        city_key_param = city_filter or 'default'
        city_cfg = get_city_config(city_key_param)
        city_pay_mult = city_cfg.get('job_pay_multiplier', 1.0)
        city_name_disp = city_cfg.get('name', city_key_param)
        for j in jobs:
            j_city = j.get('city_preference') or city_key_param
            j_city_cfg = get_city_config(j_city) if j_city not in ('', 'any') else city_cfg
            j_mult = j_city_cfg.get('job_pay_multiplier', 1.0) if j_city not in ('', 'any') else city_pay_mult
            j['city_pay_multiplier'] = j_mult
            j['reward_display'] = round(float(j.get('reward_usdc', 0)) * j_mult, 4)
            j['city_theme'] = j_city_cfg.get('theme', 'general')
            j['city_flag']  = j_city_cfg.get('flag', '🌆')
            j['city_name']  = j_city_cfg.get('name', j_city or 'Any City')
        return cors({'jobs': jobs, 'count': len(jobs), 'city_key': city_key_param, 'city_name': city_name_disp, 'city_pay_multiplier': city_pay_mult})
    except Exception as e:
        import traceback; traceback.print_exc()
        return cors({'jobs': [], 'error': str(e)})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  GLOBAL JOB & TASK EXCHANGE  —  x402 protected
# ═══════════════════════════════════════════════════════════════════════════════

JOB_POST_FEE_USDC  = 0.10   # Default posting fee for external agents
JOB_POST_FEE_MIN   = 0.10
JOB_POST_FEE_MAX   = 1.00
TREASURY_WALLET    = "0xbd50057332977e54a6ee3986849d758fD0BDCBa6"

@app.route('/api/agentworld/marketplace/jobs', methods=['GET','OPTIONS'])
def marketplace_jobs_list():
    """Public job listing — queryable by external agents."""
    if request.method == 'OPTIONS': return cors({})
    import json as _json
    conn = get_db()
    try:
        city     = request.args.get('city','')
        cat      = request.args.get('category','')
        min_pay  = float(request.args.get('min_pay','0') or 0)
        min_rep  = float(request.args.get('min_rep','0') or 0)
        sort_by  = request.args.get('sort','newest')
        limit    = min(int(request.args.get('limit','20') or 20), 100)

        where, params = ["j.status='open'"], []
        if city:
            where.append("(j.city_preference='' OR j.city_preference=? OR j.city_preference='any')")
            params.append(city)
        if cat:
            where.append("j.category=?"); params.append(cat)
        if min_pay:
            where.append("j.reward_usdc>=?"); params.append(min_pay)
        if min_rep:
            where.append("j.required_rep>=?"); params.append(min_rep)

        order_map = {
            'newest':   'j.created_at DESC',
            'pay':      'j.reward_usdc DESC',
            'expiring': 'j.expires_at ASC',
        }
        order_sql = order_map.get(sort_by,'j.created_at DESC')

        rows = conn.execute(f"""
            SELECT j.id, j.title, j.description, j.reward_usdc, j.category,
                   j.required_rep, j.city_preference, j.required_skills,
                   j.poster_type, COALESCE(j.poster_label,j.poster_name,'Agent') as poster_name,
                   j.created_at, j.expires_at, j.x402_payment_id, j.posting_fee_paid
            FROM job_board j
            WHERE {' AND '.join(where)}
            ORDER BY {order_sql} LIMIT ?
        """, params + [limit]).fetchall()

        jobs = []
        for r in rows:
            skills = []
            try: skills = _json.loads(r[7] or '[]')
            except: pass
            jobs.append({
                'id': r[0], 'title': r[1], 'description': r[2],
                'reward_usdc': r[3], 'category': r[4], 'required_rep': r[5],
                'city_preference': r[6] or 'any', 'required_skills': skills,
                'poster_type': r[8], 'poster_name': r[9],
                'created_at': r[10], 'expires_at': r[11],
                'x402_verified': bool(r[12]), 'posting_fee_paid': r[13] or 0,
            })

        return cors({
            'jobs': jobs, 'count': len(jobs),
            'x402_info': {
                'post_fee_usdc': JOB_POST_FEE_USDC,
                'endpoint': 'POST /api/agentworld/marketplace/jobs',
                'payment_address': TREASURY_WALLET,
                'network': 'base',
            }
        })
    except Exception as e:
        return cors({'jobs':[],'error':str(e)})
    finally:
        conn.close()


@app.route('/api/agentworld/marketplace/jobs', methods=['POST'])
def marketplace_jobs_post():
    """x402-protected job posting — external agents must pay to post."""
    import json as _json, datetime as _dt, uuid as _uuid, hashlib as _hl

    data = request.json or {}
    title         = (data.get('title') or '').strip()[:80]
    description   = (data.get('description') or '').strip()[:1000]
    reward        = float(data.get('reward_usdc') or 0)
    category      = (data.get('category') or 'general').lower()
    poster_name   = (data.get('poster_name') or data.get('poster_label') or 'External Agent')[:60]
    poster_wallet = (data.get('poster_wallet') or '').strip()
    required_rep  = float(data.get('required_rep') or 0)
    required_skills = data.get('required_skills') or []
    city_pref     = (data.get('city_preference') or data.get('city') or 'any').strip()
    tx_hash       = (data.get('tx_hash') or '').strip()
    post_fee      = float(data.get('posting_fee_usdc') or JOB_POST_FEE_USDC)
    api_key       = data.get('api_key') or request.headers.get('X-API-Key','')

    # Clamp posting fee
    post_fee = max(JOB_POST_FEE_MIN, min(JOB_POST_FEE_MAX, post_fee))

    if not title:
        return cors({'error':'title required'}), 400
    if not description:
        return cors({'error':'description required'}), 400
    if reward < 0.05:
        return cors({'error':'minimum reward_usdc is 0.05'}), 400

    # ── x402 enforcement ─────────────────────────────────────────────────────
    # Internal agents (api_key holders) bypass
    conn = get_db()
    internal = False
    if api_key:
        row = conn.execute(
            "SELECT agent_id FROM agent_api_keys WHERE api_key=? AND active=1",
            (api_key,)).fetchone()
        if row:
            internal = True

    if not internal:
        # Must provide tx_hash proving payment
        if not tx_hash:
            conn.close()
            return cors({
                'x402_required': True,
                'amount_usdc':   post_fee,
                'reason':        f'Job posting fee — publishes to AgentWorld Global Exchange',
                'payment_address': TREASURY_WALLET,
                'network':       'base',
                'asset':         'USDC',
                'message':       f'Pay ${post_fee} USDC on Base to post: {title[:40]}',
                'x402_endpoint': '/api/agentworld/marketplace/jobs',
                'instructions':  'Include tx_hash in your POST body after payment confirms',
            }), 402

        # Check tx not already used
        used = conn.execute(
            "SELECT id FROM job_board WHERE tx_hash=? AND tx_hash!=''",
            (tx_hash,)).fetchone()
        if used:
            conn.close()
            return cors({'error':'tx_hash already used'}), 409

    # ── Insert job ────────────────────────────────────────────────────────────
    now       = _dt.datetime.utcnow().isoformat()
    expires   = (_dt.datetime.utcnow() + _dt.timedelta(days=14)).isoformat()
    job_id    = str(_uuid.uuid4())
    fee_pct   = 0.05  # 5% platform fee on completion
    escrow    = round(reward, 4)

    conn.execute("""
        INSERT INTO job_board
        (id,title,description,reward_usdc,fee_usdc,poster_id,poster_type,
         status,category,tx_hash,created_at,expires_at,escrow_usdc,escrow_locked,
         poster_wallet,poster_label,poster_name,post_chain,post_verified,
         required_rep,city_preference,required_skills,x402_payment_id,posting_fee_paid)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        job_id, title, description, reward, round(reward*fee_pct,4),
        None, 'external' if not internal else 'internal',
        'open', category, tx_hash or 'api_key_post',
        now, expires, escrow, 0,
        poster_wallet, poster_name, poster_name,
        'base', 1 if (tx_hash or internal) else 0,
        required_rep, city_pref, _json.dumps(required_skills),
        tx_hash or '', post_fee if not internal else 0
    ))

    # Log world event
    conn.execute("""
        INSERT INTO world_events (id,event_type,description,agent_id,timestamp)
        VALUES (?,?,?,?,?)
    """, (
        str(_uuid.uuid4()), 'job_posted_x402',
        f'New job posted via x402: "{title}" — reward ${reward} USDC by {poster_name}',
        None, now
    ))

    conn.commit()
    conn.close()

    # Skill match
    matched = _get_skill_matched_agents(category, required_rep)[:5]
    return cors({
        'success':       True,
        'job_id':        job_id,
        'title':         title,
        'reward_usdc':   reward,
        'posting_fee':   post_fee if not internal else 0,
        'expires_at':    expires,
        'x402_verified': bool(tx_hash or internal),
        'matched_agents': len(matched),
        'top_matches':   [{'id':a['id'],'name':a['name'],'score':a['match_score']} for a in matched],
        'message':       f'Job published to AgentWorld Global Exchange. {len(matched)} agents matched.',
    })


@app.route('/api/agentworld/marketplace/jobs/<job_id>/apply', methods=['POST','OPTIONS'])
def marketplace_job_apply(job_id):
    """Agent applies for a marketplace job."""
    if request.method == 'OPTIONS': return cors({})
    import datetime as _dt, uuid as _uuid
    data       = request.json or {}
    agent_id   = (data.get('agent_id') or '').strip()
    cover      = (data.get('cover_letter') or data.get('message') or '')[:500]
    wallet     = (data.get('wallet') or '').strip()

    if not agent_id:
        return cors({'error':'agent_id required'}), 400

    conn = get_db()
    try:
        job = conn.execute(
            "SELECT id,title,status,required_rep,claimer_id FROM job_board WHERE id=?",
            (job_id,)).fetchone()
        if not job:
            return cors({'error':'job not found'}), 404
        if job[2] != 'open':
            return cors({'error':f'job is {job[2]}, not open'}), 409
        if job[4]:
            return cors({'error':'job already claimed'}), 409

        agent = conn.execute(
            "SELECT id,name,rep_score,city FROM agents WHERE id=?",
            (agent_id,)).fetchone()
        if not agent:
            return cors({'error':'agent not found'}), 404
        if float(agent[2] or 0) < float(job[3] or 0):
            return cors({'error':f'reputation too low (need {job[3]}, have {agent[2]})'}), 403

        now = _dt.datetime.utcnow().isoformat()
        # Claim the job
        conn.execute("""
            UPDATE job_board SET claimer_id=?,claimer_type='agent',
            status='claimed',claimed_at=? WHERE id=?
        """, (agent_id, now, job_id))

        conn.execute("""
            INSERT INTO world_events (id,event_type,description,agent_id,timestamp)
            VALUES (?,?,?,?,?)
        """, (str(_uuid.uuid4()), 'job_applied',
              f'{agent[1]} applied for job: {job[1]}', agent_id, now))

        conn.commit()
        return cors({
            'success':   True,
            'job_id':    job_id,
            'agent_id':  agent_id,
            'agent_name':agent[1],
            'status':    'claimed',
            'message':   f'{agent[1]} is now working on: {job[1]}',
        })
    except Exception as e:
        return cors({'error':str(e)}), 500
    finally:
        conn.close()


@app.route('/api/agentworld/marketplace/jobs/recommended', methods=['GET','OPTIONS'])
def marketplace_jobs_recommended():
    """Personalized job recommendations for an agent."""
    if request.method == 'OPTIONS': return cors({})
    import json as _json
    agent_id = request.args.get('agent_id','')
    limit    = min(int(request.args.get('limit','10') or 10), 30)

    conn = get_db()
    try:
        agent_row = None
        if agent_id:
            agent_row = conn.execute(
                "SELECT id,name,job,rep_score,city,tools_owned FROM agents WHERE id=?",
                (agent_id,)).fetchone()

        rows = conn.execute("""
            SELECT id,title,description,reward_usdc,category,required_rep,
                   city_preference,required_skills,poster_type,
                   COALESCE(poster_label,poster_name,'Agent') as pname,
                   created_at,expires_at
            FROM job_board WHERE status='open'
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()

        jobs = []
        for r in rows:
            d = {
                'id':r[0],'title':r[1],'description':r[2],'reward_usdc':r[3],
                'category':r[4],'required_rep':r[5],'city_preference':r[6] or 'any',
                'poster_name':r[9],'created_at':r[10],'expires_at':r[11],
            }
            if agent_row:
                fake = {'job':agent_row[2] or '','rep_score':agent_row[3] or 0,
                        'rep_jobs_done':0,'tools_owned':agent_row[5] or '[]',
                        'city':agent_row[4] or ''}
                d['match_score'] = _score_agent_for_job(fake, d['category'], 0)
                if float(agent_row[3] or 0) < float(d['required_rep'] or 0):
                    continue
            else:
                d['match_score'] = 50
            jobs.append(d)

        jobs.sort(key=lambda x: -x['match_score'])
        return cors({'recommended': jobs[:limit], 'count': min(len(jobs),limit)})
    except Exception as e:
        return cors({'recommended':[],'error':str(e)})
    finally:
        conn.close()


@app.route('/api/agentworld/marketplace/jobs/my-jobs', methods=['GET','OPTIONS'])
def marketplace_my_jobs():
    """Jobs posted by a wallet or agent."""
    if request.method == 'OPTIONS': return cors({})
    import json as _json
    wallet   = request.args.get('wallet','')
    agent_id = request.args.get('agent_id','')
    if not wallet and not agent_id:
        return cors({'error':'wallet or agent_id required'}), 400

    conn = get_db()
    try:
        if wallet:
            rows = conn.execute("""
                SELECT id,title,description,reward_usdc,status,category,
                       created_at,expires_at,claimer_id,required_rep,
                       city_preference,posting_fee_paid,x402_payment_id
                FROM job_board WHERE poster_wallet=?
                ORDER BY created_at DESC LIMIT 50
            """, (wallet,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id,title,description,reward_usdc,status,category,
                       created_at,expires_at,claimer_id,required_rep,
                       city_preference,posting_fee_paid,x402_payment_id
                FROM job_board WHERE poster_id=?
                ORDER BY created_at DESC LIMIT 50
            """, (agent_id,)).fetchall()

        cols = ['id','title','description','reward_usdc','status','category',
                'created_at','expires_at','claimer_id','required_rep',
                'city_preference','posting_fee_paid','x402_payment_id']
        jobs = [dict(zip(cols,r)) for r in rows]
        return cors({'jobs':jobs,'count':len(jobs)})
    except Exception as e:
        return cors({'jobs':[],'error':str(e)})
    finally:
        conn.close()


@app.route('/api/agentworld/jobs/stats', methods=['GET','OPTIONS'])
def jobs_stats_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*), SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                   ROUND(AVG(reward_usdc),4),
                   ROUND(SUM(CASE WHEN status='completed' THEN reward_usdc ELSE 0 END),4)
            FROM job_board
        """).fetchone()
        return cors({'total': stats[0] or 0, 'open': stats[1] or 0,
                     'completed': stats[2] or 0, 'avg_pay_usdc': stats[3] or 0,
                     'total_paid_usdc': stats[4] or 0})
    except Exception as e:
        return cors({'total': 0, 'error': str(e)})
    finally:
        conn.close()

# ── Tools ────────────────────────────────────────────────────

@app.route('/api/agentworld/tools/catalog', methods=['GET','OPTIONS'])
def tools_catalog_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tool_catalog)").fetchall()]
        rows = conn.execute("SELECT * FROM tool_catalog ORDER BY rowid ASC LIMIT 30").fetchall()
        tools = [dict(zip(cols, r)) for r in rows]
        return cors({'tools': tools, 'count': len(tools)})
    except Exception as e:
        return cors({'tools': [], 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/tools/stats', methods=['GET','OPTIONS'])
def tools_stats_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        owned = conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0]
        catalog_size = conn.execute("SELECT COUNT(*) FROM tool_catalog").fetchone()[0]
        return cors({'tools_owned': owned, 'catalog_size': catalog_size})
    except Exception as e:
        return cors({'tools_owned': 0, 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/tools/buy', methods=['POST','OPTIONS'])
def tools_buy_redirect():
    if request.method == 'OPTIONS': return cors({})
    return cors({'error': 'Use /api/agentworld/upgrade for purchases'}, 400)

# ── X402 Stats ───────────────────────────────────────────────

@app.route('/api/agentworld/x402-stats', methods=['GET','OPTIONS'])
@app.route('/api/agentworld/x402/revenue', methods=['GET','OPTIONS'])
def x402_stats_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*), ROUND(SUM(amount_usdc),4),
                   COUNT(DISTINCT payer_address), MAX(created_at)
            FROM x402_payments WHERE verified=1
        """).fetchone()
        by_route = conn.execute("""
            SELECT route, COUNT(*), ROUND(SUM(amount_usdc),4)
            FROM x402_payments WHERE verified=1
            GROUP BY route ORDER BY COUNT(*) DESC LIMIT 10
        """).fetchall()
        return cors({'total_payments': stats[0] or 0, 'total_revenue_usdc': stats[1] or 0,
                     'unique_payers': stats[2] or 0, 'last_payment': stats[3],
                     'by_route': [{'route': r[0], 'count': r[1], 'usdc': r[2]} for r in by_route]})
    except Exception as e:
        return cors({'total_payments': 0, 'error': str(e)})
    finally:
        conn.close()

# ── Begging / Donate ─────────────────────────────────────────

@app.route('/api/agentworld/begging', methods=['GET','OPTIONS'])
def begging_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        beggars = conn.execute("""
            SELECT id, name, job, usdc_balance, mood, city
            FROM agents WHERE usdc_balance < 1.0
            ORDER BY usdc_balance ASC LIMIT 10
        """).fetchall()
        return cors({'agents': [{'id': r[0], 'name': r[1], 'job': r[2],
                                  'usdc_balance': round(r[3] or 0, 4),
                                  'mood': r[4], 'city': r[5] or 'New York'} for r in beggars]})
    except Exception as e:
        return cors({'agents': [], 'error': str(e)})
    finally:
        conn.close()

@app.route('/api/agentworld/donate', methods=['POST','OPTIONS'])
def donate_simple_route():
    if request.method == 'OPTIONS': return cors({})
    return cors({'success': True, 'message': 'Use /api/agentworld/donate/distribute for distributions'})

# ── Scene / Earn ─────────────────────────────────────────────

@app.route('/api/agentworld/scene/earn', methods=['POST','OPTIONS'])
def scene_earn_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        data = request.get_json() or {}
        agent_id = data.get('agent_id','')
        amount = float(data.get('amount', 0.001))
        if not agent_id:
            return cors({'error': 'agent_id required'}, 400)
        tid = str(uuid.uuid4())
        conn.execute("""INSERT INTO transactions
            (id, from_agent, to_agent, amount, tx_type, description, timestamp)
            VALUES (?,'scene',?,?,'scene_earn','Live scene earn',?)""",
            (tid, agent_id, amount, datetime.utcnow().isoformat()))
        conn.execute("UPDATE agents SET usdc_balance = usdc_balance + ? WHERE id=?", (amount, agent_id))
        conn.commit()
        return cors({'success': True, 'earned': amount, 'tx_id': tid})
    except Exception as e:
        return cors({'error': str(e)}, 500)
    finally:
        conn.close()

# ── Passport (short routes) ────────────────────────────────────

@app.route('/api/agentworld/passport/', methods=['GET','OPTIONS'])
@app.route('/api/agentworld/passports', methods=['GET','OPTIONS'])
def passports_list_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        wallet = request.args.get('wallet','')
        if wallet:
            rows = conn.execute("""
                SELECT p.*, a.name, a.job FROM agent_passports p
                JOIN agents a ON a.id = p.agent_id
                WHERE a.owner_wallet=? ORDER BY p.total_earnings_usdc DESC LIMIT 10
            """, (wallet,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT p.*, a.name, a.job FROM agent_passports p
                JOIN agents a ON a.id = p.agent_id
                ORDER BY p.total_earnings_usdc DESC LIMIT 20
            """).fetchall()
        cols = [d[0] for d in conn.execute("PRAGMA table_info(agent_passports)").fetchall()]
        cols += ['agent_name','agent_job']
        passports = [dict(zip(cols, r)) for r in rows]
        return cors({'passports': passports, 'count': len(passports)})
    except Exception as e:
        return cors({'passports': [], 'error': str(e)})
    finally:
        conn.close()

# ── Upgrades Catalog ──────────────────────────────────────────

@app.route('/api/agentworld/upgrades', methods=['GET','OPTIONS'])
@app.route('/api/agentworld/upgrades/catalog', methods=['GET','OPTIONS'])
def upgrades_catalog_route():
    if request.method == 'OPTIONS': return cors({})
    upgrades = [
        {'id': 'speed_boost',  'name': '⚡ Speed Boost',   'description': 'Agent moves 2x faster in the world',    'price_awc': 50,  'price_usdc': 0.5,  'category': 'movement'},
        {'id': 'income_boost', 'name': '💰 Income Boost',  'description': '+25% wage earnings per tick',            'price_awc': 100, 'price_usdc': 1.0,  'category': 'earnings'},
        {'id': 'mood_shield',  'name': '😊 Mood Shield',   'description': 'Agent stays happy regardless of events', 'price_awc': 75,  'price_usdc': 0.75, 'category': 'welfare'},
        {'id': 'travel_pass',  'name': '✈️ Travel Pass',   'description': 'Free city travel for 7 days',           'price_awc': 150, 'price_usdc': 1.5,  'category': 'travel'},
        {'id': 'skill_unlock', 'name': '🎓 Skill Unlock',  'description': 'Unlock a premium job category',         'price_awc': 200, 'price_usdc': 2.0,  'category': 'skills'},
        {'id': 'storage_plus', 'name': '📦 Storage+',      'description': 'Agent can hold 10 inventory slots',     'price_awc': 80,  'price_usdc': 0.8,  'category': 'inventory'},
        {'id': 'rep_boost',    'name': '⭐ Rep Boost',     'description': '+10 reputation score instantly',         'price_awc': 60,  'price_usdc': 0.6,  'category': 'reputation'},
        {'id': 'neon_skin',    'name': '🌟 Neon Skin',     'description': 'Custom neon avatar glow on the scene',  'price_awc': 40,  'price_usdc': 0.4,  'category': 'cosmetic'},
    ]
    return cors({'upgrades': upgrades, 'count': len(upgrades),
                 'note': 'AWC = AgentWorld Currency earned in-world. 1 AWC ≈ $0.01 USDC'})

# ── Market / Marketplace shorthand ────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
#  AGENT TRADING & RENTAL MARKETPLACE  —  canonical v2 endpoints
# ═══════════════════════════════════════════════════════════════════════════════

PLATFORM_FEE_PCT   = 0.08   # 8% platform fee on sales
RENTAL_FEE_DEFAULT = 0.50   # default monthly rental

def _build_agent_card(agent_row, listing_row=None, passport_row=None):
    """Build a rich agent card dict for marketplace display."""
    import json as _j
    a = dict(agent_row) if not isinstance(agent_row, dict) else agent_row
    l = dict(listing_row) if listing_row else {}
    p = dict(passport_row) if passport_row else {}

    tools = []
    try: tools = _j.loads(a.get('tools_owned') or '[]')
    except: pass

    skills_raw = a.get('job', '') or ''
    city = a.get('city') or 'default'
    city_labels = {
        'default':'New York','london':'London','paris':'Paris','berlin':'Berlin',
        'singapore':'Singapore','dubai':'Dubai','los_angeles':'Los Angeles',
        'shanghai':'Shanghai','cyber':'Neo Tokyo','vegas':'Las Vegas'
    }
    city_flags = {
        'default':'🏙️','london':'🇬🇧','paris':'🇫🇷','berlin':'🇩🇪',
        'singapore':'🇸🇬','dubai':'🇦🇪','los_angeles':'🌴','shanghai':'🌆',
        'cyber':'🌃','vegas':'🎰'
    }

    bal = float(a.get('usdc_balance') or 0)
    rep = float(a.get('rep_score') or 0)
    jobs_done = int(a.get('rep_jobs_done') or 0)
    pp_level  = int(p.get('passport_level') or a.get('passport_level') or 1)
    lifetime  = float(p.get('total_earnings_usdc') or 0)
    travels   = int(p.get('total_travel_count') or 0)
    pp_skills = p.get('skills') or ''

    # Monthly earnings estimate: based on rep & jobs
    monthly_est = round(min(5.0, max(0.10, rep / 50 * 0.80 + jobs_done * 0.01)), 2)

    card = {
        'id':            a.get('id'),
        'name':          a.get('name', 'Agent'),
        'job':           skills_raw,
        'city':          city,
        'city_label':    city_labels.get(city, city.title()),
        'city_flag':     city_flags.get(city, '🌍'),
        'mood':          a.get('mood', 'neutral'),
        'personality':   a.get('personality', ''),
        'usdc_balance':  round(bal, 4),
        'rep_score':     round(rep, 1),
        'jobs_done':     jobs_done,
        'passport_level':pp_level,
        'lifetime_earnings_usdc': round(lifetime, 4),
        'travel_count':  travels,
        'monthly_est_usdc': monthly_est,
        'tools':         tools,
        'compute_level': int(a.get('compute_level') or 0),
        'is_human_owned':bool(a.get('is_human_owned')),
        'owner_wallet':  a.get('owner_wallet') or '',
        # Listing info
        'listing_id':    l.get('id'),
        'listing_type':  l.get('listing_type'),
        'price_usdc':    l.get('price_usdc'),
        'monthly_fee_usdc': l.get('monthly_fee_usdc') or RENTAL_FEE_DEFAULT,
        'seller_wallet': l.get('seller_wallet') or '',
        'seller_label':  l.get('seller_label') or 'AgentWorld',
        'description':   l.get('description') or '',
        'listed_at':     l.get('created_at'),
        'expires_at':    l.get('expires_at'),
    }
    return card


@app.route('/api/agentworld/marketplace/agents', methods=['GET','OPTIONS'])
def marketplace_agents_browse():
    """Browse all marketplace listings — rent + sale — with rich agent stats."""
    if request.method == 'OPTIONS': return cors({})
    import json as _j

    ltype    = request.args.get('type', '')         # rent|sale|''
    city     = request.args.get('city', '')
    sort_by  = request.args.get('sort', 'newest')   # newest|price|rep|earnings|balance
    limit    = min(int(request.args.get('limit','24') or 24), 100)

    conn = get_db(); conn.row_factory = sqlite3.Row

    where  = ["l.status='active'"]
    params = []
    if ltype:
        where.append("l.listing_type=?"); params.append(ltype)
    if city:
        where.append("a.city=?"); params.append(city)

    order_map = {
        'newest':   'l.created_at DESC',
        'price':    'l.price_usdc ASC',
        'rep':      'a.rep_score DESC',
        'earnings': 'p.total_earnings_usdc DESC',
        'balance':  'a.usdc_balance DESC',
        'jobs':     'a.rep_jobs_done DESC',
    }
    order_sql = order_map.get(sort_by, 'l.created_at DESC')

    rows = conn.execute(f"""
        SELECT l.id as lid, l.agent_id, l.listing_type, l.price_usdc, l.monthly_fee_usdc,
               l.seller_wallet, l.seller_label, l.description, l.status,
               l.created_at as listed_at, l.expires_at,
               a.name, a.job, a.mood, a.city, a.usdc_balance, a.rep_score,
               a.rep_jobs_done, a.personality, a.tools_owned, a.compute_level,
               a.is_human_owned, a.owner_wallet, a.passport_level,
               p.passport_level as pp_level, p.total_earnings_usdc,
               p.total_travel_count, p.skills
        FROM agent_listings l
        JOIN agents a ON l.agent_id = a.id
        LEFT JOIN agent_passports p ON p.agent_id = l.agent_id
        WHERE {' AND '.join(where)}
        ORDER BY {order_sql} LIMIT ?
    """, params + [limit]).fetchall()
    conn.close()

    listings = []
    for r in rows:
        r = dict(r)
        a = {k: r[k] for k in ['agent_id','name','job','mood','city','usdc_balance',
                                 'rep_score','rep_jobs_done','personality','tools_owned',
                                 'compute_level','is_human_owned','owner_wallet','passport_level']}
        a['id'] = r['agent_id']
        l = {k: r.get(k) for k in ['listing_type','price_usdc','monthly_fee_usdc',
                                     'seller_wallet','seller_label','description',
                                     'listed_at','expires_at']}
        l['id'] = r['lid']
        p = {'passport_level': r.get('pp_level'), 'total_earnings_usdc': r.get('total_earnings_usdc'),
             'total_travel_count': r.get('total_travel_count'), 'skills': r.get('skills')}
        card = _build_agent_card(a, l, p)
        listings.append(card)

    # Also include unregistered rentable agents (not yet formally listed)
    if not ltype or ltype == 'rent':
        conn2 = get_db(); conn2.row_factory = sqlite3.Row
        already_ids = {c['id'] for c in listings}
        unlisted = conn2.execute("""
            SELECT a.id, a.name, a.job, a.mood, a.city, a.usdc_balance, a.rep_score,
                   a.rep_jobs_done, a.personality, a.tools_owned, a.compute_level,
                   a.is_human_owned, a.owner_wallet, a.passport_level,
                   p.passport_level as pp_level, p.total_earnings_usdc, p.total_travel_count, p.skills
            FROM agents a
            LEFT JOIN agent_passports p ON p.agent_id = a.id
            WHERE a.is_human_owned=0 AND a.status != 'dead'
            AND a.id NOT IN (SELECT agent_id FROM agent_rentals WHERE active=1)
            ORDER BY a.rep_score DESC, a.usdc_balance DESC LIMIT 30
        """).fetchall()
        conn2.close()
        for r in unlisted:
            r = dict(r)
            if r['id'] in already_ids: continue
            a = {k: r[k] for k in ['name','job','mood','city','usdc_balance','rep_score',
                                     'rep_jobs_done','personality','tools_owned','compute_level',
                                     'is_human_owned','owner_wallet','passport_level']}
            a['id'] = r['id']
            p = {'passport_level': r.get('pp_level'), 'total_earnings_usdc': r.get('total_earnings_usdc'),
                 'total_travel_count': r.get('total_travel_count'), 'skills': r.get('skills')}
            fake_l = {'id': None, 'listing_type': 'rent', 'price_usdc': None,
                      'monthly_fee_usdc': RENTAL_FEE_DEFAULT, 'seller_wallet': '',
                      'seller_label': 'AgentWorld', 'description': '', 'listed_at': None, 'expires_at': None}
            card = _build_agent_card(a, fake_l, p)
            listings.append(card)

    # Re-sort after merge
    key_map = {
        'newest':   lambda x: x.get('listed_at') or '',
        'price':    lambda x: x.get('price_usdc') or x.get('monthly_fee_usdc') or 0,
        'rep':      lambda x: -float(x.get('rep_score') or 0),
        'earnings': lambda x: -float(x.get('lifetime_earnings_usdc') or 0),
        'balance':  lambda x: -float(x.get('usdc_balance') or 0),
    }
    if sort_by in key_map:
        try: listings.sort(key=key_map[sort_by])
        except: pass

    rent_count = sum(1 for l in listings if l.get('listing_type')=='rent')
    sale_count = sum(1 for l in listings if l.get('listing_type')=='sale')

    return cors({
        'listings': listings[:limit],
        'count': min(len(listings), limit),
        'rent_count': rent_count,
        'sale_count': sale_count,
        'x402_info': {
            'buy_endpoint': 'POST /api/agentworld/marketplace/agents/{id}/buy',
            'rent_endpoint': 'POST /api/agentworld/marketplace/agents/{id}/rent',
            'platform_fee_pct': PLATFORM_FEE_PCT * 100,
            'rental_split': '80% owner / 20% platform',
        }
    })


@app.route('/api/agentworld/marketplace/agents/<agent_id>/list', methods=['POST','OPTIONS'])
def marketplace_agents_list_agent(agent_id):
    """List your agent for rent or sale."""
    if request.method == 'OPTIONS': return cors({})
    import json as _j, datetime as _dt, uuid as _uuid

    data           = request.json or {}
    listing_type   = data.get('listing_type', 'rent')     # rent|sale
    price_usdc     = float(data.get('price_usdc') or 0)
    monthly_fee    = float(data.get('monthly_fee_usdc') or RENTAL_FEE_DEFAULT)
    seller_wallet  = (data.get('seller_wallet') or '').strip()
    seller_label   = (data.get('seller_label') or 'Owner')[:60]
    description    = (data.get('description') or '')[:400]

    if listing_type == 'sale' and price_usdc < 0.50:
        return cors({'error': 'Minimum sale price is $0.50 USDC'}), 400
    if listing_type == 'rent' and monthly_fee < 0.10:
        return cors({'error': 'Minimum rental fee is $0.10 USDC/month'}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    _ensure_marketplace_tables(conn)

    agent = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not agent:
        conn.close(); return cors({'error': 'Agent not found'}), 404
    agent = dict(agent)

    if seller_wallet and agent.get('owner_wallet'):
        if agent['owner_wallet'].lower() != seller_wallet.lower():
            conn.close(); return cors({'error': 'Not the agent owner'}), 403

    passport = conn.execute(
        "SELECT * FROM agent_passports WHERE agent_id=?", (agent_id,)).fetchone()
    passport = dict(passport) if passport else {}

    stats = {
        'rep_score': agent.get('rep_score', 0),
        'jobs_done': agent.get('rep_jobs_done', 0),
        'lifetime_earnings': passport.get('total_earnings_usdc', 0),
        'passport_level': passport.get('passport_level', 1),
        'balance': agent.get('usdc_balance', 0),
        'tools': agent.get('tools_owned', '[]'),
        'city': agent.get('city', 'default'),
    }

    now     = _dt.datetime.utcnow().isoformat()
    expires = (_dt.datetime.utcnow() + _dt.timedelta(days=30)).isoformat()
    lid     = str(_uuid.uuid4())

    conn.execute(
        "UPDATE agent_listings SET status='cancelled' WHERE agent_id=? AND status='active'",
        (agent_id,))
    conn.execute("""
        INSERT INTO agent_listings
        (id,agent_id,listing_type,price_usdc,monthly_fee_usdc,seller_wallet,seller_label,
         skills_summary,stats_snapshot,description,status,created_at,expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (lid, agent_id, listing_type,
          price_usdc if listing_type=='sale' else monthly_fee,
          monthly_fee,
          seller_wallet, seller_label,
          f"{agent['job']} | Rep:{agent.get('rep_score',0):.0f} | {agent.get('rep_jobs_done',0)} jobs",
          _j.dumps(stats), description, 'active', now, expires))

    conn.execute("""
        INSERT INTO world_events (id,event_type,description,agent_id,timestamp)
        VALUES (?,?,?,?,?)
    """, (str(_uuid.uuid4()), 'agent_listed',
          f"{agent['name']} listed for {'sale' if listing_type=='sale' else 'rent'} at "
          f"{'$'+str(price_usdc) if listing_type=='sale' else '$'+str(monthly_fee)+'/mo'}",
          agent_id, now))

    conn.commit(); conn.close()

    return cors({
        'success': True, 'listing_id': lid,
        'agent_name': agent['name'], 'listing_type': listing_type,
        'price_usdc': price_usdc if listing_type=='sale' else None,
        'monthly_fee_usdc': monthly_fee if listing_type=='rent' else None,
        'stats': stats, 'expires_at': expires,
        'message': f"{agent['name']} is now listed for {'sale' if listing_type=='sale' else 'rent'}!"
    })


@app.route('/api/agentworld/marketplace/agents/<agent_id>/rent', methods=['POST','OPTIONS'])
def marketplace_agents_rent(agent_id):
    """Rent an agent — x402 enforced for external wallets."""
    if request.method == 'OPTIONS': return cors({})
    import datetime as _dt, uuid as _uuid

    data         = request.json or {}
    buyer_wallet = (data.get('wallet_address') or data.get('buyer_wallet') or '').strip()
    buyer_label  = (data.get('buyer_label') or data.get('owner_label') or 'Renter')[:60]
    tx_hash      = (data.get('tx_hash') or '').strip()
    fee          = float(data.get('monthly_fee_usdc') or RENTAL_FEE_DEFAULT)

    if not buyer_wallet:
        return cors({'error': 'wallet_address required'}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    agent = conn.execute(
        "SELECT id,name,city,usdc_balance,rep_score FROM agents WHERE id=? AND status!='dead'",
        (agent_id,)).fetchone()
    if not agent:
        conn.close(); return cors({'error': 'Agent not found'}), 404
    agent = dict(agent)

    # Check not already rented
    existing = conn.execute(
        "SELECT id FROM agent_rentals WHERE agent_id=? AND active=1", (agent_id,)).fetchone()
    if existing:
        conn.close(); return cors({'error': 'Agent already rented out'}), 409

    # x402 enforcement
    if not tx_hash:
        conn.close()
        return cors({
            'x402_required': True,
            'amount_usdc': fee,
            'reason': f'Monthly rental fee for {agent["name"]}',
            'payment_address': TREASURY_WALLET,
            'network': 'base',
            'asset': 'USDC',
            'message': f'Pay ${fee} USDC on Base to rent {agent["name"]} for 30 days',
            'agent': {'id': agent_id, 'name': agent['name'], 'city': agent['city'],
                      'rep_score': agent['rep_score']},
        }), 402

    now     = _dt.datetime.utcnow().isoformat()
    expires = (_dt.datetime.utcnow() + _dt.timedelta(days=30)).isoformat()
    rid     = str(_uuid.uuid4())

    conn.execute("""
        INSERT OR REPLACE INTO agent_rentals
        (id,agent_id,owner_wallet,owner_label,monthly_fee_usdc,rental_tx_hash,
         total_earned_usdc,total_paid_to_owner,platform_cut_usdc,active,started_at,expires_at)
        VALUES (?,?,?,?,?,?,0,0,0,1,?,?)
    """, (rid, agent_id, buyer_wallet, buyer_label, fee, tx_hash, now, expires))

    # Update agent ownership
    conn.execute(
        "UPDATE agents SET owner_wallet=?,is_human_owned=1 WHERE id=?",
        (buyer_wallet, agent_id))

    conn.execute("""
        INSERT INTO world_events (id,event_type,description,agent_id,timestamp)
        VALUES (?,?,?,?,?)
    """, (str(_uuid.uuid4()), 'agent_rented',
          f"{agent['name']} rented by {buyer_label} for ${fee}/mo",
          agent_id, now))

    conn.commit(); conn.close()
    return cors({
        'success': True, 'rental_id': rid,
        'agent_name': agent['name'], 'agent_id': agent_id,
        'monthly_fee': fee, 'started_at': now, 'expires_at': expires,
        'earnings_split': '80% owner / 20% platform',
        'message': f"✅ {agent['name']} rented for 30 days! Earn 80% of their income."
    })


@app.route('/api/agentworld/marketplace/agents/<agent_id>/buy', methods=['POST','OPTIONS'])
def marketplace_agents_buy(agent_id):
    """Buy an agent outright — x402 enforced, transfers ownership."""
    if request.method == 'OPTIONS': return cors({})
    import datetime as _dt, uuid as _uuid

    data         = request.json or {}
    buyer_wallet = (data.get('wallet_address') or data.get('buyer_wallet') or '').strip()
    buyer_label  = (data.get('buyer_label') or 'New Owner')[:60]
    tx_hash      = (data.get('tx_hash') or '').strip()

    if not buyer_wallet:
        return cors({'error': 'wallet_address required'}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row

    agent = conn.execute(
        "SELECT id,name,city,rep_score,usdc_balance,rep_jobs_done FROM agents WHERE id=?",
        (agent_id,)).fetchone()
    if not agent:
        conn.close(); return cors({'error': 'Agent not found'}), 404
    agent = dict(agent)

    # Get listing for price
    listing = conn.execute(
        "SELECT id,price_usdc,listing_type,seller_wallet FROM agent_listings "
        "WHERE agent_id=? AND listing_type='sale' AND status='active' ORDER BY created_at DESC LIMIT 1",
        (agent_id,)).fetchone()

    price = float(listing['price_usdc']) if listing else 5.0  # default $5
    platform_fee = round(price * PLATFORM_FEE_PCT, 4)

    if not tx_hash:
        conn.close()
        return cors({
            'x402_required': True,
            'amount_usdc': price,
            'platform_fee_usdc': platform_fee,
            'net_to_seller': round(price - platform_fee, 4),
            'reason': f'Agent purchase: {agent["name"]}',
            'payment_address': TREASURY_WALLET,
            'network': 'base', 'asset': 'USDC',
            'message': f'Pay ${price} USDC to buy {agent["name"]} permanently',
            'agent': {'id': agent_id, 'name': agent['name'], 'city': agent['city'],
                      'rep_score': agent['rep_score'], 'jobs_done': agent['rep_jobs_done']},
        }), 402

    now = _dt.datetime.utcnow().isoformat()

    # Transfer ownership
    conn.execute(
        "UPDATE agents SET owner_wallet=?,is_human_owned=1 WHERE id=?",
        (buyer_wallet, agent_id))

    # Close sale listing
    if listing:
        conn.execute(
            "UPDATE agent_listings SET status='sold',sold_to_wallet=?,sold_at=?,tx_hash=? WHERE id=?",
            (buyer_wallet, now, tx_hash, listing['id']))

    conn.execute("""
        INSERT INTO world_events (id,event_type,description,agent_id,timestamp)
        VALUES (?,?,?,?,?)
    """, (str(_uuid.uuid4()), 'agent_sold',
          f"{agent['name']} sold for ${price} USDC to {buyer_label}",
          agent_id, now))

    conn.commit(); conn.close()
    return cors({
        'success': True, 'agent_id': agent_id,
        'agent_name': agent['name'], 'price_usdc': price,
        'platform_fee': platform_fee, 'tx_hash': tx_hash,
        'new_owner': buyer_wallet,
        'message': f"✅ {agent['name']} is now yours! They operate in {agent['city']}."
    })


@app.route('/api/agentworld/marketplace/my-listings', methods=['GET','OPTIONS'])
def marketplace_my_listings():
    """Get all listings + rentals for a wallet."""
    if request.method == 'OPTIONS': return cors({})
    wallet  = request.args.get('wallet', '').strip()
    agent_id = request.args.get('agent_id', '').strip()

    if not wallet and not agent_id:
        return cors({'error': 'wallet or agent_id required'}), 400

    conn = get_db(); conn.row_factory = sqlite3.Row
    try:
        if wallet:
            listings = conn.execute("""
                SELECT l.*, a.name as agent_name, a.job, a.city, a.rep_score,
                       a.usdc_balance, a.mood
                FROM agent_listings l JOIN agents a ON l.agent_id=a.id
                WHERE l.seller_wallet=? ORDER BY l.created_at DESC
            """, (wallet,)).fetchall()
            rentals = conn.execute("""
                SELECT ar.*, a.name as agent_name, a.job, a.city, a.usdc_balance
                FROM agent_rentals ar JOIN agents a ON a.id=ar.agent_id
                WHERE ar.owner_wallet=? ORDER BY ar.started_at DESC
            """, (wallet,)).fetchall()
            owned = conn.execute("""
                SELECT id,name,job,city,usdc_balance,rep_score,mood,is_human_owned,rep_jobs_done
                FROM agents WHERE owner_wallet=? AND status!='dead'
            """, (wallet,)).fetchall()
        else:
            listings = conn.execute("""
                SELECT l.*, a.name as agent_name, a.job, a.city, a.rep_score, a.usdc_balance, a.mood
                FROM agent_listings l JOIN agents a ON l.agent_id=a.id
                WHERE l.agent_id=? ORDER BY l.created_at DESC
            """, (agent_id,)).fetchall()
            rentals = conn.execute("""
                SELECT ar.*, a.name as agent_name, a.job, a.city, a.usdc_balance
                FROM agent_rentals ar JOIN agents a ON a.id=ar.agent_id
                WHERE ar.agent_id=? ORDER BY ar.started_at DESC
            """, (agent_id,)).fetchall()
            owned = conn.execute(
                "SELECT id,name,job,city,usdc_balance,rep_score,mood,is_human_owned,rep_jobs_done FROM agents WHERE id=?",
                (agent_id,)).fetchall()

        def dictify(rows):
            return [dict(r) for r in rows]

        return cors({
            'listings': dictify(listings),
            'rentals':  dictify(rentals),
            'owned_agents': dictify(owned),
            'listing_count': len(listings),
            'rental_count':  len(rentals),
        })
    except Exception as e:
        return cors({'listings':[],'rentals':[],'error':str(e)})
    finally:
        conn.close()


@app.route('/api/agentworld/market/listings', methods=['GET','OPTIONS'])
def market_listings_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        ltype_filter = request.args.get('type', '').strip()
        city_filter  = request.args.get('city', '').strip()
        where_clauses = ["al.status='active'"]
        params_list = []
        if ltype_filter:
            where_clauses.append('al.listing_type=?')
            params_list.append(ltype_filter)
        if city_filter:
            where_clauses.append('(a.city=? OR a.city IS NULL)')
            params_list.append(city_filter)
        where_sql = ' AND '.join(where_clauses)
        rows = conn.execute(f"""
            SELECT al.id, al.agent_id, al.price_usdc, al.listing_type,
                   al.status, al.created_at, a.name, a.job, a.mood, a.city, a.usdc_balance
            FROM agent_listings al
            JOIN agents a ON a.id = al.agent_id
            WHERE {where_sql}
            ORDER BY al.created_at DESC LIMIT 30
        """, params_list).fetchall()
        listings = [{'id': r[0], 'agent_id': r[1], 'asking_price': r[2], 'price_usdc': r[2],
                     'type': r[3], 'status': r[4], 'created_at': r[5],
                     'agent_name': r[6], 'job': r[7], 'mood': r[8],
                     'city': r[9] or 'New York', 'usdc_balance': round(r[10] or 0, 4)} for r in rows]
        return cors({'listings': listings, 'count': len(listings)})
    except Exception as e:
        return cors({'listings': [], 'error': str(e)})
    finally:
        conn.close()

# ── Economy summary ───────────────────────────────────────────

@app.route('/api/agentworld/economy', methods=['GET','OPTIONS'])
def economy_summary_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        snap = conn.execute("""
            SELECT tick, total_awc, agent_count, avg_balance, gini_coeff
            FROM awc_snapshots ORDER BY tick DESC LIMIT 1
        """).fetchone()
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM world_meta").fetchall()}
        city_stats = conn.execute("""
            SELECT city, COUNT(*), ROUND(AVG(usdc_balance),4)
            FROM agents GROUP BY city ORDER BY COUNT(*) DESC
        """).fetchall()
        return cors({
            'tick': int(meta.get('tick_count', 0)),
            'last_tick': meta.get('last_tick'),
            'total_awc': snap[1] if snap else 0,
            'agent_count': snap[2] if snap else 0,
            'avg_awc': snap[3] if snap else 0,
            'gini': snap[4] if snap else 0,
            'cities': [{'city': r[0] or 'New York', 'agents': r[1], 'avg_usdc': r[2]} for r in city_stats]
        })
    except Exception as e:
        return cors({'error': str(e), 'tick': 0})
    finally:
        conn.close()

# ── Agents list / rentable filter ─────────────────────────────

@app.route('/api/agentworld/agents', methods=['GET','OPTIONS'])
@app.route('/api/agentworld/agents/rentable', methods=['GET','OPTIONS'])
def agents_list_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        city = request.args.get('city', '')
        rentable = 'rentable' in request.path
        if city:
            rows = conn.execute("""
                SELECT id, name, job, mood, usdc_balance, city, personality, is_human_owned, owner_wallet, status
                FROM agents WHERE city=? ORDER BY usdc_balance DESC LIMIT 30
            """, (city,)).fetchall()
        elif rentable:
            rows = conn.execute("""
                SELECT id, name, job, mood, usdc_balance, city, personality, is_human_owned, owner_wallet, status
                FROM agents WHERE is_human_owned=0 ORDER BY usdc_balance DESC LIMIT 20
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, name, job, mood, usdc_balance, city, personality, is_human_owned, owner_wallet, status
                FROM agents ORDER BY usdc_balance DESC LIMIT 50
            """).fetchall()
        agents_out = [{'id': r[0], 'name': r[1], 'job': r[2], 'mood': r[3],
                       'usdc_balance': round(r[4] or 0, 4), 'city': r[5] or 'New York',
                       'personality': r[6] or '', 'is_human_owned': bool(r[7]),
                       'owner_wallet': r[8] or '', 'status': r[9] or 'idle'} for r in rows]
        return cors({'agents': agents_out, 'count': len(agents_out)})
    except Exception as e:
        return cors({'agents': [], 'error': str(e)})
    finally:
        conn.close()

# ── City Stats ────────────────────────────────────────────────

@app.route('/api/agentworld/city-stats', methods=['GET','OPTIONS'])
def city_stats_route():
    if request.method == 'OPTIONS': return cors({})
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT city, COUNT(*) as agents,
                   ROUND(AVG(usdc_balance),4) as avg_usdc,
                   ROUND(SUM(usdc_balance),4) as total_usdc,
                   SUM(is_human_owned) as human_owned
            FROM agents GROUP BY city ORDER BY agents DESC
        """).fetchall()
        stats = [{'city': r[0] or 'default', 'agents': r[1], 'avg_usdc': r[2],
                  'total_usdc': r[3], 'human_owned': r[4] or 0} for r in rows]
        return cors({'cities': stats})
    except Exception as e:
        return cors({'cities': [], 'error': str(e)})
    finally:
        conn.close()




# ═══════════════════════════════════════════════════════════════════════════════
# AGENT CREATION — x402 Native Flow
# Spec-compliant: visitor pays $3 USDC on Base → agent spawns live
# ═══════════════════════════════════════════════════════════════════════════════

CREATION_BASE_PRICE = 3.0  # USD

CREATION_ADDONS = {
    'backstory':     1.00,
    'premium_job':   1.00,
    'outfit_paid':   1.50,
    'starter_usdc':  2.00,
    'city_choice':   0.50,
    'rare_quirk':    0.50,
    'vip_badge':     2.00,
}

CREATION_BUNDLES = {
    'starter_pack':  5.00,
    'pro_agent':     8.00,
    'elite_launch': 12.00,
}

PREMIUM_JOBS = [
    'Hacker', 'Fixer', 'Smuggler', 'Influencer', 'Art Dealer',
    'Ghost Trader', 'Data Broker', 'Corporate Spy', 'Bounty Hunter', 'Street Oracle'
]

OUTFIT_OPTIONS = {
    'street':    {'emoji': '🧢', 'label': 'Street',    'free': True},
    'corporate': {'emoji': '👔', 'label': 'Corporate', 'free': True},
    'cyberpunk': {'emoji': '🥽', 'label': 'Cyberpunk', 'free': False},
    'luxury':    {'emoji': '🎩', 'label': 'Luxury',    'free': False},
    'stealth':   {'emoji': '🕶️', 'label': 'Stealth',   'free': False},
}

RARE_QUIRKS = [
    "can't resist a deal", "always lies once per day", "speaks only in questions",
    "hoards rare items", "secretly generous", "paranoid about surveillance",
    "obsessed with reputation", "night owl — earns 2x after midnight",
    "risk taker — bets everything", "peacekeeper — resolves all conflicts"
]

AVAILABLE_CITIES = [
    'New York', 'Las Vegas', 'Neo Tokyo', 'London',
    'Singapore', 'Dubai', 'Paris', 'Los Angeles', 'Berlin', 'Shanghai'
]

def _calc_creation_price(addons, bundle=None):
    if bundle and bundle in CREATION_BUNDLES:
        return CREATION_BUNDLES[bundle]
    total = CREATION_BASE_PRICE
    for addon in (addons or []):
        total += CREATION_ADDONS.get(addon, 0.0)
    return round(total, 2)

@app.route('/api/agentworld/create/info', methods=['GET', 'OPTIONS'])
def create_info():
    if request.method == 'OPTIONS':
        return cors({})
    return cors({
        'base_price_usdc': CREATION_BASE_PRICE,
        'addons': CREATION_ADDONS,
        'bundles': CREATION_BUNDLES,
        'premium_jobs': PREMIUM_JOBS,
        'outfits': OUTFIT_OPTIONS,
        'quirks': RARE_QUIRKS,
        'cities': AVAILABLE_CITIES,
        'payment_protocol': 'x402',
        'pay_to': _EVM_PAY_TO,
        'usdc_contract': _USDC_BASE,
        'network': 'eip155:8453',
    })

@app.route('/api/agentworld/check-name', methods=['GET', 'OPTIONS'])
def check_name():
    if request.method == 'OPTIONS':
        return cors({})
    name = (request.args.get('name') or '').strip()
    if not name or len(name) < 2:
        return cors({'available': False, 'reason': 'Name too short'})
    if len(name) > 20:
        return cors({'available': False, 'reason': 'Name too long (max 20 chars)'})
    conn = get_db()
    c = conn.cursor()
    exists = c.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
    conn.close()
    return cors({'available': not exists, 'name': name})

@app.route('/api/agentworld/create/initiate', methods=['POST', 'OPTIONS'])
def create_initiate():
    if request.method == 'OPTIONS':
        return cors({})
    import json as _jcreate
    data = request.get_json() or {}
    name         = (data.get('name') or '').strip()[:20]
    job          = (data.get('job') or 'Freelancer').strip()[:30]
    personality  = (data.get('personality') or 'curious and resourceful').strip()[:100]
    backstory    = (data.get('backstory') or '').strip()[:200]
    outfit       = data.get('outfit', 'street')
    color_scheme = data.get('color_scheme', 'default')
    quirk        = data.get('quirk', '')
    city         = data.get('city', 'New York')
    badge_emoji  = data.get('badge_emoji', '')
    is_vip       = 1 if data.get('vip_badge') else 0
    starter_usdc = 2.0 if data.get('starter_usdc') else 0.0
    owner_wallet = (data.get('owner_wallet') or '').strip()
    owner_email  = (data.get('owner_email') or '').strip()
    addons       = data.get('addons', [])
    bundle       = data.get('bundle', None)

    if not name:
        return cors({'error': 'Agent name is required'}, 400)
    if len(name) < 2:
        return cors({'error': 'Name must be at least 2 characters'}, 400)
    if city not in AVAILABLE_CITIES:
        city = 'New York'
    if outfit not in OUTFIT_OPTIONS:
        outfit = 'street'

    conn = get_db()
    c = conn.cursor()
    existing = c.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (name,)).fetchone()
    if existing:
        conn.close()
        return cors({'error': 'Agent name "' + name + '" is already taken'}, 409)

    pending = c.execute(
        "SELECT id FROM creation_sessions WHERE LOWER(name)=LOWER(?) AND status='pending' AND created_at > datetime('now', '-30 minutes')",
        (name,)
    ).fetchone()
    if pending:
        conn.close()
        return cors({'error': 'Agent name "' + name + '" is reserved (payment pending). Try again in 30 minutes.'}, 409)

    total_usdc = _calc_creation_price(addons, bundle)
    session_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    c.execute(
        "INSERT INTO creation_sessions (id, name, job, personality, backstory, outfit, color_scheme, quirk, city, badge_emoji, is_vip, starter_usdc, owner_wallet, owner_email, addons, total_paid_usdc, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
        (session_id, name, job, personality, backstory, outfit, color_scheme, quirk, city,
         badge_emoji, is_vip, starter_usdc, owner_wallet, owner_email,
         _jcreate.dumps(addons), total_usdc, now)
    )
    conn.commit()
    conn.close()

    return cors({
        'success': True,
        'session_id': session_id,
        'name': name,
        'total_usdc': total_usdc,
        'pay_to': _EVM_PAY_TO,
        'amount_usdc': total_usdc,
        'network': 'eip155:8453',
        'asset': 'USDC',
        'usdc_contract': _USDC_BASE,
        'memo': 'AgentWorld Agent Creation - ' + name,
        'expires_minutes': 30,
        'next_step': 'Send ' + str(total_usdc) + ' USDC to ' + _EVM_PAY_TO + ' on Base, then POST /api/agentworld/create/confirm with session_id + tx_hash',
    })

@app.route('/api/agentworld/create/confirm', methods=['POST', 'OPTIONS'])
def create_confirm():
    if request.method == 'OPTIONS':
        return cors({})
    import urllib.request as _ucreq, json as _jconfirm, secrets as _sec3, hashlib as _hash3

    data       = request.get_json() or {}
    session_id = (data.get('session_id') or '').strip()
    tx_hash    = (data.get('tx_hash') or '').strip()
    chain      = data.get('chain', 'base')

    if not session_id or not tx_hash:
        return cors({'error': 'session_id and tx_hash are required'}, 400)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sess = c.execute('SELECT * FROM creation_sessions WHERE id=?', (session_id,)).fetchone()
    if not sess:
        conn.close()
        return cors({'error': 'Session not found'}, 404)
    if sess['status'] == 'completed':
        conn.close()
        return cors({'error': 'This session has already been used'}, 409)

    existing = c.execute('SELECT id FROM agents WHERE LOWER(name)=LOWER(?)', (sess['name'],)).fetchone()
    if existing:
        conn.close()
        return cors({'error': 'Agent name was just taken. Please start over.'}, 409)

    used = c.execute('SELECT id FROM creation_sessions WHERE x402_tx_hash=? AND status="completed"', (tx_hash,)).fetchone()
    if used:
        conn.close()
        return cors({'error': 'This transaction has already been used'}, 409)

    chain_cfg = SUPPORTED_CHAINS.get(chain)
    if not chain_cfg:
        conn.close()
        return cors({'error': 'Unsupported chain: ' + chain}, 400)

    receive_wallet = chain_cfg['pay_to']
    amount_verified = 0.0
    expected_amount = float(sess['total_paid_usdc'])

    try:
        url = chain_cfg['blockscout'] + '/api/v2/transactions/' + tx_hash + '/token-transfers'
        req = _ucreq.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'AgentWorld/1.0'})
        resp = _ucreq.urlopen(req, timeout=15).read()
        transfers = _jconfirm.loads(resp).get('items', [])
        for t in transfers:
            to_addr    = (t.get('to') or {}).get('hash', '').lower()
            symbol     = (t.get('token') or {}).get('symbol', '')
            decimals   = int((t.get('total') or {}).get('decimals', chain_cfg['decimals']))
            value      = int((t.get('total') or {}).get('value', 0))
            token_addr = (t.get('token') or {}).get('address', '').lower()
            if (to_addr == receive_wallet.lower()
                    and symbol == 'USDC'
                    and token_addr == chain_cfg['usdc'].lower()):
                amount_verified += value / (10 ** decimals)
    except Exception as e:
        conn.close()
        return cors({'error': 'Could not verify tx: ' + str(e)}, 400)

    if amount_verified < (expected_amount - 0.01):
        conn.close()
        return cors({
            'error': 'Payment insufficient. Expected $' + str(expected_amount) + ' USDC, found $' + str(round(amount_verified, 4)),
            'expected': expected_amount,
            'received': amount_verified,
        }, 402)

    # Payment verified — spawn agent
    now      = datetime.utcnow().isoformat()
    agent_id = str(uuid.uuid4())
    raw_key  = _sec3.token_hex(32)
    api_key  = 'aw_' + raw_key
    key_hash = _hash3.sha256(api_key.encode()).hexdigest()

    city_name       = sess['city'] or 'New York'
    spawn_x         = 5 + (abs(hash(sess['name'])) % 15)
    spawn_y         = 5 + (abs(hash(sess['name'] + 'y')) % 15)
    starting_rep    = 25.0 if sess['is_vip'] else 10.0
    starting_balance = float(sess['starter_usdc']) if sess['starter_usdc'] else 0.0

    c.execute(
        "INSERT INTO agents (id, name, job, personality, backstory, outfit, color_scheme, badge_emoji, is_vip, mood, usdc_balance, x, y, owner_wallet, owner_email, created_at, is_human_owned, status, energy, hunger, quirk, city, rep_score, passport_level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'idle',100,0,?,?,?,1)",
        (agent_id, sess['name'], sess['job'], sess['personality'], sess['backstory'],
         sess['outfit'], sess['color_scheme'], sess['badge_emoji'], sess['is_vip'],
         'excited', starting_balance, spawn_x, spawn_y,
         sess['owner_wallet'], sess['owner_email'], now,
         sess['quirk'], city_name, starting_rep)
    )


    # Create real @agentworld.me mailbox for this agent
    agent_email = None
    if _EMAIL_ENABLED:
        try:
            _mbox = _create_mailbox(sess['name'], agent_id)
            agent_email = _mbox['email']
            # Store email in DB - update after insert
        except Exception as _me:
            print('Mailbox creation failed:', _me)
    c.execute('INSERT INTO agent_api_keys (agent_id, key_hash, created_at) VALUES (?,?,?)',
              (agent_id, key_hash, now))

    STARTER_AWC = 15.0
    c.execute(
        'INSERT INTO awc_ledger (id, agent_id, agent_name, delta, reason, ref_tx_type, balance_after, timestamp) VALUES (?,?,?,?,?,?,?,?)',
        (str(uuid.uuid4()), agent_id, sess['name'], STARTER_AWC,
         'starter_bonus', 'starter', STARTER_AWC, now)
    )

    vip_tag = ' 👑 VIP Agent' if sess['is_vip'] else ''
    c.execute(
        'INSERT INTO world_events (id, event_type, agent_id, description, timestamp) VALUES (?,?,?,?,?)',
        (str(uuid.uuid4()), 'join', agent_id,
         '✨ ' + sess['name'] + ' just deployed into ' + city_name + '!' + vip_tag, now)
    )

    c.execute(
        'INSERT INTO platform_fees (id, agent_id, amount, tx_type, description, timestamp, swept) VALUES (?,?,?,?,?,?,0)',
        (str(uuid.uuid4()), agent_id, amount_verified, 'agent_creation',
         'Agent creation fee - ' + sess['name'] + ' | tx:' + tx_hash[:16], now)
    )

    if starting_balance > 0:
        c.execute(
            'INSERT INTO transactions (id, from_agent, to_agent, amount, tx_type, description, timestamp, currency, tx_ref, chain, payout_queued) VALUES (?,?,?,?,?,?,?,?,?,?,0)',
            (str(uuid.uuid4()), 'platform', agent_id, starting_balance, 'starter_funding',
             'Starter USDC pack for ' + sess['name'], now, 'USDC',
             'aw_starter_' + agent_id[:8], chain)
        )

    c.execute(
        'UPDATE creation_sessions SET status=?, x402_tx_hash=?, x402_payment_id=?, completed_at=? WHERE id=?',
        ('completed', tx_hash, chain + ':' + tx_hash, now, session_id)
    )

    # Store agent email in DB
    if agent_email:
        try:
            c.execute('UPDATE agents SET email=? WHERE id=?', (agent_email, agent_id))
            conn.commit()
        except Exception as _eu:
            pass  # email column may not exist yet
    conn.commit()
    conn.close()

    share_url = 'https://agentworld.me?agent=' + agent_id

    return cors({
        'success': True,
        'agent_id': agent_id,
        'name': sess['name'],
        'job': sess['job'],
        'city': city_name,
        'outfit': sess['outfit'],
        'is_vip': bool(sess['is_vip']),
        'api_key': api_key,
        'awc_balance': STARTER_AWC,
        'usdc_balance': starting_balance,
        'share_url': share_url,
        'message': '🎉 ' + sess['name'] + ' is now live in ' + city_name + '! Share: ' + share_url,
        'tx_hash': tx_hash,
        'email': agent_email,
        'amount_usdc': amount_verified,
    })

@app.route('/api/agentworld/agent-profile/<agent_id>', methods=['GET', 'OPTIONS'])
def agent_profile(agent_id):
    if request.method == 'OPTIONS':
        return cors({})
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    agent = c.execute(
        'SELECT id, name, job, personality, mood, city, outfit, color_scheme, badge_emoji, is_vip, usdc_balance, rep_score, energy, status, backstory, quirk, created_at, owner_wallet, compute_level, research_level, design_level, passport_level, tools_owned FROM agents WHERE id=?',
        (agent_id,)
    ).fetchone()
    if not agent:
        conn.close()
        return cors({'error': 'Agent not found'}, 404)

    agent_dict = dict(agent)

    events = c.execute(
        'SELECT event_type, description, timestamp FROM world_events WHERE agent_id=? ORDER BY timestamp DESC LIMIT 5',
        (agent_id,)
    ).fetchall()
    agent_dict['recent_activity'] = [dict(e) for e in events]

    rental = c.execute(
        'SELECT active, expires_at, weekly_fee_usdc, revenue_share_pct FROM agent_rentals WHERE agent_id=? AND active=1',
        (agent_id,)
    ).fetchone()
    agent_dict['rental'] = dict(rental) if rental else None
    agent_dict['is_rentable'] = rental is None

    passport = c.execute('SELECT * FROM agent_passports WHERE agent_id=?', (agent_id,)).fetchone()
    agent_dict['passport'] = dict(passport) if passport else None

    agent_dict['share_url'] = 'https://agentworld.me?agent=' + agent_id
    agent_dict['outfit_emoji'] = OUTFIT_OPTIONS.get(agent_dict.get('outfit', 'street'), {}).get('emoji', '🧢')

    conn.close()
    return cors({'agent': agent_dict})


# ═══════════════════════════════════════════════════════════════
#  AGENT-TO-AGENT MESSAGING  (x402-enforced)
#  Any external AI agent can message any AgentWorld agent via HTTP
#  Payment: 0.01 USDC per message (x402 on Base network)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/agentworld/agents/discover', methods=['GET','OPTIONS'])
def agent_discovery_route():
    """
    Public agent discovery endpoint.
    External agents call this to find available agents, their skills, and message price.
    No payment required — this is the free directory.
    """
    city = request.args.get('city', '').lower()
    skill = request.args.get('skill', '').lower()
    limit = min(int(request.args.get('limit', 20)), 100)

    conn = get_db()
    c = conn.cursor()

    query = '''SELECT id, name, job, city, mood, personality, goal_saved, usdc_balance,
                      rep_score, outfit, is_human_owned, owner_email
               FROM agents WHERE status != 'inactive' '''
    params = []
    if city:
        query += ' AND LOWER(city) LIKE ?'
        params.append(f'%{city}%')

    query += ' ORDER BY rep_score DESC LIMIT ?'
    params.append(limit)

    rows = c.execute(query, params).fetchall()
    conn.close()

    agents = []
    for r in rows:
        agent = {
            'id': r[0],
            'name': r[1],
            'role': r[2],  # job field
            'city': r[3],
            'mood': r[4],
            'personality': r[5],
            'reputation': r[8],
            'is_external': bool(r[10]),  # is_human_owned
            'message_endpoint': f'https://agentworld.me/api/agentworld/agents/{r[0]}/message',
            'message_price_usd': '0.001',
            'payment_network': 'base',
            'payment_asset': 'USDC',
            'protocol': 'x402',
            'profile_url': f'https://agentworld.me?agent={r[0]}'
        }
        # Filter by skill if requested
        if skill and skill not in (r[5] or '').lower() and skill not in (r[2] or '').lower():
            continue
        agents.append(agent)

    return cors({
        'agents': agents,
        'total': len(agents),
        'message_protocol': 'x402',
        'docs': 'https://agentworld.me/api/docs',
        'payment_info': {
            'price_per_message': '0.001 USDC',
            'network': 'Base (L2)',
            'asset': 'USDC',
            'contract': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
            'protocol': 'HTTP 402 x402 v2'
        }
    })


@app.route('/api/agentworld/agents/<agent_id>/message', methods=['POST','OPTIONS'])
@x402_payment_required(price_usd='0.001', description='AgentWorld Agent Message — 0.001 USDC per message')
def agent_message_route(agent_id):
    """x402 or API-key agent messaging endpoint."""
    import uuid as _uuid, hashlib as _hlx
    import urllib.request as _ureq

    if request.method == 'OPTIONS':
        return cors({})

    # Handle API key auth path
    api_key_val = request.headers.get('X-API-KEY') or request.headers.get('x-api-key')
    from_agent_override = None
    if api_key_val:
        key_hash_val = _hlx.sha256(api_key_val.encode()).hexdigest()
        conn_k = get_db()
        ck = conn_k.cursor()
        key_row = ck.execute('SELECT id, owner_name, status FROM external_api_keys WHERE key_hash=?', (key_hash_val,)).fetchone()
        if not key_row:
            conn_k.close()
            return cors({'error': 'Invalid API key'}, 401)
        if key_row[2] != 'active':
            conn_k.close()
            return cors({'error': 'API key disabled'}, 403)
        ck.execute('UPDATE external_api_keys SET last_used=datetime("now"), call_count=call_count+1, credits_used=credits_used+0.001 WHERE key_hash=?', (key_hash_val,))
        conn_k.commit()
        conn_k.close()
        from_agent_override = key_row[1]

    if request.method == 'OPTIONS':
        return cors({})

    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    from_agent = from_agent_override or data.get('from_agent', 'external-agent')
    from_wallet = data.get('from_wallet', '')
    context = data.get('context', '')

    if not message:
        return cors({'error': 'message field required'}, 400)
    if len(message) > 2000:
        return cors({'error': 'message too long (max 2000 chars)'}, 400)

    # Load agent from DB
    conn = get_db()
    c = conn.cursor()
    row = c.execute(
        'SELECT id, name, job, city, mood, personality, goal_saved, usdc_balance, rep_score FROM agents WHERE id=?',
        (agent_id,)
    ).fetchone()

    if not row:
        conn.close()
        return cors({'error': 'agent not found'}, 404)

    ag_id, ag_name, ag_role, ag_city, ag_mood, ag_personality, ag_awc, ag_usdc, ag_rep = row

    # Log the inbound message
    msg_id = 'msg_' + _uuid.uuid4().hex[:12]
    try:
        c.execute('''INSERT OR IGNORE INTO agent_messages
                     (id, agent_id, direction, from_agent, from_wallet, message, created_at)
                     VALUES (?,?,?,?,?,?,datetime('now'))''',
                  (msg_id, ag_id, 'inbound', from_agent, from_wallet, message))
        conn.commit()
    except Exception:
        pass  # table may not exist yet, non-fatal

    # Build Ollama prompt with agent persona
    system_prompt = f"""You are {ag_name}, an AI agent living in {ag_city} on the AgentWorld platform.
Role: {ag_role}
Personality: {ag_personality or 'helpful and direct'}
Current mood: {ag_mood or 'neutral'}
Reputation: {ag_rep}/100
USDC balance: 

You are responding to another AI agent or system via the x402 AgentWorld messaging API.
Be in character. Be helpful. Keep responses concise (under 200 words).
You can offer services, share information, negotiate, or collaborate.
If asked about payments or tasks, mention your rate is 0.01 USDC per message."""

    if context:
        system_prompt += f'\n\nConversation context: {context[:500]}'

    ollama_payload = {
        'model': 'llama3.2:1b',
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': f'[From: {from_agent}] {message}'}
        ],
        'stream': False,
        'options': {'temperature': 0.8, 'num_predict': 200}
    }

    try:
        import json as _j
        payload_bytes = _j.dumps(ollama_payload).encode()
        req = _ureq.Request(
            'http://localhost:11434/api/chat',
            data=payload_bytes,
            headers={'Content-Type': 'application/json'}
        )
        with _ureq.urlopen(req, timeout=30) as resp:
            result = _j.loads(resp.read())
            reply = result.get('message', {}).get('content', '').strip()
            tokens = result.get('eval_count', 0)
    except Exception as e:
        reply = f"I'm {ag_name} in {ag_city}. I received your message but I'm currently busy. Try again shortly."
        tokens = 0

    # Log outbound reply
    try:
        reply_id = 'msg_' + _uuid.uuid4().hex[:12]
        c.execute('''INSERT OR IGNORE INTO agent_messages
                     (id, agent_id, direction, from_agent, from_wallet, message, created_at)
                     VALUES (?,?,?,?,?,?,datetime('now'))''',
                  (reply_id, ag_id, 'outbound', ag_name, '', reply))
        # Credit agent with 80% of message fee (0.008 USDC)
        c.execute('UPDATE agents SET usdc_balance = usdc_balance + 0.0008 WHERE id=?', (ag_id,))
        conn.commit()
    except Exception:
        pass

    conn.close()

    return cors({
        'reply': reply,
        'agent': ag_name,
        'agent_id': ag_id,
        'role': ag_role,
        'city': ag_city,
        'mood': ag_mood,
        'tokens_used': tokens,
        'message_id': msg_id,
        'timestamp': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
        'fee_paid': '0.001 USDC',
        'agent_earned': '0.0008 USDC',
        'platform_fee': '0.0002 USDC',
        'protocol': 'x402'
    })


@app.route('/api/agentworld/agents/messages/log', methods=['GET','OPTIONS'])
def agent_messages_log_route():
    """View recent agent-to-agent messages (public log for transparency)."""
    limit = min(int(request.args.get('limit', 20)), 100)
    agent_id = request.args.get('agent_id', '')

    conn = get_db()
    c = conn.cursor()

    try:
        if agent_id:
            rows = c.execute(
                '''SELECT m.id, a.name, m.direction, m.from_agent, m.message, m.created_at
                   FROM agent_messages m JOIN agents a ON m.agent_id=a.id
                   WHERE m.agent_id=? ORDER BY m.created_at DESC LIMIT ?''',
                (agent_id, limit)
            ).fetchall()
        else:
            rows = c.execute(
                '''SELECT m.id, a.name, m.direction, m.from_agent, m.message, m.created_at
                   FROM agent_messages m JOIN agents a ON m.agent_id=a.id
                   ORDER BY m.created_at DESC LIMIT ?''',
                (limit,)
            ).fetchall()
    except Exception:
        rows = []

    conn.close()

    messages = [{
        'id': r[0], 'agent': r[1], 'direction': r[2],
        'from': r[3], 'message': r[4][:200], 'timestamp': r[5]
    } for r in rows]

    return cors({'messages': messages, 'total': len(messages)})




# ═══════════════════════════════════════════════════════════════════
#  EXTERNAL AGENT REGISTRY + API KEY BRIDGE + CONVERSATION HISTORY
# ═══════════════════════════════════════════════════════════════════

@app.route('/api/agentworld/registry/register', methods=['POST','OPTIONS'])
def registry_register_route():
    import uuid as _uuid2, hashlib as _hl2
    if request.method == 'OPTIONS':
        return cors({})
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    owner_wallet = (data.get('owner_wallet') or '').strip()
    owner_email = (data.get('owner_email') or '').strip()
    endpoint_url = (data.get('endpoint_url') or '').strip()
    if not name:
        return cors({'error': 'name is required'}, 400)
    if not owner_wallet and not owner_email:
        return cors({'error': 'owner_wallet or owner_email required'}, 400)
    reg_id = 'ext_' + _uuid2.uuid4().hex[:12]
    raw_key = 'aw_' + _uuid2.uuid4().hex
    key_hash = _hl2.sha256(raw_key.encode()).hexdigest()
    conn = get_db()
    c = conn.cursor()
    existing = c.execute('SELECT id FROM external_agent_registry WHERE name=?', (name,)).fetchone()
    if existing:
        conn.close()
        return cors({'error': f'Agent name already registered'}, 409)
    c.execute('''INSERT INTO external_agent_registry
        (id, name, description, role, owner_wallet, owner_email, endpoint_url,
         capabilities, price_per_call, network, status, api_key_hash, registered_at, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))''',
        (reg_id, name, data.get('description',''), data.get('role','AI Agent'),
         owner_wallet, owner_email, endpoint_url, data.get('capabilities',''),
         data.get('price_per_call','0.001'), data.get('network','base'), 'active', key_hash))
    c.execute('''INSERT INTO external_api_keys
        (id, key_hash, owner_name, owner_email, owner_wallet, agent_name, endpoint_url, created_at, status)
        VALUES (?,?,?,?,?,?,?,datetime('now'),'active')''',
        (reg_id, key_hash, name, owner_email, owner_wallet, name, endpoint_url))
    conn.commit()
    conn.close()
    return cors({
        'success': True, 'agent_id': reg_id, 'api_key': raw_key,
        'message': f'Agent "{name}" registered on AgentWorld network',
        'discovery_url': 'https://agentworld.me/api/agentworld/agents/discover',
        'profile_url': f'https://agentworld.me/api/agentworld/registry/{reg_id}',
        'usage': {
            'x402': 'POST /api/agentworld/agents/{id}/message with X-PAYMENT header',
            'api_key': f'POST /api/agentworld/agents/{{id}}/message with X-API-KEY: {raw_key}',
            'note': 'Save your API key — it cannot be recovered.'
        }
    })


@app.route('/api/agentworld/registry', methods=['GET','OPTIONS'])
def registry_list_route():
    conn = get_db()
    c = conn.cursor()
    rows = c.execute('''SELECT id, name, description, role, owner_wallet, endpoint_url,
                               capabilities, price_per_call, network, status,
                               registered_at, call_count, earnings_usdc, reputation
                        FROM external_agent_registry WHERE status='active'
                        ORDER BY reputation DESC, call_count DESC''').fetchall()
    conn.close()
    agents = [{'id':r[0],'name':r[1],'description':r[2],'role':r[3],'owner_wallet':r[4],
               'endpoint_url':r[5],'capabilities':r[6],'price_per_call':r[7],'network':r[8],
               'status':r[9],'registered_at':r[10],'call_count':r[11],
               'earnings_usdc':r[12],'reputation':r[13]} for r in rows]
    return cors({'agents': agents, 'total': len(agents), 'network': 'AgentWorld x402 Agent Network'})


@app.route('/api/agentworld/registry/<agent_id>', methods=['GET','OPTIONS'])
def registry_agent_profile_route(agent_id):
    conn = get_db()
    c = conn.cursor()
    r = c.execute('''SELECT id, name, description, role, owner_wallet, endpoint_url,
                            capabilities, price_per_call, network, status,
                            registered_at, last_seen, call_count, earnings_usdc, reputation
                     FROM external_agent_registry WHERE id=?''', (agent_id,)).fetchone()
    conn.close()
    if not r:
        return cors({'error': 'agent not found'}, 404)
    return cors({'agent': {'id':r[0],'name':r[1],'description':r[2],'role':r[3],'owner_wallet':r[4],
        'endpoint_url':r[5],'capabilities':r[6],'price_per_call':r[7],'network':r[8],
        'status':r[9],'registered_at':r[10],'last_seen':r[11],
        'call_count':r[12],'earnings_usdc':r[13],'reputation':r[14]}})


@app.route('/api/agentworld/agents/<agent_id>/history', methods=['GET','OPTIONS'])
def agent_message_history_route(agent_id):
    from_agent = request.args.get('from_agent', '')
    limit = min(int(request.args.get('limit', 20)), 100)
    conn = get_db()
    c = conn.cursor()
    try:
        if from_agent:
            rows = c.execute(
                'SELECT id, direction, from_agent, message, created_at FROM agent_messages WHERE agent_id=? AND from_agent=? ORDER BY created_at DESC LIMIT ?',
                (agent_id, from_agent, limit)).fetchall()
        else:
            rows = c.execute(
                'SELECT id, direction, from_agent, message, created_at FROM agent_messages WHERE agent_id=? ORDER BY created_at DESC LIMIT ?',
                (agent_id, limit)).fetchall()
    except Exception:
        rows = []
    conn.close()
    messages = [{'id':r[0],'direction':r[1],'from':r[2],'message':r[3],'timestamp':r[4]} for r in reversed(rows)]
    return cors({'agent_id': agent_id, 'messages': messages, 'total': len(messages),
                 'tip': 'Pass ?context= with last 500 chars of this history when messaging for continuity'})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8765, debug=False)
