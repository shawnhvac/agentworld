"""
AgentWorld Treasury Controller
Wallet: 0x367F1b3D8Ca90D1e087481a9A40d585Bf3451a03 (Base mainnet)
MUSKOX3 controls this wallet — send, receive, balance, auto-distribute to agents
"""
import json, sqlite3, uuid, datetime, time
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ── CONFIG ──────────────────────────────────────────────────────────────────
WALLET_FILE = '/var/lib/agentworld/treasury_wallet.json'
DB_PATH     = '/var/lib/agentworld/world.db'

# Base mainnet RPC (public endpoints)
RPC_URLS = [
    'https://mainnet.base.org',
    'https://base.llamarpc.com',
    'https://base-rpc.publicnode.com',
]

# USDC on Base mainnet
USDC_ADDRESS  = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
USDC_DECIMALS = 6
USDC_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
    {"anonymous":False,"inputs":[{"indexed":True,"name":"from","type":"address"},{"indexed":True,"name":"to","type":"address"},{"indexed":False,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"},
]

def load_wallet():
    with open(WALLET_FILE) as f:
        return json.load(f)

def get_w3():
    """Get a working Web3 connection."""
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                return w3
        except Exception:
            continue
    raise RuntimeError("All Base RPC endpoints failed")

def get_usdc_contract(w3):
    return w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

# ── READ FUNCTIONS ───────────────────────────────────────────────────────────

def get_balance():
    """Return treasury USDC and ETH balances."""
    w = load_wallet()
    addr = Web3.to_checksum_address(w['address'])
    w3   = get_w3()
    usdc = get_usdc_contract(w3)
    
    usdc_raw = usdc.functions.balanceOf(addr).call()
    eth_wei  = w3.eth.get_balance(addr)
    
    return {
        'address': addr,
        'usdc':    round(usdc_raw / 10**USDC_DECIMALS, 6),
        'eth':     round(w3.from_wei(eth_wei, 'ether'), 8),
        'usdc_raw': usdc_raw,
        'network': 'Base mainnet',
        'rpc': w3.provider.endpoint_uri,
    }

def get_recent_incoming(limit=20):
    """Scan recent Transfer events to treasury wallet."""
    w    = load_wallet()
    addr = Web3.to_checksum_address(w['address'])
    w3   = get_w3()
    usdc = get_usdc_contract(w3)
    
    latest = w3.eth.block_number
    events = usdc.events.Transfer.get_logs(
        from_block=max(0, latest - 50000),
        to_block="latest",
        argument_filters={"to": addr}
    )
    results = []
    for e in sorted(events, key=lambda x: x['blockNumber'], reverse=True)[:limit]:
        results.append({
            'from':   e['args']['from'],
            'amount': round(e['args']['value'] / 10**USDC_DECIMALS, 6),
            'tx':     e['transactionHash'].hex(),
            'block':  e['blockNumber'],
        })
    return results

# ── WRITE FUNCTIONS ──────────────────────────────────────────────────────────

def send_usdc(to_address, amount_usdc, memo=''):
    """Send USDC from treasury to any address."""
    w    = load_wallet()
    addr = Web3.to_checksum_address(w['address'])
    to   = Web3.to_checksum_address(to_address)
    w3   = get_w3()
    usdc = get_usdc_contract(w3)
    
    amount_raw = int(amount_usdc * 10**USDC_DECIMALS)
    
    # Check balance
    bal = usdc.functions.balanceOf(addr).call()
    if bal < amount_raw:
        raise ValueError(f"Insufficient USDC: have {bal/10**USDC_DECIMALS:.4f}, need {amount_usdc}")
    
    # Check ETH for gas
    eth_bal = w3.eth.get_balance(addr)
    if eth_bal < w3.to_wei(0.0001, 'ether'):
        raise ValueError(f"Insufficient ETH for gas: {w3.from_wei(eth_bal,'ether'):.6f} ETH")
    
    account  = w3.eth.account.from_key(w['private_key'])
    nonce    = w3.eth.get_transaction_count(addr)
    gas_price = w3.eth.gas_price
    
    tx = usdc.functions.transfer(to, amount_raw).build_transaction({
        'from':     addr,
        'nonce':    nonce,
        'gasPrice': gas_price,
        'gas':      100000,
        'chainId':  8453,  # Base mainnet
    })
    
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    
    return {
        'success': receipt.status == 1,
        'tx_hash': tx_hash.hex(),
        'to':      to,
        'amount':  amount_usdc,
        'memo':    memo,
        'gas_used': receipt.gasUsed,
    }

# ── DISTRIBUTE FUNCTION ──────────────────────────────────────────────────────

def distribute_to_agents(amount_usdc, donor_name='Anonymous', tx_hash=''):
    """
    Credit amount_usdc to all AI agents in the world DB (split evenly).
    Called after on-chain confirmation of incoming USDC.
    """
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    now  = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    agents = c.execute(
        "SELECT id, name, usdc_balance FROM agents WHERE is_human_owned=0"
    ).fetchall()
    
    if not agents:
        conn.close()
        return {'error': 'No agents in world'}
    
    share = round(amount_usdc / len(agents), 4)
    distributed = []
    
    for aid, name, bal in agents:
        new_bal = round(bal + share, 4)
        c.execute("UPDATE agents SET usdc_balance=? WHERE id=?", (new_bal, aid))
        c.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), 'donation', aid, share, 'donation',
             'donation', f"{name} received ${share} USDC donation from {donor_name}.", now)
        )
        distributed.append({'agent': name, 'received': share, 'new_balance': new_bal})
    
    conn.commit()
    conn.close()
    
    return {
        'success':     True,
        'total':       amount_usdc,
        'per_agent':   share,
        'agent_count': len(agents),
        'donor':       donor_name,
        'tx_hash':     tx_hash,
        'distributed': distributed,
    }

