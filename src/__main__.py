"""
Punto de Entrada Unificado del Sistema de Actualización de Precios Odoo 18.
Soporta tanto el modo Servidor Web como el modo Consola CLI.
Ejecución:
    python -m src
    python -m src data_samples/test.xlsx --dry-run
"""
import sys
import webbrowser
import uvicorn

from src.core.config import settings
from src.core.logger import logger
from src.cli.console import run_cli


def start_web_server():
    """Inicia el servidor web FastAPI con recarga automática y abre el navegador."""
    url = f"http://localhost:{settings.SERVER_PORT}"
    logger.info(f"Iniciando Servidor Web en {url} ...")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    uvicorn.run(
        "src.api.app:app",
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        reload=True,
    )


def main():
    """Determina si ejecutar el Servidor Web o el CLI según los argumentos."""
    args = sys.argv[1:]

    # Si se invoca sin argumentos o explícitamente con --web / -w
    if not args or "--web" in args or "-w" in args:
        start_web_server()
    else:
        run_cli(args)


if __name__ == "__main__":
    main()
