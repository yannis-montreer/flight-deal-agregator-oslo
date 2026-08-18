"""Ouverture de connexion SQLite : PRAGMAs + application idempotente du schema.

Un seul point d'entree : get_connection(db_path). Autocommit (isolation_level=None) est
utilise deliberement : chaque insert_observation() (voir repository.py) est ainsi commite
immediatement et survit a un crash du process juste apres, plutot que d'etre perdu dans une
grosse transaction de fin de run non commitee (voir risques du plan : "crash pendant un run").
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: "Path | str") -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")   # permet d'inspecter la DB avec un outil externe pendant que le conteneur tourne
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    return conn
