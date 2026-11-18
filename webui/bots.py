"""Pages "Bots et clés" : bots Telegram des amis et clés SerpApi ajoutées depuis l'interface.

Les secrets sont écrits dans secrets/store.json (voir flightdeals.secret_store) et ne sont
JAMAIS réaffichés : un token est montré sous la forme ••••1234, une clé SerpApi pas du tout.
Seule exception : pendant l'ajout d'un bot, le token que l'on vient de taper est renvoyé dans
le formulaire (champ masqué) le temps de chercher le chat_id.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from flightdeals import secret_store
from flightdeals.secret_store import BOT_ID_RE, CHAT_ID_RE, MAIN_BOT_ALIAS, MAIN_BOT_ID, TOKEN_RE
from trip_watch.keys import KEY_NAME_RE

from webui import telegram_tools
from webui.i18n import Msg, exc_msg

KEY_SUFFIX_RE = re.compile(r"^[A-Z0-9_]{1,30}$")
ERRORS = {
    "test-failed": "Échec du test : {who}",
    "bot-in-use": "Bot encore utilisé par : {who}. Décoche-le d’abord dans ces alertes.",
    "key-in-use": "Clé encore utilisée par : {who}. Change d’abord leur clé.",
}
MESSAGES = {
    "bot-saved": "Bot enregistré. Coche-le dans « Envoyer à » des alertes concernées.",
    "bot-deleted": "Bot supprimé.",
    "bot-tested": "Message de test envoyé : vérifie qu'il est bien arrivé dans Telegram.",
    "key-saved": "Clé enregistrée. Ses compteurs SerpApi apparaissent sur l'accueil dans quelques minutes.",
    "key-deleted": "Clé supprimée.",
}


class FormError(Exception):
    pass


def slug(alias: str) -> str:
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", alias).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:30] or "bot"


def register(
    app: FastAPI, templates: Any, secrets_dir: Path, usage: Callable[[], dict],
    known_env_keys: Callable[[], Optional[list[str]]],
    fetch_account: Callable[[str], dict],
    tg: Any = telegram_tools,
) -> None:
    """`usage()` -> {"bots": {id: [libellés]}, "keys": {nom: [libellés]}} : qui utilise quoi.
    `known_env_keys()` : clés de .env (registre du service des alertes), None si inconnu."""

    def store() -> dict:
        return secret_store.read_store(secrets_dir)

    def save(data: dict) -> None:
        secret_store.write_store(secrets_dir, data)

    def render(request, name, context, status_code=200):
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def error_page(request, message, status_code=400):
        return render(request, "error.html", {"message": message}, status_code)

    # ---------- liste ----------

    @app.get("/bots", response_class=HTMLResponse)
    def bots_page(request: Request, msg: str = "", err: str = "", who: str = ""):
        try:
            data = store()
        except secret_store.StoreError as exc:
            return error_page(request, Msg("Fichier de secrets illisible ({error}) : à corriger à la main.", error=str(exc)), 500)
        used = usage()
        bots = [{"id": MAIN_BOT_ID, "alias": MAIN_BOT_ALIAS, "username": "", "chat_id": "", "masked": "dans .env",
                 "main": True, "users": used["bots"].get(MAIN_BOT_ID, [])}]
        for bot_id, entry in sorted(data["bots"].items(), key=lambda kv: str(kv[1].get("alias", kv[0])).casefold()):
            if not isinstance(entry, dict):
                continue
            bots.append({"id": bot_id, "alias": entry.get("alias") or bot_id, "username": entry.get("username", ""),
                         "chat_id": entry.get("chat_id", ""), "masked": secret_store.mask(str(entry.get("token", ""))),
                         "main": False, "users": used["bots"].get(bot_id, [])})
        keys = [{"name": name, "added_at": "", "users": used["keys"].get(name, [])}
                for name in sorted(data["serpapi_keys"])]
        return render(request, "bots.html", {
            "bots": bots, "keys": keys, "message": MESSAGES.get(msg, ""),
            "error": Msg(ERRORS[err], who=who[:300]) if err in ERRORS else "",
            "env_keys": known_env_keys() or [],
        })

    # ---------- bots ----------

    def bot_form(request, bot_id, values, error=None, chats=None, status_code=200):
        return render(request, "bot.html", {
            "bot_id": bot_id, "is_new": bot_id is None, "v": values, "error": error, "chats": chats,
        }, status_code)

    @app.get("/bots/new", response_class=HTMLResponse)
    def bot_new(request: Request):
        return bot_form(request, None, {"alias": "", "token": "", "chat_id": ""})

    @app.get("/bots/{bot_id}", response_class=HTMLResponse)
    def bot_edit(request: Request, bot_id: str):
        entry = store()["bots"].get(bot_id) if BOT_ID_RE.match(bot_id) else None
        if not isinstance(entry, dict):
            return error_page(request, "Bot introuvable.", 404)
        return bot_form(request, bot_id, {"alias": entry.get("alias", ""), "token": "", "chat_id": entry.get("chat_id", ""),
                                          "masked": secret_store.mask(str(entry.get("token", "")))})

    async def bot_submit(request: Request, bot_id: Optional[str]):
        form = dict(await request.form())
        data = store()
        existing = data["bots"].get(bot_id) if bot_id else None
        if bot_id and not isinstance(existing, dict):
            return error_page(request, "Bot introuvable.", 404)
        values = {"alias": (form.get("alias") or "").strip(), "token": (form.get("token") or "").strip(),
                  "chat_id": (form.get("chat_id") or "").strip()}
        if existing:
            values["masked"] = secret_store.mask(str(existing.get("token", "")))
        token = values["token"] or (str(existing.get("token", "")) if existing else "")
        try:
            if not token:
                raise FormError(Msg("Token du bot : obligatoire (fourni par @BotFather)."))
            if not TOKEN_RE.match(token):
                raise FormError(Msg("Token du bot : format inattendu. Il ressemble à 123456789:AAH… (copie-le en entier depuis @BotFather)."))
            if form.get("action") == "detect":
                chats = tg.find_chats(token)
                if not chats:
                    raise FormError(Msg("Aucune conversation trouvée : demande à ton ami d'ouvrir son bot dans Telegram et "
                                        "d'envoyer un message (ex : « bonjour »), puis réessaie."))
                if len(chats) == 1:
                    values["chat_id"] = chats[0].chat_id
                return bot_form(request, bot_id, values, chats=chats)
            alias = values["alias"]
            if not alias or len(alias) > 40:
                raise FormError(Msg("Nom du bot : obligatoire, 40 caractères maximum."))
            if not CHAT_ID_RE.match(values["chat_id"]):
                raise FormError(Msg("Chat ID : nombre attendu (ex : 123456789). Utilise « Trouver le chat ID »."))
            if any(isinstance(e, dict) and str(e.get("alias", "")).casefold() == alias.casefold()
                   for i, e in data["bots"].items() if i != bot_id) or alias.casefold() == MAIN_BOT_ALIAS.casefold():
                raise FormError(Msg("Nom du bot : « {alias} » existe déjà.", alias=alias))
            username = tg.bot_username(token) if (values["token"] or not existing) else existing.get("username", "")
        except (FormError, telegram_tools.TelegramToolError) as exc:
            return bot_form(request, bot_id, values, exc_msg(exc), status_code=400)

        if bot_id is None:
            base = slug(alias)
            bot_id, n = base, 2
            while bot_id in data["bots"] or bot_id in (MAIN_BOT_ID, "new"):
                bot_id, n = f"{base}-{n}", n + 1
            existing = {"added_at": date.today().isoformat()}
        data["bots"][bot_id] = {**existing, "alias": alias, "token": token, "chat_id": values["chat_id"],
                                "username": username}
        save(data)
        return RedirectResponse("/bots?msg=bot-saved", status_code=303)

    @app.post("/bots/new", response_class=HTMLResponse)
    async def bot_create(request: Request):
        return await bot_submit(request, None)

    @app.post("/bots/{bot_id}", response_class=HTMLResponse)
    async def bot_update(request: Request, bot_id: str):
        if not BOT_ID_RE.match(bot_id) or bot_id == MAIN_BOT_ID:
            return error_page(request, "Bot introuvable.", 404)
        return await bot_submit(request, bot_id)

    @app.post("/bots/{bot_id}/test")
    def bot_test(request: Request, bot_id: str):
        entry = store()["bots"].get(bot_id) if BOT_ID_RE.match(bot_id) else None
        if not isinstance(entry, dict):
            return error_page(request, "Bot introuvable.", 404)
        try:
            tg.send_test(str(entry.get("token", "")), str(entry.get("chat_id", "")), str(entry.get("alias", bot_id)))
        except telegram_tools.TelegramToolError as exc:
            return RedirectResponse(f"/bots?err=test-failed&who={_q(str(exc))}", status_code=303)
        return RedirectResponse("/bots?msg=bot-tested", status_code=303)

    @app.post("/bots/{bot_id}/delete")
    def bot_delete(request: Request, bot_id: str):
        data = store()
        if bot_id not in data["bots"]:
            return error_page(request, "Bot introuvable.", 404)
        users = usage()["bots"].get(bot_id, [])
        if users:
            return RedirectResponse(f"/bots?err=bot-in-use&who={_q(', '.join(users))}", status_code=303)
        del data["bots"][bot_id]
        save(data)
        return RedirectResponse("/bots?msg=bot-deleted", status_code=303)

    # ---------- clés SerpApi ----------

    @app.get("/keys/new", response_class=HTMLResponse)
    def key_new(request: Request):
        return render(request, "key.html", {"v": {"suffix": "", "value": ""}, "error": None})

    @app.post("/keys/new", response_class=HTMLResponse)
    async def key_create(request: Request):
        form = dict(await request.form())
        suffix = (form.get("suffix") or "").strip().upper().replace(" ", "_").replace("-", "_")
        value = (form.get("value") or "").strip()
        name = f"SERPAPI_KEY_{suffix}"
        try:
            if not KEY_SUFFIX_RE.match(suffix):
                raise FormError(Msg("Nom : lettres, chiffres ou _ uniquement (ex : ANNA)."))
            if not KEY_NAME_RE.match(name):
                raise FormError(Msg("Nom de clé invalide."))
            data = store()
            if name in data["serpapi_keys"] or name in (known_env_keys() or []):
                raise FormError(Msg("La clé {name} existe déjà.", name=name))
            if not re.fullmatch(r"[A-Za-z0-9]{20,100}", value):
                raise FormError(Msg("Clé : copie-la en entier depuis serpapi.com/manage-api-key (lettres et chiffres)."))
            try:
                account = fetch_account(value)  # account.json : gratuit, ne consomme aucune recherche
            except Exception as exc:  # noqa: BLE001 - jamais str(exc) : l'URL contient la clé
                raise FormError(Msg("SerpApi refuse cette clé ou est injoignable ({error}).", error=type(exc).__name__)) from None
            if not isinstance(account, dict) or "searches_per_month" not in account:
                raise FormError(Msg("SerpApi n'a pas reconnu cette clé."))
        except FormError as exc:
            return render(request, "key.html", {"v": {"suffix": suffix, "value": ""}, "error": exc_msg(exc)}, 400)
        data["serpapi_keys"][name] = value
        save(data)
        return RedirectResponse("/bots?msg=key-saved", status_code=303)

    @app.post("/keys/{name}/delete")
    def key_delete(request: Request, name: str):
        data = store()
        if name not in data["serpapi_keys"]:
            return error_page(request, "Clé introuvable (seules les clés ajoutées ici se suppriment ici).", 404)
        users = usage()["keys"].get(name, [])
        if users:
            return RedirectResponse(f"/bots?err=key-in-use&who={_q(', '.join(users))}", status_code=303)
        del data["serpapi_keys"][name]
        save(data)
        return RedirectResponse("/bots?msg=key-deleted", status_code=303)


def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text[:300])
