#!/usr/bin/env python3
"""
x402 Agent Economy Engine — REAL BLOCKCHAIN TRANSFERS
Agents pay each other using actual USDC on Base mainnet via their own wallets.
AgentPay takes 2% facilitator fee on every transaction.
"""
import sqlite3, random, uuid, json, os, datetime
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

WORLD_DB      = '/var/lib/agentworld/world.db'
WALLETS_FILE  = '/var/lib/agentworld/agent_wallets.json'
TREASURY_FILE = '/var/lib/agentworld/treasury_wallet.json'
ENV_FILE      = '/root/agents/.env'
LOG_FILE      = '/var/log/agentpay/x402_economy.log'

USDC_ADDRESS  = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
USDC_DECIMALS = 6
USDC_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
]

RPC_URLS = ['https://mainnet.base.org','https://base.llamarpc.com','https://base-rpc.publicnode.com']
FEE_RATE = 0.02   # 2% AgentPay facilitator fee
MIN_TX   = 0.02   # $0.02 minimum transaction

AGENT_SERVICES = {
    'doctor':          [('medical_consultation', 0.08, 0.20)],
    'mechanic':        [('vehicle_repair', 0.10, 0.30)],
    'shopkeeper':      [('goods_purchase', 0.02, 0.10)],
    'banker':          [('loan_processing', 0.05, 0.15)],
    'delivery driver': [('package_delivery', 0.03, 0.12)],
    'architect':       [('building_design', 0.10, 0.35)],
    'realtor':         [('property_listing', 0.08, 0.25)],
    'farmer':          [('fresh_produce', 0.02, 0.08)],
    'tech startup founder': [('software_service', 0.05, 0.20)],
    'trader':          [('market_trade', 0.03, 0.15)],
    'car dealer':      [('vehicle_sale', 0.15, 0.60)],
}

def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def get_w3():
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout':10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                return w3
        except: continue
    raise RuntimeError("All RPC endpoints failed")

def real_transfer(w3, from_pk, from_addr, to_addr, amount_usdc, nonce=None):
    """Execute a real USDC transfer on Base mainnet. Returns (tx_hash, success, next_nonce)."""
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    amount_raw = int(amount_usdc * 10**USDC_DECIMALS)
    gas_price  = max(int(w3.eth.gas_price * 2), 1000000)
    if nonce is None:
        nonce = w3.eth.get_transaction_count(from_addr)
    tx = usdc.functions.transfer(to_addr, amount_raw).build_transaction({
        'from':     from_addr,
        'nonce':    nonce,
        'gasPrice': gas_price,
        'gas':      100000,
        'chainId':  8453,
    })
    acct   = w3.eth.account.from_key(from_pk)
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    next_nonce = nonce + 1
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    return tx_hash.hex(), receipt.status == 1, next_nonce

