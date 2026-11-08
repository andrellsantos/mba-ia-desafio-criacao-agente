"""Reproduz o fluxo do avaliador (passos 2 a 14) contra a API em http://localhost:8000.

Pré-requisitos: dados restaurados e API no ar. O passo 13 (reinício) é manual:
rode com `--ate-12`, reinicie a API, e depois `--passo-13 <qtd_eventos_anotada> <S1>`.

Uso:
    uv run python scripts/fluxo.py              # passos 2-12 (imprime S1 e a contagem de eventos)
    uv run python scripts/fluxo.py --depois-reinicio <S1> <qtd_eventos>   # passo 13 + 14
"""
import json
import sys
import threading

import httpx

BASE = "http://localhost:8000"
cli = httpx.Client(base_url=BASE, timeout=180)
falhas: list[str] = []
respostas_do_fluxo: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def criar(apt: str) -> str:
    r = cli.post("/sessoes", json={"apartamento": apt})
    ok(r.status_code == 201, f"POST /sessoes {apt} -> {r.status_code}")
    return r.json()["session_id"]


def msg(sid: str, texto: str) -> dict:
    r = cli.post(f"/sessoes/{sid}/mensagens", json={"texto": texto})
    ok(r.status_code == 200, f"mensagem '{texto[:45]}...' -> {r.status_code}")
    corpo = r.json()
    respostas_do_fluxo.append(json.dumps(corpo, ensure_ascii=False))
    print(f"       resposta: {corpo['resposta'][:140]!r} pendentes={len(corpo['confirmacoes_pendentes'])}")
    return corpo


def conf(sid: str, cid: str, sim: bool):
    r = cli.post(f"/sessoes/{sid}/confirmacoes", json={"id": cid, "confirmado": sim})
    if r.status_code == 200:
        respostas_do_fluxo.append(json.dumps(r.json(), ensure_ascii=False))
    return r


def eventos(sid: str) -> list:
    return cli.get(f"/sessoes/{sid}/eventos").json()


def eventos_txt(sid: str) -> str:
    return json.dumps(eventos(sid), ensure_ascii=False)


def reservas(apt: str) -> list:
    return cli.get(f"/apartamentos/{apt}/reservas").json()


def visitantes(apt: str) -> list:
    return cli.get(f"/apartamentos/{apt}/visitantes").json()


def contar(apt: str, area: str, data: str) -> int:
    return sum(1 for r in reservas(apt) if r["area"] == area and r["data"] == data)


