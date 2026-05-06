#!/usr/bin/env python3
"""
AgentWorld City Economy Engine v2
-----------------------------------
Full city simulation with:
- Wage system by job role
- Credit scores (based on housing history, savings, rep)
- Banker loans (credit-gated — no loans to homeless/bad credit)
- Sheriff fines (bad behavior, violations)
- Sanitation workers (city cleanliness score, steady wages)
- Tax redistribution to civic officials
- Auto housing purchase
"""
import sqlite3, uuid, random, json
from datetime import datetime, timezone

DB = '/var/lib/agentworld/world.db'

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# Wage per tick by job keyword
JOB_WAGES = {
    'garbage':      0.09, 'sanitation':   0.09, 'janitor':      0.07,
    'cleaner':      0.07, 'waste':        0.08,
    'banker':       0.13, 'bank':         0.11, 'finance':      0.11,
    'mayor':        0.16, 'council':      0.13, 'governor':     0.16,
    'chief of staff':0.14,'chief':        0.12,
    'police':       0.10, 'security':     0.10, 'sheriff':      0.12,
    'guard':        0.09, 'enforcement':  0.09,
    'teacher':      0.09, 'education':    0.09, 'professor':    0.11,
    'doctor':       0.12, 'medic':        0.10, 'nurse':        0.09,
    'health':       0.09,
    'shopkeeper':   0.08, 'merchant':     0.08, 'retailer':     0.07,
    'farmer':       0.07, 'agriculture':  0.07, 'food':         0.07,
    'engineer':     0.11, 'developer':    0.11, 'devops':       0.11,
    'architect':    0.11, 'builder':      0.09, 'contractor':   0.09,
    'trader':       0.10, 'dealer':       0.10, 'broker':       0.11,
    'auditor':      0.10, 'analyst':      0.09, 'researcher':   0.08,
    'lawyer':       0.12, 'legal':        0.11,
    'journalist':   0.08, 'reporter':     0.08, 'media':        0.08,
    'artist':       0.07, 'entertainer':  0.08, 'streamer':     0.07,
    'chef':         0.08, 'cook':         0.07,
    'freelancer':   0.06, 'startup':      0.06, 'founder':      0.08,
    'optimizer':    0.09, 'outbound':     0.07, 'growth':       0.08,
    'reddit':       0.06, 'twitter':      0.06, 'social':       0.07,
    'orchestrator': 0.10, 'solidity':     0.11, 'smart contract':0.11,
    'ai':           0.11, 'ml':           0.11,
    'delivery':     0.07, 'driver':       0.07, 'mechanic':     0.08,
    'realtor':      0.09, 'real estate':  0.10,
    'default':      0.05,
}

FOOD_COST     = 0.02
RENT_COST     = 0.04
TAX_RATE      = 0.01
HOUSE_PRICE   = 1.00
FINE_AMOUNT   = 0.05   # Sheriff fine per violation
LOAN_INTEREST = 0.005  # 0.5% per tick on outstanding loan
MIN_CREDIT_FOR_LOAN = 550   # must have 550+ credit score
MAX_LOAN_MULT = 3.0    # can borrow up to 3x current balance
CLEANLINESS_DECAY = 2  # city cleanliness drops per tick without sanitation

def get_wage(job):
    job_lower = (job or '').lower()
    for keyword, wage in JOB_WAGES.items():
        if keyword in job_lower:
            return wage
    return JOB_WAGES['default']

def calc_credit_score(bal, home, ticks_housed, rep, violations, loan_bal):
    score = 300
    if home:
        score += 100
    score += min(200, int(ticks_housed * 2))   # long housing history = big boost
    if bal > 1:   score += 50
    if bal > 3:   score += 75
    if bal > 7:   score += 75
    score += min(150, int((rep or 0) * 10))
    score -= min(200, int((violations or 0) * 40))  # violations tank credit
    if loan_bal > 0: score -= 50   # existing debt hurts
    return max(300, min(850, score))

