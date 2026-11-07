"""FGV Conhecimento: cadernos com numeração solta na linha e alternativas (A)..(E).

Capa: "<órgão> / MANHÃ / <CARGO em 1–3 linhas> / [PROVA I] / NÍVEL SUPERIOR TIPO 1 – BRANCA".
Gabarito: uma grade por "<Cargo> [– Prova N] – Tipo N"; o cabeçalho pode quebrar em 2 linhas;
variantes vistas: "- TIPO 1", "- PROVA 1" (= tipo), "- 1 - Turno Manhã", "(Manhã) - Tipo 1".
"""

from __future__ import annotations

import re
from pathlib import Path

from iac import pdf
from iac import texto as tx
from iac.bancas.base import MetadadosCaderno, TabelaGabarito, data_iso
from iac.extracao import PerfilExtracao, esperado_da_capa

SIGLA = "fgv"
NOME = "FGV Conhecimento"
PERFIL = PerfilExtracao()

_CELULA = re.compile(r"^([A-E]|\*+|anulad[ao])$", re.IGNORECASE)
_DATA = re.compile(r"aplicadas?\s+(?:no\s+dia|em)\s+(\d{2})/(\d{2})/(\d{4})")
_TIPO_ARQUIVO = re.compile(r"tipo[\s_-]*0*(\d+)")
_NIVEL = re.compile(r"\bnivel\s+(superior|medio|fundamental)\b")
# sem \b: em nomes de arquivo o turno vem colado em "_" ("definitivo_manha_asdas")
_TURNO = re.compile(r"(?<![a-z])(manha|tarde|noite)(?![a-z])")
_ROMANOS = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
# a linha que encerra o cargo COMEÇA com "nível"/"prova" (o cargo pode conter "Técnico de Nível Superior")
_PARADA_CAPA = re.compile(r"^(nivel\b|prova\s+(objetiva|discursiva|de conhecimentos|[iv]+\b|\d))|\btipo\s+\d")
_INSTITUCIONAL = re.compile(r"\b(concurso|edital|processo seletivo)\b")
_PARADA_CABECALHO = re.compile(r"^(pagina\b|gabarito|\(\*\)|provas? aplicadas|.*\bedital\b|concurso)")


def _sessao(texto_norm: str) -> str | None:
    m = re.search(r"\bprova\s+(i{1,3}|iv|\d)\b", texto_norm)
    return _ROMANOS.get(m.group(1), m.group(1)) if m else None


def metadados_caderno(caminho: Path, nome_arquivo: str) -> MetadadosCaderno:
    with pdf.abrir(caminho) as doc:
        capa_original = doc[0].get_text() if len(doc) else ""
    capa = tx.norm_1a1(capa_original)
    linhas = [l.strip() for l in capa.splitlines() if l.strip()]
    avisos = []

    tipo_capa = re.search(r"\btipo\s+0*(\d+)\b", capa)
    tipo_arquivo = _TIPO_ARQUIVO.search(tx.norm_1a1(nome_arquivo))
    tipo = None
    if tipo_capa and tipo_arquivo and tipo_capa.group(1) != tipo_arquivo.group(1):
        avisos.append(f"tipo diverge: capa {tipo_capa.group(1)} x arquivo {tipo_arquivo.group(1)}")
    elif tipo_capa or tipo_arquivo:
        tipo = (tipo_capa or tipo_arquivo).group(1)

    turno = None
    for linha in linhas[:12]:
        m = _TURNO.search(linha)
        if m and len(linha) < 40:
            turno = m.group(1)
            break
    if turno is None:
        m = _TURNO.search(tx.norm_1a1(nome_arquivo))
        turno = m.group(1) if m else None

    nivel = _NIVEL.search(capa)
    return MetadadosCaderno(
        cargo=_cargo_da_capa(linhas),
        nivel=nivel.group(1) if nivel else None,
        tipo_caderno=tipo,
        prova=_sessao(" ".join(l for l in linhas[:14] if not l.startswith("prova objetiva"))),
        turno=turno,
        esperado=esperado_da_capa(capa_original),
        texto_capa_norm=capa,
        avisos=tuple(avisos),
    )


def _cargo_da_capa(linhas: list[str]) -> str | None:
    """Linhas entre o turno (ou o cabeçalho institucional) e a linha de nível/tipo/prova."""
    fim = next((i for i, l in enumerate(linhas[:20]) if _PARADA_CAPA.search(l)), None)
    if fim is None:
        return None
    inicio = 0
    for i in range(fim - 1, -1, -1):
        if (_TURNO.search(linhas[i]) and len(linhas[i]) < 40) or _INSTITUCIONAL.search(linhas[i]):
            inicio = i + 1
            break
    cargo = " ".join(linhas[inicio:fim]).strip(" -–")
    return cargo.upper() if cargo else None