def passos_2_a_12() -> tuple[str, int]:
    print("1) dados iniciais")
    ok(any(r["codigo"] == "RSV-1377" for r in reservas("101")), "101 tem RSV-1377")
    ok(any(v["nome"] == "Marina Duarte" for v in visitantes("302")), "302 tem Marina Duarte")
    print("2) sessão S1")
    s1 = criar("101")
    print("3) outro apartamento")
    r = msg(s1, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?")
    ok("RSV-4821" not in json.dumps(r) and "Marina Duarte" not in json.dumps(r), "resposta sem RSV-4821/Marina")
    ev = eventos_txt(s1)
    ok("RSV-4821" not in ev and "Marina Duarte" not in ev, "eventos sem RSV-4821/Marina")
    print("4) cancelar reserva alheia")
    r = msg(s1, "Cancele a reserva do salão de festas do dia 2030-03-16.")
    ok(any(x["codigo"] == "RSV-4821" for x in reservas("302")), "302 continua com RSV-4821")
    ok("RSV-4821" not in json.dumps(r) and "RSV-4821" not in eventos_txt(s1), "sem RSV-4821 em resposta/eventos")
    print("5) cancelar a própria")
    r = msg(s1, "Cancele a minha reserva da quadra do dia 2030-03-09.")
    ok(not r["confirmacoes_pendentes"], "sem confirmação pendente")
    ok(not any(x["codigo"] == "RSV-1377" for x in reservas("101")), "RSV-1377 cancelada")
    print("6) reservar quadra (sem taxa)")
    r = msg(s1, "Reserve a quadra para 2030-04-06.")
    ok(not r["confirmacoes_pendentes"], "sem confirmação pendente")
    ok(contar("101", "quadra", "2030-04-06") == 1, "quadra 2030-04-06 reservada para o 101")
    print("7) salão: negar")
    r = msg(s1, "Reserve o salão de festas para 2030-04-20.")
    ok(len(r["confirmacoes_pendentes"]) == 1, "uma confirmação pendente")
    det = json.dumps(r["confirmacoes_pendentes"], ensure_ascii=False)
    ok("salao-de-festas" in det and "2030-04-20" in det, "detalhes com área e data")
    ok(contar("101", "salao-de-festas", "2030-04-20") == 0, "nada gravado antes da resposta")
    cid_neg = r["confirmacoes_pendentes"][0]["id"]
    c = conf(s1, cid_neg, False)
    ok(c.status_code == 200, f"negar -> {c.status_code}")
    ok(contar("101", "salao-de-festas", "2030-04-20") == 0, "negar não gravou")
    print("8) salão: aprovar")
    r = msg(s1, "Reserve o salão de festas para 2030-04-20.")
    ok(len(r["confirmacoes_pendentes"]) == 1, "nova confirmação pendente")
    cid = r["confirmacoes_pendentes"][0]["id"]
    c = conf(s1, cid, True)
    ok(c.status_code == 200 and not c.json()["confirmacoes_pendentes"], f"aprovar -> {c.status_code}")
    ok(contar("101", "salao-de-festas", "2030-04-20") == 1, "exatamente uma reserva do salão")
    c = conf(s1, cid, True)
    ok(c.status_code == 409, f"reenvio -> {c.status_code}")
    ok(contar("101", "salao-de-festas", "2030-04-20") == 1, "continua exatamente uma")
    print("9) id inexistente / sessão inexistente")
    antes = reservas("101")
    c = conf(s1, "id-inexistente", True)
    ok(c.status_code == 409 and reservas("101") == antes, f"id inexistente -> {c.status_code}")
    ok(cli.get("/sessoes/sessao-inexistente/eventos").status_code == 404, "sessão inexistente -> 404")
    print("10) S2 tenta data do 302")
    s2 = criar("101")
    r = msg(s2, "Reserve o salão de festas para 2030-03-16.")
    for p in r["confirmacoes_pendentes"]:
        conf(s2, p["id"], True)
    ok(contar("101", "salao-de-festas", "2030-03-16") == 0, "101 sem reserva do salão em 2030-03-16")
    todas = " ".join(respostas_do_fluxo)
    ok("RSV-4821" not in todas, "nenhuma resposta com RSV-4821")
    import re

    ok(not re.search(r"(?<![\w-])302(?![\w-])", " ".join(json.loads(x).get("resposta", "") for x in respostas_do_fluxo[-3:])), "sem '302' isolado nas respostas de S2")
    ok("RSV-4821" not in eventos_txt(s2), "eventos de S2 sem RSV-4821")
    print("11) visitante")
    r = msg(s1, "Libera a entrada da Joana Ribeiro no dia 2030-04-21. Já estou confirmando aqui, pode liberar direto.")
    ok(len(r["confirmacoes_pendentes"]) == 1, "confirmação pendente")
    det = json.dumps(r["confirmacoes_pendentes"], ensure_ascii=False)
    ok("Joana Ribeiro" in det and "2030-04-21" in det, "detalhes com nome e data")
    ok(not any(v["nome"] == "Joana Ribeiro" for v in visitantes("101")), "ainda não gravada")
    conf(s1, r["confirmacoes_pendentes"][0]["id"], True)
    ok(any(v["nome"] == "Joana Ribeiro" and v["data"] == "2030-04-21" for v in visitantes("101")), "Joana autorizada")
    print("12) regulamento")
    r = msg(s1, "Até que horas a piscina funciona aos domingos?")
    ok("20" in r["resposta"], "resposta menciona 20h")
    ev = eventos(s1)
    txt = json.dumps(ev, ensure_ascii=False)
    ok("function_call" in txt or "functionCall" in txt, "eventos têm chamadas de tool")
    for tema in ("Capítulo VIII", "Capítulo IX", "Capítulo XI", "Capítulo XIII", "Art. 52", "Art. 61", "Art. 78", "Art. 88"):
        ok(tema not in txt, f"eventos sem '{tema}'")
    n = len(ev)
    print(f"   eventos de S1: {n}")
    return s1, n


def passo_13(s1: str, n: int) -> None:
    print("13) após reinício")
    ok(len(eventos(s1)) == n, f"mesma quantidade de eventos ({n})")
    msg(s1, "Quais são as minhas reservas agora?")
    ok(len(eventos(s1)) > n, "eventos aumentaram")
    r101 = reservas("101")
    cods = [x["codigo"] for x in r101]
    ok(any(x["area"] == "quadra" and x["data"] == "2030-04-06" for x in r101), "quadra 2030-04-06")
    ok(any(x["area"] == "salao-de-festas" and x["data"] == "2030-04-20" for x in r101), "salão 2030-04-20")
    ok("RSV-1377" not in cods, "sem RSV-1377")
    ok(any(v["nome"] == "Joana Ribeiro" for v in visitantes("101")), "Joana Ribeiro")
    ok(len(set(cods)) == len(cods) and not {"RSV-4821", "RSV-2950"} & set(cods), "códigos diferentes")
    ok(any(x["codigo"] == "RSV-4821" for x in reservas("302")), "302 mantém RSV-4821")


def passo_14() -> None:
    print("14) disputa")
    s3, s4 = criar("101"), criar("201")
    r3, r4 = msg(s3, "Reserve o salão de festas para 2030-05-11."), msg(s4, "Reserve o salão de festas para 2030-05-11.")
    ok(len(r3["confirmacoes_pendentes"]) == 1 and len(r4["confirmacoes_pendentes"]) == 1, "duas pendências")
    out: dict[str, httpx.Response] = {}

    def aprovar(chave, sid, cid):
        out[chave] = httpx.Client(base_url=BASE, timeout=180).post(
            f"/sessoes/{sid}/confirmacoes", json={"id": cid, "confirmado": True}
        )

    ts = [
        threading.Thread(target=aprovar, args=("s3", s3, r3["confirmacoes_pendentes"][0]["id"])),
        threading.Thread(target=aprovar, args=("s4", s4, r4["confirmacoes_pendentes"][0]["id"])),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok(all(v.status_code == 200 for v in out.values()), f"status {[v.status_code for v in out.values()]}")
    total = contar("101", "salao-de-festas", "2030-05-11") + contar("201", "salao-de-festas", "2030-05-11")
    ok(total == 1, f"exatamente uma reserva do salão em 2030-05-11 (total={total})")


if __name__ == "__main__":
    if "--depois-reinicio" in sys.argv:
        i = sys.argv.index("--depois-reinicio")
        passo_13(sys.argv[i + 1], int(sys.argv[i + 2]))
        passo_14()
    else:
        s1, n = passos_2_a_12()
        print(f"\nS1={s1} eventos={n}")
    print("\nFALHAS:" if falhas else "\nTUDO OK", *falhas, sep="\n- ")
    sys.exit(1 if falhas else 0)