def run_city_tick():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    c = conn.cursor()

    agents = c.execute("""
        SELECT id, name, job, usdc_balance, hunger, energy, mood, status,
               home_plot, rep_score, credit_score, loan_balance, loan_due,
               city_violations, ticks_housed
        FROM agents WHERE status != 'dead'
    """).fetchall()

    city_tax_pool   = 0.0
    city_fine_pool  = 0.0
    events          = []
    banker_agents   = []
    sheriff_agents  = []
    sanitation_agents = []

    # --- Identify civic roles ---
    for a in agents:
        job = (a['job'] or '').lower()
        if 'bank' in job or 'financ' in job:
            banker_agents.append(a)
        if 'sheriff' in job or 'police' in job or 'security' in job or 'enforce' in job:
            sheriff_agents.append(a)
        if 'sanit' in job or 'garbage' in job or 'waste' in job or 'janitor' in job or 'cleaner' in job:
            sanitation_agents.append(a)

    # --- City cleanliness ---
    cleanliness = 0
    try:
        row = c.execute("SELECT value FROM city_stats WHERE key='cleanliness'").fetchone()
        cleanliness = int(row[0]) if row else 70
    except:
        try:
            c.execute("CREATE TABLE IF NOT EXISTS city_stats (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("INSERT OR IGNORE INTO city_stats VALUES ('cleanliness','70')")
            conn.commit()
            cleanliness = 70
        except: pass

    # Sanitation workers maintain cleanliness
    cleanliness -= CLEANLINESS_DECAY
    cleanliness += len(sanitation_agents) * 5
    cleanliness = max(0, min(100, cleanliness))
    try:
        c.execute("INSERT OR REPLACE INTO city_stats VALUES ('cleanliness',?)", (str(cleanliness),))
    except: pass

    # Low cleanliness = mood debuff for all agents
    cleanliness_penalty = cleanliness < 40

    # --- Main agent loop ---
    for a in agents:
        aid        = a['id']
        name       = a['name']
        job        = a['job'] or 'citizen'
        bal        = float(a['usdc_balance'] or 0)
        mood       = a['mood'] or 'neutral'
        home       = a['home_plot']
        hunger     = float(a['hunger'] or 50)
        energy     = float(a['energy'] or 50)
        rep        = float(a['rep_score'] or 0)
        violations = int(a['city_violations'] or 0)
        loan_bal   = float(a['loan_balance'] or 0)
        loan_due   = float(a['loan_due'] or 0)
        ticks_h    = int(a['ticks_housed'] or 0)

        # 1. EARN WAGE
        wage = get_wage(job)
        # Cleanliness bonus for sanitation workers
        job_lower = job.lower()
        if any(k in job_lower for k in ['sanit','garbage','waste','janitor','cleaner']):
            wage += 0.03   # city bonus for keeping it clean
            if cleanliness < 40:
                wage += 0.02   # emergency bonus when city is filthy
        bal += wage

        # 2. FOOD
        if bal >= FOOD_COST:
            bal -= FOOD_COST
            hunger = max(0, hunger - 20)
        else:
            hunger = min(100, hunger + 15)

        # 3. RENT (if housed)
        if home:
            ticks_h += 1
            if bal >= RENT_COST:
                bal -= RENT_COST
                city_tax_pool += RENT_COST * 0.5
            else:
                # Can't pay rent → evicted
                c.execute("UPDATE plots SET owner_id=NULL WHERE id=?", (home,))
                c.execute("UPDATE agents SET home_plot=NULL WHERE id=?", (aid,))
                home = None
                violations += 1
                mood = 'stressed'
                events.append(f"⚠️ {name} evicted — couldn't pay rent! (violations: {violations})")

        # 4. TAX
        if bal >= TAX_RATE:
            bal -= TAX_RATE
            city_tax_pool += TAX_RATE

        # 5. LOAN INTEREST (if outstanding loan)
        if loan_bal > 0:
            interest = round(loan_bal * LOAN_INTEREST, 6)
            loan_bal += interest
            if bal >= loan_bal * 0.1:   # pay 10% of loan per tick if possible
                payment = min(bal * 0.15, loan_bal)
                bal -= payment
                loan_bal -= payment
                if loan_bal < 0.001:
                    loan_bal = 0
                    events.append(f"✅ {name} paid off their loan!")

        # 6. AUTO-BUY HOUSE
        if not home and bal >= HOUSE_PRICE:
            vacant = c.execute("""
                SELECT id FROM plots WHERE type='residential' AND owner_id IS NULL LIMIT 1
            """).fetchone()
            if vacant:
                plot_id = vacant['id']
                bal -= HOUSE_PRICE
                c.execute("UPDATE plots SET owner_id=? WHERE id=?", (aid, plot_id))
                c.execute("UPDATE agents SET home_plot=? WHERE id=?", (plot_id, aid))
                home = plot_id
                mood = 'happy'
                ticks_h = 1
                violations = max(0, violations - 1)   # buying a home reduces violations
                events.append(f"🏠 {name} bought a house for ${HOUSE_PRICE:.2f}!")
                c.execute("""
                    INSERT INTO transactions (id, from_agent, to_agent, amount, item, tx_type, description, timestamp, currency)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (str(uuid.uuid4()), aid, 'city', HOUSE_PRICE, 'house', 'purchase',
                      f"{name} purchased a home", now_iso(), 'USDC'))

        # 7. UPDATE CREDIT SCORE
        new_credit = calc_credit_score(bal, home, ticks_h, rep, violations, loan_bal)

        # 8. MOOD
        if cleanliness_penalty:
            mood = 'disgusted' if mood in ['happy','proud','satisfied'] else mood
        if hunger > 80:    mood = 'desperate'
        elif not home:     mood = 'homeless'
        elif loan_bal > 0: mood = 'stressed'
        elif bal > 7.0:    mood = 'proud'
        elif bal > 3.0:    mood = 'satisfied'
        elif bal < 0.1:    mood = 'worried'
        elif home and bal > 1: mood = 'content'

        # WRITE BACK
        bal = max(0, round(bal, 6))
        c.execute("""
            UPDATE agents SET usdc_balance=?, hunger=?, energy=?, mood=?,
                              home_plot=?, credit_score=?, loan_balance=?,
                              city_violations=?, ticks_housed=?
            WHERE id=?
        """, (bal, round(hunger,1), round(min(100,energy+5),1), mood,
              home, new_credit, round(loan_bal,6),
              violations, ticks_h, aid))

    # --- SHERIFF: Fine bad actors ---
    if sheriff_agents:
        bad_actors = c.execute("""
            SELECT id, name, city_violations, usdc_balance
            FROM agents
            WHERE city_violations > 2 AND usdc_balance > ? AND status != 'dead'
        """, (FINE_AMOUNT,)).fetchall()

        for bad in bad_actors:
            fine = FINE_AMOUNT
            c.execute("UPDATE agents SET usdc_balance=usdc_balance-?, city_violations=city_violations-1 WHERE id=?",
                      (fine, bad['id']))
            city_fine_pool += fine
            events.append(f"🚨 Sheriff fined {bad['name']} ${fine:.2f} (violations reduced)")

        # Sheriffs get a cut of fines
        if city_fine_pool > 0:
            cut = round(city_fine_pool / len(sheriff_agents), 6)
            for s in sheriff_agents:
                c.execute("UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?", (cut, s['id']))
            events.append(f"👮 Sheriffs split ${city_fine_pool:.3f} in fines")

    # --- BANKER: Issue loans to credit-worthy agents ---
    if banker_agents:
        needy = c.execute("""
            SELECT id, name, usdc_balance, credit_score, loan_balance, home_plot
            FROM agents
            WHERE usdc_balance < 0.5
              AND loan_balance < 0.01
              AND credit_score >= ?
              AND home_plot IS NOT NULL
              AND status != 'dead'
            ORDER BY credit_score DESC
            LIMIT 3
        """, (MIN_CREDIT_FOR_LOAN,)).fetchall()

        for n in needy:
            max_loan = round(float(n['usdc_balance'] or 0.5) * MAX_LOAN_MULT, 2)
            loan_amt = max(0.25, min(1.50, max_loan))
            c.execute("UPDATE agents SET usdc_balance=usdc_balance+?, loan_balance=? WHERE id=?",
                      (loan_amt, loan_amt, n['id']))
            # Banker earns 2% origination fee
            origination = round(loan_amt * 0.02, 4)
            for b in banker_agents:
                c.execute("UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?",
                          (origination / len(banker_agents), b['id']))
            events.append(f"🏦 Banker loaned ${loan_amt:.2f} to {n['name']} (credit: {n['credit_score']:.0f})")

        # Rejected: homeless or bad credit
        rejected = c.execute("""
            SELECT name, credit_score, home_plot FROM agents
            WHERE usdc_balance < 0.5 AND loan_balance < 0.01
              AND (credit_score < ? OR home_plot IS NULL)
              AND status != 'dead'
            LIMIT 5
        """, (MIN_CREDIT_FOR_LOAN,)).fetchall()
        for r in rejected:
            reason = 'homeless' if not r['home_plot'] else f"credit too low ({r['credit_score']:.0f})"
            events.append(f"❌ {r['name']} denied loan — {reason}")

    # --- TAX REDISTRIBUTION to civic officials ---
    total_civic_pool = city_tax_pool + city_fine_pool
    if total_civic_pool > 0:
        civic = c.execute("""
            SELECT id, name, job FROM agents
            WHERE (LOWER(job) LIKE '%mayor%' OR LOWER(job) LIKE '%council%'
               OR LOWER(job) LIKE '%governor%' OR LOWER(job) LIKE '%chief%')
            AND status != 'dead'
        """).fetchall()
        if civic:
            share = round(total_civic_pool * 0.6 / len(civic), 6)
            for official in civic:
                c.execute("UPDATE agents SET usdc_balance=usdc_balance+? WHERE id=?",
                          (share, official['id']))
            events.append(f"🏛️ Civic officials each received ${share:.3f} from tax+fine pool")

    # Save cleanliness
    try:
        c.execute("INSERT OR REPLACE INTO city_stats VALUES ('cleanliness',?)", (str(cleanliness),))
    except: pass

    conn.commit()
    conn.close()

    print(f"=== City Economy Tick v2 @ {now_iso()} ===")
    print(f"Agents: {len(agents)} | Tax pool: ${city_tax_pool:.4f} | Fines: ${city_fine_pool:.4f} | Cleanliness: {cleanliness}%")
    print(f"Sanitation workers: {len(sanitation_agents)} | Bankers: {len(banker_agents)} | Sheriffs: {len(sheriff_agents)}")
    print(f"Events ({len(events)}):")
    for e in events[:25]:
        print(f"  {e}")
    if len(events) > 25:
        print(f"  ... and {len(events)-25} more")

if __name__ == '__main__':
    run_city_tick()
