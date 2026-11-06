from pathlib import Path
import sqlite3

db_file = Path(__file__).with_name("demo.sqlite3")
with sqlite3.connect(db_file) as db:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            series_id INTEGER NOT NULL,
            recorded_at TEXT NOT NULL,
            measurement REAL NOT NULL,
            PRIMARY KEY (series_id, recorded_at)
        );
        INSERT OR IGNORE INTO samples VALUES
            (1, '2026-01-01T00:00:00Z', 21.5),
            (1, '2026-01-01T01:00:00Z', 21.8),
            (2, '2026-01-01T00:00:00Z', 17.3);
    """)
print(f"Seeded {db_file}")
