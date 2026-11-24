"""Transactions and the version-aware failure ledger."""
import json
import os
import sqlite3
import meko
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SCENARIOS = [
    ("payments-api", "2.14", "Restart workers", "Checkout timeouts", "Workers reconnected simultaneously and exhausted the connection pool."),
    ("catalog-cache", "3.2", "Flush cache", "Elevated read latency", "Cold reads overloaded the catalog database."),
    ("mail-worker", "1.8", "Scale workers", "Growing delivery backlog", "Extra workers hit the provider rate limit."),
    ("search-index", "4.0", "Rebuild index", "Search results lag", "The rebuild competed with pending writes."),
    ("orders-db", "6.1", "Promote replica", "Replica lag", "The replica was missing acknowledged writes."),
]
DDL = [
    "CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,service TEXT NOT NULL,created TEXT NOT NULL,metrics TEXT NOT NULL,origin TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS controls(name TEXT PRIMARY KEY,payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meko_calls(id TEXT PRIMARY KEY,created TEXT NOT NULL,tool TEXT NOT NULL,datapack_id TEXT,state TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kb_files(content_hash TEXT NOT NULL,datapack_id TEXT NOT NULL,filename TEXT NOT NULL,document_key TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(content_hash,datapack_id))",
    "CREATE TABLE IF NOT EXISTS cloud_jobs(id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, conversation_id TEXT, created TEXT NOT NULL, updated TEXT, datapack_id TEXT)",
    "CREATE TABLE IF NOT EXISTS approvals(parent TEXT PRIMARY KEY, child TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, service TEXT NOT NULL, symptom TEXT NOT NULL, trace_id TEXT NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT, error TEXT)",
    "CREATE TABLE IF NOT EXISTS services(name TEXT PRIMARY KEY, version TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scars(id TEXT PRIMARY KEY, service TEXT NOT NULL REFERENCES services(name), version TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, author TEXT NOT NULL, status TEXT NOT NULL, reviewer TEXT, created TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'medium', reviewed_at TEXT)",
    "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, kind TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL)",
]

def uid():
    return uuid4().hex

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def transaction(write=False):
    url = os.getenv("DATABASE_URL")
    if url:
        import psycopg
        from psycopg.rows import dict_row
        db = psycopg.connect(url, row_factory=dict_row)
    else:
        path = Path(os.getenv("SCARLINE_DB", "data/scarline.sqlite3"))
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        def query(sql, args=()):
            return db.execute(sql.replace("?", "%s") if url else sql, args)
        if url and write:
            query("SELECT pg_advisory_xact_lock(810267)")
        yield query
        db.commit()
    finally:
        db.close()

def event(q, kind, payload):
    item = dict(id=uid(), kind=kind, created=now(), **payload)
    q("INSERT INTO events VALUES (?,?,?,?)", (item["id"], kind, item["created"], json.dumps(item)))
    return item

def service_version(q, name):
    row = q("SELECT version FROM services WHERE name=?", (name,)).fetchone()
    if not row:
        raise KeyError("Unknown service")
    return row["version"]

def record(q, service, action, reason, evidence, author, trace_id=None, cloud=False, severity="medium"):
    version = service_version(q, service)
    key = uid()
    q("INSERT INTO scars (id,service,version,action,reason,evidence,author,status,reviewer,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
      (key, service, version, action, reason, evidence, author, "pending", None, now()))
    q("UPDATE scars SET severity=? WHERE id=?", (severity, key))
    trace_id = trace_id or uid()
    entry = event(q, "record", dict(trace_id=trace_id, scar_id=key, service=service, version=version, action=action, author=author, reason=reason, evidence=evidence, severity=severity))
    if cloud:
        share_scar(q, entry, "pending")
    return key

def review(q, key, reviewer, status, note, cloud=False):
    scar = q("SELECT * FROM scars WHERE id=?", (key,)).fetchone()
    if not scar:
        raise KeyError("Unknown scar")
    if reviewer.casefold() == scar["author"].casefold():
        raise ValueError("A different reviewer must assess this scar.")
    q("UPDATE scars SET status=?,reviewer=?,reviewed_at=? WHERE id=?", (status, reviewer, now(), key))
    entry = event(q, "review", dict(trace_id=uid(), service=scar["service"], version=scar["version"],
                  action=scar["action"], reason=scar["reason"], evidence=scar["evidence"], scar_id=key,
                  reviewer=reviewer, status=status, note=note))
    if cloud:
        share_scar(q, entry, status)
    return entry

def share_scar(q, entry, status):
    key, service = entry["scar_id"], entry["service"]
    text = (f"Scarline scar {key} for {service} v{entry['version']}. "
            f"Action: {entry['action']}. Status: {status}. Reason: {entry['reason']}. "
            f"Evidence: {entry['evidence']}. Review: {entry.get('note', 'pending')}.")
    meko.enqueue(q, entry["trace_id"], entry["id"], "memory_add", {"text": text, "run_id": entry["trace_id"],
                 "metadata": {"app":"scarline", "service":service, "scar_id":key, "status":status,
                              "event_id":entry["id"], "role":"Scribe", "version":entry["version"], "action":entry["action"]}})


def initialize():
    from workflow import seed
    with transaction(True) as q:
        for sql in DDL:
            q(sql)
        for table, columns in {
            "cloud_jobs": {"datapack_id": "TEXT"},
            "scars": {"severity": "TEXT NOT NULL DEFAULT 'medium'", "reviewed_at": "TEXT"},
            "inbox": {"attempts": "INTEGER NOT NULL DEFAULT 0", "next_attempt": "TEXT", "error": "TEXT"},
        }.items():
            for column, definition in columns.items():
                if os.getenv("DATABASE_URL"):
                    q(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
                elif column not in {r["name"] for r in q(f"PRAGMA table_info({table})")}:
                    q(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        q("UPDATE cloud_jobs SET state='unverified' WHERE datapack_id IS NULL AND state IN ('queued','sending')")
        if not q("SELECT name FROM services LIMIT 1").fetchone():
            seed(q)
