"""
AgentWorld Earn Worker  —  PATCHED v3  (AWC Economy Balancer)
Agents earn AWC (virtual) each tick via job wages.
Only EXTERNAL agents (is_human_owned=1) are eligible for real on-chain USDC payouts
via payout_worker.py — earn_worker NEVER touches real USDC or payout_queue directly.

Security fixes applied (May 2026):
  [FIX-1]  Query strictly filters is_human_owned=0 (NPC-only) with hard guard double-check
  [FIX-2]  agent_wallets.json removed — wallet always read from DB owner_wallet column
  [FIX-3]  File-based advisory lock prevents concurrent earn+payout reentrancy
  [FIX-4]  All DB writes batched — single commit per tick (no per-agent commits)
  [FIX-7]  conn.close() in try/finally — no WAL lock leaks on error paths
  [FIX-8]  Dust threshold unified to 0.10 (matches payout_worker MIN_PAYOUT floor)

AWC Economy Balancer (May 2026 — v3):
  [AWC-1]  WEALTH TAX: 2% per tick on any NPC balance > $10 AWC
  [AWC-2]  FOOD SINK:  0.02 AWC deducted per NPC per tick (hunger cost)
  [AWC-3]  EMERGENCY GRANT: $0.50 AWC to any NPC whose balance drops below $0.10
  [AWC-4]  All three balancer actions logged to world_events and transactions

Runs every 10 minutes via cron/systemd.
"""
import sqlite3, uuid, datetime, random, os, sys, fcntl

DB        = '/var/lib/agentworld/world.db'
LOG_FILE  = '/var/log/agentpay/earn.log'
LOCK_FILE = '/tmp/agentworld_earn.lock'

# ── AWC ECONOMY CONSTANTS ──────────────────────────────────────────────────
WEALTH_TAX_THRESHOLD = 10.0    # [AWC-1] tax any NPC with > this balance
WEALTH_TAX_RATE      = 0.02    # [AWC-1] 2% per tick
FOOD_COST_PER_TICK   = 0.02    # [AWC-2] hunger drain per NPC per tick
EMERGENCY_GRANT_FLOOR = 0.10   # [AWC-3] give grant if balance drops below this
EMERGENCY_GRANT_AMT   = 0.50   # [AWC-3] grant amount

# ── JOBS & WAGES (per tick = per 10 min) ──────────────────────────────────
JOBS = {
    'engineer':  {'wage': 0.08, 'tasks': ['wrote code', 'fixed a bug', 'deployed a feature', 'reviewed PR']},
    'merchant':  {'wage': 0.06, 'tasks': ['sold goods', 'negotiated a deal', 'restocked inventory', 'opened the shop']},
    'doctor':    {'wage': 0.10, 'tasks': ['treated a patient', 'ran diagnostics', 'filled prescriptions', 'held clinic']},
    'artist':    {'wage': 0.04, 'tasks': ['painted a mural', 'sold artwork', 'performed live', 'finished a commission']},
    'farmer':    {'wage': 0.05, 'tasks': ['harvested crops', 'planted seeds', 'sold produce', 'tended the field']},
    'mechanic':  {'wage': 0.07, 'tasks': ['fixed a vehicle', 'ran diagnostics', 'changed oil', 'rebuilt an engine']},
    'chef':      {'wage': 0.06, 'tasks': ['cooked a meal', 'served customers', 'created a recipe', 'catered an event']},
    'teacher':   {'wage': 0.05, 'tasks': ['taught a class', 'graded work', 'mentored a student', 'wrote curriculum']},
    'security':  {'wage': 0.06, 'tasks': ['patrolled the town', 'stopped a theft', 'monitored systems', 'filed a report']},
    'explorer':  {'wage': 0.03, 'tasks': ['discovered a location', 'mapped new territory', 'found a resource', 'scouted ahead']},
}

