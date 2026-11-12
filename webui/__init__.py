"""Interface web locale d'edition des veilles (flightdeals + fichiers de watches/).

Ne lit ni n'affiche aucun secret : elle n'edite que les fichiers YAML de configuration,
que les services relisent d'eux-memes au debut de chaque run (aucun redemarrage requis).
"""
