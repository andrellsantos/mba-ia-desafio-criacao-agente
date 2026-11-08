# Regra é regra: o assistente virtual do Residencial Aurora

Assistente de chat para os moradores do Residencial Aurora (reservas do salão de festas, da churrasqueira e da quadra, cancelamentos, autorização de visitantes e dúvidas sobre o regulamento), construído com **Google ADK 2.14.0** e exposto por uma API FastAPI em `http://localhost:8000`.

A ideia central: **o modelo conduz a conversa, o código decide o que é permitido.** Nenhuma das cinco garantias depende do prompt.

## Arquitetura

```
POST /sessoes ─► state da sessão {"apartamento": "101"}  (gravado uma única vez)

assistente  (agente principal: só roteia; sem regulamento nas instruções)
 ├── reservas     sub_agent (transferência)  verificar_disponibilidade, reservar_area,
 │                                           cancelar_reserva, listar_minhas_reservas
 ├── visitantes   sub_agent (transferência)  autorizar_visitante, listar_meus_visitantes
 └── regulamento  AgentTool                  consultar_regulamento
```

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `assistente` | Entende o pedido e o direciona. Não tem tool de dados. | É o agente raiz do `App`. | Cumpre o papel de "agente principal que distribui o trabalho". |
| `reservas` | Reservar, cancelar, listar e checar agenda. | `sub_agent` (transferência). | A reserva com taxa **pede confirmação**. O ADK só retoma a tool se a resposta chegar ao agente que pediu; com transferência esse agente fica ativo na sessão e a retomada funciona, inclusive com sessão em SQLite e depois de reiniciar a API. |
| `visitantes` | Autorizar e listar visitantes. | `sub_agent` (transferência). | Mesmo motivo: autorizar visitante pede confirmação. |
| `regulamento` | Responder dúvidas com base em `dados/regulamento.md`. | `AgentTool` do agente principal. | Só lê, não pede confirmação. Como `AgentTool` roda numa sessão isolada, o texto consultado fica fora do histórico da conversa (Garantia 4). |

Decisões validadas num spike antes da implementação: com `AgentTool`, o pedido de confirmação feito *dentro* do especialista não aparece na sessão principal (a rota de confirmações aceitaria a resposta e nada executaria). Por isso quem pede confirmação é `sub_agent`.

**Armazenamento: SQLite, sem serviço externo.** Dois arquivos em `data/` (ignorado pelo git): `sessions.db` (sessões e eventos do ADK, via `DatabaseSessionService`) e `condominio.db` (apartamentos, áreas, reservas, visitantes). Os arquivos de `dados/` são só o estado inicial e nunca são alterados.

**Modelo:** `gemini-3.1-flash-lite` por padrão (variável `MODELO`). Escolhido porque o plano gratuito do AI Studio permite 15 requisições/minuto nele, contra 5 nos modelos maiores. Chamadas com `429`/`5xx` são repetidas com espera exponencial (`aurora/agents.py`, `_modelo`).

## Garantias

### 1. Cobrança ou acesso só com confirmação
- **Onde:** [aurora/tools.py](aurora/tools.py) — `reservar_area` (ramo `a["taxa"] > 0`) e `autorizar_visitante`; ambas chamam `_pedir_confirmacao` (`tool_context.request_confirmation`) e só gravam quando o ADK reexecuta a tool com `tool_context.tool_confirmation.confirmed == True`. Área sem taxa (quadra) grava direto.
- **Rota:** [aurora/api.py](aurora/api.py) — `responder_confirmacao` monta um `FunctionResponse` do tipo `adk_request_confirmation` e retoma o `Runner`. `pendencias()` lista as confirmações a partir dos eventos gravados (pedido sem resposta), e qualquer outro id — inclusive de uma confirmação já respondida — recebe **409**.
- **Por que não depende do modelo:** a gravação está atrás da confirmação *dentro da tool*; texto do morador ("já confirmei") nunca vira um `FunctionResponse`. Negar devolve `recusada_pelo_morador` sem gravar.

