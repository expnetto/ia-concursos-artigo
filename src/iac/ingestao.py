"""Entrada de documentos no projeto: armazenamento por sha256 e importação do corpus legado.

O corpus legado é o que já foi coletado da FGV em ~/dataprev2026 (índice + PDFs). Ele traz só os
cargos de TI; o resto da FGV e as outras bancas entram pelos coletores (iac.coleta).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from iac import config
from iac import db as dbm

_ASSINATURA_PDF = b"%PDF-"


def caminho_no_acervo(sha256: str) -> Path:
    return config.PDFS / sha256[:2] / f"{sha256}.pdf"


def guardar_pdf(origem: Path | bytes) -> tuple[str, int]:
    """Copia/grava o PDF no acervo endereçado por conteúdo. Devolve (sha256, bytes)."""
    dados = origem if isinstance(origem, bytes) else Path(origem).read_bytes()
    if not dados.startswith(_ASSINATURA_PDF):
        raise ValueError("conteúdo não é PDF")
    if len(dados) > config.TAMANHO_MAXIMO_PDF:
        raise ValueError(f"PDF grande demais ({len(dados)} bytes)")
    sha = hashlib.sha256(dados).hexdigest()
    destino = caminho_no_acervo(sha)
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario = destino.with_suffix(".tmp")
        temporario.write_bytes(dados)
        temporario.replace(destino)   # escrita atômica: nunca fica PDF pela metade no acervo
    return sha, len(dados)


def ano_do_slug(slug: str) -> int | None:
    """'alema23' -> 2023, 'cgesp2025' -> 2025. Só sufixo numérico no fim do slug."""
    m = re.search(r"(20\d{2}|\d{2})$", slug)
    if not m:
        return None
    valor = int(m.group(1))
    ano = valor if valor > 2000 else 2000 + valor
    return ano if 2005 <= ano <= 2030 else None   # "abc100" não é ano


def tipo_documento_legado(tipo_v2: str | None, tipo_v1: str | None, nome: str) -> str | None:
    if tipo_v2 == "prova":
        return "prova"
    if tipo_v1 == "gabarito":
        nome = nome.lower()
        if "retific" in nome:
            return "gabarito_retificado"
        # erros de digitação reais nos nomes da FGV: "definito", "defintivo"
        if re.search(r"defini?ti?vo|definito|defintivo|(?<![a-z])final(?![a-z])", nome):
            return "gabarito_definitivo"
        return "gabarito_preliminar"
    return None


def importar_legado(conexao: sqlite3.Connection, raiz_legado: Path) -> dict[str, int]:
    indice = sqlite3.connect(raiz_legado / "data" / "index.sqlite")
    pasta = raiz_legado / "data" / "pdfs"
    contagem = {"documentos": 0, "ignorados": 0, "faltando": 0}
    with dbm.transacao(conexao):
        banca_id = dbm.obter_ou_criar(conexao, "banca", {"sigla": "fgv"}, {"nome": "FGV Conhecimento"})
        titulos = dict(indice.execute("SELECT slug, titulo FROM concursos"))
        urls = dict(indice.execute("SELECT slug, url FROM concursos"))
        linhas = indice.execute(
            "SELECT slug, url, nome_arquivo, tipo_inferido_v2, tipo_inferido FROM pdfs WHERE baixado = 1"
        ).fetchall()
        for slug, url, nome, tipo_v2, tipo_v1 in linhas:
            tipo = tipo_documento_legado(tipo_v2, tipo_v1, nome)
            if tipo is None:
                contagem["ignorados"] += 1
                continue
            arquivo = pasta / slug / nome
            if not arquivo.resolve().is_relative_to(pasta.resolve()):   # slug/nome do índice com ".."
                contagem["ignorados"] += 1
                continue
            if not arquivo.exists():
                contagem["faltando"] += 1
                continue
            sha, tamanho = guardar_pdf(arquivo)
            concurso_id = dbm.obter_ou_criar(
                conexao, "concurso", {"banca_id": banca_id, "slug": slug},
                {"titulo": titulos.get(slug), "ano_edital": ano_do_slug(slug), "url_fonte": urls.get(slug)},
            )
            dbm.obter_ou_criar(
                conexao, "documento", {"url": url},
                {"concurso_id": concurso_id, "nome_arquivo": nome, "tipo": tipo, "sha256": sha,
                 "bytes": tamanho, "baixado_em": dbm.agora()},
            )
            contagem["documentos"] += 1
    indice.close()
    return contagem
