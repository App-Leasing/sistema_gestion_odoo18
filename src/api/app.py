from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import settings
from src.core.logger import logger
from src.api.routes import router


def create_app() -> FastAPI:
    """Factory para crear la aplicación FastAPI."""
    settings.validate()

    app = FastAPI(
        title="Odoo 18 - Gestor de Listas de Precios",
        description="Sistema de integración y sincronización de precios vía XML-RPC",
        version="2.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Registrar rutas API
    app.include_router(router)

    import re

    def render_template(filename: str, templates_dir: Path) -> str:
        """Renderiza plantillas HTML resolviendo recursivamente tags {% include '...' %}."""
        target_path = templates_dir / filename
        if not target_path.exists():
            return f"<!-- Error: Template {filename} no encontrado -->"
        content = target_path.read_text(encoding="utf-8")
        return re.sub(
            r'\{%\s*include\s+[\'"]([^\'"]+)[\'"]\s*%\}',
            lambda m: render_template(m.group(1).strip(), templates_dir),
            content,
        )

    @app.get("/", response_class=HTMLResponse, tags=["Frontend"])
    def serve_frontend():
        """Sirve la interfaz web modularizada."""
        try:
            return render_template("index.html", settings.TEMPLATES_DIR)
        except Exception as e:
            logger.error(f"Error renderizando template modular: {e}")
            index_file = settings.TEMPLATES_DIR / "index.html"
            if index_file.exists():
                return index_file.read_text(encoding="utf-8")
            return f"<h1>Error al renderizar plantilla: {e}</h1>"

    logger.info("Aplicación FastAPI configurada correctamente.")
    return app


app = create_app()
