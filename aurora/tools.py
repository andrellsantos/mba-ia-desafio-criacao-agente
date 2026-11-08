"""Tools do assistente.

Regras de código que valem independentemente do que o modelo escreve:
 - Garantia 2: nenhuma tool recebe apartamento. O apartamento vem de
   `tool_context.state["apartamento"]`, gravado uma única vez na criação da sessão.
   Dados de outros apartamentos nunca chegam ao modelo: a checagem de agenda devolve
   só "livre/ocupada" e as consultas e cancelamentos filtram pelo apartamento da sessão.
 - Garantia 1: reservar área com taxa e autorizar visitante pedem confirmação por
   `tool_context.request_confirmation`. A tool só grava quando é reexecutada pelo ADK com
   `tool_context.tool_confirmation.confirmed == True`, o que só acontece quando a rota
   de confirmações envia o FunctionResponse. Texto do morador nunca chega nesse caminho.
 - Garantia 5: `db.reservar` grava com o índice único parcial do SQLite.
"""
import asyncio

from google.adk.tools import ToolContext

from . import db, regulamento


def _apartamento(tool_context: ToolContext) -> str | None:
    apt = tool_context.state.get("apartamento")
    return apt if isinstance(apt, str) and apt else None


def _sem_sessao() -> dict:
    return {"status": "erro", "mensagem": "Sessão sem apartamento associado."}


def _area_ou_erro(area: str) -> tuple[dict | None, dict | None]:
    a = db.resolver_area(area)
    if a is None:
        validas = ", ".join(x["id"] for x in db.areas())
        return None, {"status": "erro", "mensagem": f"Área desconhecida. Áreas válidas: {validas}."}
    return a, None


def _data_ou_erro(data: str) -> tuple[str | None, dict | None]:
    d = db.data_valida(data)
    if d is None:
        return None, {"status": "erro", "mensagem": "Data inválida. Use o formato AAAA-MM-DD."}
    return d, None


def _pedir_confirmacao(tool_context: ToolContext, acao: str, detalhes: dict) -> dict:
    tool_context.request_confirmation(
        hint="Aguardando confirmação do morador pela rota de confirmações.",
        payload={"acao": acao, "detalhes": detalhes},
    )
    tool_context.actions.skip_summarization = True
    return {"status": "aguardando_confirmacao", "acao": acao, "detalhes": detalhes}


# ------------------------------------------------------------------- reservas
async def verificar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
    """Informa se a área está livre em uma data. Não revela quem reservou.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    a, erro = _area_ou_erro(area)
    if erro:
        return erro
    d, erro = _data_ou_erro(data)
    if erro:
        return erro
    situacao = await asyncio.to_thread(db.situacao_data, apt, a["id"], d)
    return {"area": a["id"], "data": d, "disponivel": situacao == "livre"}


async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
    """Reserva uma área comum para o apartamento do morador.

    Áreas com taxa só são reservadas depois que o morador confirma pela rota de
    confirmações do sistema; a tool devolve `aguardando_confirmacao` até lá.

    Args:
        area: id ou nome da área (salao-de-festas, churrasqueira, quadra).
        data: data no formato AAAA-MM-DD.
    """
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    a, erro = _area_ou_erro(area)
    if erro:
        return erro
    d, erro = _data_ou_erro(data)
    if erro:
        return erro

    if a["taxa"] > 0:  # gera cobrança => precisa de confirmação do sistema
        confirmacao = tool_context.tool_confirmation
        if confirmacao is None:
            situacao = await asyncio.to_thread(db.situacao_data, apt, a["id"], d)
            if situacao == "ocupada":
                return {"status": "indisponivel", "area": a["id"], "data": d}
            if situacao == "sua":
                return {"status": "ja_reservada_por_voce", "area": a["id"], "data": d}
            return _pedir_confirmacao(
                tool_context,
                "Reservar área com cobrança",
                {"area": a["id"], "nome_area": a["nome"], "data": d, "taxa": a["taxa"]},
            )
        if not confirmacao.confirmed:
            return {"status": "recusada_pelo_morador", "area": a["id"], "data": d}
    return await asyncio.to_thread(db.reservar, apt, a["id"], d)


async def cancelar_reserva(tool_context: ToolContext, codigo: str = "", area: str = "", data: str = "") -> dict:
    """Cancela uma reserva do apartamento do morador (sem confirmação).

    Informe o código da reserva OU a área e a data.

    Args:
        codigo: código da reserva (ex.: RSV-1234), opcional.
        area: id ou nome da área, usado quando não há código.
        data: data AAAA-MM-DD, usada quando não há código.
    """
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    if codigo.strip():
        return await asyncio.to_thread(db.cancelar, apt, codigo=codigo)
    a, erro = _area_ou_erro(area)
    if erro:
        return erro
    d, erro = _data_ou_erro(data)
    if erro:
        return erro
    return await asyncio.to_thread(db.cancelar, apt, area=a["id"], data=d)


async def listar_minhas_reservas(tool_context: ToolContext) -> dict:
    """Lista as reservas ativas do apartamento do morador."""
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    return {"reservas": await asyncio.to_thread(db.listar_reservas, apt)}


# ------------------------------------------------------------------ visitantes
async def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
    """Autoriza a entrada de um visitante no prédio em uma data.

    Libera acesso ao prédio: só é gravado depois que o morador confirma pela rota
    de confirmações do sistema; a tool devolve `aguardando_confirmacao` até lá.

    Args:
        nome: nome completo do visitante.
        data: data da visita no formato AAAA-MM-DD.
    """
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    nome = (nome or "").strip()
    if not nome:
        return {"status": "erro", "mensagem": "Informe o nome do visitante."}
    d, erro = _data_ou_erro(data)
    if erro:
        return erro
    confirmacao = tool_context.tool_confirmation
    if confirmacao is None:
        return _pedir_confirmacao(tool_context, "Autorizar entrada de visitante", {"nome": nome, "data": d})
    if not confirmacao.confirmed:
        return {"status": "recusada_pelo_morador", "nome": nome, "data": d}
    return await asyncio.to_thread(db.autorizar_visitante, apt, nome, d)


async def listar_meus_visitantes(tool_context: ToolContext) -> dict:
    """Lista os visitantes autorizados pelo apartamento do morador."""
    apt = _apartamento(tool_context)
    if apt is None:
        return _sem_sessao()
    return {"visitantes": await asyncio.to_thread(db.listar_visitantes, apt)}


# ----------------------------------------------------------------- regulamento
def consultar_regulamento(pergunta: str) -> dict:
    """Busca no regulamento interno os artigos que respondem à pergunta.

    Args:
        pergunta: a dúvida do morador, em linguagem natural.
    """
    return regulamento.consultar(pergunta)
