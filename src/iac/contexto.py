"""Janela de contexto dos modelos locais (decisão de 18/10, ver docs/LIMITACOES.md §contexto do Gemma 2).

O Gemma 2 9B com 8.192 tokens de contexto não cabe nos 8 GB da GPU: transborda para a CPU, que fica a 100 %,
e o calor do gabinete leva o SSD ao limite do vigia. Com 4.096 ele cabe — e a maior execução dele até então
usou 2.954 tokens (prompt + resposta), então nenhuma questão perde texto. Duas salvaguardas:

- `equivalencia`: reenvia questões já respondidas e compara com o que está no banco (texto e letra), para
  medir se a troca de contexto muda a resposta além da variação natural do próprio modelo;
- `marcar_insuficientes`: depois de rodar com o contexto menor, toda execução que encostou no limite volta a
  'erro' e é refeita com o contexto maior pelo executor (que só refaz o que não está 'ok').
"""

from __future__ import annotations

import json
import random
import sqlite3

from iac import avaliador, catalogo, executor, prompts
from iac import db as dbm
from iac.provedores import Cliente

MARGEM_TOKENS = 128   # folga abaixo do limite: perto disso o Ollama pode ter cortado o prompt ou a resposta


def insuficientes(conexao: sqlite3.Connection, modelo: str, desde: str, limite: int,
                  margem: int = MARGEM_TOKENS) -> list[int]:
    """Execuções 'ok' do modelo desde `desde` (ISO, UTC) cujo prompt + resposta chegou a `limite - margem`."""
    return [r[0] for r in conexao.execute(
        """SELECT e.id FROM execucao e JOIN modelo m ON m.id = e.modelo_id
           WHERE m.slug = ? AND e.status = 'ok' AND e.criada_em >= ?
             AND COALESCE(e.tokens_in, 0) + COALESCE(e.tokens_out, 0) >= ?""",
        (modelo, desde, limite - margem))]


def marcar_insuficientes(conexao: sqlite3.Connection, modelo: str, desde: str, limite: int,
                         margem: int = MARGEM_TOKENS) -> int:
    """Devolve à fila (status 'erro') as execuções que encostaram no limite; registra cada uma na auditoria."""
    ids = insuficientes(conexao, modelo, desde, limite, margem)
    motivo = f"contexto {limite} insuficiente (>= {limite - margem} tokens): refazer com contexto maior"
    with dbm.transacao(conexao):
        for i in ids:
            conexao.execute("UPDATE execucao SET status = 'erro', erro = ? WHERE id = ?", (motivo, i))
            conexao.execute("DELETE FROM avaliacao WHERE execucao_id = ?", (i,))
            conexao.execute(
                """INSERT OR IGNORE INTO auditoria (entidade, entidade_id, evento, detalhe, criada_em)
                   VALUES ('execucao', ?, 'contexto_insuficiente', ?, ?)""", (i, motivo, dbm.agora()))
    return len(ids)


def _letra(texto: str | None, formato: str, finish_reason: str | None) -> str | None:
    return avaliador.interpretar(texto or "", formato, finish_reason)["resposta_exibida"]


def equivalencia(conexao: sqlite3.Connection, modelo_slug: str, condicao_slug: str, amostra: str,
                 n: int, semente: int = 20261018, log=print) -> dict:
    """Reenvia `n` execuções 'ok' sorteadas (semente fixa) e compara com as gravadas. Não escreve no banco."""
    modelo = conexao.execute("SELECT * FROM modelo WHERE slug = ?", (modelo_slug,)).fetchone()
    condicao = conexao.execute("SELECT * FROM condicao WHERE slug = ?", (condicao_slug,)).fetchone()
    via = catalogo.vias()[modelo["via"]]
    linhas = conexao.execute(
        """SELECT e.id, e.questao_id, e.repeticao, e.resposta_bruta, e.finish_reason, q.formato
           FROM execucao e JOIN questao q ON q.id = e.questao_id
           JOIN amostra_questao aq ON aq.questao_id = e.questao_id JOIN amostra a ON a.id = aq.amostra_id
           WHERE e.modelo_id = ? AND e.condicao_id = ? AND e.status = 'ok' AND a.nome = ?
           ORDER BY e.id""", (modelo["id"], condicao["id"], amostra)).fetchall()
    sorteadas = random.Random(semente).sample(list(linhas), min(n, len(linhas)))
    cliente = Cliente(via)
    resultado = {"n": 0, "texto_igual": 0, "letra_igual": 0, "max_tokens_total": 0, "divergentes": []}
    try:
        for linha in sorteadas:
            questao = prompts.carregar_questao(conexao, linha["questao_id"])
            montado = prompts.montar(condicao["template"], questao, bool(condicao["embaralhar"]), linha["repeticao"])
            nova = cliente.completar(executor._corpo(modelo, via, condicao, montado.texto))
            antes = _letra(linha["resposta_bruta"], linha["formato"], linha["finish_reason"])
            depois = _letra(nova.texto, linha["formato"], nova.finish_reason)
            resultado["n"] += 1
            resultado["texto_igual"] += nova.texto == linha["resposta_bruta"]
            resultado["letra_igual"] += antes == depois
            resultado["max_tokens_total"] = max(resultado["max_tokens_total"],
                                                (nova.tokens_in or 0) + (nova.tokens_out or 0))
            if antes != depois:
                resultado["divergentes"].append({"execucao_id": linha["id"], "antes": antes, "depois": depois})
            if resultado["n"] % 25 == 0:
                log(json.dumps({k: v for k, v in resultado.items() if k != "divergentes"}))
    finally:
        cliente.fechar()
    return resultado
