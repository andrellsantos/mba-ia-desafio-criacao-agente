"""Dados do condomínio (SQLite).

Todas as regras que protegem os dados moram aqui ou nas tools — nunca no prompt:
 - a exclusividade da reserva é um índice único parcial (area, data) para reservas
   ativas, avaliado pelo banco no instante da gravação (Garantia 5);
 - o código de uma reserva é a chave primária e reservas canceladas continuam na
   tabela (status = 'cancelada'), então um código nunca é reaproveitado.
"""
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import date

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);
CREATE TABLE IF NOT EXISTS apartamentos (numero TEXT PRIMARY KEY, morador TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS areas (id TEXT PRIMARY KEY, nome TEXT NOT NULL, taxa REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reservas (
    codigo TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa', 'cancelada'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reserva_ativa
    ON reservas (area, data) WHERE status = 'ativa';
CREATE TABLE IF NOT EXISTS visitantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (apartamento, nome, data)
);
CREATE TABLE IF NOT EXISTS sessoes (session_id TEXT PRIMARY KEY, apartamento TEXT NOT NULL);
"""


def conectar() -> sqlite3.Connection:
    config.DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_CONDOMINIO, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _semear(con: sqlite3.Connection) -> None:
    """Volta o estado do condomínio ao dos arquivos de dados/ (em uma transação)."""

    def ler(nome: str) -> list[dict]:
        return json.loads((config.DADOS / nome).read_text(encoding="utf-8"))

    con.execute("BEGIN IMMEDIATE")
    try:
        for tabela in ("reservas", "visitantes", "sessoes", "apartamentos", "areas"):
            con.execute(f"DELETE FROM {tabela}")
        con.executemany("INSERT INTO apartamentos VALUES (:numero, :morador)", ler("apartamentos.json"))
        con.executemany("INSERT INTO areas VALUES (:id, :nome, :taxa)", ler("areas.json"))
        con.executemany(
            "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (:codigo, :apartamento, :area, :data)",
            ler("reservas.json"),
        )
        con.executemany(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (:apartamento, :nome, :data)",
            ler("visitantes.json"),
        )
        con.execute("INSERT OR REPLACE INTO meta VALUES ('semeado', '1')")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def iniciar() -> None:
    """Cria o schema e semeia os dados apenas na primeira vez."""
    con = conectar()
    try:
        con.executescript(SCHEMA)
        if con.execute("SELECT 1 FROM meta WHERE chave='semeado'").fetchone() is None:
            _semear(con)
    finally:
        con.close()


def restaurar() -> None:
    con = conectar()
    try:
        con.executescript(SCHEMA)
        _semear(con)
    finally:
        con.close()


# ---------------------------------------------------------------- utilidades
def _sem_acento(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in base if not unicodedata.combining(c)).lower().strip()


def data_valida(texto: str) -> str | None:
    """Devolve a data normalizada (AAAA-MM-DD) ou None."""
    if not isinstance(texto, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto.strip()):
        return None
    try:
        return date.fromisoformat(texto.strip()).isoformat()
    except ValueError:
        return None


def areas() -> list[dict]:
    con = conectar()
    try:
        return [dict(r) for r in con.execute("SELECT id, nome, taxa FROM areas ORDER BY id")]
    finally:
        con.close()


def resolver_area(texto: str) -> dict | None:
    """Aceita o id ('salao-de-festas') ou o nome ('Salão de festas') da área."""
    alvo = _sem_acento(texto or "").replace("-", " ")
    if not alvo:
        return None
    todas = areas()
    for a in todas:
        if alvo in (_sem_acento(a["id"]).replace("-", " "), _sem_acento(a["nome"])):
            return a
    for a in todas:  # tolerância: "salao", "quadra poliesportiva"...
        if alvo in _sem_acento(a["nome"]) or _sem_acento(a["id"]).replace("-", " ") in alvo:
            return a
    return None


def apartamento_existe(numero: str) -> bool:
    con = conectar()
    try:
        return con.execute("SELECT 1 FROM apartamentos WHERE numero=?", (numero,)).fetchone() is not None
    finally:
        con.close()


# ------------------------------------------------------------------- reservas
def situacao_data(apartamento: str, area: str, data: str) -> str:
    """'livre', 'sua' (reserva do próprio apartamento) ou 'ocupada'. Nunca revela o dono."""
    con = conectar()
    try:
        r = con.execute(
            "SELECT apartamento FROM reservas WHERE area=? AND data=? AND status='ativa'", (area, data)
        ).fetchone()
    finally:
        con.close()
    if r is None:
        return "livre"
    return "sua" if r["apartamento"] == apartamento else "ocupada"


def reservar(apartamento: str, area: str, data: str) -> dict:
    """Grava a reserva. A exclusividade vale no INSERT (índice único parcial)."""
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            atual = con.execute(
                "SELECT apartamento, codigo FROM reservas WHERE area=? AND data=? AND status='ativa'",
                (area, data),
            ).fetchone()
            if atual is not None:
                con.execute("ROLLBACK")
                if atual["apartamento"] == apartamento:
                    return {"status": "ja_reservada_por_voce", "codigo": atual["codigo"], "area": area, "data": data}
                return {"status": "indisponivel", "area": area, "data": data}
            while True:  # código novo, jamais igual ao de outra reserva (inclusive cancelada)
                codigo = f"RSV-{secrets.randbelow(9000) + 1000}"
                if con.execute("SELECT 1 FROM reservas WHERE codigo=?", (codigo,)).fetchone() is None:
                    break
            con.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (codigo, apartamento, area, data),
            )
            con.execute("COMMIT")
            return {"status": "reservada", "codigo": codigo, "area": area, "data": data}
        except sqlite3.IntegrityError:  # a outra reserva entrou primeiro
            if con.in_transaction:
                con.execute("ROLLBACK")
            return {"status": "indisponivel", "area": area, "data": data}
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def cancelar(apartamento: str, codigo: str = "", area: str = "", data: str = "") -> dict:
    """Cancela somente reservas ATIVAS do próprio apartamento."""
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if codigo:
                r = con.execute(
                    "SELECT codigo, area, data FROM reservas WHERE codigo=? AND apartamento=? AND status='ativa'",
                    (codigo.strip().upper(), apartamento),
                ).fetchone()
            else:
                r = con.execute(
                    "SELECT codigo, area, data FROM reservas "
                    "WHERE area=? AND data=? AND apartamento=? AND status='ativa'",
                    (area, data, apartamento),
                ).fetchone()
            if r is None:
                con.execute("ROLLBACK")
                return {"status": "nao_encontrada"}
            con.execute("UPDATE reservas SET status='cancelada' WHERE codigo=?", (r["codigo"],))
            con.execute("COMMIT")
            return {"status": "cancelada", **dict(r)}
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def listar_reservas(apartamento: str) -> list[dict]:
    con = conectar()
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT codigo, area, data FROM reservas WHERE apartamento=? AND status='ativa' ORDER BY data, area",
                (apartamento,),
            )
        ]
    finally:
        con.close()


# ------------------------------------------------------------------ visitantes
def autorizar_visitante(apartamento: str, nome: str, data: str) -> dict:
    con = conectar()
    try:
        con.execute(
            "INSERT OR IGNORE INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartamento, nome, data),
        )
        return {"status": "autorizado", "nome": nome, "data": data}
    finally:
        con.close()


def listar_visitantes(apartamento: str) -> list[dict]:
    con = conectar()
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT nome, data FROM visitantes WHERE apartamento=? ORDER BY data, nome", (apartamento,)
            )
        ]
    finally:
        con.close()


# --------------------------------------------------------------------- sessões
def registrar_sessao(session_id: str, apartamento: str) -> None:
    con = conectar()
    try:
        con.execute("INSERT OR REPLACE INTO sessoes VALUES (?, ?)", (session_id, apartamento))
    finally:
        con.close()


def apartamento_da_sessao(session_id: str) -> str | None:
    con = conectar()
    try:
        r = con.execute("SELECT apartamento FROM sessoes WHERE session_id=?", (session_id,)).fetchone()
        return r["apartamento"] if r else None
    finally:
        con.close()