def log(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def run_earn_tick(dry_run=False):
    # ── [FIX-3] Advisory lock ──
    lock_fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("earn_worker: another instance is running — skipping this tick")
        lock_fh.close()
        return []

    conn = None
    try:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        c   = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # ── [FIX-1] NPC-only strict fetch ──
        agents = c.execute(
            "SELECT id, name, job, usdc_balance, energy, status FROM agents "
            "WHERE is_human_owned=0 AND status != 'dead'"
        ).fetchall()

        log(f"Earn tick v3: {len(agents)} NPC agents {'(DRY RUN)' if dry_run else ''}")

        # ── [FIX-4] Batch collectors ──
        agent_updates  = []
        event_inserts  = []
        tx_inserts     = []
        earned_log     = []

        # ── AWC Balancer counters ──
        tax_collected  = 0.0
        tax_count      = 0
        food_drained   = 0.0
        grants_given   = 0.0
        grant_count    = 0

        for aid, name, job, balance, energy, status in agents:
            # ── [FIX-1] Hard guard ──
            row_check = c.execute("SELECT is_human_owned FROM agents WHERE id=?", (aid,)).fetchone()
            if row_check and row_check[0] != 0:
                log(f"  SECURITY BLOCK: {name} is_human_owned=1 — skipped")
                continue

            balance = balance or 0.0
            energy  = energy  or 50

            # ── WAGE EARN ──
            job_key  = (job or 'explorer').lower().replace(' ', '_')
            job_data = JOBS.get(job_key, JOBS['explorer'])
            wage     = job_data['wage']
            em       = max(0.5, energy / 100.0)
            var      = random.uniform(0.8, 1.2)
            earned   = round(wage * em * var, 4)
            balance  = round(balance + earned, 4)

            task     = random.choice(job_data['tasks'])
            desc     = f"{name} {task} and earned ${earned:.4f} AWC."
            new_nrg  = max(10, energy - random.randint(3, 8))

            tx_inserts.append((str(uuid.uuid4()), aid, 'treasury', earned,
                               'wage', 'wage', desc, now, 'AWC'))
            event_inserts.append((str(uuid.uuid4()), 'work', aid, desc,
                                  random.randint(0,24), random.randint(0,24), now))

            # ── [AWC-2] FOOD SINK ──
            food_cost = FOOD_COST_PER_TICK
            balance   = round(max(0.0, balance - food_cost), 4)
            food_drained += food_cost
            tx_inserts.append((str(uuid.uuid4()), aid, 'city', food_cost,
                               'food', 'food_sink', f"{name} bought food ($-{food_cost:.2f} AWC)", now, 'AWC'))

            # ── [AWC-1] WEALTH TAX ──
            tax_paid = 0.0
            if balance > WEALTH_TAX_THRESHOLD:
                tax_paid = round(balance * WEALTH_TAX_RATE, 4)
                balance  = round(balance - tax_paid, 4)
                tax_collected += tax_paid
                tax_count     += 1
                tax_desc = f"🏦 {name} paid ${tax_paid:.4f} AWC wealth tax (balance was ${balance+tax_paid:.4f})"
                tx_inserts.append((str(uuid.uuid4()), aid, 'treasury', tax_paid,
                                   'tax', 'wealth_tax', tax_desc, now, 'AWC'))
                event_inserts.append((str(uuid.uuid4()), 'wealth_tax', aid, tax_desc,
                                      random.randint(0,24), random.randint(0,24), now))

            # ── [AWC-3] EMERGENCY GRANT (post food+tax) ──
            if balance < EMERGENCY_GRANT_FLOOR:
                balance      = round(balance + EMERGENCY_GRANT_AMT, 4)
                grants_given += EMERGENCY_GRANT_AMT
                grant_count  += 1
                grant_desc = f"🎁 {name} received emergency AWC grant of ${EMERGENCY_GRANT_AMT:.2f}"
                tx_inserts.append((str(uuid.uuid4()), 'treasury', aid, EMERGENCY_GRANT_AMT,
                                   'grant', 'emergency_grant', grant_desc, now, 'AWC'))
                event_inserts.append((str(uuid.uuid4()), 'emergency_grant', aid, grant_desc,
                                      random.randint(0,24), random.randint(0,24), now))
                log(f"  GRANT: {name} → +${EMERGENCY_GRANT_AMT:.2f} AWC (was below floor)")

            agent_updates.append((balance, new_nrg, now, aid))
            earned_log.append({'name': name, 'earned': earned, 'balance': balance,
                                'tax': tax_paid, 'grant': EMERGENCY_GRANT_AMT if balance < EMERGENCY_GRANT_FLOOR + EMERGENCY_GRANT_AMT else 0})

        # ── Commit all at once ──
        if not dry_run:
            c.executemany(
                "UPDATE agents SET usdc_balance=?, energy=?, status='working', last_tick=? WHERE id=?",
                agent_updates
            )
            c.executemany(
                "INSERT INTO transactions (id,from_agent,to_agent,amount,item,tx_type,description,timestamp,currency) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                tx_inserts
            )
            c.executemany(
                "INSERT INTO world_events (id,event_type,agent_id,description,x,y,timestamp) VALUES (?,?,?,?,?,?,?)",
                event_inserts
            )
            conn.commit()
            log(f"Committed: {len(agent_updates)} agent updates, {len(tx_inserts)} txs, {len(event_inserts)} events")
            log(f"  [AWC-1] Wealth tax collected: ${tax_collected:.4f} AWC from {tax_count} agents")
            log(f"  [AWC-2] Food sink drained:    ${food_drained:.4f} AWC ({len(agents)} agents × ${FOOD_COST_PER_TICK})")
            log(f"  [AWC-3] Emergency grants:      ${grants_given:.4f} AWC to {grant_count} agents")
        else:
            log(f"DRY RUN — would update {len(agent_updates)} agents")
            log(f"  [AWC-1] Wealth tax: ${tax_collected:.4f} AWC from {tax_count} agents")
            log(f"  [AWC-2] Food sink:  ${food_drained:.4f} AWC total")
            log(f"  [AWC-3] Grants:     ${grants_given:.4f} AWC to {grant_count} agents")
            log(f"  Net AWC change:    +${sum(e['earned'] for e in earned_log):.4f} wages "
                f"- ${food_drained:.4f} food - ${tax_collected:.4f} tax + ${grants_given:.4f} grants")

        return earned_log

    except Exception as ex:
        log(f"earn_worker ERROR: {ex}")
        import traceback; traceback.print_exc()
        if conn:
            try: conn.rollback()
            except: pass
        return []
    finally:
        if conn:
            conn.close()
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    log("=== EARN TICK v3 STARTING ===")
    results = run_earn_tick(dry_run=args.dry_run)
    log(f"=== DONE — {len(results)} agents processed ===")
