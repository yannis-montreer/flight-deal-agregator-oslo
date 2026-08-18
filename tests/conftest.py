"""Fixtures partagees entre tous les modules de tests."""
from __future__ import annotations

import pytest

from flightdeals.db.connection import get_connection


@pytest.fixture
def conn(tmp_path):
    """Une connexion SQLite reelle sur un fichier temporaire (pas de mock : c'est le
    comportement reel de sqlite3 — PRAGMAs, contraintes, requetes — qu'on veut valider)."""
    db_path = tmp_path / "test.db"
    connection = get_connection(db_path)
    yield connection
    connection.close()
