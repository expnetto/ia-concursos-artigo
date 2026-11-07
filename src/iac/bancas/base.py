"""Contrato comum dos adaptadores de banca e o casamento caderno ↔ tabela de gabarito."""

from __future__ import annotations

import datetime

import re
from dataclasses import dataclass, field

from iac import texto as tx

_PALAVRAS_VAZIAS = {"de", "da", "do", "das", "dos", "em", "e", "a", "o", "para", "com", "sem", "tipo", "nivel",
                    "prova", "turno", "manha", "tarde", "noite", "area", "cargo"}
SIMILARIDADE_MINIMA = 0.75


@dataclass(frozen=True)
class TabelaGabarito:
    """Uma tabela de respostas: um cargo, uma sessão de prova, um tipo de caderno."""
    cargo: str
    tipo_caderno: str | None
    respostas: dict[int, str]          # número -> 'A'..'E' | 'C' | 'E' | 'X' (anulada)
    versao: str                        # 'preliminar' | 'definitivo' | 'retificado' (de documento.tipo)
    documento_id: int | None = None
    prova: str | None = None           # sessão ("Prova 1", "Prova II") quando a banca distingue
    turno: str | None = None           # 'manha' | 'tarde' | 'noite'
    data_prova: str | None = None


@dataclass(frozen=True)
class MetadadosCaderno:
    cargo: str | None
    nivel: str | None
    tipo_caderno: str | None
    prova: str | None = None
    turno: str | None = None
    esperado: int | None = None        # nº de questões informado na capa
    texto_capa_norm: str = ""
    avisos: tuple[str, ...] = field(default_factory=tuple)
    cargo_oficial: str | None = None   # nome do cargo lido da capa, quando `cargo` é só chave de pareamento


def palavras(texto: str) -> set[str]:
    norm = re.sub(r"[^a-z0-9 ]+", " ", tx.norm_1a1(texto))
    # números ficam: "Perfil 8" × "Perfil 9" e "Cargo 1" × "Cargo 10" são cargos diferentes
    return {p for p in norm.split() if p not in _PALAVRAS_VAZIAS and (len(p) > 1 or p.isdigit())}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def escolher_tabela(meta: MetadadosCaderno, tabelas: list[TabelaGabarito],
                    n_itens: int | None = None) -> tuple[TabelaGabarito | None, str]:
    """Casa um caderno com UMA tabela, ou nenhuma. Nunca chuta.

    Filtros obrigatórios: mesmo tipo de caderno; mesma sessão (prova/turno) quando os dois lados
    informam. Pontuação: semelhança (Jaccard) entre as palavras do cargo da capa e do cargo da
    tabela; exige ≥ 0,75. Se a capa não deu um cargo legível, exige que TODAS as palavras do
    cargo da tabela estejam na capa. Qualquer empate no topo entre tabelas com respostas
    diferentes => 'ambiguo' (a revisão de 15/10 mostrou que escolher a primeira dava gabarito errado).
    """
    if not meta.tipo_caderno:
        return None, "sem_tipo"
    capa = palavras(meta.texto_capa_norm)
    cargo_capa = palavras(meta.cargo or "")
    candidatas = []
    tamanhos_vistos = set()
    for tabela in tabelas:
        if tabela.tipo_caderno != meta.tipo_caderno:
            continue
        # o nº de questões tem que bater com a capa (ou, sem capa, com o que extraímos)
        alvo_tamanho = meta.esperado or n_itens
        if alvo_tamanho and len(tabela.respostas) != alvo_tamanho:
            tamanhos_vistos.add(len(tabela.respostas))
            continue
        if meta.prova and tabela.prova and meta.prova != tabela.prova:
            continue
        if meta.turno and tabela.turno and meta.turno != tabela.turno:
            continue
        alvo = palavras(tabela.cargo)
        if not alvo:
            continue
        if cargo_capa:
            nota = _jaccard(cargo_capa, alvo)
            if nota < SIMILARIDADE_MINIMA:
                continue
        else:
            if not alvo <= capa:
                continue
            nota = len(alvo) / 100.0          # sem cargo na capa: prefere a tabela de cargo mais específico
        candidatas.append((nota, tabela))
    if not candidatas:
        if tamanhos_vistos:
            return None, f"tamanho_diferente: tabelas do tipo têm {sorted(tamanhos_vistos)} questões"
        return None, "sem_tabela"
    candidatas.sort(key=lambda c: c[0], reverse=True)
    melhor_nota, melhor = candidatas[0]
    rivais = [t for n, t in candidatas[1:] if abs(n - melhor_nota) < 1e-9 and t.respostas != melhor.respostas]
    if rivais:
        return None, f"ambiguo: '{melhor.cargo}' x '{rivais[0].cargo}'"
    return melhor, f"casado com '{melhor.cargo}' ({melhor_nota:.2f})"


def data_iso(dia: str, mes: str, ano: str) -> str | None:
    """dd/mm/aaaa → aaaa-mm-dd, só se for uma data real ("99/99/2024" passava no CHECK — revisão 2, BAIXO-9)."""
    try:
        return datetime.date(int(ano), int(mes), int(dia)).isoformat()
    except ValueError:
        return None
