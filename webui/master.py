"""Mot de passe maître : exigé pour supprimer un bot Telegram ou une clé SerpApi de l'interface.

Seule une empreinte scrypt (avec sel) est conservée, dans secrets/master.json (hors git, hors
image Docker) ; le mot de passe lui-même n'est écrit nulle part. Après MAX_FAILURES essais
faux, les suppressions sont bloquées LOCK_SECONDS (compteur en mémoire de l'interface).

Définir ou changer le mot de passe, sur la NUC (bibliothèque standard uniquement) :

    python -I webui\\master.py
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Callable, Optional

FILENAME = "master.json"
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)


def is_set(secrets_dir: Path) -> bool:
    return (secrets_dir / FILENAME).is_file()


def set_password(secrets_dir: Path, password: str) -> None:
    if not password:
        raise ValueError("mot de passe vide")
    salt = secrets.token_bytes(16)
    record = {"scheme": "scrypt", **SCRYPT, "salt": salt.hex(), "hash": _derive(password, salt, **SCRYPT).hex()}
    secrets_dir.mkdir(parents=True, exist_ok=True)
    tmp = secrets_dir / f".{FILENAME}.tmp"
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, secrets_dir / FILENAME)


def check_password(secrets_dir: Path, password: str) -> bool:
    try:
        record = json.loads((secrets_dir / FILENAME).read_text(encoding="utf-8"))
        salt, expected = bytes.fromhex(record["salt"]), bytes.fromhex(record["hash"])
        params = {k: int(record[k]) for k in ("n", "r", "p")}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(_derive(password or "", salt, **params), expected)


class Guard:
    """Vérifie le mot de passe maître et bloque après trop d'essais faux."""

    def __init__(self, secrets_dir: Path, clock: Callable[[], float] = time.monotonic):
        self.secrets_dir, self.clock = secrets_dir, clock
        self.failures = 0
        self.locked_until = 0.0

    def check(self, password: Optional[str]) -> str:
        """"ok", "unset", "locked" ou "wrong"."""
        if not is_set(self.secrets_dir):
            return "unset"
        if self.clock() < self.locked_until:
            return "locked"
        if check_password(self.secrets_dir, password or ""):
            self.failures = 0
            return "ok"
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.failures = 0
            self.locked_until = self.clock() + LOCK_SECONDS
            return "locked"
        return "wrong"

    def minutes_left(self) -> int:
        return max(1, int((self.locked_until - self.clock() + 59) // 60))


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    secrets_dir = Path(__file__).resolve().parent.parent / "secrets"
    if "--stdin" in args:
        # PowerShell peut prefixer l'entree d'une marque BOM invisible : on l'ignore.
        password = sys.stdin.readline().rstrip("\r\n").lstrip("﻿")
    else:
        password = getpass.getpass("Nouveau mot de passe maître : ")
        if getpass.getpass("Encore une fois : ") != password:
            print("Les deux saisies diffèrent : rien n'a été changé.")
            return 1
    if not password:
        print("Mot de passe vide : rien n'a été changé.")
        return 1
    set_password(secrets_dir, password)
    print(f"Mot de passe maître enregistré (empreinte seulement) dans {secrets_dir / FILENAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
