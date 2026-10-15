import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Base Directory calculations
# src/core/config.py -> parent of src is the project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BASE_DIR / "src"
TEMPLATES_DIR = SRC_DIR / "web" / "templates"

# Detect Serverless environment (Vercel / AWS Lambda)
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
if IS_SERVERLESS:
    import tempfile
    TMP_DIR = Path(tempfile.gettempdir())
    REPORTS_DIR = TMP_DIR / "reports"
    DATA_SAMPLES_DIR = TMP_DIR / "data_samples"
else:
    REPORTS_DIR = BASE_DIR / "reports"
    DATA_SAMPLES_DIR = BASE_DIR / "data_samples"

# Ensure runtime directories exist (with fallback to /tmp if read-only)
try:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    import tempfile
    TMP_DIR = Path(tempfile.gettempdir())
    REPORTS_DIR = TMP_DIR / "reports"
    DATA_SAMPLES_DIR = TMP_DIR / "data_samples"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# Load .env file from project root
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()


class Settings:
    """Configuración centralizada del microservicio."""
    ODOO_URL: str = os.getenv("ODOO_URL", "").strip().rstrip("/")
    ODOO_DB: str = os.getenv("ODOO_DB", "").strip()
    ODOO_USERNAME: str = os.getenv("ODOO_USERNAME", "").strip()
    ODOO_PASSWORD: str = os.getenv("ODOO_PASSWORD", "").strip()

    SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0").strip()
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8001"))

    # Rutas
    BASE_DIR: Path = BASE_DIR
    SRC_DIR: Path = SRC_DIR
    REPORTS_DIR: Path = REPORTS_DIR
    DATA_SAMPLES_DIR: Path = DATA_SAMPLES_DIR
    TEMPLATES_DIR: Path = TEMPLATES_DIR
    IS_SERVERLESS: bool = IS_SERVERLESS

    @classmethod
    def get_missing_vars(cls) -> list:
        missing = []
        if not cls.ODOO_URL:
            missing.append("ODOO_URL")
        if not cls.ODOO_DB:
            missing.append("ODOO_DB")
        if not cls.ODOO_USERNAME:
            missing.append("ODOO_USERNAME")
        if not cls.ODOO_PASSWORD:
            missing.append("ODOO_PASSWORD")
        return missing

    @classmethod
    def validate(cls) -> None:
        """Valida que todas las credenciales obligatorias estén presentes."""
        missing = cls.get_missing_vars()
        if missing:
            msg = f"[WARN] Faltan variables de entorno requeridas: {', '.join(missing)}"
            print(msg)
            # En Vercel o serverless no abortar para permitir arranque y health check
            if not cls.IS_SERVERLESS and not os.getenv("IGNORE_ENV_EXIT"):
                sys.exit(1)


settings = Settings()

