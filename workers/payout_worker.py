#!/usr/bin/env python3
"""
AgentWorld On-Chain Payout Worker  —  PATCHED v2
Processes pending payout_queue entries, executes real USDC transfers
via treasury.py (web3 on Base mainnet), marks tx_hash in DB.

Security fixes applied (May 2026):
  [FIX-1]  Hard NPC guard: is_human_owned=1 required — NPC payouts hard-blocked at queue entry
  [FIX-2]  Wallet always read from DB owner_wallet column — agent_wallets.json never used
  [FIX-3]  File-based advisory lock prevents concurrent earn+payout reentrancy
  [FIX-5]  Treasury imported once at module level (not inside function)
  [FIX-6]  Index on payout_queue.status ensured at startup
  [FIX-8]  Unified dust threshold: MIN_PAYOUT=0.10 (matches earn_worker floor)
  [FIX-9]  Consecutive failure counter — Telegram alert after 3+ failures on same entry

Runs every 5 minutes via systemd timer.
"""
import sqlite3, os, sys, time, uuid
from datetime import datetime

sys.path.insert(0, '/root/agentworld')

# ── [FIX-5] Import treasury once at module level ──
try:
    import treasury as TREASURY
    _treasury_available = True
except Exception as _te:
    print(f"[payout_worker] WARNING: treasury import failed: {_te}")
    _treasury_available = False

DB_PATH    = "/var/lib/agentworld/world.db"
LOCK_FILE  = '/tmp/agentworld_payout.lock'   # [FIX-3] advisory lock
BATCH_SIZE = 5      # max payouts per run (conservative — each tx costs gas)
MIN_PAYOUT = 0.10   # [FIX-8] default — overridden dynamically from world_meta

def _get_dynamic_min_payout(conn):
    """Read threshold from world_meta (set by tick_engine AWC-6). Falls back to 1.00 if not set."""
    try:
        row = conn.execute("SELECT value FROM world_meta WHERE key=payout_min_threshold").fetchone()
        if row:
            return float(row[0])
    except Exception:
        pass
    return 1.00  # safe fallback:  floor until treasury recovers
MAX_SINGLE = 5.00   # safety cap per single payout

# Platform/treasury wallets that must NEVER receive payouts
PLATFORM_WALLETS = frozenset({
    "0x2a07182afdb346c84dfc5d116d84f34e1db4617d",
    "0x367f1b3d8ca90d1e087481a9a40d585bf3451a03",
    "0xbd50057332977e54a6ee3986849d758fd0bdcba6",
})

CONSECUTIVE_FAIL_ALERT = 3  # [FIX-9] alert Shawn after this many consecutive failures

def _ensure_index(conn):
    """[FIX-6] Ensure index on payout_queue.status exists — idempotent."""
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_payout_queue_status "
        "ON payout_queue(status, created_at)"
    )
    conn.commit()

def _alert_shawn(msg):
    """[FIX-9] Send Telegram alert for repeated failures."""
    try:
        import subprocess
        subprocess.run(
            ['python3', '/root/agentworld/telegram_alert.py', msg],
            timeout=10, capture_output=True
        )
    except Exception:
        print(f"[payout_worker] Could not send Telegram alert: {msg}")

