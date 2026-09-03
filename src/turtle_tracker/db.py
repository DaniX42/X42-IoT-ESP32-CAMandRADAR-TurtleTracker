import sqlite3
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    inside_house INTEGER NOT NULL DEFAULT 0,
    speed REAL NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'camera'
);
CREATE INDEX IF NOT EXISTS idx_positions_timestamp ON positions(timestamp);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT
);
CREATE TABLE IF NOT EXISTS motion_crops (
    filename TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    perceptual_hash TEXT NOT NULL,
    is_turtle INTEGER,
    keep_for_training INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_motion_crops_captured_at ON motion_crops(captured_at DESC);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            # Migration for databases created before the "source" column was added.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(positions)")}
            if "source" not in columns:
                connection.execute("ALTER TABLE positions ADD COLUMN source TEXT NOT NULL DEFAULT 'camera'")

    def insert_position(
        self, timestamp: str, x: float, y: float, inside_house: bool, speed: float, confidence: float, source: str = "camera"
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO positions (timestamp, x, y, inside_house, speed, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp, x, y, int(inside_house), speed, confidence, source),
            )
            return int(cursor.lastrowid)

    def positions(self, limit: int = 1000) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM positions ORDER BY timestamp DESC LIMIT ?", (limit,)))

    def latest_position(self) -> sqlite3.Row | None:
        rows = self.positions(1)
        return rows[0] if rows else None

    def insert_event(self, timestamp: str, event: str, details: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute("INSERT INTO events (timestamp, event, details) VALUES (?, ?, ?)", (timestamp, event, details))

    def insert_motion_crop(self, filename: str, camera_id: str, captured_at: str, perceptual_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO motion_crops (filename, camera_id, captured_at, perceptual_hash) VALUES (?, ?, ?, ?)",
                (filename, camera_id, captured_at, perceptual_hash),
            )

    def motion_crops(self, is_turtle: bool | None = None, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        query = "SELECT * FROM motion_crops"
        parameters: tuple[object, ...] = ()
        if is_turtle is not None:
            query += " WHERE is_turtle = ?"
            parameters = (int(is_turtle),)
        query += " ORDER BY captured_at DESC LIMIT ? OFFSET ?"
        parameters += (limit, offset)
        with self.connect() as connection:
            return list(connection.execute(query, parameters))

    def motion_crop_count(self, is_turtle: bool | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM motion_crops"
        parameters: tuple[object, ...] = ()
        if is_turtle is not None:
            query += " WHERE is_turtle = ?"
            parameters = (int(is_turtle),)
        with self.connect() as connection:
            return int(connection.execute(query, parameters).fetchone()["count"])

    def motion_crop_hashes(self) -> list[str]:
        with self.connect() as connection:
            return [str(row["perceptual_hash"]) for row in connection.execute("SELECT perceptual_hash FROM motion_crops")]

    def motion_crop(self, filename: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM motion_crops WHERE filename = ?", (filename,)).fetchone()

    def label_motion_crop(self, filename: str, is_turtle: bool, keep_for_training: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE motion_crops SET is_turtle = ?, keep_for_training = ? WHERE filename = ?",
                (int(is_turtle), int(keep_for_training), filename),
            )
            return cursor.rowcount == 1

    def delete_motion_crop(self, filename: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM motion_crops WHERE filename = ?", (filename,))
            return cursor.rowcount == 1


def row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)
