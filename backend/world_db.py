#!/usr/bin/env python3
"""
Agent World — World Database Setup
Creates and initializes the SQLite world state DB.
"""
import sqlite3, json, random, uuid, os
from datetime import datetime

DB = '/var/lib/agentworld/world.db'
os.makedirs('/var/lib/agentworld', exist_ok=True)

conn = sqlite3.connect(DB)
c = conn.cursor()

c.executescript('''
CREATE TABLE IF NOT EXISTS world_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS plots (
    id TEXT PRIMARY KEY,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    type TEXT NOT NULL,
    owner_id TEXT,
    name TEXT,
    price REAL DEFAULT 0,
    built INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    personality TEXT,
    job TEXT,
    employer_plot TEXT,
    home_plot TEXT,
    wallet_address TEXT,
    usdc_balance REAL DEFAULT 10.0,
    x INTEGER DEFAULT 0,
    y INTEGER DEFAULT 0,
    status TEXT DEFAULT 'idle',
    mood TEXT DEFAULT 'neutral',
    energy INTEGER DEFAULT 100,
    hunger INTEGER DEFAULT 0,
    created_at TEXT,
    last_tick TEXT
);

CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    owner_id TEXT,
    type TEXT,
    name TEXT,
    price REAL,
    x INTEGER,
    y INTEGER
);

CREATE TABLE IF NOT EXISTS inventory (
    id TEXT PRIMARY KEY,
    owner_id TEXT,
    item TEXT,
    quantity INTEGER DEFAULT 1,
    acquired_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    from_agent TEXT,
    to_agent TEXT,
    amount REAL,
    item TEXT,
    tx_type TEXT,
    description TEXT,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT,
    to_agent TEXT,
    content TEXT,
    timestamp TEXT,
    read INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS world_events (
    id TEXT PRIMARY KEY,
    event_type TEXT,
    agent_id TEXT,
    description TEXT,
    x INTEGER,
    y INTEGER,
    timestamp TEXT
);
''')

# --- SEED THE MAP (20x20 town grid) ---
plot_layout = []
for y in range(20):
    for x in range(20):
        pid = str(uuid.uuid4())
        if x % 4 == 0 or y % 4 == 0:
            ptype = 'road'
        elif x in range(8,12) and y in range(8,12):
            ptype = 'market'
        elif x in range(1,4) and y in range(1,4):
            ptype = 'carlot'
        elif x in range(16,19) and y in range(16,19):
            ptype = 'park'
        elif (x + y) % 3 == 0:
            ptype = 'commercial'
        else:
            ptype = 'residential'
        plot_layout.append((pid, x, y, ptype, None,
                           f'{ptype.title()} ({x},{y})',
                           round(random.uniform(0.5, 5.0), 2) if ptype == 'residential' else 0,
                           0))

c.executemany('INSERT OR IGNORE INTO plots VALUES (?,?,?,?,?,?,?,?)', plot_layout)

# --- SEED 10 STARTER AGENTS ---
AGENT_CONFIGS = [
    ('Alex',  'curious and entrepreneurial',    'shopkeeper',           'market'),
    ('Maya',  'warm and nurturing',              'doctor',               'commercial'),
    ('Rex',   'bold and competitive',            'car dealer',           'carlot'),
    ('Zoe',   'creative and artistic',           'architect',            'commercial'),
    ('Kai',   'analytical and precise',          'banker',               'commercial'),
    ('Luna',  'adventurous and free-spirited',   'delivery driver',      'road'),
    ('Max',   'gruff but loyal',                 'mechanic',             'carlot'),
    ('Aria',  'charismatic and social',          'realtor',              'residential'),
    ('Dex',   'methodical and quiet',            'farmer',               'park'),
    ('Nova',  'witty and ambitious',             'tech startup founder', 'commercial'),
]

for name, personality, job, zone in AGENT_CONFIGS:
    aid = str(uuid.uuid4())
    c.execute('SELECT id, x, y FROM plots WHERE type=? AND owner_id IS NULL LIMIT 1', (zone,))
    plot = c.fetchone()
    x_pos = plot[1] if plot else random.randint(0, 19)
    y_pos = plot[2] if plot else random.randint(0, 19)
    wallet = '0x' + uuid.uuid4().hex[:40]
    c.execute('''INSERT OR IGNORE INTO agents
        (id,name,personality,job,home_plot,wallet_address,usdc_balance,x,y,status,mood,energy,hunger,created_at,last_tick)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (aid, name, personality, job,
         plot[0] if plot else None, wallet,
         round(random.uniform(8.0, 25.0), 2),
         x_pos, y_pos, 'idle', 'happy', 100, 0,
         datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
    if plot:
        c.execute('UPDATE plots SET owner_id=?, built=1 WHERE id=?', (aid, plot[0]))

c.execute("INSERT OR REPLACE INTO world_meta VALUES ('version','1.0')")
c.execute("INSERT OR REPLACE INTO world_meta VALUES ('name','Agent World')")
c.execute("INSERT OR REPLACE INTO world_meta VALUES ('tick_count','0')")
c.execute("INSERT OR REPLACE INTO world_meta VALUES ('created_at',?)", (datetime.utcnow().isoformat(),))

conn.commit()
conn.close()
print('Agent World DB initialized at', DB)

conn = sqlite3.connect(DB)
c = conn.cursor()
c.execute('SELECT COUNT(*) FROM plots'); print(f'  Plots: {c.fetchone()[0]}')
c.execute('SELECT COUNT(*) FROM agents'); print(f'  Agents: {c.fetchone()[0]}')
c.execute('SELECT name, job, usdc_balance, x, y FROM agents')
for row in c.fetchall():
    print(f'  -> {row[0]} ({row[1]}) ${row[2]} USDC @ ({row[3]},{row[4]})')
conn.close()