def run_payout_worker():
    import fcntl

    if not _treasury_available:
        print("[payout_worker] Treasury unavailable — aborting")
        return 0, 0, 0

    # ── [FIX-3] Advisory lock — prevent concurrent earn+payout reentrancy ──
    lock_fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[payout_worker] Another instance is running — skipping this run")
        lock_fh.close()
        return 0, 0, 0

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        c   = conn.cursor()
        now = datetime.utcnow().isoformat()

        # ── [FIX-6] Ensure index exists ──
        _ensure_index(conn)

        # Get pending payouts — joined with agents to enforce is_human_owned=1 at query level
        rows = c.execute("""
            SELECT pq.*, a.is_human_owned, a.name AS agent_name
            FROM payout_queue pq
            JOIN agents a ON a.id = pq.agent_id
            WHERE pq.status = 'pending'
              AND pq.amount >= ?
              AND a.is_human_owned = 1
            ORDER BY pq.created_at ASC
            LIMIT ?
        """, (_get_dynamic_min_payout(conn), BATCH_SIZE)).fetchall()

        print(f"[payout_worker] {now} — {len(rows)} eligible external-agent payouts")

        if not rows:
            return 0, 0, 0

        # Check treasury balance before processing
        try:
            bal_info      = TREASURY.get_balance()
            treasury_usdc = float(bal_info.get('usdc', 0))
            print(f"[payout_worker] Treasury balance: ${treasury_usdc:.4f} USDC")
            if treasury_usdc < MIN_PAYOUT:
                print(f"[payout_worker] WARNING: Treasury too low (${treasury_usdc:.4f}) — payouts paused")
                return 0, 0, 0
        except Exception as be:
            print(f"[payout_worker] Balance check failed: {be}")

        processed = failed = skipped = 0

        for row in rows:
            p        = dict(row)
            pid      = p['id']
            agent_id = p.get('agent_id', '')
            amount   = round(float(p['amount']), 6)

            # ── [FIX-1] Triple-layer NPC guard ──
            # Layer 1: JOIN in SQL already filters is_human_owned=1
            # Layer 2: Explicit check on joined column
            if p.get('is_human_owned', 0) != 1:
                c.execute("UPDATE payout_queue SET status='cancelled', processed_at=? WHERE id=?", (now, pid))
                conn.commit()
                skipped += 1
                print(f"  SECURITY BLOCK: NPC agent payout cancelled (agent {agent_id[:8]})")
                continue

            # ── [FIX-2] Read wallet from DB owner_wallet — never from JSON file ──
            wallet_row = c.execute(
                "SELECT owner_wallet FROM agents WHERE id=? AND is_human_owned=1", (agent_id,)
            ).fetchone()
            if not wallet_row or not wallet_row[0]:
                c.execute("UPDATE payout_queue SET status='invalid', processed_at=? WHERE id=?", (now, pid))
                conn.commit()
                skipped += 1
                print(f"  SKIP: no owner_wallet in DB for agent {agent_id[:8]}")
                continue

            wallet = wallet_row[0].strip()

            # Validate wallet format
            if not wallet.startswith('0x') or len(wallet) != 42:
                c.execute("UPDATE payout_queue SET status='invalid', processed_at=? WHERE id=?", (now, pid))
                conn.commit()
                skipped += 1
                print(f"  SKIP invalid wallet format: {wallet}")
                continue

            # Layer 3: Block platform wallets regardless
            if wallet.lower() in PLATFORM_WALLETS:
                c.execute("UPDATE payout_queue SET status='cancelled', processed_at=? WHERE id=?", (now, pid))
                conn.commit()
                skipped += 1
                print(f"  SECURITY BLOCK: platform wallet blocked: {wallet[:12]}...")
                continue

            if amount > MAX_SINGLE:
                c.execute("UPDATE payout_queue SET status='review', processed_at=? WHERE id=?", (now, pid))
                conn.commit()
                skipped += 1
                print(f"  SKIP oversized ${amount:.4f} > ${MAX_SINGLE} cap — manual review")
                continue

            agent_name = p.get('agent_name', 'Agent')
            print(f"  -> ${amount:.4f} USDC to {wallet[:12]}... ({agent_name} / {agent_id[:8]})")

            # Mark in-progress (prevents double-spend if worker crashes mid-tx)
            c.execute("UPDATE payout_queue SET status='processing' WHERE id=?", (pid,))
            conn.commit()

            try:
                result = TREASURY.send_usdc(wallet, amount, memo=f"AgentWorld payout | agent:{agent_id[:8]}")
                if result.get('success'):
                    tx_hash = result['tx_hash']
                    c.execute(
                        "UPDATE payout_queue SET status='completed', tx_hash=?, processed_at=?, "
                        "fail_count=0 WHERE id=?",
                        (tx_hash, now, pid)
                    )
                    c.execute(
                        "UPDATE transactions SET tx_ref=?, payout_queued=2 "
                        "WHERE from_agent=? AND payout_queued=1 "
                        "AND (tx_ref IS NULL OR tx_ref LIKE 'aw_tx_%')",
                        (tx_hash, agent_id)
                    )
                    c.execute(
                        "INSERT INTO world_events (id,event_type,agent_id,description,x,y,timestamp) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, 'onchain_payout', agent_id,
                         f"\U0001f4b8 Paid ${amount:.4f} USDC on-chain to {wallet[:10]}... | tx:{tx_hash}",
                         0, 0, now)
                    )
                    conn.commit()
                    print(f"     OK: {tx_hash}")
                    processed += 1
                else:
                    raise ValueError(f"Transfer returned success=False: {result}")

            except Exception as e:
                err = str(e)[:160]
                # ── [FIX-9] Track consecutive failures, alert after threshold ──
                fail_row = c.execute(
                    "SELECT COALESCE(fail_count,0) FROM payout_queue WHERE id=?", (pid,)
                ).fetchone()
                fail_count = (fail_row[0] if fail_row else 0) + 1
                c.execute(
                    "UPDATE payout_queue SET status='failed', tx_hash=?, processed_at=?, fail_count=? "
                    "WHERE id=?",
                    (f"ERR:{err}", now, fail_count, pid)
                )
                conn.commit()
                print(f"     FAIL #{fail_count}: {err}")
                failed += 1
                if fail_count >= CONSECUTIVE_FAIL_ALERT:
                    _alert_shawn(
                        f"⚠️ AgentWorld payout REPEATED FAILURE\n"
                        f"Agent: {agent_name} ({agent_id[:8]})\n"
                        f"Amount: ${amount:.4f} USDC → {wallet[:12]}...\n"
                        f"Failures: {fail_count}\nError: {err[:100]}"
                    )

            time.sleep(2)  # rate limit between on-chain txs

        print(f"[payout_worker] done — processed={processed} failed={failed} skipped={skipped}")
        return processed, failed, skipped

    except Exception as ex:
        print(f"[payout_worker] FATAL: {ex}")
        if conn:
            try: conn.rollback()
            except: pass
        return 0, 0, 0
    finally:
        if conn:
            conn.close()
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

if __name__ == '__main__':
    run_payout_worker()
