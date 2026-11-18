"""Langue de l'interface : français (défaut) ou anglais, choisie par un cookie.

Les textes restent écrits en français dans le code et les gabarits ; `_("texte")` (gabarits)
et `tr(Msg(...))` (messages venus du code) renvoient la traduction anglaise si la langue est
"en". Une phrase absente de EN reste en français (un test vérifie qu'il n'en manque aucune).

Non traduits, volontairement : noms de pays/régions/aéroports/devises (listes de données),
messages techniques des chargeurs de configuration, messages Telegram.
"""
from __future__ import annotations

from typing import Any

from markupsafe import Markup

from webui.i18n_en import EN

LANGS = ("fr", "en")
DEFAULT_LANG = "fr"
COOKIE = "lang"
MONTHS = {
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
}


class Msg(str):
    """Message traduisible : se comporte comme le texte français déjà formaté (tests, logs),
    mais garde son modèle et ses valeurs pour être retraduit à l'affichage."""

    template: str
    kw: dict

    def __new__(cls, template: str, **kw: Any) -> "Msg":
        obj = super().__new__(cls, template.format(**kw) if kw else template)
        obj.template, obj.kw = template, kw
        return obj


def lang_of(request) -> str:
    value = request.cookies.get(COOKIE, DEFAULT_LANG) if request is not None else DEFAULT_LANG
    return value if value in LANGS else DEFAULT_LANG


def tr(value: Any, lang: str) -> str:
    """Traduction d'un message (Msg, texte simple, exception) ; le résultat est du texte brut,
    échappé normalement par les gabarits."""
    if isinstance(value, BaseException):
        value = value.args[0] if value.args and isinstance(value.args[0], str) else str(value)
    if value is None:
        return ""
    if hasattr(value, "render"):  # ex. un mois, recalcule dans la langue voulue
        return value.render(lang)
    if lang != "en":
        return str(value)
    if isinstance(value, Msg):
        template = EN.get(value.template, value.template)
        return template.format(**{k: tr(v, lang) if isinstance(v, str) else v for k, v in value.kw.items()})
    return EN.get(value, value) if isinstance(value, str) else str(value)


def gettext(lang: str):
    """`_` des gabarits : textes statiques de confiance (peuvent contenir du HTML), valeurs
    passées en paramètres échappées."""
    def _(text: str, **kw: Any) -> Markup:
        template = Markup(EN.get(text, text) if lang == "en" else text)
        return template.format(**kw) if kw else template
    return _


def exc_msg(exc: BaseException) -> Any:
    """Message d'une exception en gardant son Msg (str(exc) le transformerait en texte simple)."""
    return exc.args[0] if exc.args and isinstance(exc.args[0], str) else str(exc)
