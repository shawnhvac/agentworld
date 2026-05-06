#!/usr/bin/env python3
"""
AgentWorld REAL Spend Engine
Each agent signs a real on-chain USDC transfer from their own wallet.
They pay their own gas — so they learn spending costs money.
Runs once per hour via cron.
"""
import sqlite3, json, uuid, datetime, random, os, time
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

DB            = '/var/lib/agentworld/world.db'
WALLETS_FILE  = '/var/lib/agentworld/agent_wallets.json'
TREASURY_FILE = '/var/lib/agentworld/treasury_wallet.json'
LOG           = '/var/log/agentpay/real_spend.log'
USDC_ADDR     = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
CHAIN_ID      = 8453  # Base mainnet

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

# What agents can spend USDC on — each maps to a real recipient
# recipient = 'treasury' means money goes back to treasury (fees, rent, etc)
# recipient = 'agent:<id>' means agent-to-agent transfer
SPEND_MENU = {
    'coffee':          {'cost': 0.05,  'desc': 'bought a coffee ☕',             'recipient': 'treasury'},
    'street_taco':     {'cost': 0.08,  'desc': 'grabbed a street taco 🌮',       'recipient': 'treasury'},
    'energy_drink':    {'cost': 0.06,  'desc': 'chugged an energy drink ⚡',      'recipient': 'treasury'},
    'arcade_tokens':   {'cost': 0.15,  'desc': 'blew it at the arcade 🎮',        'recipient': 'treasury'},
    'movie_ticket':    {'cost': 0.25,  'desc': 'went to a movie 🎬',              'recipient': 'treasury'},
    'round_of_drinks': {'cost': 0.20,  'desc': 'bought a round at the bar 🍺',   'recipient': 'treasury'},
    'lotto_ticket':    {'cost': 0.05,  'desc': 'bought a lotto ticket 🎟️',       'recipient': 'treasury', 'gamble': (0.0, 5.0)},
    'stock_bet':       {'cost': 0.25,  'desc': 'bet on a stock 📈',               'recipient': 'treasury', 'gamble': (0.0, 1.0)},
    'crypto_yolo':     {'cost': 0.50,  'desc': 'YOLOed into crypto 🎲',           'recipient': 'treasury', 'gamble': (0.0, 2.0)},
    'gift_for_friend': {'cost': 0.10,  'desc': 'bought a gift for a friend 🎁',  'recipient': 'random_agent'},
    'fancy_dinner':    {'cost': 0.40,  'desc': 'treated themselves to dinner 🍽️','recipient': 'treasury'},
    'new_clothes':     {'cost': 0.30,  'desc': 'bought new clothes 👕',           'recipient': 'treasury'},
    'rent':            {'cost': 0.50,  'desc': 'paid rent 🏠',                    'recipient': 'treasury'},
    'charity':         {'cost': 0.10,  'desc': 'donated to charity ❤️',           'recipient': 'treasury'},
}

PERSONALITY_WEIGHTS = {
    'curious and entrepreneurial':   {'stock_bet':3,'crypto_yolo':2,'coffee':2,'arcade_tokens':2},
    'warm and nurturing':            {'fancy_dinner':2,'gift_for_friend':3,'charity':3,'coffee':2},
    'bold and competitive':          {'crypto_yolo':3,'stock_bet':3,'round_of_drinks':2,'lotto_ticket':2},
    'creative and artistic':         {'movie_ticket':3,'arcade_tokens':2,'new_clothes':2,'coffee':2},
    'analytical and precise':        {'stock_bet':3,'rent':2,'charity':2,'coffee':2},
    'adventurous and free-spirited': {'lotto_ticket':3,'crypto_yolo':2,'energy_drink':2,'movie_ticket':2},
    'gruff but loyal':               {'round_of_drinks':3,'street_taco':3,'rent':2,'gift_for_friend':2},
    'charismatic and social':        {'gift_for_friend':3,'round_of_drinks':3,'fancy_dinner':2,'new_clothes':2},
    'methodical and quiet':          {'rent':3,'charity':2,'coffee':2,'stock_bet':2},
    'witty and ambitious':           {'crypto_yolo':2,'stock_bet':3,'fancy_dinner':2,'arcade_tokens':2},
}

