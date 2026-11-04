import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .errors import SongSeekError


class Database:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self._depth = 0
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tracks_playlist ON tracks(playlist_id);
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                provider TEXT NOT NULL, source_id TEXT NOT NULL,
                data TEXT NOT NULL, raw TEXT NOT NULL,
                UNIQUE(track_id, provider, source_id)
            );
            CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
        """)

        with self.transaction() as connection:
            connection.execute("UPDATE tracks SET data=json_set(data, '$.lyrics_wanted', NULL) "
                               "WHERE json_type(data, '$.lyrics_wanted') IS NULL")

    @contextmanager
    def transaction(self):
        with self.lock:
            savepoint = f"songseek_{self._depth}"
            self.connection.execute("BEGIN" if self._depth == 0 else f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK" if self._depth == 1 else f"ROLLBACK TO {savepoint}")
                if self._depth > 1:
                    self.connection.execute(f"RELEASE {savepoint}")
                raise
            else:
                self.connection.execute("COMMIT" if self._depth == 1 else f"RELEASE {savepoint}")
            finally:
                self._depth -= 1

    def get(self, table: str, identifier: int) -> dict:
        with self.lock:
            row = self.connection.execute(f"SELECT data FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise SongSeekError("记录不存在", "not_found", 404)
        return json.loads(row["data"])

    def list(self, table: str, column: str | None = None, value: int | None = None) -> list[dict]:
        with self.lock:
            query = f"SELECT data FROM {table}" + (f" WHERE {column}=?" if column else "") + " ORDER BY id"
            rows = self.connection.execute(query, (value,) if column else ()).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def insert(self, table: str, data: dict, **columns) -> int:
        with self.transaction() as connection:
            values = {"data": json.dumps(data, ensure_ascii=False), **columns}
            cursor = connection.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
            identifier = cursor.lastrowid
            data["id"] = identifier
            connection.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), identifier))
        return identifier

    def save(self, table: str, data: dict) -> None:
        with self.transaction() as connection:
            connection.execute(f"UPDATE {table} SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), data["id"]))

    def delete(self, table: str, identifier: int) -> None:
        self.get(table, identifier)
        with self.transaction() as connection:
            connection.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))

    def raw_candidate(self, identifier: int) -> dict:
        with self.lock:
            row = self.connection.execute("SELECT raw FROM candidates WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise SongSeekError("候选不存在", "not_found", 404)
        return json.loads(row["raw"])

    def close(self) -> None:
        self.connection.close()
