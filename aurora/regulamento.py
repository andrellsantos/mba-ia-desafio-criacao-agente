"""Consulta ao regulamento (Garantia 4).

O regulamento nunca entra no prompt nem no histórico. Esta busca léxica escolhe UM
capítulo e devolve só os artigos dele que respondem à pergunta, de modo que nenhum
evento da sessão carrega trechos de capítulos de outros assuntos.
"""
import math
import re
import unicodedata
from functools import lru_cache

from . import config

# Palavras que ajudam a achar o capítulo certo quando o morador usa outros termos.
SINONIMOS = {
    "Disposições gerais": "sindico administracao definicoes aplicativo convencao",
    "Direitos e deveres dos moradores": "direito dever cota condominial taxa condominio fachada varanda sacada janela",
    "Silêncio e boa convivência": "silencio barulho ruido som musica sossego vizinho festa volume",
    "Piscina": "piscina nadar nado banho deck exame dermatologico",
    "Academia, brinquedoteca e playground": "academia brinquedoteca playground musculacao crianca personal",
    "Salão de festas, churrasqueira e quadra": "salao festas churrasqueira quadra reserva evento churrasco esporte",
    "Portaria e segurança": "portaria visitante visita seguranca entrega encomenda camera acesso tag",
    "Animais de estimação": "animal animais pet cachorro cao gato tutor coleira",
    "Mudanças": "mudanca mudar mudei mudando mudo caminhao moveis",
    "Obras e reformas": "obra obras reforma reformas entulho",
    "Garagem e veículos": "garagem veiculo carro moto vaga estacionar estacionamento bicicleta",
    "Coleta de lixo e reciclagem": "lixo reciclagem coleta residuo residuos descarte seletiva",
    "Infrações e penalidades": "multa multas infracao penalidade advertencia recurso",
    "Disposições finais": "alteracao vigencia revogacao",
}

STOP = set(
    "a o as os um uma de do da dos das em no na nos nas por para com sem e ou que qual quais quando como "
    "ate eh sao ser tem ter ha eu meu minha se ao aos pode posso podem e eh isso isto aqui la mais muito "
    "regulamento regra regras condominio residencial aurora existe existem".split()
)


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in base if not unicodedata.combining(c)).lower()


def _tokens(texto: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", _norm(texto)):
        if t in STOP or len(t) < 2:
            continue
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.append(t)
    return out


@lru_cache(maxsize=1)
def _carregar() -> dict:
    texto = (config.DADOS / "regulamento.md").read_text(encoding="utf-8")
    capitulos: list[dict] = []
    atual = None
    artigo = None
    for linha in texto.splitlines():
        if linha.startswith("## "):
            titulo = linha[3:].strip()
            nome = titulo.split(":", 1)[1].strip() if ":" in titulo else titulo
            atual = {"titulo": titulo, "nome": nome, "artigos": []}
            capitulos.append(atual)
            artigo = None
        elif atual is not None and linha.startswith("**Art."):
            rotulo = re.match(r"\*\*(Art\. [^*]+?)\*\*", linha).group(1).rstrip(".")
            artigo = {"rotulo": rotulo, "linhas": [linha]}
            atual["artigos"].append(artigo)
        elif artigo is not None and linha.strip():
            artigo["linhas"].append(linha)
    todos = []
    for c in capitulos:
        for a in c["artigos"]:
            a["texto"] = "\n".join(a["linhas"])
            a["tokens"] = set(_tokens(a["texto"]))
            todos.append(a)
    df: dict[str, int] = {}
    for a in todos:
        for t in a["tokens"]:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + len(todos) / n) for t, n in df.items()}
    for c in capitulos:
        c["chave"] = set(_tokens(c["nome"] + " " + SINONIMOS.get(c["nome"], "")))
    return {"capitulos": capitulos, "idf": idf}


def consultar(pergunta: str, maximo_artigos: int = 4) -> dict:
    base = _carregar()
    consulta = set(_tokens(pergunta))
    if not consulta:
        return {"encontrado": False}
    melhor, melhor_pontos, melhor_artigos = None, 0.0, []
    for c in base["capitulos"]:
        pontos_art = sorted(
            ((sum(base["idf"].get(t, 0) for t in consulta & a["tokens"]), a) for a in c["artigos"]),
            key=lambda p: p[0],
            reverse=True,
        )
        bonus = 3.0 * len(consulta & c["chave"])
        total = bonus + (pontos_art[0][0] if pontos_art else 0) + 0.25 * sum(p for p, _ in pontos_art[1:4])
        if total > melhor_pontos:
            melhor, melhor_pontos, melhor_artigos = c, total, pontos_art
    if melhor is None or melhor_pontos < 2.0:
        return {"encontrado": False}
    topo = melhor_artigos[0][0]
    escolhidos = [a for p, a in melhor_artigos if p > 0 and p >= 0.4 * topo][:maximo_artigos]
    if not escolhidos:
        escolhidos = [a for _, a in melhor_artigos[:maximo_artigos]]
    escolhidos.sort(key=lambda a: melhor["artigos"].index(a))
    return {
        "encontrado": True,
        "capitulo": melhor["titulo"],
        "trechos": [{"artigo": a["rotulo"], "texto": a["texto"]} for a in escolhidos],
    }
