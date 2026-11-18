"""SQLite persistence. Connections never span network operations."""
import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


DEFAULTS = {
    "node_name": platform.system() + " 本机", "global_concurrency": 4,
    "daily_request_limit": 500, "retention_days": 365, "temporary_daily_request_limit": 100,
}


def now():
    return time.time()


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return salt + ":" + digest


def password_ok(password, stored):
    return secrets.compare_digest(password_hash(password, stored.split(":")[0]), stored)


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "llmirror.db"
        legacy_path = self.root / "pulse.db"
        key_path = self.root / "vault.key"
        if not key_path.exists():
            if self.path.exists() or legacy_path.exists():
                raise RuntimeError("vault.key 丢失。请从备份恢复密钥，避免已有接口凭证无法解密。")
            with open(key_path, "xb") as f:
                f.write(Fernet.generate_key())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        self.vault = Fernet(key_path.read_bytes())
        if not self.path.exists() and legacy_path.exists():
            # The normal launcher holds app.lock during startup. Retain the original database.
            temporary = self.root / (".llmirror-migration-" + secrets.token_hex(6) + ".db")
            try:
                with contextlib.closing(sqlite3.connect(legacy_path.as_uri() + "?mode=ro", uri=True)) as incoming:
                    with contextlib.closing(sqlite3.connect(temporary)) as outgoing:
                        incoming.backup(outgoing)
                temporary.replace(self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,role TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,
                    csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS endpoints(id INTEGER PRIMARY KEY,name TEXT NOT NULL,
                    base_url TEXT NOT NULL,model TEXT NOT NULL,api_key TEXT NOT NULL DEFAULT '',
                    tag TEXT NOT NULL DEFAULT '',demo INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
                    schedule_minutes INTEGER NOT NULL DEFAULT 0,next_run REAL NOT NULL DEFAULT 0,
                    platform TEXT NOT NULL DEFAULT '天翼云',schedule_scenario TEXT NOT NULL DEFAULT 'probe');
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,endpoint_id INTEGER NOT NULL,
                    endpoint_name TEXT NOT NULL,model TEXT NOT NULL,demo INTEGER NOT NULL,
                    suite TEXT NOT NULL,profile TEXT NOT NULL,status TEXT NOT NULL,planned INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,started REAL,ended REAL,
                    node TEXT NOT NULL,config TEXT NOT NULL,owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL,
                    endpoint_id INTEGER NOT NULL,demo INTEGER NOT NULL,created REAL NOT NULL,
                    status TEXT NOT NULL,ttft_ms REAL,answer_ms REAL,e2e_ms REAL,finish_ms REAL,
                    tps REAL,cps REAL,max_gap_ms REAL,queue_ms REAL,prompt_tokens INTEGER,
                    output_tokens INTEGER,cached_tokens INTEGER,reasoning_tokens INTEGER,
                    token_source TEXT NOT NULL DEFAULT 'unavailable',finish_reason TEXT,
                    http_status INTEGER,error TEXT,request_id TEXT,prompt TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',reasoning TEXT NOT NULL DEFAULT '',
                    quality_pass INTEGER,case_name TEXT,quality_note TEXT);
                CREATE INDEX IF NOT EXISTS samples_window ON samples(demo,created,endpoint_id);
                CREATE INDEX IF NOT EXISTS samples_job ON samples(job_id,id);
                CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
                CREATE TABLE IF NOT EXISTS daily_budget(day TEXT PRIMARY KEY,reserved INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS temporary_budget(day TEXT NOT NULL,user_id INTEGER NOT NULL,
                    reserved INTEGER NOT NULL,PRIMARY KEY(day,user_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(endpoints)")}
            if "platform" not in columns:
                db.execute("ALTER TABLE endpoints ADD COLUMN platform TEXT NOT NULL DEFAULT '天翼云'")
            if "schedule_scenario" not in columns:
                db.execute("ALTER TABLE endpoints ADD COLUMN schedule_scenario TEXT NOT NULL DEFAULT 'probe'")
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "private_owner_id" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN private_owner_id INTEGER")
            if "temporary" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN temporary INTEGER NOT NULL DEFAULT 0")
            if not db.execute("SELECT key FROM settings WHERE key='economy_sampling_v1'").fetchone():
                db.execute("UPDATE endpoints SET schedule_scenario='probe' WHERE schedule_scenario='chat'")
                db.execute("INSERT INTO settings(key,value) VALUES('economy_sampling_v1','true')")
            # Keep legacy storage flags for compatible upgrades; only real data remains.
            db.execute("DELETE FROM samples WHERE demo!=0 OR job_id IN (SELECT id FROM jobs WHERE demo!=0)")
            db.execute("DELETE FROM jobs WHERE demo!=0")
            db.execute("DELETE FROM endpoints WHERE demo!=0")
            for k, v in DEFAULTS.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connection() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connection() as db:
            return db.execute(sql, args).lastrowid

    def settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.rows("SELECT * FROM settings")}

    def set_settings(self, values):
        with self.connection() as db:
            db.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)",
                           [(k, json.dumps(v, ensure_ascii=False)) for k, v in values.items()])

    def encrypt(self, value):
        return self.vault.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, value):
        return self.vault.decrypt(value.encode()).decode() if value else ""

    def endpoint(self, endpoint_id, secret=False):
        row = self.one("SELECT * FROM endpoints WHERE id=? AND demo=0", (endpoint_id,))
        if row and not secret:
            row["has_key"] = bool(row.pop("api_key"))
        return row

    def endpoints(self):
        result = self.rows("SELECT * FROM endpoints WHERE demo=0 ORDER BY id")
        for row in result:
            row["has_key"] = bool(row.pop("api_key"))
        return result

    def add_sample(self, sample):
        keys = list(sample)
        with self.connection() as db:
            db.execute("INSERT INTO samples(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")",
                       [sample[k] for k in keys])
            db.execute("UPDATE jobs SET completed=completed+1 WHERE id=?", (sample["job_id"],))

    def reserve(self, day, amount, limit):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO daily_budget VALUES(?,0)", (day,))
            used = db.execute("SELECT reserved FROM daily_budget WHERE day=?", (day,)).fetchone()[0]
            if used + amount > limit:
                return False
            db.execute("UPDATE daily_budget SET reserved=reserved+? WHERE day=?", (amount, day))
            return True

    def backup(self):
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        folder = self.root / "backups" / stamp
        folder.mkdir(parents=True)
        with self.connection() as src:
            with contextlib.closing(sqlite3.connect(folder / "llmirror.db")) as dst:
                src.backup(dst)
        shutil.copy2(self.root / "vault.key", folder / "vault.key")
        return folder

    def reserve_temporary(self, day, amount, limit, user_id, member_limit):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            global_row = db.execute("SELECT reserved FROM daily_budget WHERE day=?", (day,)).fetchone()
            member_row = db.execute("SELECT reserved FROM temporary_budget WHERE day=? AND user_id=?", (day, user_id)).fetchone()
            if (member_row[0] if member_row else 0) + amount > member_limit:
                return "member"
            if (global_row[0] if global_row else 0) + amount > limit:
                return "global"
            db.execute("INSERT INTO daily_budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET reserved=reserved+excluded.reserved", (day, amount))
            db.execute("INSERT INTO temporary_budget VALUES(?,?,?) ON CONFLICT(day,user_id) DO UPDATE SET reserved=reserved+excluded.reserved", (day, user_id, amount))
            return None

    def recover(self):
        self.execute("UPDATE jobs SET status='interrupted',ended=? WHERE status IN ('queued','running','cancelling')", (now(),))
        self.execute("DELETE FROM sessions WHERE expires<?", (now(),))

    def cleanup(self):
        threshold = now() - self.settings()["retention_days"] * 86400
        with self.connection() as db:
            # Active jobs must retain their samples even if a very long run crosses a boundary.
            db.execute("DELETE FROM samples WHERE created<? AND job_id IN (SELECT id FROM jobs WHERE status NOT IN ('queued','running','cancelling'))", (threshold,))
            db.execute("DELETE FROM jobs WHERE created<? AND status NOT IN ('queued','running','cancelling')", (threshold,))
            db.execute("DELETE FROM sessions WHERE expires<?", (now(),))