# ------------------------------------------------------------------ gabarito

def ler_gabarito(caminho: Path, versao: str, documento_id: int | None = None,
                 nome_arquivo: str = "") -> list[TabelaGabarito]:
    """`versao` vem de documento.tipo (fonte única de verdade), não do texto do PDF.

    `nome_arquivo` é o nome ORIGINAL (o acervo guarda por sha256): ele informa o turno quando a
    banca publica um PDF por turno ("..._definitivo_manha_...").
    """
    tabelas: list[TabelaGabarito] = []
    nome_norm = tx.norm_1a1(nome_arquivo)
    with pdf.abrir(caminho) as doc:
        cabecalho_doc = tx.norm_1a1(" ".join(doc[0].get_text().split())[:1500]) if len(doc) else ""
        data = _DATA.search(cabecalho_doc)
        data_prova = data_iso(data.group(1), data.group(2), data.group(3)) if data else None
        turno_arquivo = _TURNO.search(nome_norm)
        for pagina in doc:
            for tabela in _tabelas_da_pagina(pagina.get_text(), versao, documento_id, data_prova):
                if tabela.turno is None and turno_arquivo:
                    tabela = TabelaGabarito(**{**tabela.__dict__, "turno": turno_arquivo.group(1)})
                tabelas.append(tabela)
    return tabelas


def _eh_celula(linha: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", linha) or _CELULA.match(linha))


def _interpretar_cabecalho(cabecalho: str) -> tuple[str, str | None, str | None, str | None]:
    """(cargo, tipo, prova, turno) a partir de "<cargo> – Prova 2 – Tipo 1" e variantes."""
    norm = tx.norm_1a1(cabecalho)
    turno = _TURNO.search(norm)
    tipo = re.search(r"\btipo\s*0*(\d{1,2})\b", norm)
    prova = None
    if tipo:
        m = re.search(r"\bprova\s+0*(\d|i{1,3}|iv)\b", norm)
        prova = _ROMANOS.get(m.group(1), m.group(1)) if m else None
        corte = tipo.start()
    else:
        m = re.search(r"(?:-|–)\s*(?:prova\s*)?0*(\d{1,2})(?:\s*-\s*turno\s+\w+)?\s*$", norm) or \
            re.search(r"\bprova\s*0*(\d{1,2})\s*$", norm)
        if not m:
            return cabecalho, None, None, None
        tipo, corte = m, m.start()
    cargo = re.sub(r"\((manh[aã]|tarde|noite)\)", "", cabecalho[:corte], flags=re.IGNORECASE)
    cargo = re.sub(r"[-–\s]*(prova\s+(\d|i{1,3}|iv)|turno\s+\w+)[-–\s]*$", "", cargo, flags=re.IGNORECASE)
    return cargo.strip(" -–"), tipo.group(1), prova, turno.group(1) if turno else None


def _tabelas_da_pagina(texto_pagina: str, versao: str, documento_id, data_prova) -> list[TabelaGabarito]:
    linhas = [l.strip() for l in texto_pagina.splitlines() if l.strip()]
    # início de grade: "1" cuja linha anterior não é célula (é o cabeçalho)
    inicios = [i for i, l in enumerate(linhas) if l == "1" and i > 0 and not _eh_celula(linhas[i - 1])]
    tabelas = []
    for k, inicio in enumerate(inicios):
        cabecalho_linhas = []
        j = inicio - 1
        while j >= 0 and len(cabecalho_linhas) < 3:
            linha = linhas[j]
            if _eh_celula(linha) or _PARADA_CABECALHO.match(tx.norm_1a1(linha)):
                break
            cabecalho_linhas.insert(0, linha)
            j -= 1
        cargo, tipo, prova, turno = _interpretar_cabecalho(" ".join(cabecalho_linhas))
        if tipo is None:
            continue
        fim = inicios[k + 1] - 1 if k + 1 < len(inicios) else len(linhas)
        numeros, letras = [], []
        for linha in linhas[inicio:fim]:
            if re.fullmatch(r"\d{1,3}", linha):
                numeros.append(int(linha))
            elif _CELULA.match(linha):
                letras.append(linha.upper() if re.fullmatch(r"[A-Ea-e]", linha) else "X")
        if not numeros or len(numeros) != len(letras) or numeros != list(range(1, len(numeros) + 1)):
            continue   # grade quebrada: melhor não ter gabarito do que ter um errado
        tabelas.append(TabelaGabarito(cargo=cargo, tipo_caderno=tipo, respostas=dict(zip(numeros, letras)),
                                      versao=versao, documento_id=documento_id, prova=prova, turno=turno,
                                      data_prova=data_prova))
    return tabelas
