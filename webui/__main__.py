"""`python -m webui` : lance l'interface. Dans Docker, le port n'est publie que sur
127.0.0.1 de l'hote (voir docker-compose.yml) : accessible depuis la NUC uniquement."""
from __future__ import annotations

import os

import uvicorn

from webui.app import create_app_from_env

if __name__ == "__main__":
    uvicorn.run(
        create_app_from_env(),
        host=os.environ.get("WEBUI_HOST", "127.0.0.1"),
        port=int(os.environ.get("WEBUI_PORT", "8765")),
        proxy_headers=False,
    )
