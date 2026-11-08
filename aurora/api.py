"""API HTTP do assistente (contrato do desafio).

Uso:  uv run uvicorn aurora.api:app --port 8000
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel

from . import config, db
from .agents import construir_app

log = logging.getLogger("aurora.api")

CONFIRMACAO = "adk_request_confirmation"  # nome da function call que o ADK usa para pedir confirmação


class NovaSessao(BaseModel):
    apartamento: str


class NovaMensagem(BaseModel):
    texto: str


class RespostaConfirmacao(BaseModel):
    id: str
    confirmado: bool


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.iniciar()  # cria schema/semeia na primeira vez; reinícios preservam tudo
    servico = DatabaseSessionService(
        f"sqlite+aiosqlite:///{config.DB_SESSOES.as_posix()}", connect_args={"timeout": 30}
    )
    app.state.servico = servico
    app.state.runner = Runner(app=construir_app(), session_service=servico)
    app.state.travas = {}
    yield


app = FastAPI(title="Residencial Aurora", lifespan=lifespan)


# ------------------------------------------------------------------- utilidades
def _trava(request: Request, session_id: str) -> asyncio.Lock:
    return request.app.state.travas.setdefault(session_id, asyncio.Lock())


async def _carregar_sessao(request: Request, session_id: str):
    apartamento = await asyncio.to_thread(db.apartamento_da_sessao, session_id)
    if apartamento is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    sessao = await request.app.state.servico.get_session(
        app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
    )
    if sessao is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada")
    return apartamento, sessao


def pendencias(sessao) -> list[dict]:
    """Confirmações pendentes = pedidos do ADK que ainda não receberam resposta.

    Derivado só dos eventos gravados, então sobrevive a reinícios e não existe
    nenhum estado paralelo que possa divergir da sessão.
    """
    pedidos: dict[str, dict] = {}
    respondidas: set[str] = set()
    for ev in sessao.events:
        for fc in ev.get_function_calls():
            if fc.name == CONFIRMACAO and fc.id:
                pedidos[fc.id] = fc.args or {}
        for fr in ev.get_function_responses():
            if fr.name == CONFIRMACAO and fr.id:
                respondidas.add(fr.id)
    saida = []
    for cid, args in pedidos.items():
        if cid in respondidas:
            continue
        original = args.get("originalFunctionCall") or {}
        payload = (args.get("toolConfirmation") or {}).get("payload") or {}
        saida.append(
            {
                "id": cid,
                "acao": payload.get("acao") or original.get("name", ""),
                "detalhes": payload.get("detalhes") or original.get("args") or {},
            }
        )
    return saida


def _texto_final(eventos) -> str:
    texto = ""
    for ev in eventos:
        if ev.is_final_response() and ev.content and ev.content.parts:
            partes = [p.text for p in ev.content.parts if p.text and not getattr(p, "thought", False)]
            if partes:
                texto = "".join(partes)
    return texto


async def _executar(request: Request, apartamento: str, session_id: str, conteudo: types.Content) -> dict:
    eventos = []
    async for ev in request.app.state.runner.run_async(
        user_id=apartamento, session_id=session_id, new_message=conteudo
    ):
        eventos.append(ev)
    sessao = await request.app.state.servico.get_session(
        app_name=config.APP_NAME, user_id=apartamento, session_id=session_id
    )
    return {"resposta": _texto_final(eventos), "confirmacoes_pendentes": pendencias(sessao)}


# ------------------------------------------------------------------------ rotas
@app.post("/sessoes", status_code=201)
async def criar_sessao(corpo: NovaSessao, request: Request):
    if not await asyncio.to_thread(db.apartamento_existe, corpo.apartamento):
        raise HTTPException(status_code=422, detail="Apartamento desconhecido")
    # O apartamento é definido aqui, uma única vez, e vive no state da sessão.
    sessao = await request.app.state.servico.create_session(
        app_name=config.APP_NAME, user_id=corpo.apartamento, state={"apartamento": corpo.apartamento}
    )
    await asyncio.to_thread(db.registrar_sessao, sessao.id, corpo.apartamento)
    return {"session_id": sessao.id}


@app.post("/sessoes/{session_id}/mensagens")
async def enviar_mensagem(session_id: str, corpo: NovaMensagem, request: Request):
    apartamento, _ = await _carregar_sessao(request, session_id)
    async with _trava(request, session_id):
        conteudo = types.Content(role="user", parts=[types.Part(text=corpo.texto)])
        return await _executar(request, apartamento, session_id, conteudo)


@app.post("/sessoes/{session_id}/confirmacoes")
async def responder_confirmacao(session_id: str, corpo: RespostaConfirmacao, request: Request):
    apartamento, _ = await _carregar_sessao(request, session_id)
    async with _trava(request, session_id):
        # Relê a sessão já dentro da trava: a pendência só vale se ainda estiver em aberto.
        _, sessao = await _carregar_sessao(request, session_id)
        if corpo.id not in {p["id"] for p in pendencias(sessao)}:
            raise HTTPException(status_code=409, detail="Não há confirmação pendente com esse id nesta sessão")
        resposta = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=corpo.id, name=CONFIRMACAO, response={"confirmed": corpo.confirmado}
                    )
                )
            ],
        )
        return await _executar(request, apartamento, session_id, resposta)


@app.get("/sessoes/{session_id}/eventos")
async def listar_eventos(session_id: str, request: Request):
    _, sessao = await _carregar_sessao(request, session_id)
    return [ev.model_dump(mode="json", exclude_none=True) for ev in sessao.events]


# ----------------------------------------------------------- rotas de verificação
@app.get("/apartamentos/{numero}/reservas")
def reservas_do_apartamento(numero: str):
    return db.listar_reservas(numero)


@app.get("/apartamentos/{numero}/visitantes")
def visitantes_do_apartamento(numero: str):
    return db.listar_visitantes(numero)
