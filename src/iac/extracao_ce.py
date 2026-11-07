"""Segmentação de provas no formato CERTO/ERRADO (Cebraspe).

Estrutura típica:
    <texto de apoio>                      (opcional, pode ser longo)
    Julgue os itens a seguir, ...         (comando do grupo)
    12 <afirmação a julgar>
    13 <afirmação a julgar>
    <novo texto de apoio> / <novo comando> ...

Cada item vira uma questão CE cujo enunciado é "<comando>\\n\\n<afirmação>" e cujo texto de apoio
é o texto que antecede o comando do grupo. Sem alternativas.
"""

from __future__ import annotations

import re

from iac import texto as tx
from iac.extracao import (P_IMAGEM, P_SIMBOLO, P_TEXTO_AUSENTE, CadernoExtraido, ItemExtraido, _titulo_de_linhas,
                          AVISO_BURACO_INICIAL, P_ENUNCIADO_VAZIO, cadeia_mais_longa, enunciado_tem_texto, esperado_da_capa, normalizar_bloco)
from iac.pdf import MARCADOR_IMAGEM, MARCADOR_SIMBOLO, sem_destaques

VERSAO_EXTRATOR_CE = "1.1"
P_ITEM_SUSPEITO = "item_suspeito"

# "12 No trecho..." ou "1\nPela organização..." (itens de um dígito costumam vir sozinhos na linha)
_ITEM = re.compile(r"(?:^|\n)[ \t]*(\d{1,3})(?:[ \t]+|[ \t]*\n)(?=[^\W\d_]|[“\"(])")
_COMANDO = re.compile(
    r"^\s*(julgue|com base|considerando|a respeito|acerca|tendo em vista|no que se refere|no que concerne|"
    r"quanto a|quanto ao|em relacao|com relacao|relativamente|a partir d|de acordo com|segundo|"
    r"a seguir, sao apresentad|cada um dos itens a seguir|nos itens|em cada um dos|"
    r"considering|judge|based on|according to|in relation to|regarding|concerning|with reference to)",
    re.IGNORECASE)
_PEDE_JULGAMENTO = re.compile(r"\b(julgue|julgar|itens|item|assertiva|afirmativa|judge|items)\b", re.IGNORECASE)
_LIXO = re.compile(r"^\s*(espaco livre|rascunho)\b", re.IGNORECASE)
_REFERE_APOIO = re.compile(r"\b(texto|text|trecho|passage|fragmento|paragrafo|situacao hipotetica)\b")
_TITULO_DE_TEXTO = re.compile(r"^\s*(texto|text|texto-base)\s+[\w.\-]{1,12}\s*$", re.IGNORECASE)
MAX_ITEM_CARACTERES = 1200
MIN_ITEM_CARACTERES = 15


def _candidatos(norm: str) -> tuple[list[tuple[int, int, int]], list[str]]:
    """Itens na sequência contínua mais longa (o caderno de Específicos começa no 51, não no 1).

    Número seguido de texto NA MESMA LINHA é candidato forte; número sozinho na linha é fraco, porque
    a Cebraspe numera as linhas do texto de apoio na margem (5, 10, 15...). O fraco só vale quando não
    há forte daquele número mais adiante (revisão 2, MÉDIO-1).
    """
    fortes: dict[int, list[tuple[int, int]]] = {}
    fracos: dict[int, list[tuple[int, int]]] = {}
    for m in _ITEM.finditer(norm):
        sozinho = "\n" in norm[m.end(1):m.end()]
        (fracos if sozinho else fortes).setdefault(int(m.group(1)), []).append((m.start(1), m.end()))
    return cadeia_mais_longa(fortes, fracos)


def _frase_a_partir(linhas: list[str], i: int) -> str:
    """Texto normalizado da linha i até o fim da frase (primeiro ponto final ou dois-pontos)."""
    partes = []
    for linha in linhas[i:i + 6]:
        partes.append(tx.norm_1a1(sem_destaques(linha)))
        if sem_destaques(linha).rstrip().endswith((".", ":", "?")):
            break
    return " ".join(partes)


def _linha_de_comando(linhas: list[str], ultima: bool = False) -> int | None:
    """Índice da linha onde começa o comando de um grupo ("Julgue os itens...").

    O pedido de julgamento tem que estar NA MESMA FRASE: "Quanto à sua origem, o controle ... é
    externo." começa como comando, mas é afirmação de item (achado no TCU 2025, item 41).
    """
    achados = [i for i, linha in enumerate(linhas)
               if _COMANDO.match(tx.norm_1a1(sem_destaques(linha)))
               and _PEDE_JULGAMENTO.search(_frase_a_partir(linhas, i))]
    if not achados:
        return None
    return achados[-1] if ultima else achados[0]


def _fim_do_paragrafo(linhas: list[str]) -> int:
    """Primeira quebra de parágrafo: linha terminada em ponto final seguida de linha com maiúscula."""
    for i in range(len(linhas) - 1):
        atual = sem_destaques(linhas[i]).rstrip()
        seguinte = sem_destaques(linhas[i + 1]).lstrip()
        if atual.endswith((".", "?", "!", "”", '"', ")")) and seguinte[:1].isupper():
            return i + 1
    return len(linhas)


def _limpar_titulo(linha: str) -> str:
    return re.sub(r"^[-–\s]+|[-–\s]+$", "", linha)


