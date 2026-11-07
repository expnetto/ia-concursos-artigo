"""Descoberta de documentos da FGV.

Fonte 1 (já feita): o índice do crawler de ~/dataprev2026, que varreu as páginas de concursos de
conhecimento.fgv.br em ago/2026 (332 concursos, 22.780 PDFs). Aqui só CLASSIFICAMOS esses links e
registramos os que são caderno de prova ou gabarito objetivo, no período 2020–2026 (os concursos sem
ano no nome entram e são filtrados depois pela data da prova lida no gabarito).

Fonte 2 (a fazer): revarrer a lista de concursos para pegar os publicados depois de ago/2026.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from iac import db as dbm
from iac.ingestao import ano_do_slug, tipo_documento_legado

ANO_INICIAL = 2020
_PROVA = re.compile(r"(tipo[-_ ]?0?\d|cns\d+|caderno)", re.IGNORECASE)
_NAO_PROVA = re.compile(r"(resultado|convoca|edital|comunicado|gabarito|padrao|padr%c3%a3o|espelho|discursiv|"
                        r"recurso|isencao|pne|indigena|negros|retifica|redacao|pratica|titulos|heteroidentific)",
                        re.IGNORECASE)
_GABARITO = re.compile(r"gabarito", re.IGNORECASE)
_NAO_GABARITO = re.compile(r"(discursiv|espelho|avaliacao-formativa|curso|redacao|pratica)", re.IGNORECASE)


def classificar(nome: str) -> str | None:
    if _GABARITO.search(nome) and not _NAO_GABARITO.search(nome):
        return tipo_documento_legado(None, "gabarito", nome)
    if _PROVA.search(nome) and not _NAO_PROVA.search(nome):
        return "prova"
    return None


def descobrir_pelo_indice_legado(conexao: sqlite3.Connection, raiz_legado: Path, ano_inicial: int = ANO_INICIAL,
                                 ano_final: int | None = None) -> dict[str, int]:
    indice = sqlite3.connect(raiz_legado / "data" / "index.sqlite")
    contagem = {"registrados": 0, "fora_do_periodo": 0, "nao_relevantes": 0}
    with dbm.transacao(conexao):
        banca_id = dbm.obter_ou_criar(conexao, "banca", {"sigla": "fgv"}, {"nome": "FGV Conhecimento"})
        concursos = {slug: (titulo, url) for slug, titulo, url in indice.execute("SELECT slug, titulo, url FROM concursos")}
        for slug, url, nome in indice.execute("SELECT slug, url, nome_arquivo FROM pdfs"):
            ano = ano_do_slug(slug)
            if ano is not None and (ano < ano_inicial or (ano_final is not None and ano > ano_final)):
                contagem["fora_do_periodo"] += 1
                continue
            tipo = classificar(nome)
            if tipo is None:
                contagem["nao_relevantes"] += 1
                continue
            titulo, url_concurso = concursos.get(slug, (None, None))
            concurso_id = dbm.obter_ou_criar(conexao, "concurso", {"banca_id": banca_id, "slug": slug},
                                             {"titulo": titulo, "ano_edital": ano, "url_fonte": url_concurso})
            existente = conexao.execute("SELECT id FROM documento WHERE url = ?", (url,)).fetchone()
            if existente:
                continue
            conexao.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (?,?,?,?)",
                            (concurso_id, url, nome, tipo))
            contagem["registrados"] += 1
    indice.close()
    return contagem
