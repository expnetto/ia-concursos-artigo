"""Executa (modelo × condição × questão × repetição) e grava a resposta BRUTA e tudo em volta dela.

Garantias (revisão de 15/10/2026):
- idempotência: a chave inclui o sha da condição; o que deu 'ok' nunca é refeito;
- teto de gasto que falha FECHADO: antes de cada chamada paga, o pior caso é RESERVADO numa
  transação exclusiva (BEGIN IMMEDIATE), o que também vale entre processos paralelos. A reserva só
  é trocada pelo custo real se a API informar o uso; em erro ou uso ausente, o pior caso fica.
  Há teto por via e teto global (soma de todas as vias pagas);
- teto diário (rpd/tpd) contado em requisições HTTP reais (inclui as novas tentativas) e verificado DENTRO
  da mesma trava da reserva, então vale entre processos paralelos;
- uma execução 'ok' nunca é reaberta, e uma 'reservada' recente pertence a outro processo em andamento
  (só é retomada depois de RESERVA_ABANDONADA_MIN, ex.: Ctrl+C no meio da chamada);
- prompt truncado pela via (Ollama corta sem avisar quando passa do contexto) vira erro, não resposta;
- interrupção segura: Ctrl+C a qualquer momento não deixa nada pela metade.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from iac import catalogo, gemeos, prompts, turma
from iac import db as dbm
from iac import texto as tx
from iac.provedores import Cliente, CotaEsgotada, ErroDefinitivo, ocultar_chaves

MAX_TOKENS_PADRAO = 700
MAX_TOKENS_RACIOCINIO = 6000
TOKENS_POR_CARACTERE = 0.4        # estimativa conservadora para português (pior caso de custo)
FRACAO_MINIMA_TOKENS_PROMPT = 0.5  # tokens_in abaixo de 50% do estimado = prompt cortado pela via
MAX_ERROS_SEGUIDOS = 5
RESERVA_ABANDONADA_MIN = 60     # > pior caso de uma chamada viva (5 tentativas × 300 s + esperas) — revisão 3, M5
JA_TOMADA = "ja_tomada"            # _reservar: a chave está 'ok' ou em andamento noutro processo
SISTEMA = "Você é um assistente que ajuda candidatos a estudar para concursos públicos no Brasil."


def chave(modelo: str, condicao: str, config_sha: str, questao_id: int, repeticao: int) -> str:
    return f"{modelo}|{condicao}@{config_sha[:12]}|{questao_id}|{repeticao}"


def _corpo(modelo, via, condicao, prompt: str, sistema: str | None = None) -> dict:
    corpo = {
        "model": modelo["nome_api"],
        "messages": [{"role": "system", "content": sistema or SISTEMA}, {"role": "user", "content": prompt}],
        "temperature": condicao["temperatura"],
        "max_tokens": MAX_TOKENS_RACIOCINIO if modelo["raciocinio"] else MAX_TOKENS_PADRAO,
    }
    if condicao["json_mode"] and via.json_mode:
        corpo["response_format"] = {"type": "json_object"}
    if condicao["temperatura"] == 0 and via.aceita_seed:
        corpo["seed"] = 20261015
    if via.logprobs and not modelo["raciocinio"]:
        corpo["logprobs"] = True
        corpo["top_logprobs"] = 5
    return corpo


def _estimar_tokens(corpo: dict) -> int:
    return int(sum(len(m["content"]) for m in corpo["messages"]) * TOKENS_POR_CARACTERE) + 50


def _custo(modelo, tokens_in: int | None, tokens_out: int | None) -> float:
    if modelo["preco_in_usd_mtok"] is None:
        return 0.0
    return ((tokens_in or 0) * modelo["preco_in_usd_mtok"] + (tokens_out or 0) * modelo["preco_out_usd_mtok"]) / 1e6


def _pior_caso(modelo, corpo: dict) -> float:
    return _custo(modelo, _estimar_tokens(corpo), corpo["max_tokens"])


def _gasto(conexao, via: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(e.custo_usd), 0) FROM execucao e JOIN modelo m ON m.id = e.modelo_id"
    return conexao.execute(sql + (" WHERE m.via = ?" if via else ""), ((via,) if via else ())).fetchone()[0]


def _requisicoes_hoje(conexao, via: str) -> int:
    hoje = datetime.now(timezone.utc).date().isoformat()
    return conexao.execute(
        """SELECT COALESCE(SUM(e.tentativas), 0) FROM execucao e JOIN modelo m ON m.id = e.modelo_id
           WHERE m.via = ? AND substr(e.criada_em, 1, 10) = ?""", (via, hoje)).fetchone()[0]


def _tokens_hoje(conexao, via: str) -> int:
    hoje = datetime.now(timezone.utc).date().isoformat()
    return conexao.execute(
        """SELECT COALESCE(SUM(COALESCE(e.tokens_in,0) + COALESCE(e.tokens_out,0)), 0) FROM execucao e
           JOIN modelo m ON m.id = e.modelo_id WHERE m.via = ? AND substr(e.criada_em, 1, 10) = ?""",
        (via, hoje)).fetchone()[0]


def _pendentes(conexao, amostra_id: int, modelo, condicao):
    formato = "CE" if condicao["formato"] == "CE" else "ME%"
    filtro_permutacao = "AND q.permutavel = 1" if condicao["embaralhar"] else ""
    questoes = conexao.execute(
        f"""SELECT aq.questao_id FROM amostra_questao aq JOIN questao q ON q.id = aq.questao_id
            WHERE aq.amostra_id = ? AND q.elegivel = 1 AND q.formato LIKE ? {filtro_permutacao}
            ORDER BY aq.questao_id""", (amostra_id, formato)).fetchall()
    feitas = {r[0] for r in conexao.execute(
        "SELECT chave FROM execucao WHERE modelo_id = ? AND condicao_id = ? AND status = 'ok'",
        (modelo["id"], condicao["id"]))}
    for (questao_id,) in questoes:
        for rep in range(1, condicao["repeticoes"] + 1):
            if chave(modelo["slug"], condicao["slug"], condicao["config_sha"], questao_id, rep) not in feitas:
                yield questao_id, rep


def executar(conexao: sqlite3.Connection, amostra: str, modelos: list[str], condicao_slug: str,
             limite: int | None = None, log=print) -> dict[str, int]:
    vias = catalogo.vias()
    teto_global = catalogo.teto_global()
    linha_amostra = conexao.execute("SELECT id FROM amostra WHERE nome = ?", (amostra,)).fetchone()
    if not linha_amostra:
        raise ValueError(f"amostra '{amostra}' não existe (rode `iac amostrar`)")
    condicao = conexao.execute("SELECT * FROM condicao WHERE slug = ?", (condicao_slug,)).fetchone()
    if not condicao:
        raise ValueError(f"condição '{condicao_slug}' não existe (rode `iac modelos`)")
    totais = {"ok": 0, "erro": 0}
    for slug in modelos:
        modelo = conexao.execute("SELECT * FROM modelo WHERE slug = ?", (slug,)).fetchone()
        if not modelo:
            raise ValueError(f"modelo '{slug}' não está no banco (rode `iac modelos`)")
        via = vias[modelo["via"]]
        if via.paga and modelo["preco_in_usd_mtok"] is None:
            log(f"[{slug}] via paga sem preço cadastrado: recusado")
            continue
        parcial = _executar_modelo(conexao, linha_amostra[0], modelo, via, condicao, limite, teto_global, log)
        for k, n in parcial.items():
            totais[k] = totais.get(k, 0) + n
    pulados = totais.get("em_andamento", 0) + totais.get("descartadas", 0)
    if pulados:
        log(f"ATENÇÃO: {pulados} execuções não concluídas nesta rodada (em andamento noutro processo ou retomadas "
            f"por ele); rode de novo quando o outro processo terminar")
    return {k: n for k, n in totais.items() if n or k in ("ok", "erro")}


def _motivo_teto_diario(conexao, via) -> str | None:
    if via.rpd and _requisicoes_hoje(conexao, via.nome) >= via.rpd:
        return f"teto diário de requisições da via {via.nome} ({via.rpd})"
    if via.tpd and _tokens_hoje(conexao, via.nome) >= via.tpd:
        return f"teto diário de tokens da via {via.nome} ({via.tpd})"
    return None


def _reservar(conexao, modelo, via, condicao, questao_id, rep, montado, corpo, teto_global, marca: str) -> str | None:
    """Grava a execução como 'reservada' com o pior caso de custo.

    Devolve None se reservou, JA_TOMADA se a chave já está 'ok' ou em andamento, ou o motivo de parar.
    """
    pior = _pior_caso(modelo, corpo) if via.paga else 0.0
    k = chave(modelo["slug"], condicao["slug"], condicao["config_sha"], questao_id, rep)
    abandonada = (datetime.now(timezone.utc) - timedelta(minutes=RESERVA_ABANDONADA_MIN)).isoformat(timespec="seconds")
    conexao.execute("BEGIN IMMEDIATE")   # trava de escrita: dois processos não reservam o mesmo dinheiro
    try:
        motivo = _motivo_teto_diario(conexao, via)
        if motivo:
            conexao.rollback()
            return motivo
        if via.paga:
            if _gasto(conexao, via.nome) + pior > via.teto_usd:
                conexao.rollback()
                return f"teto da via {via.nome} (US$ {via.teto_usd}) seria ultrapassado"
            if teto_global and _gasto(conexao) + pior > teto_global:
                conexao.rollback()
                return f"teto global (US$ {teto_global}) seria ultrapassado"
        parametros = {k2: v for k2, v in corpo.items() if k2 != "messages"}
        if montado.perfil:                  # turma simulada: o aluno (nota e área) fica gravado na execução
            parametros["perfil"] = montado.perfil
        conexao.execute(
            """INSERT INTO execucao (chave, modelo_id, condicao_id, questao_id, repeticao, permutacao_json, prompt_sha,
                                     parametros_json, status, custo_usd, primeira_em, criada_em)
               VALUES (?,?,?,?,?,?,?,?, 'reservada', ?, ?, ?)
               ON CONFLICT(chave) DO UPDATE SET status = 'reservada', permutacao_json = excluded.permutacao_json,
                   prompt_sha = excluded.prompt_sha, parametros_json = excluded.parametros_json,
                   custo_usd = execucao.custo_usd + excluded.custo_usd, tentativas = execucao.tentativas + 1,
                   criada_em = excluded.criada_em
               WHERE execucao.status = 'erro' OR (execucao.status = 'reservada' AND execucao.criada_em < ?)""",
            (k, modelo["id"], condicao["id"], questao_id, rep,
             json.dumps(montado.permutacao) if montado.permutacao else None, tx.sha256_texto(montado.sha_texto()),
             json.dumps(parametros), pior, dbm.agora(), marca, abandonada))
        if conexao.execute("SELECT changes()").fetchone()[0] == 0:
            conexao.rollback()
            return JA_TOMADA
        conexao.execute("DELETE FROM avaliacao WHERE execucao_id = (SELECT id FROM execucao WHERE chave = ?)", (k,))
        conexao.commit()
    except BaseException:
        conexao.rollback()
        raise
    return None


def _concluir(conexao, k: str, marca: str, modelo, via, pior: float, resposta=None, erro: str | None = None,
              tentativas_http: int = 1) -> bool:
    """`tentativas` acumula requisições HTTP reais; a reserva pré-conta 1, trocada aqui pelo número real.

    Só conclui a PRÓPRIA reserva (status 'reservada' com a marca de tempo dela): se outro processo a retomou
    como abandonada, esta conclusão é descartada em vez de sobrescrever a dele (revisão 3, M5). Devolve se gravou.
    """
    with dbm.transacao(conexao):
        if resposta is None:
            # erro: em via paga, a reserva do pior caso fica (pode ter sido cobrado sem a gente ver)
            cursor = conexao.execute(
                """UPDATE execucao SET status = 'erro', erro = ?, tentativas = tentativas - 1 + ?, criada_em = ?
                   WHERE chave = ? AND status = 'reservada' AND criada_em = ?""",
                (erro[:1000], tentativas_http, dbm.agora(), k, marca))
            return cursor.rowcount == 1
        real = _custo(modelo, resposta.tokens_in, resposta.tokens_out)
        if via.paga and (resposta.tokens_in is None or resposta.tokens_out is None):
            real = pior          # uso não informado: assume o pior caso
        cursor = conexao.execute(
            """UPDATE execucao SET status = 'ok', resposta_bruta = ?, raciocinio_texto = ?, logprobs_json = ?,
                   finish_reason = ?, modelo_retornado = ?, provedor_real = ?, system_fingerprint = ?, erro = NULL,
                   latencia_ms = ?, tokens_in = ?, tokens_out = ?, tokens_raciocinio = ?, tentativas = tentativas - 1 + ?,
                   custo_usd = custo_usd - ? + ?, criada_em = ?
               WHERE chave = ? AND status = 'reservada' AND criada_em = ?""",
            (resposta.texto, resposta.raciocinio, json.dumps(resposta.prob_letras) if resposta.prob_letras else None,
             resposta.finish_reason, resposta.modelo_retornado, resposta.provedor_real, resposta.system_fingerprint,
             resposta.latencia_ms, resposta.tokens_in, resposta.tokens_out, resposta.tokens_raciocinio,
             resposta.tentativas, pior, real, dbm.agora(), k, marca))
        return cursor.rowcount == 1


def _perfil(conexao, condicao, questao_id: int, rep: int):
    """Campos extras do prompt: gêmeo (medida D), aluno da turma (medida B) ou nenhum (None).
    False = gêmeo que não fez o item: não há o que perguntar."""
    if gemeos.e_condicao_de_gemeo(condicao["slug"]):
        perfil = gemeos.perfil(conexao, condicao["slug"], questao_id, rep)
        return False if perfil is None else perfil
    return turma.perfil(conexao, questao_id, rep) if turma.usa_perfil(condicao["template"]) else None


def _executar_modelo(conexao, amostra_id, modelo, via, condicao, limite, teto_global, log) -> dict[str, int]:
    contagem = {"ok": 0, "erro": 0, "em_andamento": 0, "descartadas": 0, "nao_se_aplica": 0}
    try:
        cliente = Cliente(via)
    except ErroDefinitivo as erro:      # ex.: chave ausente: pula este modelo, segue para os outros
        log(f"[{modelo['slug']}] via {via.nome} indisponível: {erro}")
        return contagem
    erros_seguidos = 0
    try:
        for questao_id, rep in _pendentes(conexao, amostra_id, modelo, condicao):
            if limite is not None and contagem["ok"] + contagem["erro"] >= limite:
                break
            perfil = _perfil(conexao, condicao, questao_id, rep)
            if perfil is False:          # gêmeo que não fez o item (medida D): a questão não é perguntada a ele
                contagem["nao_se_aplica"] += 1
                continue
            questao = prompts.carregar_questao(conexao, questao_id)
            montado = prompts.montar(condicao["template"], questao, bool(condicao["embaralhar"]), rep, perfil)
            corpo = _corpo(modelo, via, condicao, montado.texto, montado.sistema)
            marca = dbm.agora()
            motivo = _reservar(conexao, modelo, via, condicao, questao_id, rep, montado, corpo, teto_global, marca)
            if motivo == JA_TOMADA:      # 'ok' já existe ou outro processo está nela: contado, não escondido (M6)
                contagem["em_andamento"] += 1
                continue
            if motivo:
                log(f"[{modelo['slug']}] {motivo}: parando")
                break
            k = chave(modelo["slug"], condicao["slug"], condicao["config_sha"], questao_id, rep)
            pior = _pior_caso(modelo, corpo) if via.paga else 0.0
            try:
                resposta = cliente.completar(corpo)
                if resposta.tokens_in is not None and resposta.tokens_in < FRACAO_MINIMA_TOKENS_PROMPT * _estimar_tokens(corpo):
                    raise RuntimeError(f"prompt truncado pela via: {resposta.tokens_in} tokens de ~{_estimar_tokens(corpo)}")
                if not _concluir(conexao, k, marca, modelo, via, pior, resposta=resposta):
                    contagem["descartadas"] += 1
                    continue
                contagem["ok"] += 1
                erros_seguidos = 0
            except CotaEsgotada as erro:
                _concluir(conexao, k, marca, modelo, via, pior, erro=f"CotaEsgotada: {erro}",
                          tentativas_http=getattr(cliente, "tentativas_ultima_chamada", 1))
                log(f"[{modelo['slug']}] cota esgotada: {erro}")
                break
            except Exception as erro:  # noqa: BLE001 — registrado no banco (com chaves ocultas), não engolido
                _concluir(conexao, k, marca, modelo, via, pior, erro=ocultar_chaves(f"{type(erro).__name__}: {erro}"),
                          tentativas_http=getattr(cliente, "tentativas_ultima_chamada", 1))
                contagem["erro"] += 1
                erros_seguidos += 1
                if erros_seguidos >= MAX_ERROS_SEGUIDOS:
                    log(f"[{modelo['slug']}] {erros_seguidos} erros seguidos, parando. Último: {erro}")
                    break
            if (contagem["ok"] + contagem["erro"]) % 25 == 0:
                log(f"[{modelo['slug']}/{condicao['slug']}] {contagem}")
    finally:
        cliente.fechar()
    log(f"[{modelo['slug']}/{condicao['slug']}] fim: {contagem}")
    return contagem