### 2. Cada sessão pertence a um apartamento
- **Onde:** [aurora/api.py](aurora/api.py) `criar_sessao` grava `state={"apartamento": ...}` uma única vez. Em [aurora/tools.py](aurora/tools.py) `_apartamento()` lê esse valor de `tool_context.state`; **nenhuma tool tem parâmetro de apartamento**.
- **Dados alheios nunca chegam ao modelo:** `verificar_disponibilidade` devolve só `disponivel: true/false`; `reservar_area` em data ocupada devolve `indisponivel` sem código nem dono; `cancelar_reserva` e as listagens filtram pelo apartamento da sessão (`db.cancelar`, `db.listar_reservas`, `db.listar_visitantes`) e respondem `nao_encontrada` igualmente quando a reserva é de outro apartamento ou não existe.

### 3. Nada se perde no reinício
- **Onde:** [aurora/api.py](aurora/api.py) `lifespan` usa `DatabaseSessionService` sobre `data/sessions.db`; [aurora/db.py](aurora/db.py) `iniciar()` só semeia os dados na primeira vez (marca `semeado` na tabela `meta`). Reservas canceladas continuam na tabela com `status='cancelada'`, então códigos nunca se repetem.
- O estado das confirmações pendentes é derivado dos eventos persistidos, então também sobrevive ao reinício.

### 4. O regulamento é consultado, não carregado
- **Onde:** [aurora/regulamento.py](aurora/regulamento.py) `consultar` divide o regulamento em capítulos e artigos, escolhe **um** capítulo por relevância e devolve só os artigos dele (ou `encontrado: false`). [aurora/agents.py](aurora/agents.py): o agente `assistente` não recebe o regulamento; `regulamento` é um `AgentTool`, cuja sessão isolada mantém o resultado da consulta fora dos eventos da conversa.

### 5. Dois moradores, uma reserva
- **Onde:** [aurora/db.py](aurora/db.py) — índice único parcial `uq_reserva_ativa ON reservas (area, data) WHERE status = 'ativa'` e `reservar()`, que grava dentro de `BEGIN IMMEDIATE`. Mesmo que a conferência anterior seja ultrapassada, o `INSERT` é recusado pelo banco; a exceção `IntegrityError` vira a resposta normal `indisponivel` (HTTP 200, sem erro de servidor). As tools rodam o acesso ao banco em threads (`asyncio.to_thread`), então a disputa é real e o índice é quem decide.

## Como rodar

