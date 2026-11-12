"""Lecture/ecriture des fichiers YAML de configuration, en preservant les commentaires.

Garanties a l'enregistrement (dans cet ordre) :
1. le nouveau contenu est ecrit dans un fichier temporaire du MEME dossier ;
2. ce fichier est valide par le VRAI chargeur du service concerne (flightdeals.config /
   trip_watch.config, en mode sans secrets) : ce que l'interface accepte, le service
   l'acceptera au prochain run ;
3. le budget SerpApi est verifie par l'appelant (voir costs.py) ;
4. l'ancienne version est copiee dans `.history/` du dossier ;
5. remplacement atomique (os.replace) : un service qui lit le fichier au meme instant voit
   l'ancienne ou la nouvelle version, jamais un fichier a moitie ecrit.
"""
from __future__ import annotations

import io
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, ScalarString

HISTORY_DIRNAME = ".history"
HISTORY_KEEP = 30


def _yaml() -> YAML:
    y = YAML()  # round-trip : conserve commentaires, ordre des cles et styles
    y.preserve_quotes = True
    y.width = 4096  # ne jamais re-couper une ligne existante
    y.indent(mapping=2, sequence=4, offset=2)
    # `null` explicite (au lieu d'une valeur vide) : plus lisible pour un humain qui rouvre le fichier.
    y.representer.add_representer(
        type(None), lambda rep, _data: rep.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def load_document(path: Path) -> Any:
    return _yaml().load(path.read_text(encoding="utf-8"))


def _flow_key(line: str) -> str:
    return re.sub(r"\{\s+", "{", re.sub(r"\s+\}", "}", line))


def dump_document(doc: Any, *, original: str = "") -> str:
    """`original` : texte d'origine du fichier. Sert a conserver ses fins de ligne (CRLF sous
    Windows) et la mise en forme `{ a: 1 }` des lignes non modifiees (ruamel ecrit `{a: 1}`)."""
    buffer = io.StringIO()
    _yaml().dump(doc, buffer)
    lines = buffer.getvalue().split("\n")
    originals = {_flow_key(line): line for line in original.replace("\r\n", "\n").split("\n") if "{" in line}
    lines = [originals.get(_flow_key(line), line) if "{" in line else line for line in lines]
    text = "\n".join(lines)
    return text.replace("\n", "\r\n") if "\r\n" in original else text


def _same(old: Any, new: Any) -> bool:
    # bool est un int en Python (True == 1) : on exige le meme "genre" de valeur.
    if isinstance(old, bool) or isinstance(new, bool):
        return type(old) is type(new) and old == new
    if old is None or new is None:
        return old is new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return old == new
    return isinstance(old, str) and isinstance(new, str) and str(old) == new


def set_value(mapping: Any, key: str, value: Any, *, quoted: bool = False) -> None:
    """Remplace une valeur en gardant le style de l'ancienne (guillemets ou non). `quoted`
    force les guillemets : indispensable pour "10:30" (sans guillemets, YAML 1.1 / PyYAML le
    lit comme l'entier 630) et pour les dates ISO (lues comme objets date).
    Valeur identique : rien n'est touche (garde l'ecriture d'origine, ex: `0.30`)."""
    if key in mapping and _same(mapping[key], value):
        return
    if isinstance(value, str):
        old = mapping.get(key)
        if quoted:
            value = DoubleQuotedScalarString(value)
        elif isinstance(old, ScalarString):
            value = type(old)(value)
    mapping[key] = value


def set_list(mapping: Any, key: str, values: list) -> None:
    """Modifie la liste EN PLACE pour conserver son style (ex: `[1, 2, 3]` sur une ligne)."""
    seq = mapping.get(key)
    if seq is not None and list(seq) == list(values):
        return
    if seq is None or not hasattr(seq, "extend"):
        mapping[key] = list(values)
        return
    del seq[:]
    seq.extend(values)


def _backup(path: Path) -> Path:
    history = path.parent / HISTORY_DIRNAME
    history.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = history / f"{path.stem}-{stamp}{path.suffix}"
    counter = 1
    while target.exists():
        target = history / f"{path.stem}-{stamp}-{counter}{path.suffix}"
        counter += 1
    shutil.copy2(path, target)
    old_versions = sorted(history.glob(f"{path.stem}-*{path.suffix}"))
    for old in old_versions[:-HISTORY_KEEP]:
        old.unlink()
    return target


def save_document(path: Path, doc: Any, validate: Callable[[Path], Any], check: Callable[[Any], None] | None = None) -> Any:
    """Ecrit `doc` dans `path` apres validation. `validate(tmp_path)` doit lever une
    exception si le contenu est invalide et renvoie la config chargee ; `check(config)`
    (optionnel) applique des controles supplementaires (budget). Rien n'est modifie si
    l'une ou l'autre echoue."""
    # read_bytes (pas read_text) : read_text convertit les CRLF en LF a la lecture, ce qui
    # ferait reecrire tout le fichier en LF et rendrait la comparaison `text == original` fausse.
    original = path.read_bytes().decode("utf-8")
    text = dump_document(doc, original=original)
    tmp = path.parent / f".{path.name}.tmp"
    try:
        tmp.write_bytes(text.encode("utf-8"))
        config = validate(tmp)
        if check is not None:
            check(config)
        if text == original:
            tmp.unlink()
            return config
        _backup(path)
        os.replace(tmp, path)
        return config
    finally:
        if tmp.exists():
            tmp.unlink()