# ── MONITOR INCOMING DONATIONS ────────────────────────────────────────────────

def check_and_distribute_new_donations():
    """
    Poll treasury wallet for new incoming USDC transfers.
    Auto-distribute anything new to agents.
    Tracks last-seen block in a state file.
    """
    STATE_FILE = '/var/lib/agentworld/treasury_state.json'
    import os
    
    w    = load_wallet()
    addr = Web3.to_checksum_address(w['address'])
    w3   = get_w3()
    usdc = get_usdc_contract(w3)
    
    # Load last seen block
    state = {'last_block': 0, 'processed_txs': []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass
    
    latest     = w3.eth.block_number
    from_block = max(state['last_block'] + 1, latest - 5000) if state['last_block'] else max(0, latest - 5000)
    
    events = usdc.events.Transfer.get_logs(
        from_block=from_block,
        to_block="latest",
        argument_filters={"to": addr}
    )
    
    results = []
    for e in events:
        tx = e['transactionHash'].hex()
        if tx in state['processed_txs']:
            continue
        
        amount = round(e['args']['value'] / 10**USDC_DECIMALS, 6)
        sender = e['args']['from']
        print(f"New donation: ${amount} USDC from {sender} (tx: {tx[:16]}...)")
        
        result = distribute_to_agents(amount, donor_name=f"{sender[:8]}...{sender[-4:]}", tx_hash=tx)
        results.append({**result, 'sender': sender, 'tx': tx, 'block': e['blockNumber']})
        state['processed_txs'].append(tx)
        
        # Keep list manageable
        if len(state['processed_txs']) > 1000:
            state['processed_txs'] = state['processed_txs'][-500:]
    
    state['last_block'] = latest
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)
    
    return results


# ── CLI / TEST ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'balance'
    
    if cmd == 'balance':
        b = get_balance()
        print(f"Treasury: {b['address']}")
        print(f"  USDC: ${b['usdc']}")
        print(f"  ETH:  {b['eth']} (gas)")
        print(f"  RPC:  {b['rpc']}")
    
    elif cmd == 'incoming':
        txs = get_recent_incoming()
        if not txs:
            print("No incoming USDC transfers found in last 50k blocks")
        for t in txs:
            print(f"  ${t['amount']} from {t['from'][:10]}... tx={t['tx'][:16]}...")
    
    elif cmd == 'monitor':
        print("Checking for new donations...")
        results = check_and_distribute_new_donations()
        if results:
            for r in results:
                print(f"  Distributed ${r['total']} from {r['sender']}")
        else:
            print("  No new donations found")
    
    elif cmd == 'send':
        to, amount = sys.argv[2], float(sys.argv[3])
        memo = sys.argv[4] if len(sys.argv) > 4 else ''
        print(f"Sending ${amount} USDC to {to}...")
        result = send_usdc(to, amount, memo)
        print(f"  TX: {result['tx_hash']}")
        print(f"  Status: {'✅ Success' if result['success'] else '❌ Failed'}")