def _contexto(linhas: list[str], secao_atual: str | None, apoio_atual: str | None = None):
    """(comando, texto de apoio, seção) a partir das linhas que antecedem um grupo de itens.

    Usa o ÚLTIMO comando das linhas (a capa também tem frases como "cada um dos itens..."), e o
    texto de apoio começa depois do último título de seção ("-- CONHECIMENTOS BÁSICOS --").
    """
    linhas = [l for l in linhas if l.strip() and not _LIXO.match(tx.norm_1a1(l))]
    corte = _linha_de_comando(linhas, ultima=True)
    if corte is None:
        return None, apoio_atual, secao_atual
    antes, comando_linhas = linhas[:corte], linhas[corte:]
    secao, inicio = secao_atual, 0
    for i, linha in enumerate(antes):
        titulo, _ = _titulo_de_linhas([_limpar_titulo(linha)])
        if titulo:
            secao, inicio = titulo, i + 1
    texto_apoio = "\n".join(antes[inicio:]).strip()
    comando_norm = tx.norm_1a1(" ".join(comando_linhas))
    if len(texto_apoio) > 80:
        apoio = texto_apoio
    elif _REFERE_APOIO.search(comando_norm):
        apoio = apoio_atual          # "julgue os itens a seguir, relativos ao texto": o texto continua
    else:
        apoio = None
    return normalizar_bloco("\n".join(comando_linhas)), apoio, secao


def segmentar_ce(texto: str) -> CadernoExtraido:
    norm = tx.norm_1a1(texto)
    esperado = esperado_da_capa(texto)
    posicoes, avisos = _candidatos(norm)
    if not posicoes:
        return CadernoExtraido([], esperado, "quarentena", "nenhum item reconhecido")

    comando, apoio, secao = _contexto(texto[: posicoes[0][0]].splitlines(), None)
    itens: list[ItemExtraido] = []
    for i, (_ini, fim, numero) in enumerate(posicoes):
        fim_corpo = posicoes[i + 1][0] if i + 1 < len(posicoes) else len(texto)
        linhas = [l for l in texto[fim:fim_corpo].splitlines() if l.strip()]
        corte = _linha_de_comando(linhas)
        if corte is not None:
            # antes do comando: o resto da afirmação e, talvez, um novo texto de apoio
            antes = linhas[:corte]
            titulo = next((j for j, l in enumerate(antes) if j > 0 and _TITULO_DE_TEXTO.match(sem_destaques(l))), None)
            if titulo is not None:
                fim_item = titulo                 # "Texto 1A1-II" sozinho na linha abre o apoio do grupo seguinte
            else:
                fim_item = _fim_do_paragrafo(antes) if len(" ".join(antes)) > 400 else len(antes)
            afirmacao_linhas, proximo_contexto = antes[:fim_item], linhas[fim_item:]
        else:
            afirmacao_linhas, proximo_contexto = linhas, []
        afirmacao_linhas = [l for l in afirmacao_linhas if not _LIXO.match(tx.norm_1a1(l))]
        afirmacao = normalizar_bloco("\n".join(afirmacao_linhas))
        enunciado = f"{comando}\n\n{afirmacao}" if comando else afirmacao
        item = ItemExtraido(numero=numero, enunciado=enunciado, alternativas={}, secao=secao, texto_base=apoio)
        _diagnosticar_ce(item, afirmacao, comando)
        itens.append(item)
        if proximo_contexto:
            comando, apoio, secao = _contexto(proximo_contexto, secao, apoio)
    return _validar_ce(itens, esperado, avisos)


def _diagnosticar_ce(item: ItemExtraido, afirmacao: str, comando: str | None) -> None:
    conteudo = item.enunciado + " " + (item.texto_base or "")
    if MARCADOR_IMAGEM in conteudo:
        item.problemas.append(P_IMAGEM)
    if MARCADOR_SIMBOLO in conteudo:
        item.problemas.append(P_SIMBOLO)
    if _REFERE_APOIO.search(tx.norm_1a1(sem_destaques(item.enunciado))) and not item.texto_base:
        item.problemas.append(P_TEXTO_AUSENTE)
    if not comando or not (MIN_ITEM_CARACTERES <= len(afirmacao) <= MAX_ITEM_CARACTERES):
        item.problemas.append(P_ITEM_SUSPEITO)
    if not enunciado_tem_texto(afirmacao):
        item.problemas.append(P_ENUNCIADO_VAZIO)


def _validar_ce(itens: list[ItemExtraido], esperado: int | None, avisos: list[str]) -> CadernoExtraido:
    buraco = next((a for a in avisos if a.startswith(AVISO_BURACO_INICIAL)), None)
    if buraco:
        return CadernoExtraido(itens, esperado, "quarentena", buraco)
    if esperado and len(itens) != esperado:
        return CadernoExtraido(itens, esperado, "quarentena", f"{len(itens)} itens, capa diz {esperado}")
    if len(itens) < 10:
        return CadernoExtraido(itens, esperado, "quarentena", f"apenas {len(itens)} itens")
    suspeitos = sum(P_ITEM_SUSPEITO in i.problemas for i in itens)
    if suspeitos > 0.2 * len(itens):
        return CadernoExtraido(itens, esperado, "quarentena", f"{suspeitos}/{len(itens)} itens suspeitos")
    return CadernoExtraido(itens, esperado, "ok", None)
