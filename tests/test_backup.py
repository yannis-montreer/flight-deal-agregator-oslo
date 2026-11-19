"""Sauvegarde nocturne (ops/backup.py) : uniquement sur des bases creees dans tmp_path."""
import importlib.util
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("ops_backup", Path(__file__).parent.parent / "ops" / "backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)

TODAY = date(2026, 11, 20)


def make_db(path: Path, rows: int) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE obs (id INTEGER PRIMARY KEY, price INTEGER)")
    con.executemany("INSERT INTO obs (price) VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    make_db(root / "data" / "flightdeals.db", 12)
    make_db(root / "data" / "trip_watch.db", 3)
    (root / ".env").write_text("SERPAPI_KEY=secret\n", encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "store.json").write_text("{}", encoding="utf-8")
    for d in ("config", "watches", "deals"):
        (root / d / ".history").mkdir(parents=True)
        (root / d / "a.yaml").write_text("x: 1\n", encoding="utf-8")
        (root / d / ".history" / "old.yaml").write_text("x: 0\n", encoding="utf-8")
    return root


def test_full_backup_leaves_sources_untouched(root, tmp_path):
    dest = tmp_path / "usb" / "flight-deals-backup"
    sources = {p: (backup.sha256(p), p.stat().st_mtime_ns) for p in (root / "data").glob("*.db")}

    result = backup.run_backup(root, dest, keep=30, today=TODAY)

    folder = dest / "2026-11-20"
    assert result["folder"] == str(folder)
    assert result["databases"]["flightdeals.db"]["rows"] == 12
    assert result["databases"]["trip_watch.db"]["rows"] == 3
    for src, (digest, mtime) in sources.items():
        assert backup.sha256(src) == digest and src.stat().st_mtime_ns == mtime
        assert backup.sha256(folder / "data" / src.name) == digest
    assert sorted((root / "data").iterdir()) == sorted(sources)  # aucun journal cree a cote
    assert (folder / ".env").read_text(encoding="utf-8") == "SERPAPI_KEY=secret\n"
    assert (folder / "secrets" / "store.json").exists()
    assert (folder / "watches" / ".history" / "old.yaml").exists()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "SERPAPI_KEY" not in json.dumps(manifest)
    assert not (dest / "2026-11-20.partial").exists()


def test_same_day_rerun_replaces(root, tmp_path):
    dest = tmp_path / "usb"
    backup.run_backup(root, dest, keep=30, today=TODAY)
    (dest / "2026-11-20" / "stray.txt").write_text("x")
    backup.run_backup(root, dest, keep=30, today=TODAY)
    assert not (dest / "2026-11-20" / "stray.txt").exists()


def test_no_database_stops_without_writing(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    dest = tmp_path / "usb"
    with pytest.raises(backup.BackupError, match="aucune base"):
        backup.run_backup(root, dest, keep=30, today=TODAY)
    assert not dest.exists()


def test_busy_database_is_retried(root, tmp_path):
    db = root / "data" / "flightdeals.db"
    journal = db.with_name(db.name + "-journal")
    journal.write_bytes(b"")
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        journal.unlink()  # l'ecriture se termine pendant l'attente

    target = tmp_path / "copy.db"
    assert backup.copy_db(db, target, sleep=fake_sleep) == backup.sha256(db)
    assert waits == [30.0]


def test_always_busy_database_fails(root, tmp_path):
    db = root / "data" / "flightdeals.db"
    db.with_name(db.name + "-journal").write_bytes(b"")
    with pytest.raises(backup.BackupError, match="copie stable"):
        backup.copy_db(db, tmp_path / "copy.db", attempts=3, sleep=lambda s: None)


def test_corrupt_copy_detected(tmp_path):
    bad = tmp_path / "bad.db"
    make_db(bad, 200)
    raw = bytearray(bad.read_bytes())
    raw[4096:8192] = b"\xff" * 4096  # page de donnees ecrasee
    bad.write_bytes(bytes(raw))
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.check_copy(bad)


def test_prune_only_old_dated_folders(tmp_path):
    dest = tmp_path / "usb"
    for name in ("2026-10-20", "2026-10-21", "2026-11-19", "2026-11-18.partial", "2026-11-20.partial", "photos"):
        (dest / name).mkdir(parents=True)
    (dest / "notes.txt").write_text("x")
    removed = backup.prune(dest, keep=30, today=TODAY)
    assert sorted(removed) == ["2026-10-20", "2026-11-18.partial"]
    assert sorted(p.name for p in dest.iterdir()) == ["2026-10-21", "2026-11-19", "2026-11-20.partial", "notes.txt", "photos"]


def test_status_keeps_last_success_on_failure(tmp_path):
    path = tmp_path / "status" / "backup.json"
    backup.write_status(path, ok=True, message="", folder="D:\\b\\2026-11-20")
    first = json.loads(path.read_text(encoding="utf-8"))
    backup.write_status(path, ok=False, message="destination D:\\ absente")
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["ok"] is False and second["last_success"] == first["last_success"]
    assert second["folder"] == "D:\\b\\2026-11-20"


def test_main_failure_writes_log_and_status(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    assert backup.main(["--dest", str(tmp_path / "usb"), "--root", str(root)]) == 1
    status = json.loads((root / "status" / "backup.json").read_text(encoding="utf-8"))
    assert status["ok"] is False and "aucune base" in status["message"]
    assert "ECHEC" in (root / "logs" / "backup.log").read_text(encoding="utf-8")


def test_check_leaves_no_side_files_next_to_copy(tmp_path):
    db = tmp_path / "copy.db"
    make_db(db, 5)
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.close()
    assert backup.check_copy(db) == 5
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copy.db"]
