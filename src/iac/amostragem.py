"""Amostra estratificada e CONGELADA (uma vez criada, nunca é re-sorteada).

Estrato = banca × macroárea × faixa de ano. Alocação proporcional à raiz do tamanho do estrato
(compromisso entre representar o corpus e ter questões suficientes nas áreas pequenas).

Os tamanhos de população (N_h) e de amostra (n_h) de cada estrato são GRAVADOS no momento da
criação (`amostra_estrato`): o peso N_h/n_h não muda se o corpus crescer depois (revisão de 15/10).
Questões usadas em amostras de piloto podem ser excluídas (o prompt foi ajustado nelas).
"""

from __future__ import annotations

import math
import random
import sqlite3
import subprocess
from collections import defaultdict

from iac import config
from iac import db as dbm

MINIMO_POR_ESTRATO = 2


def faixa_ano(ano: int | None) -> str:
    if ano is None:
        return "sem_ano"
    if ano <= 2021:
        return "2020-2021"
    if ano <= 2023:
        return "2022-2023"
    return "2024-2026"


def _versao_codigo() -> str | None:
    try:
        return subprocess.run(["git", "-C", str(config.RAIZ), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def criar(conexao: sqlite3.Connection, nome: str, tamanho: int, semente: int,
          bancas: list[str] | None = None, excluir_amostras: list[str] | None = None,
          formato: str | None = None, so_classificadas: bool = False) -> dict:
    """formato: 'ME' (ME4+ME5) ou 'CE'. so_classificadas: sorteia só entre questões com disciplina, para que o
    estrato de macroárea seja real (a classificação corre em ordem aleatória, então o conjunto é aleatório)."""
    if conexao.execute("SELECT 1 FROM amostra WHERE nome = ?", (nome,)).fetchone():
        raise ValueError(f"amostra '{nome}' já existe e é congelada; use outro nome")
    filtros, parametros = ["q.elegivel = 1"], []
    if formato:
        filtros.append("q.formato LIKE ?")
        parametros.append("ME%" if formato == "ME" else "CE")
    if so_classificadas:
        filtros.append("q.disciplina IS NOT NULL")
    if bancas:
        filtros.append(f"b.sigla IN ({','.join('?' for _ in bancas)})")
        parametros.extend(bancas)
    else:   # pontes do ENEM (banca 'inep') só entram nas amostras próprias (iac.enem), nunca por acaso
        filtros.append("b.sigla <> 'inep'")
    if excluir_amostras:
        filtros.append(f"""q.id NOT IN (SELECT aq.questao_id FROM amostra_questao aq JOIN amostra a ON a.id = aq.amostra_id
                           WHERE a.nome IN ({','.join('?' for _ in excluir_amostras)}))""")
        parametros.extend(excluir_amostras)
    linhas = conexao.execute(
        f"""SELECT q.id, b.sigla, COALESCE(d.macroarea, 'a_classificar') AS macroarea,
                   CAST(substr(COALESCE(q.data_primeira_prova, co.data_prova), 1, 4) AS INTEGER) AS ano, co.ano_edital
            FROM questao q JOIN item i ON i.id = q.item_canonico_id JOIN caderno ca ON ca.id = i.caderno_id
            JOIN concurso co ON co.id = ca.concurso_id JOIN banca b ON b.id = co.banca_id
            LEFT JOIN disciplina d ON d.slug = q.disciplina
            WHERE {' AND '.join(filtros)} ORDER BY q.id""", parametros).fetchall()
    sem_ano = [linha["id"] for linha in linhas if not (linha["ano"] or linha["ano_edital"])]
    if sem_ano:   # o consolidar já exclui por `sem_data`; chegar aqui é defeito, não dado a estratificar
        raise ValueError(f"{len(sem_ano)} questões elegíveis sem ano (ex.: {sem_ano[:5]}); rode `iac consolidar`")
    estratos: dict[str, list[int]] = defaultdict(list)
    for linha in linhas:
        ano = linha["ano"] or linha["ano_edital"]
        estratos[f"{linha['sigla']}|{linha['macroarea']}|{faixa_ano(ano)}"].append(linha["id"])
    if not estratos:
        raise ValueError("nenhuma questão elegível com esses filtros")
    escolhidas = dict(estratos) if tamanho >= len(linhas) else _alocar(estratos, tamanho, random.Random(semente))
    with dbm.transacao(conexao):
        amostra_id = conexao.execute(
            "INSERT INTO amostra (nome, semente, criterio, n_pedido, versao_codigo, criada_em) VALUES (?,?,?,?,?,?)",
            (nome, semente, f"estratificada sqrt(N_h); estrato=banca|macroarea|faixa_ano; bancas={bancas or 'todas'}; "
                            f"excluidas={excluir_amostras or []}; formato={formato or 'todos'}; so_classificadas={so_classificadas}", tamanho, _versao_codigo(), dbm.agora()),
        ).lastrowid
        conexao.executemany(
            "INSERT INTO amostra_estrato (amostra_id, estrato, n_populacao, n_amostra) VALUES (?,?,?,?)",
            [(amostra_id, e, len(estratos[e]), len(ids)) for e, ids in escolhidas.items() if ids])
        conexao.executemany(
            "INSERT INTO amostra_questao (amostra_id, questao_id, estrato) VALUES (?,?,?)",
            [(amostra_id, qid, e) for e, ids in escolhidas.items() for qid in ids])
    total = sum(len(v) for v in escolhidas.values())
    return {"amostra": nome, "questoes": total, "pedido": tamanho, "estratos": len(escolhidas),
            "aviso": "acima do pedido (mínimo por estrato)" if total > tamanho else None}


def _alocar(estratos: dict[str, list[int]], tamanho: int, gerador: random.Random) -> dict[str, list[int]]:
    pesos = {e: math.sqrt(len(ids)) for e, ids in estratos.items()}
    total = sum(pesos.values())
    cotas = {e: min(len(estratos[e]), max(MINIMO_POR_ESTRATO, round(tamanho * p / total))) for e, p in pesos.items()}
    ordem = sorted(estratos, key=lambda e: -len(estratos[e]))
    while sum(cotas.values()) > tamanho:
        reduziu = False
        for e in ordem:
            if sum(cotas.values()) <= tamanho:
                break
            if cotas[e] > MINIMO_POR_ESTRATO:
                cotas[e] -= 1
                reduziu = True
        if not reduziu:
            break
    while sum(cotas.values()) < tamanho and any(cotas[e] < len(estratos[e]) for e in ordem):
        for e in ordem:
            if sum(cotas.values()) >= tamanho:
                break
            if cotas[e] < len(estratos[e]):
                cotas[e] += 1
    return {e: sorted(gerador.sample(sorted(estratos[e]), cotas[e])) for e in sorted(estratos)}
