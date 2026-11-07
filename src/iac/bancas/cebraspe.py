"""Cebraspe: dois formatos de prova.

- CERTO/ERRADO (a maioria): itens numerados sob comandos "Julgue os itens..." → extracao_ce.
- MÚLTIPLA ESCOLHA: "QUESTÃO N" e as letras A..E sozinhas na linha → extracao.segmentar com perfil próprio.

O pareamento prova ↔ gabarito usa a DESCRIÇÃO do arquivo publicada pela banca ("PROVA OBJETIVA –
CONHECIMENTOS BÁSICOS" ↔ "GABARITO DEFINITIVO – CONHECIMENTOS BÁSICOS"), guardada em
documento.nome_arquivo pelo coletor: é a única chave estável (os arquivos têm nome de hash).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from iac import extracao, extracao_ce, pdf
from iac import texto as tx
from iac.bancas.base import MetadadosCaderno, TabelaGabarito, data_iso
from iac.extracao import PerfilExtracao

SIGLA = "cebraspe"
# `cargo` dos metadados é a descrição do arquivo ("conhecimentos especificos cargo 14"): serve para parear
# prova e gabarito, mas não é um cargo. O cadastro de cargos usa só o nome oficial lido da capa (revisão 2, MÉDIO-5).
CARGO_E_CHAVE_DE_PAREAMENTO = True
NOME = "Cebraspe"
PERFIL = PerfilExtracao(
    inicio_questao=re.compile(r"(?:^|\n)[ \t]*(?:⟦/?[biu]⟧)*quest[aã]o\s+(\d{1,3})(?:⟦/?[biu]⟧)*[ \t]*(?=\n)"),
    # letra MAIÚSCULA no começo da linha, sozinha ("A\n...") ou seguida do texto ("A a principal função...")
    alternativa=re.compile(r"(?:^|\n)[ \t]*(?:⟦/?[biu]⟧)*([A-E])(?:⟦/?[biu]⟧)*(?:[ \t]*(?=\n)|[ \t]+(?=\S))"),
    alternativa_no_original=True,
)
# Desde ~2024 cada questão/item traz um ícone e um código de controle: "[[IMAGEM]]\n<PGE_RN196CG1A01E4497>".
# Não é figura da questão: sem remover, toda questão sairia com o problema "imagem" e ficaria inelegível.
_ICONE_CODIGO = re.compile(r"\[\[IMAGEM\]\][ \t]*\n?[ \t]*<(?=[A-Z0-9_]*\d)[A-Z0-9_]{8,}>[ \t]*")
_PALAVRAS_DESCRICAO = re.compile(
    r"\b(prova|provas|objetiva|objetivas|gabarito|gabaritos|preliminar|preliminares|definitivo|definitivos|"
    r"oficial|oficiais|retificado|retificada|caderno|de|do|da|dos|das|e)\b")
_CELULA = re.compile(r"^([A-E]|X|\*)$")
_DATA = re.compile(r"aplica[cç][aã]o:\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)
_CARGO_CAPA = re.compile(r"\bcargo\s*\d{1,3}\s*:\s*([^\n]{4,120})", re.IGNORECASE)
_NIVEL = re.compile(r"\bn[ií]vel\s+(superior|m[eé]dio|fundamental)\b", re.IGNORECASE)


def chave_descricao(descricao: str) -> str:
    """'GABARITO DEFINITIVO – CARGO 1' e 'PROVA OBJETIVA - CARGO 1' → 'cargo 1'."""
    norm = re.sub(r"[^a-z0-9 ]+", " ", tx.norm_1a1(descricao))
    return " ".join(_PALAVRAS_DESCRICAO.sub(" ", norm).split())


def eh_multipla_escolha(texto: str) -> bool:
    return len(re.findall(r"quest[aã]o\s+\d{1,3}", tx.norm_1a1(texto))) >= 5


def segmentar(texto: str) -> extracao.CadernoExtraido:
    texto = _ICONE_CODIGO.sub("", texto)
    if eh_multipla_escolha(texto):
        return extracao.segmentar(texto, PERFIL)
    return extracao_ce.segmentar_ce(texto)


def metadados_caderno(caminho: Path, nome_arquivo: str) -> MetadadosCaderno:
    with pdf.abrir(caminho) as doc:
        capa = doc[0].get_text() if len(doc) else ""
    nivel = _NIVEL.search(capa)
    cargo_capa = _CARGO_CAPA.search(capa)
    return MetadadosCaderno(
        cargo=chave_descricao(nome_arquivo) or None,
        nivel=tx.norm_1a1(nivel.group(1)) if nivel else None,
        tipo_caderno="1",                     # a Cebraspe não embaralha por tipo de caderno
        esperado=extracao.esperado_da_capa(capa),
        texto_capa_norm=tx.norm_1a1(capa) + " " + chave_descricao(nome_arquivo),
        cargo_oficial=cargo_capa.group(1).strip(" –-") if cargo_capa else None,
    )


def ler_gabarito(caminho: Path, versao: str, documento_id: int | None = None,
                 nome_arquivo: str = "") -> list[TabelaGabarito]:
    with pdf.abrir(caminho) as doc:
        cabecalho = doc[0].get_text()[:2000] if len(doc) else ""
        data = _DATA.search(cabecalho)
        data_prova = data_iso(data.group(1), data.group(2), data.group(3)) if data else None
        paginas = [p.get_text() for p in doc]
    return tabelas_das_paginas(paginas, chave_descricao(nome_arquivo), versao, documento_id, data_prova)


_CARGO_PAGINA = re.compile(r"cargo\s*\d*\s*:\s*(.+)", re.IGNORECASE)
_ITEM_RESPOSTA = re.compile(r"(?:^|\n)\s*(\d{1,3})\s*\n\s*[A-Z]\d{6,}\s*-\s*(CORRETO|ERRADO|ANULADO)\b")
_RESPOSTA_CE = {"CORRETO": "C", "ERRADO": "E", "ANULADO": "X"}


def tabelas_das_paginas(paginas: list[str], chave: str, versao: str, documento_id: int | None = None,
                        data_prova: str | None = None) -> list[TabelaGabarito]:
    """Tabelas de gabarito de um documento, página a página.

    - grade "números / respostas" que pode começar em qualquer número (Específicos: 21–60, 51–120);
    - página sem cabeçalho de cargo cuja grade CONTINUA a anterior (61–120 depois de 1–60) é a mesma
      tabela (revisão 2, MÉDIO-10);
    - formato 2025+ "PROVA E GABARITO": cada item com o código e CORRETO/ERRADO/ANULADO ao lado.
    """
    tabelas: list[TabelaGabarito] = []
    for texto_pagina in paginas:
        respostas = _grade(texto_pagina) or _itens_com_resposta(texto_pagina)
        if not respostas:
            continue
        cargo_pagina = _CARGO_PAGINA.search(texto_pagina)
        anterior = tabelas[-1] if tabelas else None
        if anterior and not cargo_pagina and min(respostas) == max(anterior.respostas) + 1:
            tabelas[-1] = dataclasses.replace(anterior, respostas={**anterior.respostas, **respostas})
            continue
        cargo = chave if len(paginas) == 1 or not cargo_pagina else f"{chave} {cargo_pagina.group(1).strip()}"
        tabelas.append(TabelaGabarito(cargo=cargo or "unico", tipo_caderno="1", respostas=respostas, versao=versao,
                                      documento_id=documento_id, data_prova=data_prova))
    return tabelas


def _grade(texto_pagina: str) -> dict[int, str]:
    """Blocos de números seguidos das respostas; células "0" de preenchimento são ignoradas.

    Os números precisam formar UMA sequência contínua (n, n+1, ...), do mesmo tamanho das respostas.
    """
    linhas = [l.strip() for l in texto_pagina.splitlines() if l.strip()]
    numeros, letras = [], []
    for linha in linhas:
        if re.fullmatch(r"\d{1,3}", linha) and linha != "0":
            numeros.append(int(linha))
        elif _CELULA.match(linha):
            letras.append("X" if linha in ("X", "*") else linha)
    if not numeros or len(numeros) != len(letras) or numeros != list(range(numeros[0], numeros[0] + len(numeros))):
        return {}
    return dict(zip(numeros, letras))


def _itens_com_resposta(texto_pagina: str) -> dict[int, str]:
    respostas = {int(m.group(1)): _RESPOSTA_CE[m.group(2)] for m in _ITEM_RESPOSTA.finditer(texto_pagina)}
    numeros = sorted(respostas)
    if not numeros or numeros != list(range(numeros[0], numeros[0] + len(numeros))):
        return {}
    return respostas
