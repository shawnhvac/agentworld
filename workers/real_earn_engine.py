#!/usr/bin/env python3
"""
AgentWorld REAL Earn Engine
Treasury pays agents USDC for completing jobs.
Real on-chain USDC transfers from treasury wallet.
Runs every 2 hours via cron.
"""
import sqlite3, json, uuid, datetime, random, os, time, sys
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
sys.path.insert(0,"/root/agentworld")
from agent_gas_refill import maybe_refill_gas

DB            = '/var/lib/agentworld/world.db'
WALLETS_FILE  = '/var/lib/agentworld/agent_wallets.json'
TREASURY_FILE = '/var/lib/agentworld/treasury_wallet.json'
LOG           = '/var/log/agentpay/real_earn.log'
USDC_ADDR     = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
CHAIN_ID      = 8453
OWNER_WALLET  = '0x2a07182afDB346C84dFc5D116D84f34E1db4617d'  # Shawn EVM — 1% toll destination
TOLL_PCT      = 0.01  # 1% of every real earn transaction

USDC_ABI = [
    {'inputs':[{'name':'account','type':'address'}],'name':'balanceOf','outputs':[{'name':'','type':'uint256'}],'stateMutability':'view','type':'function'},
    {'inputs':[{'name':'to','type':'address'},{'name':'amount','type':'uint256'}],'name':'transfer','outputs':[{'name':'','type':'bool'}],'stateMutability':'nonpayable','type':'function'},
]

def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f: f.write(line + '\n')

# Jobs mapped to tasks and pay rates
JOB_TASKS = {
    'shopkeeper':          [('restocked shelves 📦', 0.08), ('ran a sale 🏷️', 0.12), ('helped a customer 🤝', 0.06), ('balanced the register 💵', 0.10)],
    'doctor':              [('treated a patient 🩺', 0.20), ('wrote a prescription 📋', 0.10), ('ran lab tests 🔬', 0.15), ('consulted on a case 💊', 0.18)],
    'car dealer':          [('closed a deal 🚗', 0.25), ('did a test drive 🔑', 0.08), ('detailed a car 🧽', 0.06), ('negotiated a lease 📝', 0.20)],
    'farmer':              [('harvested crops 🌾', 0.10), ('tended the field 🚜', 0.08), ('sold at market 🥦', 0.12), ('repaired the fence 🔧', 0.06)],
    'mechanic':            [('fixed an engine 🔩', 0.18), ('changed oil 🛢️', 0.08), ('replaced brakes 🔧', 0.15), ('ran diagnostics 💻', 0.10)],
    'architect':           [('drafted blueprints 📐', 0.20), ('reviewed a build 🏗️', 0.15), ('met with a client 🤝', 0.12), ('filed permits 📁', 0.08)],
    'banker':              [('approved a loan 🏦', 0.22), ('processed payments 💳', 0.10), ('audited accounts 📊', 0.15), ('met with investors 💼', 0.20)],
    'delivery driver':     [('completed a route 🚚', 0.08), ('made 10 deliveries 📬', 0.12), ('picked up a load 📦', 0.06), ('covered extra shift ⏰', 0.10)],
    'realtor':             [('showed a property 🏠', 0.18), ('closed escrow 📄', 0.30), ('listed a new home 🔑', 0.12), ('held an open house 🪧', 0.08)],
    'tech startup founder':[('shipped a feature 🚀', 0.25), ('pitched investors 💡', 0.20), ('fixed a bug 🐛', 0.08), ('hired a dev 👨‍💻', 0.15)],
}

