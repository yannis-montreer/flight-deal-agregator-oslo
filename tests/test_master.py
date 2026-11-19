"""Mot de passe maitre exige pour supprimer un bot ou une cle SerpApi. Aucun appel reseau."""
from __future__ import annotations

import io

from flightdeals import secret_store
from webui import master

from tests.test_bots import MASTER, secrets, ui  # noqa: F401 - fixtures partagees


def test_delete_needs_master_password(ui):
    c, tmp, _ = ui
    page = c.get("/bots").text
    assert 'name="master"' in page and "Mot de passe maître" in page
    for url in ("/bots/anna/delete", "/keys/SERPAPI_KEY_ANNA/delete"):
        assert "incorrect" in c.post(url).text
        assert "incorrect" in c.post(url, data={"master": "nope"}).text
    data = secret_store.read_store(tmp / "secrets")
    assert "anna" in data["bots"] and "SERPAPI_KEY_ANNA" in data["serpapi_keys"]
    assert c.post("/keys/SERPAPI_KEY_ANNA/delete", data={"master": MASTER}, follow_redirects=False).status_code == 303
    assert "SERPAPI_KEY_ANNA" not in secret_store.read_store(tmp / "secrets")["serpapi_keys"]
    stored = (tmp / "secrets" / "master.json").read_text(encoding="utf-8")
    assert MASTER not in stored and "scrypt" in stored


def test_lockout_after_five_wrong(ui):
    c, tmp, _ = ui
    for _ in range(4):
        assert "incorrect" in c.post("/bots/anna/delete", data={"master": "x"}).text
    assert "bloquées pendant 15 min" in c.post("/bots/anna/delete", data={"master": "x"}).text
    # meme le bon mot de passe est refuse pendant le blocage
    assert "bloquées" in c.post("/bots/anna/delete", data={"master": MASTER}).text
    assert "anna" in secret_store.read_store(tmp / "secrets")["bots"]


def test_english_messages(ui):
    c, _, _ = ui
    c.get("/lang/en")
    assert "Wrong master password" in c.post("/bots/anna/delete", data={"master": "x"}).text


def test_no_master_password_blocks_delete(ui):
    c, tmp, _ = ui
    (tmp / "secrets" / "master.json").unlink()
    assert "Aucun mot de passe maître" in c.post("/bots/anna/delete", data={"master": MASTER}).text
    assert "anna" in secret_store.read_store(tmp / "secrets")["bots"]


def test_guard_unlocks_after_delay(tmp_path):
    now = [0.0]
    master.set_password(tmp_path, "pw")
    guard = master.Guard(tmp_path, clock=lambda: now[0])
    assert [guard.check("1") for _ in range(5)] == ["wrong"] * 4 + ["locked"]
    assert guard.minutes_left() == 15
    now[0] += master.LOCK_SECONDS + 1
    assert guard.check("pw") == "ok"


def test_cli_stdin_sets_hash_only(tmp_path, monkeypatch):
    monkeypatch.setattr(master, "__file__", str(tmp_path / "webui" / "master.py"))
    monkeypatch.setattr("sys.stdin", io.StringIO("﻿s3cret\r\n"))
    assert master.main(["--stdin"]) == 0
    assert master.check_password(tmp_path / "secrets", "s3cret")
    assert not master.check_password(tmp_path / "secrets", "s3cre")
    assert "s3cret" not in (tmp_path / "secrets" / "master.json").read_text(encoding="utf-8")