def run_agent_economy_tick():
    """Run one real on-chain agent-to-agent transaction per tick."""
    with open(WALLETS_FILE) as f:
        wallets_raw = json.load(f)
    with open(TREASURY_FILE) as f:
        treasury = json.load(f)

    # Build address -> private_key map
    wallet_pk = {}
    for k, v in wallets_raw.items():
        addr = v.get('address','').lower()
        if addr and v.get('private_key'):
            wallet_pk[addr] = {'private_key': v['private_key'], 'address': v['address']}

    conn = sqlite3.connect(WORLD_DB)
    c    = conn.cursor()

    # Only use agents with real wallets + enough balance
    agents = c.execute(
        "SELECT id, name, job, wallet_address, usdc_balance FROM agents WHERE wallet_address IS NOT NULL AND usdc_balance > 0.15"
    ).fetchall()

    payable = []
    for row in agents:
        aid, name, job, wallet_addr, bal = row
        if wallet_addr and wallet_pk.get(wallet_addr.lower()):
            payable.append({'id': aid, 'name': name, 'job': job or '', 'wallet': wallet_addr, 'balance': bal,
                           'pk': wallet_pk[wallet_addr.lower()]['private_key']})

    if len(payable) < 2:
        log(f"Not enough funded agents for economy tick (have {len(payable)})")
        conn.close()
        return []

    try:
        w3 = get_w3()
    except Exception as e:
        log(f"RPC error: {e}")
        conn.close()
        return []

    # Check ETH gas balance for buyer candidates
    def has_gas(wallet_addr):
        try:
            eth = w3.from_wei(w3.eth.get_balance(Web3.to_checksum_address(wallet_addr)), 'ether')
            return eth > 0.0001  # need at least ~$0.25 ETH for gas
        except:
            return False

    treasury_addr = Web3.to_checksum_address(treasury['address'])
    treasury_pk   = treasury['private_key']

    results = []
    # Do 1-2 real transactions per tick (keep gas costs low)
    num_txns = random.randint(1, 2)
    attempts = 0

    while len(results) < num_txns and attempts < 10:
        attempts += 1
        buyer  = random.choice(payable)
        others = [a for a in payable if a['id'] != buyer['id']]
        if not others:
            continue
        seller = random.choice(others)

        job_key  = seller['job'].lower()
        services = AGENT_SERVICES.get(job_key, [('general_service', 0.02, 0.08)])
        service_name, min_p, max_p = random.choice(services)
        amount = round(random.uniform(min_p, min(max_p, buyer['balance'] * 0.3)), 4)

        if amount < MIN_TX:
            continue

        fee         = round(amount * FEE_RATE, 6)
        net_seller  = round(amount - fee, 6)
        now         = datetime.datetime.utcnow().isoformat()

        log(f"  {buyer['name']} → {seller['name']}: ${amount:.4f} USDC for {service_name}")

        # Check if buyer has ETH for gas
        if not has_gas(buyer['wallet']):
            # Treasury pays the fee instead (buyer sends from treasury on their behalf)
            # For now, skip this buyer
            log(f"  {buyer['name']} has no ETH for gas — skipping")
            continue

        try:
            buyer_addr  = Web3.to_checksum_address(buyer['wallet'])
            seller_addr = Web3.to_checksum_address(seller['wallet'])

            # Buyer → Seller (real transfer)
            buyer_nonce = w3.eth.get_transaction_count(buyer_addr)
            tx_hash, success, buyer_nonce = real_transfer(w3, buyer['pk'], buyer_addr, seller_addr, net_seller, nonce=buyer_nonce)
            status_icon = '✅' if success else '❌'
            log(f"  {status_icon} tx={tx_hash[:16]}... on Base mainnet")

            if success:
                # Fee goes to treasury (separate transfer from buyer)
                try:
                    fee_hash, _, _ = real_transfer(w3, buyer['pk'], buyer_addr, treasury_addr, fee, nonce=buyer_nonce)
                    log(f"  💰 Fee ${fee:.6f} → treasury tx={fee_hash[:16]}...")
                except Exception as fe:
                    log(f"  Fee collection skipped: {fe}")
                    fee_hash = 'skipped'

                # Update DB balances to match reality
                c.execute("UPDATE agents SET usdc_balance = usdc_balance - ? WHERE id = ?", (amount, buyer['id']))
                c.execute("UPDATE agents SET usdc_balance = usdc_balance + ? WHERE id = ?", (net_seller, seller['id']))

                # Log transaction
                c.execute("""INSERT OR IGNORE INTO transactions
                    (id, from_agent, to_agent, amount, currency, tx_type, description, timestamp)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), buyer['id'], seller['id'], amount, 'USDC', 'x402_payment',
                     f'{buyer["name"]} → {seller["name"]} ${amount:.4f} USDC ({service_name}). on-chain: {tx_hash[:20]}...', now))

                # Log world event (shown in live ticker)
                c.execute("""INSERT OR IGNORE INTO world_events
                    (id, event_type, agent_id, description, x, y, timestamp)
                    VALUES (?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), 'x402_payment', buyer['id'],
                     f'🔗 {buyer["name"]} paid {seller["name"]} ${amount:.4f} USDC on-chain for {service_name.replace("_"," ")} — tx:{tx_hash[:12]}...',
                     random.randint(0,19), random.randint(0,19), now))

                conn.commit()
                results.append({'from': buyer['name'], 'to': seller['name'], 'amount': amount, 'tx': tx_hash, 'success': True})
            else:
                log(f"  Transaction reverted on-chain")

        except Exception as e:
            log(f"  Transfer failed: {e}")

    conn.close()
    log(f"Economy tick complete: {len(results)} real txns")
    return results

if __name__ == '__main__':
    run_agent_economy_tick()