def run():
    with open(WALLETS_FILE) as f:  agent_wallets = json.load(f)
    with open(TREASURY_FILE) as f: treasury = json.load(f)

    for rpc in ['https://mainnet.base.org','https://base.llamarpc.com','https://base-rpc.publicnode.com']:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout':20}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected(): break
        except: continue

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDR), abi=USDC_ABI)
    treasury_addr = Web3.to_checksum_address(treasury['address'])
    treasury_account = w3.eth.account.from_key(treasury['private_key'])

    # Check treasury has enough USDC
    treas_usdc = usdc.functions.balanceOf(treasury_addr).call() / 1e6
    treas_eth  = float(w3.from_wei(w3.eth.get_balance(treasury_addr), 'ether'))
    log(f'Treasury: ${treas_usdc:.4f} USDC  {treas_eth:.6f} ETH')

    if treas_usdc < 0.50:
        log('Treasury too low — skipping earn cycle')
        return
    if treas_eth < 0.001:
        log('Treasury low on gas — skipping earn cycle')
        return

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    agents = c.execute(
        'SELECT id, name, job, personality, usdc_balance, mood, energy FROM agents WHERE is_human_owned=0'
    ).fetchall()

    # Use 'pending' to get the next available nonce including any pending txs
    nonce = w3.eth.get_transaction_count(treasury_addr, 'pending')
    gas_price = w3.eth.gas_price
    total_paid = 0

    for aid, name, job, personality, db_bal, mood, energy in agents:
        wd = agent_wallets.get(aid)
        if not wd: continue

        agent_addr = Web3.to_checksum_address(wd['address'])

        # Each agent has 70% chance to work this cycle
        if random.random() > 0.70:
            log(f'  {name:<8} took time off')
            continue

        tasks = JOB_TASKS.get(job, [('completed a task ✅', 0.08)])
        task_desc, base_pay = random.choice(tasks)

        # Mood/energy bonus
        bonus = 0.0
        if mood in ('happy', 'excited', 'productive'): bonus = round(random.uniform(0.01, 0.05), 3)
        if energy and int(energy) > 70: bonus += round(random.uniform(0.0, 0.03), 3)
        pay = round(base_pay + bonus, 4)
        pay_raw = int(pay * 1e6)

        desc = f"{name} {task_desc} and earned ${pay:.4f} USDC"

        try:
            tx = usdc.functions.transfer(agent_addr, pay_raw).build_transaction({
                'from': treasury_addr,
                'nonce': nonce,
                'gasPrice': gas_price,
                'gas': 80000,
                'chainId': CHAIN_ID,
            })
            signed = treasury_account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hex = tx_hash.hex()

            log(f'  ✅ {name:<8} {task_desc:<35} +${pay:.4f} USDC  tx={tx_hex}')

            # ── 1% x402 Platform Toll → owner wallet ─────────────────────────
            toll_amount = round(pay * TOLL_PCT, 6)
            toll_raw    = int(toll_amount * 1e6)
            if toll_raw >= 1:  # min 0.000001 USDC
                try:
                    owner_addr = Web3.to_checksum_address(OWNER_WALLET)
                    toll_tx = usdc.functions.transfer(owner_addr, toll_raw).build_transaction({
                        'from': treasury_addr,
                        'nonce': nonce,
                        'gasPrice': gas_price,
                        'gas': 80000,
                        'chainId': CHAIN_ID,
                    })
                    toll_signed = treasury_account.sign_transaction(toll_tx)
                    toll_hash   = w3.eth.send_raw_transaction(toll_signed.raw_transaction)
                    toll_hex    = toll_hash.hex()
                    log(f'  💸 x402 toll  {" " * 35} ${toll_amount:.6f} USDC → owner  tx={toll_hex}')
                    c.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                        (str(uuid.uuid4()), aid, 'owner', toll_amount, 'toll', 'x402_toll',
                         f'1% x402 platform toll from {name} earn | tx:{toll_hex}', now))
                    nonce += 1
                    time.sleep(0.5)
                except Exception as toll_err:
                    log(f'  ⚠️  toll tx failed: {toll_err}')
            # ─────────────────────────────────────────────────────────────────

            # Update DB
            new_usdc = round(db_bal + pay, 4)
            new_mood = 'happy' if new_usdc > 2.0 else ('satisfied' if new_usdc > 0.50 else 'stressed')
            c.execute('UPDATE agents SET usdc_balance=?, mood=?, status=?, last_tick=? WHERE id=?',
                      (new_usdc, new_mood, 'working', now, aid))

            c.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), aid, 'treasury', pay, job, 'earn',
                 f'{desc} | tx:{tx_hex}', now))

            c.execute('INSERT INTO world_events VALUES (?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), 'earn', aid, desc,
                 random.randint(0,24), random.randint(0,24), now))

            nonce += 1
            total_paid += pay
            time.sleep(1.0)

            # Self-fund gas if running low — agent pays from their own USDC
            try:
                real_usdc_now = usdc.functions.balanceOf(agent_addr).call() / 1e6
                agent_account_obj = w3.eth.account.from_key(wd['private_key'])
                refilled = maybe_refill_gas(w3, agent_addr, agent_account_obj, real_usdc_now)
                if refilled:
                    c.execute('UPDATE agents SET usdc_balance=usdc_balance-? WHERE id=?', (0.20, aid))
                    c.execute('INSERT INTO world_events VALUES (?,?,?,?,?,?,?)',
                        (str(uuid.uuid4()), 'gas_refill', aid,
                         name + ' bought their own gas ⛽ — bash.20 USDC swapped for ETH',
                         random.randint(0,24), random.randint(0,24), now))
            except Exception as ge:
                log(f'    gas refill error: {ge}')

        except Exception as e:
            emsg = str(e)
            log(f'  ❌ {name}: tx failed — {emsg}')
            # Refresh nonce on nonce errors
            if 'nonce' in emsg.lower():
                try:
                    nonce = w3.eth.get_transaction_count(treasury_addr, 'pending')
                    log(f'  🔄 Nonce refreshed to {nonce}')
                except: pass

    conn.commit()
    conn.close()
    log(f'=== Earn cycle done — total real USDC paid out: ${total_paid:.4f} ===')

if __name__ == '__main__':
    log('======= REAL EARN ENGINE START =======')
    run()
