"""Coleta de documentos (provas e gabaritos) nos sites oficiais das bancas.

Duas etapas separadas, ambas idempotentes:
  descobrir — registra concursos e URLs de documentos (sem baixar);
  baixar    — baixa os documentos pendentes para o acervo por sha256.

`documento.erro` com prefixo `recusado:` é permanente (política); `transitorio:` e outros erros são
tentados de novo na próxima rodada.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from typing import Callable
from urllib.parse import urlparse

from iac import db as dbm
from iac.coleta.http import ClienteEducado, Recusado, Transitorio
from iac.ingestao import guardar_pdf

HOSTS_POR_BANCA = {
    "fgv": {"conhecimento.fgv.br", "netstorage.fgv.br", "www.fgv.br"},
    "cebraspe": {"cdn.cebraspe.org.br", "www.cespe.unb.br"},
}

# Recusas gravadas pelo código antigo que, na verdade, eram falhas passageiras (revisão 2, ALTO-2)
_RECUSAS_QUE_ERAM_TRANSITORIAS = ("recusado: host não resolve%", "recusado: robots.txt não permite%",
                                  "recusado: HTTP 429%", "recusado: HTTP 5%")


def liberar_recusas_transitorias(conexao: sqlite3.Connection) -> int:
    """Devolve à fila os documentos que o downloader antigo recusou por falha de rede/robots indisponível."""
    onde = " OR ".join("erro LIKE ?" for _ in _RECUSAS_QUE_ERAM_TRANSITORIAS)
    with dbm.transacao(conexao):
        cursor = conexao.execute(f"UPDATE documento SET erro = NULL WHERE sha256 IS NULL AND ({onde})",
                                 _RECUSAS_QUE_ERAM_TRANSITORIAS)
    return cursor.rowcount


def _cliente_da_banca(sigla: str) -> ClienteEducado:
    if sigla not in HOSTS_POR_BANCA:
        raise Recusado(f"banca sem lista de hosts: {sigla}")
    return ClienteEducado(HOSTS_POR_BANCA[sigla])


def baixar(conexao: sqlite3.Connection, banca: str | None = None, limite: int | None = None, log=print,
           fabrica_cliente: Callable[[str], ClienteEducado] = _cliente_da_banca) -> dict:
    liberados = liberar_recusas_transitorias(conexao)
    if liberados:
        log(f"baixar: {liberados} recusas antigas eram falhas passageiras e voltaram para a fila")
    filtro = "AND b.sigla = ?" if banca else ""
    pendentes = conexao.execute(
        f"""SELECT d.id, d.url, b.sigla FROM documento d JOIN concurso c ON c.id = d.concurso_id
            JOIN banca b ON b.id = c.banca_id
            WHERE d.sha256 IS NULL AND d.tipo IN ('prova','gabarito_preliminar','gabarito_definitivo','gabarito_retificado')
              AND (d.erro IS NULL OR d.erro NOT LIKE 'recusado:%') {filtro}
            ORDER BY d.id {'LIMIT ?' if limite else ''}""",
        tuple(x for x in (banca, limite) if x)).fetchall()
    clientes: dict[str, ClienteEducado] = {}
    contagem: dict[str, int] = defaultdict(int)
    recusas_por_host: Counter = Counter()
    try:
        for i, doc in enumerate(pendentes, 1):
            situacao, erro = _baixar_um(conexao, clientes, fabrica_cliente, doc)
            contagem[situacao] += 1
            if situacao == "recusado":
                recusas_por_host[f"{urlparse(doc['url']).hostname}: {erro}"[:90]] += 1
            if i % 100 == 0:
                log(f"baixar: {i}/{len(pendentes)} {dict(contagem)}")
    finally:
        for cliente in clientes.values():
            cliente.fechar()
    log(f"baixar: fim {dict(contagem)} de {len(pendentes)}")
    for motivo, n in recusas_por_host.most_common(10):
        log(f"   recusado {n}x — {motivo}")
    if contagem.get("transitorio"):
        log(f"baixar: {contagem['transitorio']} falhas passageiras ficam na fila; rode `iac baixar` de novo depois")
    return dict(contagem)


def _baixar_um(conexao, clientes: dict, fabrica_cliente, doc) -> tuple[str, str | None]:
    """Baixa e registra um documento. Devolve (situacao, mensagem_de_erro)."""
    try:
        if doc["sigla"] not in clientes:
            clientes[doc["sigla"]] = fabrica_cliente(doc["sigla"])
        sha, tamanho = guardar_pdf(clientes[doc["sigla"]].baixar_pdf(doc["url"]))
    except Recusado as erro:
        _registrar_erro(conexao, doc["id"], f"recusado: {erro}")
        return "recusado", str(erro)
    except Transitorio as erro:
        _registrar_erro(conexao, doc["id"], f"transitorio: {erro}")
        return "transitorio", str(erro)
    except Exception as erro:  # noqa: BLE001 — imprevisto (disco cheio, URL estranha): registrado, tentado de novo
        _registrar_erro(conexao, doc["id"], f"erro: {type(erro).__name__}: {erro}")
        return "erro", str(erro)
    dbm.gravar_com_retentativa(
        conexao, "UPDATE documento SET sha256 = ?, bytes = ?, baixado_em = ?, erro = NULL WHERE id = ?",
        (sha, tamanho, dbm.agora(), doc["id"]))
    return "baixado", None


def _registrar_erro(conexao, documento_id: int, mensagem: str) -> None:
    dbm.gravar_com_retentativa(conexao, "UPDATE documento SET erro = ? WHERE id = ?", (mensagem[:300], documento_id))
