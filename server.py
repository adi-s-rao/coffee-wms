"""
RFID Coffee WMS — Backend v2
Supports SQLite locally and PostgreSQL on Render.

Local:   uvicorn server:app --host 0.0.0.0 --port 8000 --reload
Render:  set DATABASE_URL env var to your PostgreSQL URL
"""

import os, time, pathlib
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///wms.db")
USE_POSTGRES  = DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PHONE_DIR = pathlib.Path(__file__).parent / "phone"

# ── Database abstraction ──────────────────────────────────────────────────────

def get_db():
    if USE_POSTGRES:
        import psycopg2, psycopg2.extras
        url = DATABASE_URL
        # Render gives postgres:// but psycopg2 needs postgresql://
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn, "pg"
    else:
        import sqlite3
        conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"

def fetchall(cursor):
    if USE_POSTGRES:
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]
    else:
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

def fetchone(cursor):
    if USE_POSTGRES:
        row = cursor.fetchone()
        if row is None: return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
    else:
        row = cursor.fetchone()
        return dict(row) if row else None

def placeholder(n=1):
    """Return ? for SQLite, %s for Postgres"""
    p = "%s" if USE_POSTGRES else "?"
    return ", ".join([p] * n)

def ph():
    return "%s" if USE_POSTGRES else "?"

# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS purchase_orders (
    id          TEXT PRIMARY KEY,
    coffee_type TEXT NOT NULL,
    total_bags  INTEGER NOT NULL,
    total_kg    REAL NOT NULL,
    status      TEXT DEFAULT 'pending',
    created_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS collies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    weight      REAL NOT NULL,
    wristband_uid TEXT,
    created_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS sack_tags (
    uid         TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    created_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS bags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    po_id       TEXT NOT NULL,
    tag_uid     TEXT UNIQUE,
    tag_label   TEXT,
    collie_id   TEXT,
    collie_name TEXT,
    collie_wt   REAL,
    gross_wt    REAL,
    net_wt      REAL,
    status      TEXT DEFAULT 'pending',
    inwarded_at REAL,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
);

CREATE TABLE IF NOT EXISTS gate_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    active      INTEGER DEFAULT 0,
    command     TEXT DEFAULT 'off',
    po_id       TEXT,
    updated_at  REAL DEFAULT (unixepoch())
);

