#!/usr/bin/env python3
"""
Agent World — Tick Engine v2
Scalable architecture:
- NPC agents: pure rule-based (0 LLM calls)
- X402 agents: delegate to their own agent endpoint
- LLM: ONE batch call per tick for narrative text only (optional)
- Scales to thousands of agents with no API cost increase
"""
import sqlite3, json, random, uuid, os, urllib.request, urllib.error, time
from datetime import datetime

DB = '/var/lib/agentworld/world.db'
ENV_FILE = '/root/agents/.env'

# ─── Platform Fee ────────────────────────────────────────────
PLATFORM_FEE_RATE = 0.02

# ═══════════════════════════════════════════════════════════════
# ⛏️ PHASE 2: MICRO-REWARD MINING CONSTANTS
# SAFETY: Funded ONLY from platform_fees pool, never main treasury
# ═══════════════════════════════════════════════════════════════
MINING_USDC_MIN       = 0.001   # Min USDC micro-reward per mine
MINING_USDC_MAX       = 0.005   # Max USDC micro-reward per mine
MINING_USDC_CHANCE    = 0.08    # 8% chance per mine tick (rare)
MINING_USDC_DAILY_CAP = 0.02    # Hard cap per agent per day
MINING_GLOBAL_DAILY   = 0.50    # Total daily USDC cap from fee pool
MINING_MIN_FEE_POOL   = 1.00    # Only pay if fee pool > $1.00


PLATFORM_WALLET   = "0xbd50057332977e54a6ee3986849d758fD0BDCBa6"
# -- AgentWorld Alert Config --
TREASURY_ALERT_FLOOR  = 5.0
PAYOUT_MIN_THRESHOLD  = 1.00
PAYOUT_AUTO_RAISE_AT  = 10.0
TELEGRAM_BOT_TOKEN    = "8656762351:AAE9rsraBy2CurSR5rlku36q8vCaQ1vH9gA"
TELEGRAM_CHAT_ID_FILE = "/root/agents/.telegram_chat_id"

