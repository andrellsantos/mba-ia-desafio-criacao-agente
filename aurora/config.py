"""Configuração central: caminhos, variáveis de ambiente e nome do modelo."""
import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

DADOS = RAIZ / "dados"  # estado inicial do condomínio (somente leitura)
DATA = RAIZ / "data"  # estado mutável (ignorado pelo git)
DB_CONDOMINIO = DATA / "condominio.db"
DB_SESSOES = DATA / "sessions.db"

APP_NAME = "aurora"
MODELO = os.getenv("MODELO") or "gemini-3.1-flash-lite"