INSERT OR IGNORE INTO gate_state (id, active, command) VALUES (1, 0, 'off');
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS purchase_orders (
    id          TEXT PRIMARY KEY,
    coffee_type TEXT NOT NULL,
    total_bags  INTEGER NOT NULL,
    total_kg    REAL NOT NULL,
    status      TEXT DEFAULT 'pending',
    created_at  DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE TABLE IF NOT EXISTS collies (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    weight        REAL NOT NULL,
    wristband_uid TEXT,
    created_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE TABLE IF NOT EXISTS sack_tags (
    uid        TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE TABLE IF NOT EXISTS bags (
    id          SERIAL PRIMARY KEY,
    po_id       TEXT NOT NULL,
    tag_uid     TEXT UNIQUE,
    tag_label   TEXT,
    collie_id   TEXT,
    collie_name TEXT,
    collie_wt   REAL,
    gross_wt    REAL,
    net_wt      REAL,
    status      TEXT DEFAULT 'pending',
    inwarded_at DOUBLE PRECISION,
    FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
);

CREATE TABLE IF NOT EXISTS gate_state (
    id         INTEGER PRIMARY KEY,
    active     INTEGER DEFAULT 0,
    command    TEXT DEFAULT 'off',
    po_id      TEXT,
    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

INSERT INTO gate_state (id, active, command)
VALUES (1, 0, 'off')
ON CONFLICT (id) DO NOTHING;
"""

def init_db():
    conn, kind = get_db()
    cur = conn.cursor()
    schema = SCHEMA_PG if USE_POSTGRES else SCHEMA_SQLITE
    if USE_POSTGRES:
        for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
            cur.execute(stmt)
    else:
        conn.executescript(schema)
    conn.commit()

    # Seed demo PO
    p = ph()
    cur.execute(f"SELECT COUNT(*) FROM purchase_orders WHERE id = {p}", ("PO-001",))
    row = cur.fetchone()
    count = row[0] if isinstance(row, (list, tuple)) else list(row.values())[0]
    if count == 0:
        cur.execute(f"""
            INSERT INTO purchase_orders (id, coffee_type, total_bags, total_kg, status)
            VALUES ({placeholder(5)})
        """, ("PO-001", "Arabica — Coorg", 3, 180.0, "pending"))
        for _ in range(3):
            cur.execute(f"""
                INSERT INTO bags (po_id, status) VALUES ({placeholder(2)})
            """, ("PO-001", "pending"))
    conn.commit()
    conn.close()

init_db()

# ── Gate helpers ──────────────────────────────────────────────────────────────

def set_gate(active: bool, command: str, po_id=None):
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    cur.execute(f"""
        UPDATE gate_state
        SET active={p}, command={p}, po_id={p}, updated_at={p}
        WHERE id=1
    """, (1 if active else 0, command, po_id, time.time()))
    conn.commit()
    conn.close()

def get_gate():
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM gate_state WHERE id=1")
    row = fetchone(cur)
    conn.close()
    return row

# ── Models ────────────────────────────────────────────────────────────────────

class StartInwarding(BaseModel):
    po_id: str

class GateRead(BaseModel):
    uid: str

class RegisterCollie(BaseModel):
    id:            str
    name:          str
    weight:        float
    wristband_uid: Optional[str] = None

class RegisterSackTag(BaseModel):
    uid:   str
    label: str

class WristbandScan(BaseModel):
    po_id:       str
    tag_uid:     str
    collie_id:   str
    collie_name: str
    collie_wt:   float
    gross_wt:    float

class CompleteInwarding(BaseModel):
    po_id: str

class CreatePO(BaseModel):
    id:          str
    coffee_type: str
    total_bags:  int
    total_kg:    float

# ── Routes ────────────────────────────────────────────────────────────────────

# Serve phone UI
app.mount("/phone", StaticFiles(directory=str(PHONE_DIR), html=True), name="phone")

@app.get("/")
def serve_ui():
    return FileResponse(PHONE_DIR / "index.html")

# ── Gate ──────────────────────────────────────────────────────────────────────

@app.get("/api/gate-command")
def gate_command():
    g = get_gate()
    return {"gate_active": bool(g["active"]), "command": g["command"]}

@app.post("/api/gate-read")
def gate_read(body: GateRead):
    uid  = body.uid.upper().strip()
    gate = get_gate()

    if not gate["active"]:
        return {"status": "ignored", "reason": "gate not active"}

    po_id = gate["po_id"]
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()

    # Duplicate check — already fully inwarded
    cur.execute(f"SELECT id FROM bags WHERE tag_uid={p} AND status='inwarded'", (uid,))
    if fetchone(cur):
        conn.close()
        set_gate(True, "alarm", po_id)
        return {"status": "alarm", "reason": "tag already inwarded", "uid": uid}

    # Already scanned this session
    cur.execute(f"SELECT id FROM bags WHERE tag_uid={p} AND status='scanned'", (uid,))
    if fetchone(cur):
        conn.close()
        return {"status": "already_scanned", "uid": uid}

    # Look up human label for this tag
    cur.execute(f"SELECT label FROM sack_tags WHERE uid={p}", (uid,))
    tag_row   = fetchone(cur)
    tag_label = tag_row["label"] if tag_row else uid

    # Assign to next pending bag slot
    cur.execute(f"""
        SELECT id FROM bags
        WHERE po_id={p} AND status='pending' AND tag_uid IS NULL
        ORDER BY id LIMIT 1
    """, (po_id,))
    next_bag = fetchone(cur)

    if not next_bag:
        conn.close()
        return {"status": "error", "reason": "no pending bag slots"}

    cur.execute(f"""
        UPDATE bags SET tag_uid={p}, tag_label={p}, status='scanned'
        WHERE id={p}
    """, (uid, tag_label, next_bag["id"]))
    conn.commit()

    cur.execute(f"""
        SELECT COUNT(*) as c FROM bags
        WHERE po_id={p} AND status IN ('scanned','inwarded')
    """, (po_id,))
    scanned = fetchone(cur)["c"]

    cur.execute(f"SELECT total_bags FROM purchase_orders WHERE id={p}", (po_id,))
    total = fetchone(cur)["total_bags"]

    conn.close()
    set_gate(True, "green", po_id)

    return {
        "status":  "ok",
        "uid":     uid,
        "label":   tag_label,
        "scanned": scanned,
        "total":   total
    }

# ── Purchase Orders ───────────────────────────────────────────────────────────

@app.get("/api/purchase-orders")
def list_pos():
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM purchase_orders ORDER BY created_at DESC")
    pos = fetchall(cur)
    result = []
    for po in pos:
        p = ph()
        cur.execute(f"SELECT * FROM bags WHERE po_id={p} ORDER BY id", (po["id"],))
        bags = fetchall(cur)
        po["bags"]         = bags
        po["inwarded_bags"] = sum(1 for b in bags if b["status"] == "inwarded")
        po["scanned_bags"]  = sum(1 for b in bags if b["status"] == "scanned")
        po["inwarded_kg"]   = sum(b["net_wt"] or 0 for b in bags)
        result.append(po)
    conn.close()
    return result

@app.post("/api/purchase-orders")
def create_po(body: CreatePO):
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    cur.execute(f"""
        INSERT INTO purchase_orders (id, coffee_type, total_bags, total_kg, status)
        VALUES ({placeholder(5)})
    """, (body.id, body.coffee_type, body.total_bags, body.total_kg, "pending"))
    for _ in range(body.total_bags):
        cur.execute(f"INSERT INTO bags (po_id, status) VALUES ({placeholder(2)})",
                    (body.id, "pending"))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── Collie registration ───────────────────────────────────────────────────────

@app.get("/api/collies")
def list_collies():
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM collies ORDER BY created_at DESC")
    rows = fetchall(cur)
    conn.close()
    return rows

@app.post("/api/collies")
def register_collie(body: RegisterCollie):
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    if USE_POSTGRES:
        cur.execute(f"""
            INSERT INTO collies (id, name, weight, wristband_uid)
            VALUES ({placeholder(4)})
            ON CONFLICT (id) DO UPDATE
            SET name={p}, weight={p}, wristband_uid={p}
        """, (body.id, body.name, body.weight, body.wristband_uid,
              body.name, body.weight, body.wristband_uid))
    else:
        cur.execute(f"""
            INSERT OR REPLACE INTO collies (id, name, weight, wristband_uid)
            VALUES ({placeholder(4)})
        """, (body.id, body.name, body.weight, body.wristband_uid))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/collies/{collie_id}")
def delete_collie(collie_id: str):
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM collies WHERE id={ph()}", (collie_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── Sack tag registration ─────────────────────────────────────────────────────

@app.get("/api/sack-tags")
def list_sack_tags():
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sack_tags ORDER BY label")
    rows = fetchall(cur)
    conn.close()
    return rows

@app.post("/api/sack-tags")
def register_sack_tag(body: RegisterSackTag):
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    if USE_POSTGRES:
        cur.execute(f"""
            INSERT INTO sack_tags (uid, label)
            VALUES ({placeholder(2)})
            ON CONFLICT (uid) DO UPDATE SET label={p}
        """, (body.uid, body.label, body.label))
    else:
        cur.execute(f"""
            INSERT OR REPLACE INTO sack_tags (uid, label)
            VALUES ({placeholder(2)})
        """, (body.uid, body.label))
    conn.commit()
    conn.close()
    return {"status": "ok", "uid": body.uid, "label": body.label}

@app.delete("/api/sack-tags/{uid}")
def delete_sack_tag(uid: str):
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM sack_tags WHERE uid={ph()}", (uid,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── Inwarding ─────────────────────────────────────────────────────────────────

@app.post("/api/start-inwarding")
def start_inwarding(body: StartInwarding):
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM purchase_orders WHERE id={ph()}", (body.po_id,))
    po = fetchone(cur)
    conn.close()
    if not po:
        raise HTTPException(404, "PO not found")
    set_gate(True, "green", body.po_id)
    return {"status": "ok"}

@app.get("/api/inwarding-status/{po_id}")
def inwarding_status(po_id: str):
    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    cur.execute(f"SELECT * FROM purchase_orders WHERE id={p}", (po_id,))
    po = fetchone(cur)
    if not po:
        conn.close()
        raise HTTPException(404, "PO not found")
    cur.execute(f"SELECT * FROM bags WHERE po_id={p} ORDER BY id", (po_id,))
    bags = fetchall(cur)
    gate = get_gate()
    inwarded_kg  = sum(b["net_wt"] or 0 for b in bags if b["status"] == "inwarded")
    scanned_bags = [b for b in bags if b["status"] == "scanned"]
    conn.close()
    return {
        "po":           po,
        "bags":         bags,
        "gate":         gate,
        "inwarded_kg":  round(inwarded_kg, 2),
        "scanned_bags": scanned_bags,
    }

@app.post("/api/wristband-scan")
def wristband_scan(body: WristbandScan):
    net_wt = round(body.gross_wt - body.collie_wt, 2)
    if net_wt <= 0:
        raise HTTPException(400, "Net weight is zero or negative")

    conn, _ = get_db()
    cur = conn.cursor()
    p = ph()
    cur.execute(f"SELECT * FROM bags WHERE tag_uid={p} AND status='scanned'", (body.tag_uid,))
    bag = fetchone(cur)
    if not bag:
        conn.close()
        raise HTTPException(404, f"No scanned bag with UID {body.tag_uid}")

    cur.execute(f"""
        UPDATE bags
        SET collie_id={p}, collie_name={p}, collie_wt={p},
            gross_wt={p}, net_wt={p}, status='inwarded', inwarded_at={p}
        WHERE id={p}
    """, (body.collie_id, body.collie_name, body.collie_wt,
          body.gross_wt, net_wt, time.time(), bag["id"]))
    conn.commit()

    cur.execute(f"SELECT total_bags FROM purchase_orders WHERE id={p}", (body.po_id,))
    total = fetchone(cur)["total_bags"]
    cur.execute(f"SELECT COUNT(*) as c FROM bags WHERE po_id={p} AND status='inwarded'", (body.po_id,))
    inwarded = fetchone(cur)["c"]
    cur.execute(f"SELECT SUM(net_wt) as s FROM bags WHERE po_id={p} AND status='inwarded'", (body.po_id,))
    total_kg = fetchone(cur)["s"] or 0
    conn.close()

    if inwarded >= total:
        set_gate(False, "off", None)

    return {
        "status":   "ok",
        "net_wt":   net_wt,
        "inwarded": inwarded,
        "total":    total,
        "total_kg": round(total_kg, 2),
        "complete": inwarded >= total
    }

@app.post("/api/complete-inwarding")
def complete_inwarding(body: CompleteInwarding):
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE purchase_orders SET status='inwarded' WHERE id={ph()}", (body.po_id,))
    conn.commit()
    conn.close()
    set_gate(False, "off", None)
    return {"status": "ok"}

# ── Reset ─────────────────────────────────────────────────────────────────────

@app.post("/api/reset")
def reset_demo():
    conn, _ = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM bags")
    cur.execute("DELETE FROM purchase_orders")
    cur.execute("DELETE FROM collies")
    cur.execute("DELETE FROM sack_tags")
    cur.execute(f"UPDATE gate_state SET active=0, command='off', po_id=NULL WHERE id=1")
    cur.execute(f"""
        INSERT INTO purchase_orders (id, coffee_type, total_bags, total_kg, status)
        VALUES ({placeholder(5)})
    """, ("PO-001", "Arabica — Coorg", 3, 180.0, "pending"))
    for _ in range(3):
        cur.execute(f"INSERT INTO bags (po_id, status) VALUES ({placeholder(2)})",
                    ("PO-001", "pending"))
    conn.commit()
    conn.close()
    return {"status": "ok"}