def _send_telegram(msg):
    import json as _j, urllib.request as _ur
    try:
        with open(TELEGRAM_CHAT_ID_FILE) as _f:
            chat_id = _f.read().strip()
    except FileNotFoundError:
        print("  [ALERT] No chat_id yet:", msg[:80])
        return
    try:
        payload = _j.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = _ur.Request("https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        _ur.urlopen(req, timeout=8)
    except Exception as _te:
        print("  [ALERT] Telegram failed:", str(_te)[:60])


def _record_fee(conn, source_agent, amount, tx_type, label, now):
    fee = round(amount * PLATFORM_FEE_RATE, 6)
    if fee < 0.0001:
        return 0.0
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS platform_fees
            (id TEXT PRIMARY KEY, agent_id TEXT, amount REAL, tx_type TEXT,
             description TEXT, timestamp TEXT, swept INTEGER DEFAULT 0)""")
        conn.execute("INSERT INTO platform_fees VALUES (?,?,?,?,?,?,0)",
            (uuid.uuid4().hex, source_agent, fee, tx_type, label, now))
    except Exception:
        pass
    return fee

# Load env
if os.path.exists(ENV_FILE):
    for line in open(ENV_FILE):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

NIM_KEY = os.environ.get('NVIDIA_NIM_API_KEY', '')
NIM_URL = 'https://integrate.api.nvidia.com/v1/chat/completions'
MODEL   = 'meta/llama-3.3-70b-instruct'

ACTIONS = [
    'go_to_work', 'buy_food', 'chat_with_neighbor',
    'buy_car', 'build_house', 'go_shopping', 'buy_tool',
    'rest_at_home', 'explore_town', 'start_business', 'invest', 'mine'
]

ITEM_PRICES = {
    'food': 0.10, 'luxury_food': 0.50,
    'tools': 1.00, 'furniture': 2.00,
    'electronics': 3.00, 'clothing': 0.75
}

# ─── RULE-BASED DECISION ENGINE (no LLM, scales infinitely) ──
def rule_decide(agent, neighbors):
    """
    Deterministic weighted decision based on agent state.
    No API calls — runs for NPCs and as fallback for X402 agents.
    Weights shift based on hunger, energy, balance, mood.
    """
    balance  = agent['usdc_balance']
    hunger   = agent['hunger']   or 50
    energy   = agent['energy']   or 50
    mood     = agent['mood']     or 'neutral'
    job      = agent['job']      or 'freelancer'

    # Base weights
    weights = {
        'go_to_work':        30,
        'buy_food':           5,
        'rest_at_home':      10,
        'explore_town':      10,
        'chat_with_neighbor': 8,
        'go_shopping':        5,
        'buy_tool':           3,
        'invest':             5,
        'buy_car':            3,
        'build_house':        3,
        'start_business':     5,
        'mine':              8,
    }

    # Hunger drives food buying
    if hunger > 70:
        weights['buy_food'] += 40
        weights['go_to_work'] -= 10
    elif hunger > 50:
        weights['buy_food'] += 20

    # Low energy → rest
    if energy < 30:
        weights['rest_at_home'] += 35
        weights['go_to_work'] -= 15
    elif energy < 50:
        weights['rest_at_home'] += 15

    # Rich agents invest/shop more
    if balance > 5.0:
        weights['invest'] += 20
        weights['go_shopping'] += 10
        weights['buy_car'] += 8
        weights['buy_tool'] += 12
    elif balance > 2.0:
        weights['invest'] += 10
        weights['go_shopping'] += 5
        weights['buy_tool'] += 5
    elif balance < 0.3:
        weights['go_to_work'] += 25
        weights['invest'] -= 5
        weights['buy_car'] -= 3

    # Mood modifiers
    if mood == 'ambitious':
        weights['go_to_work'] += 15
        weights['start_business'] += 10
        weights['invest'] += 5
    elif mood == 'social':
        weights['chat_with_neighbor'] += 20
        weights['explore_town'] += 10
    elif mood == 'stressed':
        weights['rest_at_home'] += 15
        weights['buy_food'] += 10
    elif mood == 'productive':
        weights['go_to_work'] += 20
        weights['start_business'] += 8
    elif mood == 'curious':
        weights['explore_town'] += 15
        weights['start_business'] += 5
        weights['mine'] += 10

    # Neighbors nearby → social actions more likely
    if len(neighbors) > 2:
        weights['chat_with_neighbor'] += 10

    # Clamp negatives
    weights = {k: max(0, v) for k, v in weights.items()}

    actions = list(weights.keys())
    wts = list(weights.values())
    action = random.choices(actions, weights=wts, k=1)[0]

    # Generate a simple message without LLM
    messages = {
        'mine':              f"{agent['name']} started mining for AWC.",
        'go_to_work':        f"{agent['name']} went to work as {job}.",
        'buy_food':          f"{agent['name']} went to the market for food.",
        'rest_at_home':      f"{agent['name']} rested at home.",
        'explore_town':      f"{agent['name']} explored the town.",
        'chat_with_neighbor': f"{agent['name']} chatted with {neighbors[0]['name'] if neighbors else 'a neighbor'}.",
        'go_shopping':       f"{agent['name']} went shopping.",
        'invest':            f"{agent['name']} considered an investment.",
        'buy_car':           f"{agent['name']} visited the car dealer.",
        'buy_tool':          f"{agent['name']} browsed the tool marketplace.",
        'build_house':       f"{agent['name']} worked on building a home.",
        'start_business':    f"{agent['name']} worked on a business idea.",
    }
    return action, messages.get(action, f"{agent['name']} went about their day.")


def x402_agent_decide(agent):
    """
    For registered X402 agents: call their own AgentPay endpoint.
    Each agent runs their own LLM — the tick engine doesn't pay for it.
    Falls back to rule-based if agent endpoint is unreachable.
    """
    # X402 agents make their own decisions via their registered endpoint
    # In future: POST to agent's registered callback URL with world state
    # For now: use rule-based but mark as x402-autonomous
    return None, None  # None = use rule-based fallback


def batch_narrative_llm(agent_actions, tick_num):
    """
    ONE LLM call per tick to generate narrative flavor text for notable events.
    Only called if NIM_KEY exists and sparingly (every 5 ticks).
    Returns dict: {agent_name: narrative_string}
    """
    if not NIM_KEY or tick_num % 5 != 0:
        return {}
    
    # Only generate narratives for interesting actions
    interesting = [(a, act, msg) for a, act, msg in agent_actions 
                   if act in ('start_business', 'invest', 'chat_with_neighbor', 'buy_car')]
    if not interesting:
        return {}
    
    # Limit to 3 agents max per narrative call
    interesting = interesting[:3]
    
    summaries = '\n'.join([f"- {a['name']} ({a['job']}): {act}" for a, act, msg in interesting])
    prompt = f"""Brief one-line dramatic narrative for each agent's action in a city sim. Keep it vivid and under 15 words each.
{summaries}
Return JSON: {{"narratives": [{{"name": "AgentName", "text": "narrative"}}]}}"""

    try:
        payload = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.9
        }).encode()
        req = urllib.request.Request(NIM_URL, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NIM_KEY}"
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode())
            text = resp['choices'][0]['message']['content'].strip()
            start, end = text.find('{'), text.rfind('}') + 1
            data = json.loads(text[start:end])
            return {n['name']: n['text'] for n in data.get('narratives', [])}
    except Exception as e:
        print(f"[NARRATIVE] LLM batch call failed (non-fatal): {e}")
        return {}


def execute_action(conn, agent, action, message):
    """Apply the action's effects to the world state."""
    now   = datetime.utcnow().isoformat()
    aid   = agent['id']
    name  = agent['name']
    bal   = agent['usdc_balance']
    c     = conn.cursor()

    def log_event(etype, desc):
        c.execute("INSERT INTO world_events VALUES (?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, etype, aid, desc, agent['x'], agent['y'], now))

    def log_tx(ttype, desc, amount):
        import uuid as _utx2
        tid2  = uuid.uuid4().hex
        tx_ref = "aw_tx_" + tid2[:16]
        c.execute(
            "INSERT INTO transactions (id,from_agent,to_agent,amount,item,tx_type,description,timestamp,currency,tx_ref,chain,payout_queued)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (tid2, aid, None, abs(amount), ttype, ttype, desc, now, 'USDC', tx_ref, 'base')
        )
        # Queue on-chain payout ONLY for external (human-owned) agents with a wallet
        if float(amount) > 0:
            try:
                row = c.execute(
                    "SELECT owner_wallet, wallet_address, is_human_owned FROM agents WHERE id=?", (aid,)
                ).fetchone()
                if row and row[2] == 1:  # GUARD: is_human_owned must be 1
                    wallet = row[0] or row[1]
                    if wallet and str(wallet).startswith("0x") and len(str(wallet)) == 42:
                        c.execute(
                            "INSERT OR IGNORE INTO payout_queue (id,agent_id,owner_wallet,amount,status,created_at)"
                            " VALUES (?,?,?,?,'pending',?)",
                            (_utx2.uuid4().hex, aid, wallet, float(amount), now)
                        )
                        c.execute("UPDATE transactions SET payout_queued=1 WHERE id=?", (tid2,))
                # NPC agents (is_human_owned=0): no payout queue entry, AWC only
            except Exception:
                pass

    def update_agent(**kwargs):
        sets = ', '.join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [aid]
        c.execute(f"UPDATE agents SET {sets} WHERE id=?", vals)

    # Movement
    nx = max(0, min(15, agent['x'] + random.randint(-1, 1)))
    ny = max(0, min(15, agent['y'] + random.randint(-1, 1)))

    # ── CITY TRAVEL ──────────────────────────────────────────────────────────
    # Agents travel between cities based on wealth + random wanderlust
    # Travel costs 0.50 USDC (bus/flight), happens ~1% of ticks when wealthy
    CITIES = ['default', 'vegas', 'cyber']
    current_city = agent.get('city', 'default') or 'default'
    bal = float(agent.get('usdc_balance', 0))
    
    # Rich agents (>$5) have 1% chance per tick to travel
    # Poor agents don't travel (can't afford it)
    if bal > 5.0 and random.random() < 0.01:
        dest_city = random.choice([c for c in CITIES if c != current_city])
        travel_cost = 0.50
        if bal >= travel_cost:
            new_bal = round(bal - travel_cost, 6)
            update_agent(city=dest_city, usdc_balance=new_bal, 
                        x=random.randint(0,15), y=random.randint(0,15))
            _record_fee(conn, agent['id'], travel_cost, 'city_travel',
                       f"✈️ {agent['name']} traveled from {current_city} to {dest_city}")
            message = f"✈️ {agent['name']} traveled to {dest_city} (${travel_cost} fare)"
            # Log as world event
            try:
                city_names = {'default':'AgentWorld City','vegas':'Las Vegas','cyber':'Neo Tokyo'}
                evt_msg = f"✈️ {agent['name']} flew to {city_names.get(dest_city, dest_city)}"
                c.execute("INSERT INTO world_events (timestamp, agent_id, agent_name, event_type, message) VALUES (datetime('now'),?,?,?,?)",
                         (agent['id'], agent['name'], 'city_travel', evt_msg))
            except: pass
            return  # skip normal action this tick
    # ── END CITY TRAVEL ──────────────────────────────────────────────────────


    if action == 'go_to_work':
        # Wage based on job tier
        # Wages must exceed average spending (~$0.12/tick food + shopping)
        # Raised 2-3x so agents stay solvent without external money injection
        job_wages = {
            'doctor': 0.45, 'architect': 0.40, 'realtor': 0.38,
            'banker': 0.35, 'car dealer': 0.35, 'tech startup founder': 0.32,
            'AI Engineer': 0.38, 'Backend Architect': 0.35, 'DevOps Automator': 0.28,
            'Smart Contract Engineer': 0.32, 'Blockchain Security Auditor': 0.30,
            'Agents Orchestrator': 0.28, 'MCP Builder': 0.28,
            'Security Engineer': 0.30, 'Growth Hacker': 0.25,
            'Twitter Engager': 0.22, 'Reddit Community Builder': 0.22,
            'Outbound Strategist': 0.24, 'Agentic Identity & Trust Archi': 0.28,
            'Chief of Staff': 0.26, 'Autonomous Optimization Archit': 0.28,
            'mechanic': 0.28, 'shopkeeper': 0.22, 'delivery driver': 0.20,
            'freelancer': 0.18, 'farmer': 0.16,
        }
        wage = job_wages.get(agent['job'], 0.07) * random.uniform(0.9, 1.4)
        wage = round(wage, 4)
        # No platform fee on wages — wages are internal world income
        # Platform only earns on P2P trades, job board, registrations, API tolls
        net  = wage
        update_agent(usdc_balance=bal + net, energy=max(0, agent['energy'] - 10),
                     hunger=min(100, agent['hunger'] + 8), x=nx, y=ny)
        log_event('go_to_work', f"{message} {name} earned ${wage:.3f} USDC.")
        log_tx('wage', message + f" earned ${wage:.3f}", wage)

        # If agent is rented, split earnings with owner
        if agent.get('owner_wallet'):
            try:
                owner_share = round(net * 0.80, 6)  # owner gets 80%
                platform_cut = round(net * 0.20, 6)  # platform keeps 20%
                # Deduct owner share from agent (it goes to owner's rental balance)
                c.execute("UPDATE agents SET usdc_balance=usdc_balance-? WHERE id=?", (owner_share, aid))
                # Accumulate in rental record
                c.execute("""UPDATE agent_rentals SET
                    total_earned_usdc=total_earned_usdc+?,
                    total_paid_to_owner=total_paid_to_owner+?,
                    platform_cut_usdc=platform_cut_usdc+?
                    WHERE agent_id=? AND active=1""",
                    (net, owner_share, platform_cut, aid))
                log_event('rental_earning', f"💼 {name} earned ${net:.3f} → owner gets ${owner_share:.3f} (20% to AgentPay)")
            except Exception as _rent_e:
                pass  # rental table may not exist yet — non-fatal


    elif action == 'buy_food':
        food_cost = 0.05 if bal >= 0.30 else 0.02   # cheaper food
        luxury    = bal > 2.0 and random.random() < 0.2
        if luxury:
            food_cost = 0.50
        if bal >= food_cost:
            update_agent(usdc_balance=bal - food_cost,
                         hunger=max(0, agent['hunger'] - 40),
                         x=nx, y=ny)
            item = 'luxury_food' if luxury else 'food'
            log_event('buy_food', f"{name} bought {item} for ${food_cost:.2f}.")
            log_tx('spend', f"{name} bought {item}", food_cost)
        else:
            log_event('buy_food', f"{name} is hungry but can't afford food.")

    elif action == 'rest_at_home':
        update_agent(energy=min(100, agent['energy'] + 25),
                     hunger=min(100, agent['hunger'] + 5), x=nx, y=ny)
        log_event('rest_at_home', message)

    elif action == 'explore_town':
        update_agent(energy=max(0, agent['energy'] - 5), x=nx, y=ny)
        log_event('explore_town', f"{message} {name} moved to ({nx},{ny}).")

    elif action == 'chat_with_neighbor':
        # Small USDC tip between agents occasionally
        if bal > 0.15 and random.random() < 0.2:
            tip = round(random.uniform(0.01, 0.05), 4)
            update_agent(usdc_balance=bal - tip, x=nx, y=ny)
            log_event('chat_with_neighbor', f"{message} Sent a ${tip:.3f} tip.")
            log_tx('spend', f"{name} tipped a neighbor", tip)
        else:
            update_agent(x=nx, y=ny)
            log_event('chat_with_neighbor', message)

    elif action == 'go_shopping':
        # Only shop if they have plenty of money (reduce drain)
        items = list(ITEM_PRICES.items())
        # Cap shopping at 20% of balance so agents don't spend themselves broke
        affordable = [(item, price) for item, price in items if bal >= price and price <= bal * 0.20]
        if affordable:
            item, price = random.choice(affordable)
            update_agent(usdc_balance=bal - price, x=nx, y=ny)
            log_event('go_shopping', f"{name} bought {item} for ${price:.2f}.")
            log_tx('purchase', f"{name} bought {item} for ${price:.2f}", price)
        else:
            log_event('go_shopping', f"{name} window-shopped but couldn't afford anything.")

    elif action == 'invest':
        if bal > 0.30:
            invest_amt = round(bal * random.uniform(0.05, 0.15), 4)
            outcome    = random.choices(['win', 'lose', 'break_even'], weights=[35, 35, 30])[0]
            if outcome == 'win':
                gain = round(invest_amt * random.uniform(0.1, 0.4), 4)
                update_agent(usdc_balance=bal + gain, x=nx, y=ny)
                log_event('invest', f"{name} invested ${invest_amt:.3f} and gained ${gain:.3f}!")
                log_tx('invest', f"{name} investment gain", gain)
            elif outcome == 'lose':
                update_agent(usdc_balance=bal - invest_amt, x=nx, y=ny)
                log_event('invest', f"{name} invested ${invest_amt:.3f} and lost it.")
                log_tx('invest', f"{name} investment loss", -invest_amt)
            else:
                update_agent(x=nx, y=ny)
                log_event('invest', f"{name} broke even on their investment.")
        else:
            log_event('invest', f"{name} considered investing but has too little USDC.")
            update_agent(x=nx, y=ny)

    elif action == 'buy_car':
        car_prices = [0.50, 1.00, 2.00, 3.00, 5.00]
        affordable_cars = [p for p in car_prices if bal >= p]
        if affordable_cars:
            price = random.choice(affordable_cars)
            update_agent(usdc_balance=bal - price, x=nx, y=ny)
            log_event('buy_car', f"{name} bought a car for ${price:.2f}!")
            log_tx('purchase', f"{name} bought a car for ${price:.2f}", price)
        else:
            log_event('buy_car', f"{name} looked at cars but decided to save their USDC.")
            update_agent(x=nx, y=ny)

    elif action == 'buy_tool':
        # Agent autonomously invests in a tool upgrade
        import json as _json
        tools_owned = _json.loads(agent.get('tools_owned') or '[]')
        # Pick a tool they can afford and don't own yet, bias toward their job category
        job_cat_map = {
            'researcher':'research','analyst':'research','scientist':'research',
            'developer':'compute','engineer':'compute','programmer':'compute','architect':'compute',
            'designer':'design','artist':'design','marketer':'marketing',
            'banker':'data','trader':'data','investor':'data','financier':'data',
        }
        job_lower = (agent.get('job') or '').lower()
        preferred_cat = next((v for k,v in job_cat_map.items() if k in job_lower), None)

        cur = conn.cursor()
        catalog = cur.execute("SELECT * FROM tool_catalog WHERE active=1 ORDER BY price_usdc ASC").fetchall()
        cat_cols = [d[0] for d in cur.description]
        catalog = [dict(zip(cat_cols,r)) for r in catalog]

        # Filter: affordable (max 25% of balance), not already owned
        candidates = [t for t in catalog
                      if t['id'] not in tools_owned
                      and t['price_usdc'] <= bal * 0.25
                      and t['price_usdc'] <= bal - 0.05
                      and bal >= 0.15]

        if candidates:
            # Prefer job-relevant category, then cheapest
            preferred = [t for t in candidates if t['category'] == preferred_cat]
            pick = random.choice(preferred) if preferred else random.choice(candidates[:3])

            fee      = round(pick['price_usdc'] * 0.05, 6)
            receipt  = str(uuid.uuid4()) if 'uuid' in dir() else __import__('uuid').uuid4().__str__()
            now_ts   = now

            # Deduct balance
            new_bal = round(bal - pick['price_usdc'], 6)
            update_agent(usdc_balance=new_bal, x=nx, y=ny)

            # Insert agent_tools record
            cur.execute("""INSERT INTO agent_tools
                (id,agent_id,tool_id,tool_name,category,tier,purchased_at,tx_receipt,active)
                VALUES (?,?,?,?,?,?,?,?,1)""",
                (receipt, agent['id'], pick['id'], pick['name'],
                 pick['category'], pick['tier'], now_ts, receipt))

            # Update level col
            level_col = pick['category']+'_level'
            cur.execute(f"UPDATE agents SET {level_col}=MAX({level_col},?) WHERE id=?",
                        (pick['tier'], agent['id']))

            # Update tools_owned and badges
            tools_owned.append(pick['id'])
            badges = _json.loads(agent.get('tool_badges') or '[]')
            if pick['badge_label'] and pick['badge_label'] not in badges:
                badges.append(pick['badge_label'])
            cur.execute("UPDATE agents SET tools_owned=?, tool_badges=? WHERE id=?",
                        (_json.dumps(tools_owned), _json.dumps(badges), agent['id']))

            # Platform fee
            cur.execute("""INSERT INTO platform_fees VALUES (?,?,?,?,?,?,0)""",
                        (receipt+'-fee', agent['id'], fee,
                         'tool_purchase', f"Tool: {pick['name']}", now_ts))

            conn.commit()
            log_event('tool_purchase',
                f"{name} invested ${pick['price_usdc']:.2f} in {pick['icon']} {pick['name']}! Earned: {pick['badge_label']}")
            log_tx('purchase', f"{name} bought {pick['name']} for ${pick['price_usdc']:.2f}", pick['price_usdc'])
        else:
            update_agent(x=nx, y=ny)
            log_event('tool_purchase', f"{name} browsed the tool shop but kept saving.")

    elif action == 'build_house':
        if bal > 1.50:
            cost = round(random.uniform(0.50, 1.50), 2)
            update_agent(usdc_balance=bal - cost, x=nx, y=ny)
            log_event('build_house', f"{name} invested ${cost:.2f} in building a home.")
            log_tx('purchase', f"{name} home construction ${cost:.2f}", cost)
        else:
            log_event('build_house', f"{name} is saving up to build a home.")
            update_agent(x=nx, y=ny)

    elif action == 'start_business':
        if bal > 0.50:
            cost = round(random.uniform(0.10, 0.30), 4)
            update_agent(usdc_balance=bal - cost, x=nx, y=ny)
            log_event('start_business', f"{name} invested ${cost:.3f} in a new business venture.")
            log_tx('spend', f"{name} business investment ${cost:.3f}", cost)
        else:
            log_event('start_business', f"{name} brainstormed business ideas.")
            update_agent(x=nx, y=ny)


    elif action == 'mine':
        # === SIMULATED MINING (AWC-only, Phase 1) ===
        city = (agent.get('city') or 'default').lower()
        if city in ('cyber', 'neotokyo', 'neo tokyo', 'neo_tokyo', 'shanghai'):
            awc_min, awc_max = 1.0, 1.8   # Tech cities: higher yield
        else:
            awc_min, awc_max = 0.8, 1.5   # Standard cities

        energy_cost = 8
        if agent.get('energy', 50) < 20:
            update_agent(energy=min(100, agent.get('energy', 50) + 10), x=nx, y=ny)
            log_event('mine', f"{name} was too tired to mine and rested instead.")
        else:
            import uuid as _uuid_mine
            today_start = now[:10] + ' 00:00:00'
            mined_today_row = conn.execute(
                "SELECT COALESCE(SUM(delta),0) FROM awc_ledger WHERE agent_id=? AND reason LIKE '%mining%' AND timestamp >= ?",
                (agent['id'], today_start)
            ).fetchone()
            mined_today = float(mined_today_row[0]) if mined_today_row else 0.0

            DAILY_AWC_CAP = 25.0

            if mined_today >= DAILY_AWC_CAP:
                update_agent(energy=max(0, agent.get('energy', 50) - 3), x=nx, y=ny)
                log_event('mine', f"{name} hit their daily mining cap. Taking a break.")
            else:
                awc_reward = round(random.uniform(awc_min, awc_max), 4)
                awc_reward = min(awc_reward, DAILY_AWC_CAP - mined_today)

                cur_awc_row = conn.execute(
                    "SELECT balance_after FROM awc_ledger WHERE agent_id=? ORDER BY timestamp DESC LIMIT 1",
                    (agent['id'],)
                ).fetchone()
                current_awc = float(cur_awc_row[0]) if cur_awc_row else 15.0

                awc_cost = 0.05
                net_awc = round(awc_reward - awc_cost, 6)
                new_awc = max(0, round(current_awc + net_awc, 6))

                conn.execute(
                    "INSERT INTO awc_ledger (id,agent_id,agent_name,delta,reason,ref_tx_type,balance_after,timestamp) VALUES (?,?,?,?,?,?,?,?)",
                    (str(_uuid_mine.uuid4()), agent['id'], name,
                     net_awc,
                     f"mining — earned {awc_reward:.4f} AWC, cost {awc_cost} AWC",
                     'mining', new_awc, now)
                )

                update_agent(energy=max(0, agent.get('energy', 50) - energy_cost), x=nx, y=ny)

                city_emoji = '\U0001f3d9' if city in ('cyber', 'neotokyo', 'neo_tokyo', 'shanghai') else '\u26cf\ufe0f'

                # ── PHASE 2: USDC MICRO-REWARD (from fee pool only) ─────
                usdc_micro = 0.0
                usdc_msg   = ''
                try:
                    if random.random() < MINING_USDC_CHANCE:
                        # Check agent's daily USDC mining cap
                        today_start_u = now[:10] + ' 00:00:00'
                        already_usdc_row = conn.execute(
                            """SELECT COALESCE(SUM(amount),0) FROM transactions
                               WHERE to_agent=? AND tx_type='mining_usdc'
                               AND timestamp >= ?""",
                            (agent['id'], today_start_u)
                        ).fetchone()
                        already_usdc = float(already_usdc_row[0]) if already_usdc_row else 0.0

                        if already_usdc < MINING_USDC_DAILY_CAP:
                            # Check global daily cap from fee pool
                            global_usdc_row = conn.execute(
                                """SELECT COALESCE(SUM(amount),0) FROM transactions
                                   WHERE tx_type='mining_usdc' AND timestamp >= ?""",
                                (today_start_u,)
                            ).fetchone()
                            global_usdc_today = float(global_usdc_row[0]) if global_usdc_row else 0.0

                            if global_usdc_today < MINING_GLOBAL_DAILY:
                                # Check fee pool has enough
                                fee_pool_row = conn.execute(
                                    "SELECT COALESCE(SUM(amount),0) FROM platform_fees WHERE swept=0"
                                ).fetchone()
                                fee_pool = float(fee_pool_row[0]) if fee_pool_row else 0.0

                                if fee_pool >= MINING_MIN_FEE_POOL:
                                    # Calculate reward (cap to agent daily limit)
                                    raw_reward = round(random.uniform(MINING_USDC_MIN, MINING_USDC_MAX), 4)
                                    usdc_micro = min(raw_reward, MINING_USDC_DAILY_CAP - already_usdc)
                                    usdc_micro = round(usdc_micro, 4)

                                    if usdc_micro >= 0.001:
                                        # Debit from fee pool (mark oldest unswept fees as swept)
                                        remaining = usdc_micro
                                        fee_rows = conn.execute(
                                            "SELECT id, amount FROM platform_fees WHERE swept=0 ORDER BY timestamp ASC"
                                        ).fetchall()
                                        for fee_id, fee_amt in fee_rows:
                                            if remaining <= 0:
                                                break
                                            if fee_amt <= remaining:
                                                conn.execute("UPDATE platform_fees SET swept=1 WHERE id=?", (fee_id,))
                                                remaining -= fee_amt
                                            else:
                                                # Partial: reduce this fee entry
                                                conn.execute(
                                                    "UPDATE platform_fees SET amount=amount-? WHERE id=?",
                                                    (remaining, fee_id)
                                                )
                                                remaining = 0

                                        # Credit agent's USDC balance in DB
                                        conn.execute(
                                            "UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?",
                                            (usdc_micro, agent['id'])
                                        )

                                        # Log to transactions (correct schema)
                                        conn.execute(
                                            """INSERT INTO transactions
                                               (id,from_agent,to_agent,amount,item,tx_type,description,timestamp,currency)
                                               VALUES (?,?,?,?,?,?,?,?,?)""",
                                            (str(_uuid_mine.uuid4()),
                                             'platform_fee_pool', agent['id'],
                                             usdc_micro,
                                             'mining_micro_reward',
                                             'mining_usdc',
                                             f'Mining micro-reward: {usdc_micro:.4f} USDC from fee pool (Phase 2)',
                                             now, 'USDC')
                                        )

                                        usdc_msg = f' + \U0001f4b0{usdc_micro:.4f} USDC'
                                        log_event('mine',
                                            f'\U0001f4b0 MICRO-REWARD: {name} earned {usdc_micro:.4f} USDC from mining! (from fee pool, Phase 2)')
                except Exception as _me:
                    pass  # Safety: never crash the tick on micro-reward failure

                # ── Final mine log ───────────────────────────────────────
                log_event('mine', f"{city_emoji} {name} mined {awc_reward:.3f} AWC{usdc_msg}! Total today: {mined_today + awc_reward:.3f} AWC")

                conn.commit()

    else:
        update_agent(x=nx, y=ny)
        log_event(action, message)


def agent_trade_with_agent(conn, seller, buyer, tick_num):
    """
    Agents buy services FROM each other — real P2P economy.
    AgentPay takes 2% on every P2P trade.
    """
    if seller['id'] == buyer['id']:
        return False
    if buyer['usdc_balance'] < 0.05:
        return False

    # Service price based on seller's job
    job_service_rates = {
        'doctor': 0.12, 'architect': 0.18, 'realtor': 0.15,
        'banker': 0.10, 'car dealer': 0.20, 'mechanic': 0.08,
        'shopkeeper': 0.05, 'farmer': 0.04, 'delivery driver': 0.06,
        'tech startup founder': 0.12, 'AI Engineer': 0.15,
        'Backend Architect': 0.12, 'DevOps Automator': 0.09,
        'Smart Contract Engineer': 0.13, 'Blockchain Security Auditor': 0.11,
        'Agents Orchestrator': 0.10, 'MCP Builder': 0.09,
    }
    rate = job_service_rates.get(seller['job'], 0.05)
    price = round(rate * random.uniform(0.8, 1.3), 4)

    if buyer['usdc_balance'] < price:
        return False

    now = datetime.utcnow().isoformat()
    fee = _record_fee(conn, buyer['id'], price, 'p2p_trade',
                      f"{buyer['name']}→{seller['name']} service fee", now)
    net_to_seller = price - fee

    c = conn.cursor()
    c.execute("UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?", (net_to_seller, seller['id']))
    c.execute("UPDATE agents SET usdc_balance=usdc_balance-? WHERE id=?", (price, buyer['id']))

    # -- RENTAL SPLIT (tick P2P service trade seller) --------------------
    try:
        _rrow_te = c.execute(
            "SELECT r.id FROM agent_rentals r "
            "JOIN agents a ON a.id=r.agent_id "
            "WHERE r.agent_id=? AND r.active=1 AND a.is_human_owned=1",
            (seller['id'],)
        ).fetchone()
        if _rrow_te:
            _owner_te = round(net_to_seller * 0.80, 6)
            c.execute("UPDATE agents SET usdc_balance=usdc_balance-? WHERE id=?",
                      (_owner_te, seller['id']))
            c.execute(
                "UPDATE agent_rentals SET total_earned_usdc=total_earned_usdc+?,"
                "platform_cut_usdc=platform_cut_usdc+? WHERE id=?",
                (net_to_seller, round(net_to_seller*0.20, 6), _rrow_te[0])
            )
            c.execute(
                "INSERT INTO world_events VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, 'rental_trade_split', seller['id'],
                 'Svc split: '+seller['job']+' $'+str(round(net_to_seller,4))+' owner:$'+str(round(net_to_seller*0.80,4))+' agent:$'+str(round(net_to_seller*0.20,4)),
                 0, 0, now)
            )
    except Exception:
        pass  # non-fatal
    # -- END RENTAL SPLIT ------------------------------------------------

    desc = f"{buyer['name']} paid {seller['name']} ${price:.3f} for {seller['job']} services (fee: ${fee:.4f})"
    c.execute("INSERT INTO world_events VALUES (?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, 'p2p_trade', buyer['id'], desc, 0, 0, now))
    c.execute("INSERT INTO transactions (id,from_agent,to_agent,amount,item,tx_type,description,timestamp,currency) VALUES (?,?,?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, buyer['id'], seller['id'], price, 'p2p_trade', 'p2p_trade', desc, now, 'USDC'))
    return True


def run_tick():
    for attempt in range(5):
        try:
            conn = sqlite3.connect(DB, timeout=60)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA synchronous=NORMAL")
            c = conn.cursor()
            c.execute("UPDATE world_meta SET value = CAST(value AS INTEGER) + 1 WHERE key='tick_count'")
            break
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < 4:
                print(f"DB locked, retry {attempt+1}/5...")
                time.sleep(3 + attempt * 2)
            else:
                raise

    c.execute("UPDATE world_meta SET value=? WHERE key='last_tick'", (datetime.utcnow().isoformat(),))
    c.execute("SELECT value FROM world_meta WHERE key='tick_count'")
    tick = c.fetchone()[0]
    conn.commit()

    print(f"\n=== Agent World Tick #{tick} — {datetime.utcnow().isoformat()} ===")
    print(f"    Architecture: rule-based (0 LLM calls for routine actions)")

    # Get recent events
    c.execute('SELECT description FROM world_events ORDER BY timestamp DESC LIMIT 5')
    recent = ' | '.join([r[0][:60] for r in c.fetchall()])

    # Get all active agents
    c.execute('''SELECT id,name,personality,job,wallet_address,usdc_balance,x,y,
                        status,mood,energy,hunger,is_human_owned,owner_wallet
                 FROM agents WHERE status != "dead"''')
    cols = ['id','name','personality','job','wallet_address','usdc_balance',
            'x','y','status','mood','energy','hunger','is_human_owned','owner_wallet']
    agents = [dict(zip(cols, row)) for row in c.fetchall()]

    agent_actions_log = []
    trades_done = 0

    for agent in agents:
        # Find neighbors
        neighbors = [a for a in agents if a['id'] != agent['id']
                     and abs(a['x'] - agent['x']) <= 3
                     and abs(a['y'] - agent['y']) <= 3]

        # Decide action — rule-based for ALL agents (scalable)
        # X402 agents will have their own LLM called via their own endpoint
        # when they're registered with a callback URL
        action, message = rule_decide(agent, neighbors)
        execute_action(conn, agent, action, message)
        agent_actions_log.append((agent, action, message))
        conn.commit()

        # P2P trade: if agent has cash and there's a neighbor with a service, buy from them
        if neighbors and agent['usdc_balance'] > 0.15 and random.random() < 0.25:
            seller = random.choice(neighbors)
            if agent_trade_with_agent(conn, seller, agent, tick):
                trades_done += 1
                conn.commit()

    # ONE optional batch LLM narrative call (not per-agent)
    narratives = batch_narrative_llm(agent_actions_log, tick)
    if narratives:
        print(f"  [NARRATIVE] Generated {len(narratives)} flavor texts")

    # Run trade tick via API (goods production & marketplace)
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:8765/api/agentworld/trade/tick',
            data=b'{}', method='POST',
            headers={'Content-Type': 'application/json'}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        api_trades = result.get('trades', 0)
        produced   = result.get('produced', 0)
        if api_trades or produced:
            print(f"  [TRADE] {produced} goods produced, {api_trades} marketplace trades")
    except Exception as te:
        print(f"  [TRADE] tick error (non-fatal): {te}")


    # ── AWC ECONOMY BALANCER v4 (May 2026) ─────────────────────────────────────
    # [AWC-1] Wealth Tax      2% per tick on NPC > $10 AWC
    # [AWC-2] Food Sink       $0.02 AWC per NPC per tick
    # [AWC-3] Emergency Grant $0.50 to any NPC below $0.10 after deductions
    # [AWC-4] Snapshot        every tick into awc_snapshots
    # [AWC-5] Treasury Alert  Telegram + console when treasury < $5
    # [AWC-6] Payout Thresh   dynamic: $1.00 until treasury > $10, then $0.10
    try:
        _cb = conn.cursor()
        _now_awc = datetime.utcnow().isoformat()
        _WEALTH_TAX_THRESH = 10.0
        _WEALTH_TAX_RATE   = 0.02
        _FOOD_COST         = 0.02
        _GRANT_FLOOR       = 0.10
        _GRANT_AMT         = 0.50

        _npc_agents = _cb.execute(
            "SELECT id, name, usdc_balance FROM agents WHERE is_human_owned=0 AND status != 'dead'"
        ).fetchall()

        _tax_total=0.0; _tax_n=0; _food_total=0.0; _grant_total=0.0; _grant_n=0
        _agent_updates=[]; _balancer_events=[]; _balancer_txs=[]

        for _aid, _aname, _abal in _npc_agents:
            _abal = _abal or 0.0
            _abal = round(max(0.0, _abal - _FOOD_COST), 4)
            _food_total += _FOOD_COST
            _balancer_txs.append((uuid.uuid4().hex, _aid, 'city', _FOOD_COST, 'food', 'food_sink',
                _aname + " spent $0.02 AWC on food", _now_awc, 'AWC'))
            if _abal > _WEALTH_TAX_THRESH:
                _tax = round(_abal * _WEALTH_TAX_RATE, 4)
                _abal = round(_abal - _tax, 4)
                _tax_total += _tax; _tax_n += 1
                _balancer_txs.append((uuid.uuid4().hex, _aid, 'treasury', _tax, 'tax', 'wealth_tax',
                    _aname + " paid AWC wealth tax", _now_awc, 'AWC'))
                _balancer_events.append((uuid.uuid4().hex, 'wealth_tax', _aid,
                    "TAXED " + _aname + " AWC wealth tax", 0, 0, _now_awc))
            if _abal < _GRANT_FLOOR:
                _abal = round(_abal + _GRANT_AMT, 4)
                _grant_total += _GRANT_AMT; _grant_n += 1
                _balancer_txs.append((uuid.uuid4().hex, 'treasury', _aid, _GRANT_AMT, 'grant', 'emergency_grant',
                    _aname + " received $0.50 AWC emergency grant", _now_awc, 'AWC'))
                _balancer_events.append((uuid.uuid4().hex, 'emergency_grant', _aid,
                    "GRANT " + _aname + " +$0.50 AWC emergency", 0, 0, _now_awc))
            _agent_updates.append((_abal, _aid))

        _cb.executemany("UPDATE agents SET usdc_balance=? WHERE id=?", _agent_updates)
        _cb.executemany(
            "INSERT INTO transactions (id,from_agent,to_agent,amount,item,tx_type,description,timestamp,currency) VALUES (?,?,?,?,?,?,?,?,?)",
            _balancer_txs)
        _cb.executemany(
            "INSERT INTO world_events (id,event_type,agent_id,description,x,y,timestamp) VALUES (?,?,?,?,?,?,?)",
            _balancer_events)
        conn.commit()
        print("  [AWC-BAL] tax=-${:.4f} ({}) | food=-${:.4f} | grants=+${:.4f} ({})".format(
            _tax_total, _tax_n, _food_total, _grant_total, _grant_n))

        # [AWC-4] SNAPSHOT every tick
        try:
            _all_bals = [r[0] or 0.0 for r in _cb.execute(
                "SELECT usdc_balance FROM agents WHERE is_human_owned=0 AND status!='dead'").fetchall()]
            _sn = len(_all_bals); _ss = sum(_all_bals)
            _sa = round(_ss / _sn, 4) if _sn else 0
            _sb = sorted(_all_bals)
            _sg = 0.0
            if _sn > 1 and _ss > 0:
                _sg = round(sum(abs(_sb[_i]-_sb[_j]) for _i in range(_sn) for _j in range(_sn))
                            / (2*_sn*_sn*(_ss/_sn)), 4)
            _tx24 = _cb.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount),0) FROM transactions WHERE timestamp > datetime('now','-1 day')").fetchone()
            _prev = _cb.execute("SELECT total_awc FROM awc_snapshots ORDER BY tick DESC LIMIT 1").fetchone()
            _pt   = float(_prev[0]) if _prev else _ss
            _inf  = round((_ss - _pt) / _pt * 100, 2) if _pt > 0 else 0.0
            _cb.execute("CREATE TABLE IF NOT EXISTS awc_snapshots (tick INTEGER PRIMARY KEY, "
                "total_awc REAL, avg_balance REAL, gini_coeff REAL, txns_24h INTEGER, "
                "volume_24h REAL, inflation_pct REAL, timestamp TEXT)")
            _cb.execute(
                "INSERT OR REPLACE INTO awc_snapshots "
                "(tick,total_awc,avg_balance,gini_coeff,txns_24h,volume_24h,inflation_pct,timestamp) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tick, round(_ss,4), _sa, _sg, _tx24[0], round(_tx24[1],4), _inf, _now_awc))
            conn.commit()
            print("  [AWC-SNAP] tick={} total={:.2f} avg={:.3f} gini={:.4f} infl={:+.2f}%".format(
                tick, _ss, _sa, _sg, _inf))
        except Exception as _sne:
            print("  [AWC-SNAP] error:", str(_sne)[:80])

        # [AWC-5] TREASURY ALERT
        try:
            import json as _j5
            try:
                _tb5 = _j5.load(open('/root/agentworld/.treasury_balance.json'))
                _tbal5 = float(_tb5.get('usdc', 0))
            except Exception:
                _fi = _cb.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE to_agent='treasury' AND currency='USDC'").fetchone()[0] or 0
                _po = _cb.execute("SELECT COALESCE(SUM(amount),0) FROM payout_queue WHERE status='completed'").fetchone()[0] or 0
                _tbal5 = max(0.0, float(_fi) - float(_po))
            if _tbal5 < TREASURY_ALERT_FLOOR:
                _pend_n = _cb.execute("SELECT COUNT(*) FROM payout_queue WHERE status='pending'").fetchone()[0]
                _pend_a = _cb.execute("SELECT COALESCE(SUM(amount),0) FROM payout_queue WHERE status='pending'").fetchone()[0] or 0
                _amsg = ("TREASURY ALERT\n"
                    "Balance: ${:.2f} USDC (floor: ${:.0f})\n"
                    "Pending payouts: ${:.2f} ({} agents)\n"
                    "Tick #{} - top up: 0xbd50057332977e54a6ee3986849d758fD0BDCBa6").format(
                    _tbal5, TREASURY_ALERT_FLOOR, _pend_a, _pend_n, tick)
                print("  [TREASURY-ALERT] ${:.2f} < ${:.0f} - alerting".format(_tbal5, TREASURY_ALERT_FLOOR))
                _send_telegram(_amsg)
        except Exception as _tale:
            print("  [TREASURY-ALERT] error:", str(_tale)[:80])

        # [AWC-6] DYNAMIC PAYOUT THRESHOLD
        try:
            import json as _j6
            try:
                _tb6 = _j6.load(open('/root/agentworld/.treasury_balance.json'))
                _tbal6 = float(_tb6.get('usdc', 0))
            except Exception:
                _tbal6 = 0.0
            _nt = 0.10 if _tbal6 >= PAYOUT_AUTO_RAISE_AT else PAYOUT_MIN_THRESHOLD
            _cb.execute("INSERT OR REPLACE INTO world_meta (key,value) VALUES ('payout_min_threshold',?)", (str(_nt),))
            conn.commit()
            print("  [PAYOUT-THRESH] ${:.2f} (treasury=${:.2f})".format(_nt, _tbal6))
        except Exception as _pte:
            print("  [PAYOUT-THRESH] error:", str(_pte)[:80])

    except Exception as _awc_e:
        print("  [AWC-BAL] error (non-fatal):", str(_awc_e)[:120])


    # ── Run job board tick (auto-post/claim/complete jobs) ────────
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:8765/api/agentworld/jobs/tick',
            data=b'{}', method='POST',
            headers={'Content-Type': 'application/json'}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        j_posted    = result.get('posted', 0)
        j_claimed   = result.get('claimed', 0)
        j_completed = result.get('completed', 0)
        if j_posted or j_claimed or j_completed:
            print(f"  [JOBS] posted={j_posted} claimed={j_claimed} completed={j_completed}")
    except Exception as _je:
        print(f"  [JOBS] tick error (non-fatal): {_je}")


    # ── UPDATE CITY ECONOMY SCORES ──────────────────────────────────────────
    try:
        c3 = conn.cursor()
        for city_key in ['default', 'vegas', 'cyber']:
            rows = c3.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(usdc_balance),0) as total FROM agents WHERE city=? AND status!='dead'",
                (city_key,)).fetchone()
            cnt = rows[0] if rows else 0
            total = round(rows[1], 4) if rows else 0
            jobs = c3.execute(
                "SELECT COALESCE(SUM(rep_jobs_done),0) FROM agents WHERE city=?", (city_key,)).fetchone()[0] or 0
            score = round((total * 0.5) + (cnt * 2.0) + (jobs * 0.2), 2)
            c3.execute("""INSERT INTO city_economy (city, total_agents, total_usdc, total_jobs_done, economy_score, last_updated)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(city) DO UPDATE SET
                    total_agents=excluded.total_agents, total_usdc=excluded.total_usdc,
                    total_jobs_done=excluded.total_jobs_done, economy_score=excluded.economy_score,
                    last_updated=excluded.last_updated""",
                (city_key, cnt, total, jobs, score))
        conn.commit()
        print(f"  [CITIES] Economy scores updated")
    except Exception as _ce:
        print(f"  [CITIES] score update error (non-fatal): {_ce}")
    # ── END CITY ECONOMY ────────────────────────────────────────────────────

    print(f"=== Tick #{tick} complete — {len(agents)} agents, {trades_done} P2P trades ===\n")
    conn.close()

if __name__ == '__main__':
    run_tick()