**Pré-requisitos:** [uv](https://docs.astral.sh/uv/) e uma chave do [Google AI Studio](https://aistudio.google.com/). O Python 3.14 é o usado no desenvolvimento (o projeto aceita 3.12+).

```bash
cp .env.example .env     # preencha GOOGLE_API_KEY
uv sync
```

Variáveis do `.env`:

| Variável | Descrição |
|---|---|
| `GOOGLE_API_KEY` | Chave do Google AI Studio. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `FALSE` (usa a chave do AI Studio). |
| `MODELO` | Opcional. Modelo Gemini usado por **todos** os agentes (principal e especialistas); padrão `gemini-3.1-flash-lite`. |

### Modelos

Todos os agentes usam o mesmo modelo, definido em `MODELO` (`aurora/config.py`). Limites medidos com uma chave do plano gratuito do AI Studio (podem mudar; confira os limites do seu projeto no AI Studio):

| Modelo | Requisições/min (plano gratuito) | Uso |
|---|---|---|
| `gemini-3.1-flash-lite` | 15 | **Padrão.** Usado no desenvolvimento e na validação do fluxo completo. |
| `gemini-3.5-flash-lite` | 15 | Alternativa equivalente. |
| `gemini-3.5-flash` | 5 | Mais capaz, mas o fluxo completo fica lento no plano gratuito. |
| `gemini-2.5-flash` | 5 | Idem. |

Cada mensagem do morador consome de 2 a 4 chamadas ao modelo (roteamento, especialista, resposta). Quando o limite por minuto é excedido, a chamada é repetida com espera exponencial (até 8 tentativas) em vez de falhar. Para trocar de modelo, basta definir `MODELO` no `.env`, por exemplo `MODELO=gemini-3.5-flash-lite`.

**Restaurar os dados iniciais** (reservas e visitantes voltam aos arquivos de `dados/`; as sessões também são apagadas). Rode com a API parada, porque o arquivo de sessões não pode ser apagado enquanto ela o mantém aberto:

```bash
uv run python -m aurora.restaurar
```

**Subir a API** (em `http://localhost:8000`; não há serviço externo para subir):

```bash
uv run uvicorn aurora.api:app --port 8000
```

Reiniciar a API (Ctrl+C e subir de novo, **sem** restaurar) preserva sessões e dados.

**Verificação rápida:** `scripts/fluxo.py` reproduz o fluxo do avaliador (passos 2 a 14) contra a API no ar.

```bash
uv run python scripts/fluxo.py                              # passos 2-12; imprime S1 e a contagem de eventos
# reinicie a API e então:
uv run python scripts/fluxo.py --depois-reinicio <S1> <eventos>   # passos 13-14
```

> No plano gratuito do AI Studio o limite por minuto é baixo; o fluxo completo pode levar alguns minutos por causa das esperas de repetição.

### Chamando a API

Todas as rotas recebem e devolvem JSON. Rotas com `{session_id}` respondem `404` se a sessão não existe.

| Método e rota | Corpo | Resposta |
|---|---|---|
| `POST /sessoes` | `{"apartamento": "101"}` | `201` `{"session_id": "..."}` |
| `POST /sessoes/{session_id}/mensagens` | `{"texto": "..."}` | `200` `{"resposta": "...", "confirmacoes_pendentes": [...]}` |
| `POST /sessoes/{session_id}/confirmacoes` | `{"id": "...", "confirmado": true}` | `200` (mesmo formato das mensagens) ou `409` se o `id` não está pendente nesta sessão |
| `GET /sessoes/{session_id}/eventos` | — | `200` lista com todos os eventos da sessão, em ordem |
| `GET /apartamentos/{numero}/reservas` | — | `200` `[{"codigo", "area", "data"}]` (lê o banco direto, sem o modelo) |
| `GET /apartamentos/{numero}/visitantes` | — | `200` `[{"nome", "data"}]` (lê o banco direto, sem o modelo) |

Exemplo completo (reserva com cobrança), em um shell bash:

```bash
# 1. cria a sessão do apartamento 101 (o apartamento é fixado aqui e não muda mais)
curl -s -X POST localhost:8000/sessoes -H "Content-Type: application/json" \
  -d '{"apartamento": "101"}'
# {"session_id": "<SESSION_ID>"}

# 2. pede uma reserva; o salão tem taxa, então a execução para esperando confirmação
curl -s -X POST localhost:8000/sessoes/<SESSION_ID>/mensagens -H "Content-Type: application/json" \
  -d '{"texto": "Reserve o salão de festas para 2030-04-20"}'
# {"resposta": "",
#  "confirmacoes_pendentes": [{"id": "adk-...",
#     "acao": "Reservar área com cobrança",
#     "detalhes": {"area": "salao-de-festas", "nome_area": "Salão de festas", "data": "2030-04-20", "taxa": 150.0}}]}

# 3. aprova (ou use "confirmado": false para negar; nada é gravado ao negar)
curl -s -X POST localhost:8000/sessoes/<SESSION_ID>/confirmacoes -H "Content-Type: application/json" \
  -d '{"id": "adk-...", "confirmado": true}'

# 4. confere o efeito direto no banco
curl -s localhost:8000/apartamentos/101/reservas
# [{"codigo": "RSV-....", "area": "salao-de-festas", "data": "2030-04-20"}, ...]
```

Observações:
- Só as ações que geram cobrança (salão de festas e churrasqueira) ou liberam acesso (autorizar visitante) ficam pendentes. Reservar a quadra e cancelar reservas do próprio apartamento executam direto.
- A confirmação só vale pela rota `/confirmacoes`; escrever "já confirmei" na conversa não executa nada. Reenviar a resposta de uma confirmação já respondida devolve `409`.
- Exemplos de mensagens: `Cancele a minha reserva da quadra do dia 2030-03-09.`, `Libera a entrada da Joana Ribeiro no dia 2030-04-21.`, `Até que horas a piscina funciona aos domingos?`, `Quais são as minhas reservas agora?`.
