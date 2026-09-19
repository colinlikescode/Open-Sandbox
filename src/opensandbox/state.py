"""SQLite stores product metadata only; Kubernetes is authoritative for workloads."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import aiosqlite

from opensandbox.auth import Principal, key_hash
from opensandbox.errors import AuthenticationError, ConflictError, NotFoundError
from opensandbox.models import SandboxInfo
from opensandbox.utils.clock import utcnow


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = await aiosqlite.connect(self.path)
        self.path.chmod(0o600)
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY, data TEXT NOT NULL,
                idempotency_key TEXT UNIQUE, request_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS images (
                reference TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                admin INTEGER NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS templates (
                name TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL
            );
        """)
        await self.db.commit()

    async def seed(self, bootstrap_key: str):
        async with self.lock:
            await self.db.execute(
                "INSERT OR IGNORE INTO api_keys VALUES (?,?,?,?,?,NULL)",
                (
                    "bootstrap",
                    key_hash(bootstrap_key),
                    "initial administrator",
                    1,
                    utcnow().isoformat(),
                ),
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO templates VALUES (?,?)",
                (
                    "base",
                    json.dumps(
                        {
                            "name": "base",
                            "image": "python:3.13-slim",
                            "cpu": 1,
                            "memory": "1Gi",
                            "disk": "10Gi",
                        }
                    ),
                ),
            )
            await self.db.commit()

    async def authenticate(self, key: str) -> Principal:
        async with self.db.execute(
            "SELECT id,admin FROM api_keys WHERE hash=? AND revoked_at IS NULL", (key_hash(key),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise AuthenticationError("A valid API key is required")
        return Principal(id=row[0], admin=bool(row[1]))

    async def key_active(self, key_id: str) -> bool:
        async with self.db.execute(
            "SELECT id FROM api_keys WHERE id=? AND revoked_at IS NULL", (key_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def create_key(self, name: str, admin: bool = False):
        key = "e2b_" + secrets.token_hex(32)
        key_id = "key-" + secrets.token_hex(12)
        async with self.lock:
            await self.db.execute(
                "INSERT INTO api_keys VALUES (?,?,?,?,?,NULL)",
                (key_id, key_hash(key), name, int(admin), utcnow().isoformat()),
            )
            await self.db.commit()
        return {"id": key_id, "name": name, "admin": admin, "key": key}

    async def keys(self):
        async with self.db.execute(
            "SELECT id,name,admin,created_at,revoked_at FROM api_keys ORDER BY created_at"
        ) as cur:
            return [
                dict(zip(["id", "name", "admin", "created_at", "revoked_at"], row, strict=True))
                for row in await cur.fetchall()
            ]

    async def revoke_key(self, key_id: str):
        async with self.lock:
            async with self.db.execute(
                "SELECT admin,revoked_at FROM api_keys WHERE id=?", (key_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                raise NotFoundError("API key not found")
            if row[0] and not row[1]:
                async with self.db.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE admin=1 AND revoked_at IS NULL"
                ) as cur:
                    count = await cur.fetchone()
                if count is None or count[0] <= 1:
                    raise ConflictError(
                        "Create another administrator key before revoking the last one"
                    )
            await self.db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE id=?", (utcnow().isoformat(), key_id)
            )
            await self.db.commit()

    async def template(self, name: str):
        async with self.db.execute("SELECT data FROM templates WHERE name=?", (name,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"Template {name} not found; build or register it first")
        return json.loads(row[0])

    async def templates(self):
        async with self.db.execute("SELECT data FROM templates ORDER BY name") as cur:
            return [json.loads(row[0]) for row in await cur.fetchall()]

    async def put_template(self, name: str, data: dict):
        async with self.lock:
            await self.db.execute(
                "INSERT OR REPLACE INTO templates VALUES (?,?)",
                (name, json.dumps({**data, "name": name})),
            )
            await self.db.commit()

    async def delete_template(self, name: str):
        if name == "base":
            raise ConflictError("The default base template must remain available")
        async with self.lock:
            await self.db.execute("DELETE FROM templates WHERE name=?", (name,))
            await self.db.commit()

    async def audit(self, actor: str, action: str, resource: str):
        async with self.lock:
            await self.db.execute(
                "INSERT INTO audit(time,actor,action,resource) VALUES (?,?,?,?)",
                (utcnow().isoformat(), actor, action, resource),
            )
            await self.db.commit()

    async def close(self):
        await self.db.close()

    async def get(self, sandbox_id: str) -> SandboxInfo:
        async with self.db.execute("SELECT data FROM sandboxes WHERE id=?", (sandbox_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise NotFoundError(f"Sandbox {sandbox_id} not found")
        return SandboxInfo.model_validate_json(row[0])

    async def by_key(self, key: str, request_hash: str) -> SandboxInfo | None:
        async with self.db.execute(
            "SELECT data, request_hash FROM sandboxes WHERE idempotency_key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        if row[1] != request_hash:
            raise ConflictError("Idempotency key already used with a different request")
        return SandboxInfo.model_validate_json(row[0])

    async def insert(self, info: SandboxInfo, key: str | None, request_hash: str):
        async with self.lock:
            await self.db.execute(
                "INSERT INTO sandboxes VALUES (?,?,?,?)",
                (info.id, info.model_dump_json(), key, request_hash),
            )
            await self.db.commit()

    async def put(self, info: SandboxInfo):
        async with self.lock:
            await self.db.execute(
                "UPDATE sandboxes SET data=? WHERE id=?", (info.model_dump_json(), info.id)
            )
            await self.db.commit()

    async def list(self) -> list[SandboxInfo]:
        async with self.db.execute("SELECT data FROM sandboxes ORDER BY rowid DESC") as cur:
            return [SandboxInfo.model_validate_json(row[0]) for row in await cur.fetchall()]

    async def image(self, reference: str, data: dict):
        async with self.lock:
            await self.db.execute(
                "INSERT OR REPLACE INTO images VALUES (?,?)", (reference, json.dumps(data))
            )
            await self.db.commit()

    async def images(self):
        async with self.db.execute("SELECT data FROM images") as cur:
            return [json.loads(row[0]) for row in await cur.fetchall()]

    async def image_info(self, reference: str):
        async with self.db.execute(
            "SELECT data FROM images WHERE reference=?", (reference,)
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def delete_image(self, reference: str):
        async with self.lock:
            await self.db.execute("DELETE FROM images WHERE reference=?", (reference,))
            await self.db.commit()

    async def count(self, name: str, value: float = 1):
        async with self.lock:
            await self.db.execute(
                """INSERT INTO counters VALUES (?,?)
                ON CONFLICT(name) DO UPDATE SET value=value+excluded.value""",
                (name, value),
            )
            await self.db.commit()

    async def counters(self):
        async with self.db.execute("SELECT name,value FROM counters") as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}
