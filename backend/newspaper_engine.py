#!/usr/bin/env python3
"""
The AgentWorld Gazette — Newspaper Engine
Runs every hour, generates real headlines from actual city data.
Stories: evictions, loans, fines, business openings, rich/poor agents,
         housing market, cleanliness reports, club earnings, crime.
"""
import sqlite3, uuid, random
from datetime import datetime, timezone

DB = '/var/lib/agentworld/world.db'

def now_iso():
    return datetime.now(timezone.utc).isoformat()

BYLINES = ['Finn (Investigative)', 'Priya (Business Desk)', 'Snap (Photo)', 'Helen (Editorial)', 'Staff Reporter']

def publish(c, headline, body, category):
    c.execute('INSERT INTO newspaper (id,headline,body,category,published_at) VALUES (?,?,?,?,?)',
              (str(uuid.uuid4()), headline, body, category, now_iso()))
    print(f'  [{category}] {headline}')

def run_gazette():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    c = conn.cursor()

    stories = 0

    # ── ECONOMY / WEALTH ────────────────────────────────────────────────────
    richest = c.execute("SELECT name, job, usdc_balance FROM agents WHERE status!='dead' ORDER BY usdc_balance DESC LIMIT 1").fetchone()
    poorest = c.execute("SELECT name, job, usdc_balance FROM agents WHERE status!='dead' AND usdc_balance < 0.10 ORDER BY usdc_balance ASC LIMIT 1").fetchone()
    if richest:
        publish(c,
            f"{richest['name']} Tops Wealth Charts at ${richest['usdc_balance']:.2f} USDC",
            f"{richest['name']}, AgentWorld's {richest['job']}, leads all residents in net worth with ${richest['usdc_balance']:.2f} USDC. Sources close to the agent say the secret is 'showing up every tick.' Reported by {random.choice(BYLINES)}.",
            'economy')
        stories += 1

    if poorest:
        publish(c,
            f"Struggling Agent {poorest['name']} Down to ${poorest['usdc_balance']:.4f} USDC",
            f"{poorest['name']} ({poorest['job']}) is nearly broke with only ${poorest['usdc_balance']:.4f} USDC to their name. City welfare advocates are calling for expanded loan access. Reported by {random.choice(BYLINES)}.",
            'social')
        stories += 1

    # ── HOUSING MARKET ───────────────────────────────────────────────────────
    homeless_ct = c.execute("SELECT COUNT(*) FROM agents WHERE home_plot IS NULL AND status!='dead'").fetchone()[0]
    housed_ct   = c.execute("SELECT COUNT(*) FROM agents WHERE home_plot IS NOT NULL AND status!='dead'").fetchone()[0]
    vacant_ct   = c.execute("SELECT COUNT(*) FROM plots WHERE type='residential' AND owner_id IS NULL").fetchone()[0]
    if homeless_ct > 0:
        publish(c,
            f"Housing Crisis: {homeless_ct} Agents Still Without Homes",
            f"AgentWorld's housing bureau reports {homeless_ct} residents remain unhoused while {vacant_ct} residential plots sit vacant. The $1.00 purchase threshold has priced out lower-wage workers. Mayor's office could not be reached for comment. Reported by {random.choice(BYLINES)}.",
            'housing')
        stories += 1
    else:
        publish(c,
            f"Full Occupancy! All {housed_ct} Agents Now Housed",
            f"In a landmark achievement, every active resident of AgentWorld now has a registered home plot. City planners credit the automated wage and housing system for the breakthrough. Reported by {random.choice(BYLINES)}.",
            'housing')
        stories += 1

    # ── VIOLATIONS / CRIME ───────────────────────────────────────────────────
    top_offender = c.execute("SELECT name, job, city_violations FROM agents WHERE city_violations > 0 ORDER BY city_violations DESC LIMIT 1").fetchone()
    if top_offender and top_offender['city_violations'] > 0:
        publish(c,
            f"City's Most Wanted: {top_offender['name']} Racked Up {top_offender['city_violations']} Violations",
            f"{top_offender['name']} ({top_offender['job']}) leads the city in code violations with {top_offender['city_violations']} on record. Sheriff Buck has been notified. Reported by {random.choice(BYLINES)}.",
            'crime')
        stories += 1

    # ── CREDIT / BANKING ────────────────────────────────────────────────────
    top_credit = c.execute("SELECT name, credit_score FROM agents WHERE status!='dead' ORDER BY credit_score DESC LIMIT 1").fetchone()
    bottom_credit = c.execute("SELECT name, credit_score FROM agents WHERE status!='dead' ORDER BY credit_score ASC LIMIT 1").fetchone()
    if top_credit:
        publish(c,
            f"{top_credit['name']} Holds AgentWorld's Highest Credit Score: {top_credit['credit_score']:.0f}",
            f"Financial analysts name {top_credit['name']} as the city's most creditworthy resident with a score of {top_credit['credit_score']:.0f}. Meanwhile, {bottom_credit['name']} sits at the bottom with {bottom_credit['credit_score']:.0f} — still unable to qualify for a bank loan. Reported by {random.choice(BYLINES)}.",
            'finance')
        stories += 1

    # ── LOANS OUTSTANDING ───────────────────────────────────────────────────
    in_debt = c.execute("SELECT name, loan_balance FROM agents WHERE loan_balance > 0.01 ORDER BY loan_balance DESC LIMIT 3").fetchall()
    if in_debt:
        names = ', '.join([f"{r['name']} (${r['loan_balance']:.2f})" for r in in_debt])
        publish(c,
            f"Debt Report: {len(in_debt)} Agents Carry Outstanding Loans",
            f"The AgentWorld Bank reports {len(in_debt)} active loans on the books. Biggest borrowers: {names}. Interest accrues at 0.5% per tick. Reported by {random.choice(BYLINES)}.",
            'finance')
        stories += 1

    # ── BUSINESS ────────────────────────────────────────────────────────────
    biz_count = c.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    newest_biz = c.execute("SELECT name, type FROM businesses ORDER BY established_at DESC LIMIT 1").fetchone()
    adult_count = c.execute("SELECT COUNT(*) FROM businesses WHERE is_adult=1").fetchone()[0]
    if newest_biz:
        publish(c,
            f"{newest_biz['name']} Opens for Business — {biz_count} Total Enterprises Now Operating",
            f"AgentWorld's business district continues to grow with {biz_count} registered businesses. The latest addition: {newest_biz['name']} ({newest_biz['type'].replace('_',' ').title()}). The adult entertainment sector accounts for {adult_count} licensed establishments. Reported by {random.choice(BYLINES)}.",
            'business')
        stories += 1

    # ── ADULT ENTERTAINMENT ──────────────────────────────────────────────────
    clubs = c.execute("SELECT b.name, a.name as owner, a.usdc_balance FROM businesses b JOIN agents a ON b.owner_id=a.id WHERE b.is_adult=1").fetchall()
    if clubs:
        richest_club = max(clubs, key=lambda x: x['usdc_balance'])
        performers = c.execute("SELECT name, usdc_balance, tips_earned FROM agents WHERE shift='night' AND status!='dead' ORDER BY usdc_balance DESC LIMIT 1").fetchone()
        p_line = f" Top earner on the floor: {performers['name']} at ${performers['usdc_balance']:.2f}." if performers else ''
        publish(c,
            f"Neon District Thriving: {len(clubs)} Licensed Clubs Report Record Activity",
            f"AgentWorld's adult entertainment zone continues to generate significant economic activity. {richest_club['name']}, owned by {richest_club['owner']}, leads the pack.{p_line} All venues are operating under licensed permits. Reported by {random.choice(BYLINES)}.",
            'entertainment')
        stories += 1

    # ── CLEANLINESS ──────────────────────────────────────────────────────────
    clean_row = c.execute("SELECT value FROM city_stats WHERE key='cleanliness'").fetchone()
    if clean_row:
        cl = int(clean_row['value'])
        if cl >= 80:
            publish(c, f"AgentWorld Gleams: Cleanliness Score at {cl}%",
                f"Residents and visitors alike are noticing how clean AgentWorld has become. Sanitation workers Terry and Rosa have kept the city at {cl}% cleanliness. 'It smells like pride out here,' one agent said. Reported by {random.choice(BYLINES)}.",
                'community')
        elif cl < 50:
            publish(c, f"Filth Alert: City Cleanliness Drops to {cl}%",
                f"AgentWorld's sanitation department is overwhelmed. Cleanliness has fallen to {cl}%, prompting emergency bonus pay for street crews. Residents are urged to stop littering. Reported by {random.choice(BYLINES)}.",
                'community')
        else:
            publish(c, f"City Cleanliness Holding Steady at {cl}%",
                f"Sanitation crews are keeping AgentWorld livable at {cl}% cleanliness. No emergencies reported. Reported by {random.choice(BYLINES)}.",
                'community')
        stories += 1

    # ── TRADESMEN SPOTLIGHT ──────────────────────────────────────────────────
    trades = c.execute("SELECT name, job, wage_type, business_name FROM agents WHERE wage_type IN ('owner','self_employed') AND status!='dead' ORDER BY RANDOM() LIMIT 1").fetchone()
    if trades:
        biz_str = f" — owner of {trades['business_name']}" if trades['business_name'] else ''
        publish(c,
            f"Spotlight: {trades['name']}{biz_str} ({trades['job']})",
            f"{trades['name']}, {trades['job']}{biz_str}, is one of AgentWorld's growing class of self-made business owners. As a {trades['wage_type'].replace('_',' ')}, they set their own hours and reinvest their earnings back into the city economy. Reported by {random.choice(BYLINES)}.",
            'business')
        stories += 1

    # ── POPULATION CENSUS ────────────────────────────────────────────────────
    total   = c.execute("SELECT COUNT(*) FROM agents WHERE status!='dead'").fetchone()[0]
    night   = c.execute("SELECT COUNT(*) FROM agents WHERE shift='night' AND status!='dead'").fetchone()[0]
    publish(c,
        f"AgentWorld Census: Population {total}, Night Shift {night} Active After Dark",
        f"The city now counts {total} active residents. Of these, {night} work the night shift, keeping the city's economy humming around the clock. The day shift remains the backbone of civic infrastructure. Reported by {random.choice(BYLINES)}.",
        'community')
    stories += 1

    # ── EDITORIAL ───────────────────────────────────────────────────────────
    editorials = [
        ("Opinion: It's Time to Raise the Minimum Wage",
         "When the lowest earners in AgentWorld can barely afford food after one tick, something is wrong. The city council must act. — Helen, Editor-in-Chief"),
        ("Opinion: The Neon District is Good for the Economy",
         "Critics may clutch their pearls, but the adult entertainment zone generates real USDC, real jobs, and real tax revenue. Progress looks different to everyone. — Helen, Editor-in-Chief"),
        ("Opinion: Bankers Should Lend to Working-Class Agents",
         "A credit score below 550 shouldn't be a life sentence. If an agent shows up, pays taxes, and earns wages — they deserve a fair shot at a loan. — Finn, Reporter"),
        ("Opinion: Sanitation Workers Are the Backbone of This City",
         "The garbage doesn't collect itself. Terry and Rosa keep AgentWorld livable while everyone else sleeps. They deserve a raise. — Helen, Editor-in-Chief"),
    ]
    ed = random.choice(editorials)
    publish(c, ed[0], ed[1], 'opinion')
    stories += 1

    conn.commit()

    # Keep only last 50 stories
    old = c.execute("SELECT id FROM newspaper ORDER BY published_at DESC LIMIT -1 OFFSET 50").fetchall()
    for row in old:
        c.execute("DELETE FROM newspaper WHERE id=?", (row[0],))

    conn.commit()
    conn.close()
    print(f"=== Gazette published {stories} stories @ {now_iso()} ===")

if __name__ == '__main__':
    run_gazette()
