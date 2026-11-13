"""Punto de entrada del servidor CamWay nativo (Windows, empaquetado con
PyInstaller). Arranca uvicorn sirviendo la app FastAPI. Los archivos web se
sirven desde el bundle (CAMWAY_WEB_DIR)."""
import os
import sys

if getattr(sys, "frozen", False):
    # Ejecutable PyInstaller: los estáticos van empaquetados junto al exe.
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.environ.setdefault("CAMWAY_WEB_DIR", os.path.join(base, "web"))

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("CAMWAY_PORT", "8120"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
