"""Restaura reservas e visitantes ao estado de dados/*.json e apaga as sessões.

Uso:  uv run python -m aurora.restaurar
"""
from . import config, db


def main() -> None:
    db.restaurar()
    apagou = False
    for sufixo in ("", "-wal", "-shm"):
        arq = config.DB_SESSOES.with_name(config.DB_SESSOES.name + sufixo)
        try:
            arq.unlink()
            apagou = True
        except FileNotFoundError:
            pass
        except PermissionError:
            print(f"Aviso: não consegui remover {arq.name}; pare a API e rode de novo.")
    print("Dados restaurados ao estado inicial." + (" Sessões apagadas." if apagou else ""))


if __name__ == "__main__":
    main()