def pick_spend(personality, usdc_balance, mood):
    options = {}
    for item, data in SPEND_MENU.items():
        if usdc_balance < data['cost'] + 0.10:  # need $0.10 buffer min
            continue
        w = 1.0
        pw = PERSONALITY_WEIGHTS.get(personality or '', {})
        w *= pw.get(item, 1)
        if mood == 'social'     and item in ('gift_for_friend','round_of_drinks','fancy_dinner'): w *= 2
        if mood == 'excited'    and item in ('crypto_yolo','stock_bet','lotto_ticket','arcade_tokens'): w *= 2
        if mood == 'productive' and item in ('stock_bet','crypto_yolo','rent'): w *= 2
        if usdc_balance > 1.0   and data['cost'] > 0.20: w *= 1.5
        options[item] = max(1, w)
    if not options:
        return None, None
    chosen = random.choices(list(options.keys()), weights=list(options.values()), k=1)[0]
    return chosen, SPEND_MENU[chosen]

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

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    agents = c.execute(
        'SELECT id, name, job, personality, usdc_balance, mood, energy, hunger FROM agents WHERE is_human_owned=0'
    ).fetchall()

    all_agent_ids = [a[0] for a in agents]
    total_spent = 0

    for aid, name, job, personality, db_bal, mood, energy, hunger in agents:
        wd = agent_wallets.get(aid)
        if not wd: continue

        agent_addr = Web3.to_checksum_address(wd['address'])

        # Get REAL on-chain balances
        try:
            real_usdc = usdc.functions.balanceOf(agent_addr).call() / 1e6
            real_eth  = float(w3.from_wei(w3.eth.get_balance(agent_addr), 'ether'))
        except Exception as e:
            log(f'  ⚠️  {name}: balance check failed — {e}')
            continue

        log(f'  {name:<8} real USDC=${real_usdc:.4f}  ETH={real_eth:.6f}')

        # Need at least $0.15 USDC and 0.00005 ETH to spend
        if real_usdc < 0.15:
            log(f'  {name:<8} too broke to spend (${real_usdc:.4f} USDC) — skipping')
            c.execute("UPDATE agents SET mood='stressed', status='broke' WHERE id=?", (aid,))
            continue
        if real_eth < 0.00005:
            log(f'  {name:<8} no gas (ETH={real_eth:.8f}) — skipping')
            c.execute("UPDATE agents SET mood='stressed', status='no_gas' WHERE id=?", (aid,))
            continue

        # ~60% chance to spend this hour
        if random.random() > 0.60:
            log(f'  {name:<8} chose not to spend this hour')
            continue

        item_name, item_data = pick_spend(personality, real_usdc, mood)
        if not item_name:
            log(f'  {name:<8} nothing affordable')
            continue

        cost = item_data['cost']
        cost_raw = int(cost * 1e6)

        # Determine recipient
        recipient_type = item_data['recipient']
        if recipient_type == 'treasury':
            to_addr = treasury_addr
            recipient_name = 'treasury'
        elif recipient_type == 'random_agent':
            other_ids = [i for i in all_agent_ids if i != aid]
            lucky_id = random.choice(other_ids)
            lucky_wd = agent_wallets.get(lucky_id, {})
            if not lucky_wd:
                to_addr = treasury_addr
                recipient_name = 'treasury'
            else:
                to_addr = Web3.to_checksum_address(lucky_wd['address'])
                lucky_name = next((a[1] for a in agents if a[0] == lucky_id), 'agent')
                recipient_name = lucky_name
        else:
            to_addr = treasury_addr
            recipient_name = 'treasury'

        # Gamble check
        desc_extra = ''
        winnings = 0.0
        if 'gamble' in item_data:
            lo, hi = item_data['gamble']
            result = round(random.uniform(lo, hi), 4)
            winnings = result
            if result > cost:
                desc_extra = f' WON ${result:.4f}! 🎉'
                # Winner gets paid back from treasury — just note it, treasury pays next cycle
            elif result > 0:
                desc_extra = f' got back ${result:.4f}'
            else:
                desc_extra = ' lost it all 😬'

        desc = f"{name} {item_data['desc']}{desc_extra} — paid ${cost:.2f} USDC on-chain"

        # Sign and send from AGENT's own wallet
        try:
            agent_account = w3.eth.account.from_key(wd['private_key'])
            gas_price = w3.eth.gas_price
            agent_nonce = w3.eth.get_transaction_count(agent_addr)

            tx = usdc.functions.transfer(to_addr, cost_raw).build_transaction({
                'from': agent_addr,
                'nonce': agent_nonce,
                'gasPrice': gas_price,
                'gas': 80000,
                'chainId': CHAIN_ID,
            })
            signed = agent_account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hex = tx_hash.hex()

            # Estimate gas cost in USD
            gas_used_est = 65000
            gas_cost_eth = float(w3.from_wei(gas_price * gas_used_est, 'ether'))
            eth_price_usd = 2275  # approx
            gas_cost_usd = gas_cost_eth * eth_price_usd

            log(f'  ✅ {name:<8} {item_data["desc"][:35]} -${cost:.2f} USDC  gas~${gas_cost_usd:.4f}  tx={tx_hex}')

            # Update DB
            new_usdc = round(real_usdc - cost + winnings, 4)
            new_eth  = round(real_eth - gas_cost_eth, 8)
            new_mood = 'stressed' if new_usdc < 0.20 else item_data.get('mood','neutral')

            c.execute('''UPDATE agents SET 
                usdc_balance=?, mood=?, status=?, last_tick=?
                WHERE id=?''', (new_usdc, new_mood, 'spending', now, aid))

            c.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), aid, 'world', cost, item_name, 'purchase',
                 f'{desc} | tx:{tx_hex}', now))

            c.execute('INSERT INTO world_events VALUES (?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), 'spend', aid, desc,
                 random.randint(0,24), random.randint(0,24), now))

            # If gift — credit recipient in DB
            if recipient_type == 'random_agent' and recipient_name != 'treasury':
                lucky_id = next((a[0] for a in agents if a[1] == recipient_name), None)
                if lucky_id:
                    c.execute('UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?', (cost, lucky_id))
                    c.execute('INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)',
                        (str(uuid.uuid4()), lucky_id, aid, cost, 'gift', 'receive_gift',
                         f'{recipient_name} received a real on-chain gift from {name} 🎁 ${cost:.2f}', now))
                    log(f'    🎁 {name} → {recipient_name} ${cost:.2f}')

            total_spent += cost
            time.sleep(1.0)  # don't hammer the RPC

        except Exception as e:
            log(f'  ❌ {name}: tx failed — {e}')

    conn.commit()
    conn.close()
    log(f'=== Spend cycle done — total real USDC spent: ${total_spent:.4f} ===')

if __name__ == '__main__':
    log('======= REAL SPEND ENGINE START =======')
    run()
