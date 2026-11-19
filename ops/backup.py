"""Sauvegarde nocturne de la NUC : bases SQLite, secrets et reglages locaux vers un autre disque.

Lance par le Planificateur de taches Windows (03:00), avec le Python de l'hote :

    python -I ops\\backup.py --dest D:\\flight-deals-backup

Regles (voir README, « Sauvegarde ») :
- data/ n'est JAMAIS ouvert en ecriture : les bases sont copiees octet par octet (aucune
  connexion SQLite sur l'original, donc ni verrou ni journal cree). Empreinte SHA-256 avant et
  apres la copie ; si la base a bouge pendant la copie (ou si un journal d'ecriture existe),
  on attend et on recommence. Seule la COPIE est verifiee avec SQLite (integrity_check).
- Rien n'est ecrit hors du dossier de destination, sauf le journal logs/backup.log et l'etat
  status/backup.json (lu par l'interface pour afficher « Derniere sauvegarde »).
- Seuls les dossiers de sauvegarde datés (AAAA-MM-JJ) plus vieux que --keep jours sont
  supprimes, et uniquement dans le dossier de destination.
- Aucun appel reseau, aucun message, n'affiche jamais le contenu des secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Reglages locaux (hors git pour une partie) : petits, copies en entier avec .history/.archive.
SETTINGS_DIRS = ("config", "watches", "deals")
SECRET_FILES = (".env", "secrets/store.json")
JOURNAL_SUFFIXES = ("-journal", "-wal")
log = logging.getLogger("backup")


class BackupError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def busy(db: Path) -> bool:
    """Une ecriture est en cours si SQLite a laisse un journal a cote de la base."""
    return any(db.with_name(db.name + suffix).exists() for suffix in JOURNAL_SUFFIXES)


def copy_db(src: Path, dst: Path, *, attempts: int = 5, wait: float = 30.0,
            sleep: Callable[[float], None] = time.sleep) -> str:
    """Copie stable d'une base : l'empreinte de l'original est la meme avant et apres, et la
    copie a cette meme empreinte. Retourne l'empreinte."""
    for attempt in range(1, attempts + 1):
        if not busy(src):
            before = sha256(src)
            shutil.copyfile(src, dst)
            after = sha256(src)
            if before == after and not busy(src) and sha256(dst) == before:
                return before
        log.warning("%s modifiee pendant la copie (essai %d/%d), nouvel essai dans %.0f s",
                    src.name, attempt, attempts, wait)
        if attempt < attempts:
            sleep(wait)
    raise BackupError(f"{src.name} : impossible d'obtenir une copie stable")


def check_copy(db: Path) -> int:
    """Verifie la COPIE (jamais l'original) et retourne son nombre total de lignes."""
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"{db.name} : copie corrompue ({result[:200]})")
        tables = [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return sum(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables)
    finally:
        con.close()


def prune(dest: Path, keep: int, today: date) -> list[str]:
    """Supprime les sauvegardes datees de plus de `keep` jours (et les copies interrompues).
    Ne touche a rien d'autre dans `dest`."""
    removed = []
    limit = today - timedelta(days=keep)
    for child in dest.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        stale_partial = name.endswith(".partial") and DAY_RE.match(name[:-8]) and name[:-8] != today.isoformat()
        old_day = False
        if DAY_RE.match(name):
            try:
                old_day = date.fromisoformat(name) < limit
            except ValueError:
                old_day = False
        if old_day or stale_partial:
            shutil.rmtree(child)
            removed.append(name)
    return removed


def run_backup(root: Path, dest: Path, keep: int, *, today: Optional[date] = None,
               sleep: Callable[[float], None] = time.sleep) -> dict:
    today = today or date.today()
    data = root / "data"
    dbs = sorted(data.glob("*.db"))
    if not dbs:
        # Rien a sauvegarder ressemble a une base disparue : on s'arrete, sans rien ecraser.
        raise BackupError(f"aucune base dans {data} : sauvegarde annulee (a verifier)")
    if not Path(dest.anchor).exists():
        raise BackupError(f"destination {dest.anchor} absente (cle USB debranchee ?)")
    dest.mkdir(parents=True, exist_ok=True)
    needed = sum(p.stat().st_size for p in dbs) * 2 + 10 * 1024 * 1024
    free = shutil.disk_usage(dest).free
    if free < needed:
        raise BackupError(f"destination presque pleine ({free // (1024 * 1024)} Mo libres)")

    partial = dest / f"{today.isoformat()}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    (partial / "data").mkdir(parents=True)
    manifest: dict = {"date": today.isoformat(), "databases": {}, "files": []}

    for db in dbs:
        digest = copy_db(db, partial / "data" / db.name, sleep=sleep)
        rows = check_copy(partial / "data" / db.name)
        manifest["databases"][db.name] = {"sha256": digest, "bytes": db.stat().st_size, "rows": rows}
        log.info("%s : %d lignes, empreinte %s", db.name, rows, digest[:12])

    for rel in SECRET_FILES:
        src = root / rel
        if src.is_file():
            target = partial / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            manifest["files"].append(rel)
    for rel in SETTINGS_DIRS:
        src = root / rel
        if src.is_dir():
            shutil.copytree(src, partial / rel, ignore=shutil.ignore_patterns("*.tmp", ".*.tmp"))
            manifest["files"].append(rel + "/")
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    final = dest / today.isoformat()
    if final.exists():  # deuxieme sauvegarde le meme jour : la nouvelle remplace l'ancienne
        shutil.rmtree(final)
    partial.rename(final)
    removed = prune(dest, keep, today)
    if removed:
        log.info("anciennes sauvegardes supprimees : %s", ", ".join(removed))
    return {"folder": str(final), "databases": manifest["databases"], "files": manifest["files"]}


def write_status(path: Path, *, ok: bool, message: str, folder: str = "") -> None:
    """Etat lu par l'interface : derniere tentative et derniere reussite (jamais de secret)."""
    previous: dict = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    status = {
        "last_attempt": now, "ok": ok, "message": message,
        "last_success": now if ok else previous.get("last_success"),
        "folder": folder if ok else previous.get("folder", ""),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", required=True, type=Path, help="ex : D:\\flight-deals-backup")
    parser.add_argument("--keep", type=int, default=30, help="jours conserves (defaut 30)")
    parser.add_argument("--root", type=Path, default=ROOT, help="dossier du projet sur la NUC")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    (root / "logs").mkdir(exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(root / "logs" / "backup.log", encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe (tache planifiee, sans fenetre) n'a pas de console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    try:
        return _run(root, args)
    finally:
        logging.shutdown()


def _run(root: Path, args: argparse.Namespace) -> int:
    status_path = root / "status" / "backup.json"
    try:
        result = run_backup(root, args.dest, args.keep)
    except Exception as exc:  # toute erreur est journalisee et visible dans l'interface
        log.error("ECHEC : %s", exc)
        write_status(status_path, ok=False, message=str(exc)[:300])
        return 1
    log.info("sauvegarde OK -> %s", result["folder"])
    write_status(status_path, ok=True, message="", folder=result["folder"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
