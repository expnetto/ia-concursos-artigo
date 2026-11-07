"""Leitura de PDF de prova preservando o que importa para a questão fazer sentido.

O que o texto puro perde e que muda a resposta, e como tratamos:
  1. ordem de leitura em duas colunas → faixas verticais separadas por blocos de largura total,
     e dentro de cada faixa coluna esquerda e depois direita;
  2. destaques ("o termo sublinhado") → marcadores internos ⟦b⟧ ⟦i⟧ ⟦u⟧, que nunca aparecem em
     prova e por isso não colidem com `2**3`, `__init__` ou lacunas `____`;
  3. imagens e figuras vetoriais → [[IMAGEM]] na posição em que aparecem;
  4. glifos de fonte Symbol (área de uso privado do Unicode) → caractere real (→, ≤, α…);
     o que não tiver mapeamento vira [[SIMBOLO?]] e a questão é marcada;
  5. cabeçalho e rodapé → removidos só quando o MESMO texto se repete na borda de várias
     páginas (um bloco único perto da borda é conteúdo, não cabeçalho).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf

MARCADOR_IMAGEM = "[[IMAGEM]]"
MARCADOR_SIMBOLO = "[[SIMBOLO?]]"
MAX_PAGINAS = 300                  # caderno de prova real tem < 100; mais que isso é suspeito

MARGEM_BORDA_PT = 70
FRACAO_PAGINAS_REPETIDO = 0.4      # texto de borda presente em ≥ 40% das páginas = cabeçalho/rodapé
AREA_MIN_IMAGEM_PT2 = 900          # menor que ~30x30 pt é ícone
AREA_MAX_LOGO_PT2 = 20000          # imagem de borda menor que isso é logotipo
ESPESSURA_MAX_SUBLINHADO_PT = 2.0
TOLERANCIA_SUBLINHADO_PT = 3.5
MIN_DESENHOS_FIGURA = 4            # desenhos relevantes numa coluna para contar como figura vetorial
MIN_LADO_FIGURA_PT = 20
AREA_MIN_FIGURA_PT2 = 8000

FLAG_ITALICO = 2
FLAG_NEGRITO = 16
_ROTULO_ESTRUTURAL = re.compile(r"^\s*(\(?[A-Ea-e]\)|\d{1,3}\.?|Questão\s+\d+)\s*$")
_ROTULO_QUESTAO = re.compile(r"^\s*(quest[aã]o|item)\s*\d+", re.IGNORECASE)

# Codificação da fonte Symbol (Adobe) para os glifos que aparecem em provas.
_SYMBOL = {
    0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2D: "−", 0x40: "≅", 0x5E: "⊥",
    0x61: "α", 0x62: "β", 0x63: "χ", 0x64: "δ", 0x65: "ε", 0x66: "φ", 0x67: "γ", 0x68: "η", 0x69: "ι",
    0x6B: "κ", 0x6C: "λ", 0x6D: "μ", 0x6E: "ν", 0x6F: "ο", 0x70: "π", 0x71: "θ", 0x72: "ρ", 0x73: "σ",
    0x74: "τ", 0x75: "υ", 0x77: "ω", 0x78: "ξ", 0x79: "ψ", 0x7A: "ζ",
    0x44: "Δ", 0x46: "Φ", 0x47: "Γ", 0x4C: "Λ", 0x50: "Π", 0x51: "Θ", 0x53: "Σ", 0x57: "Ω", 0x58: "Ξ", 0x59: "Ψ",
    0xA3: "≤", 0xA5: "∞", 0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓", 0xB0: "°", 0xB1: "±",
    0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xBC: "…",
    0xC4: "⊗", 0xC5: "⊕", 0xC6: "∅", 0xC7: "∩", 0xC8: "∪", 0xC9: "⊃", 0xCA: "⊇", 0xCB: "⊄", 0xCC: "⊂",
    0xCD: "⊆", 0xCE: "∈", 0xCF: "∉", 0xD6: "√", 0xD7: "⋅", 0xD8: "¬", 0xD9: "∧", 0xDA: "∨", 0xDB: "⇔",
    0xDC: "⇐", 0xDD: "⇑", 0xDE: "⇒", 0xDF: "⇓", 0xE5: "∑", 0xF2: "∫", 0xA7: "♣", 0xA8: "♦", 0xA9: "♥",
    0xAA: "♠", 0x20: " ",
}


def traduzir_simbolos(texto: str) -> str:
    def trocar(m: re.Match) -> str:
        codigo = ord(m.group(0)) - 0xF000
        return _SYMBOL.get(codigo, MARCADOR_SIMBOLO)
    return re.sub("[-]", trocar, texto)


@dataclass(frozen=True)
class Documento:
    texto: str
    paginas: int
    caracteres_texto: int   # só texto extraível; perto de zero = PDF escaneado (precisa OCR)


def _linhas_de_sublinhado(pagina) -> list[tuple[float, float, float]]:
    linhas = []
    for desenho in pagina.get_drawings():
        r = desenho["rect"]
        if r.height <= ESPESSURA_MAX_SUBLINHADO_PT and 3 < r.width < pagina.rect.width * 0.6:
            linhas.append((r.x0, r.x1, (r.y0 + r.y1) / 2))   # traço muito longo = filete de tabela/página
    return linhas


def _figuras_vetoriais(pagina, meio: float) -> list[tuple[int, float]]:
    """(coluna, y) de aglomerados de desenhos: gráficos, fluxogramas e diagramas vetoriais.

    Ignora linhas (sublinhado, bordas de tabela), a moldura da página e as caixinhas pequenas que a
    FGV desenha atrás de "(A)".."(E)" (14×11 pt): sem esse filtro, toda página "tinha figura".
    """
    por_coluna: dict[int, list] = {0: [], 1: []}
    for desenho in pagina.get_drawings():
        r = desenho["rect"]
        if r.height <= ESPESSURA_MAX_SUBLINHADO_PT or r.width <= ESPESSURA_MAX_SUBLINHADO_PT:
            continue
        if r.width < MIN_LADO_FIGURA_PT or r.height < 8 or r.width * r.height < 300:
            continue
        if r.width > pagina.rect.width * 0.8 and r.height > pagina.rect.height * 0.5:
            continue
        por_coluna[0 if (r.x0 + r.x1) / 2 < meio else 1].append(r)
    figuras = []
    for coluna, retangulos in por_coluna.items():
        if len(retangulos) < MIN_DESENHOS_FIGURA:
            continue
        x0 = min(r.x0 for r in retangulos); x1 = max(r.x1 for r in retangulos)
        y0 = min(r.y0 for r in retangulos); y1 = max(r.y1 for r in retangulos)
        if (x1 - x0) * (y1 - y0) >= AREA_MIN_FIGURA_PT2:
            figuras.append((coluna, y0))
    return figuras


def _char_sublinhado(bbox, linhas) -> bool:
    x0, _y0, x1, y1 = bbox
    centro = (x0 + x1) / 2
    return any(lx0 <= centro <= lx1 and abs(ly - y1) <= TOLERANCIA_SUBLINHADO_PT for lx0, lx1, ly in linhas)


def _envolver(texto: str, negrito: bool, italico: bool, sublinhado: bool) -> str:
    miolo = texto.strip()
    if not miolo or _ROTULO_ESTRUTURAL.match(miolo):
        return texto
    esquerda = texto[: len(texto) - len(texto.lstrip())]
    direita = texto[len(texto.rstrip()):]
    if sublinhado:
        miolo = f"⟦u⟧{miolo}⟦/u⟧"
    if negrito:
        miolo = f"⟦b⟧{miolo}⟦/b⟧"
    if italico:
        miolo = f"⟦i⟧{miolo}⟦/i⟧"
    return f"{esquerda}{miolo}{direita}"


def _eh_negrito(span) -> bool:
    return bool(span["flags"] & FLAG_NEGRITO) or "bold" in span["font"].lower()


def _texto_da_linha(linha, sublinhados) -> str:
    spans_visiveis = [s for s in linha["spans"] if "".join(c["c"] for c in s["chars"]).strip()]
    linha_toda_negrito = bool(spans_visiveis) and all(_eh_negrito(s) for s in spans_visiveis)
    partes: list[str] = []
    for span in linha["spans"]:
        negrito = _eh_negrito(span) and not linha_toda_negrito   # título/numeração não é destaque
        italico = bool(span["flags"] & FLAG_ITALICO) or "italic" in span["font"].lower()
        run, estado = [], None
        for ch in span["chars"]:
            sub = _char_sublinhado(ch["bbox"], sublinhados) if ch["c"].strip() else (estado[2] if estado else False)
            novo = (negrito, italico, sub)
            if estado is not None and novo != estado:
                partes.append(_envolver("".join(run), *estado))
                run = []
            run.append(ch["c"])
            estado = novo
        if run:
            partes.append(_envolver("".join(run), *estado))
    return traduzir_simbolos("".join(partes))


def _chave_borda(texto: str) -> str:
    """Texto de borda sem números (o número da página muda, o resto se repete)."""
    return re.sub(r"[\d\s]+", " ", sem_destaques(texto)).strip().lower()


def _blocos_da_pagina(pagina) -> list[dict]:
    largura, altura = pagina.rect.width, pagina.rect.height
    meio = largura / 2
    sublinhados = _linhas_de_sublinhado(pagina)
    blocos = []
    for bloco in pagina.get_text("rawdict")["blocks"]:
        x0, y0, x1, y1 = bloco["bbox"]
        borda = y0 < MARGEM_BORDA_PT or y1 > altura - MARGEM_BORDA_PT
        largo = x0 < meio - 10 and x1 > meio + 10
        coluna = -1 if largo else (0 if (x0 + x1) / 2 < meio else 1)
        if bloco["type"] == 1:
            if (x1 - x0) * (y1 - y0) >= AREA_MIN_IMAGEM_PT2:
                # logotipo de cabeçalho é pequeno; figura grande encostada na borda é conteúdo
                pequena = (x1 - x0) * (y1 - y0) < AREA_MAX_LOGO_PT2
                blocos.append(dict(coluna=coluna, y0=y0, texto=MARCADOR_IMAGEM, borda=borda and pequena, imagem=True))
            continue
        linhas = [t.rstrip() for t in (_texto_da_linha(l, sublinhados) for l in bloco["lines"]) if t.strip()]
        if linhas:
            blocos.append(dict(coluna=coluna, y0=y0, texto="\n".join(linhas), borda=borda, imagem=False))
    for coluna, y in _figuras_vetoriais(pagina, meio):
        blocos.append(dict(coluna=coluna, y0=y, texto=MARCADOR_IMAGEM, borda=False, imagem=True))
    return blocos


def _ordenar(blocos: list[dict]) -> list[dict]:
    """Faixas verticais: um bloco de largura total fecha a faixa; dentro dela, coluna 0 e depois 1."""
    ordenados = sorted(blocos, key=lambda b: b["y0"])
    resultado, faixa = [], []
    for bloco in ordenados:
        if bloco["coluna"] == -1:
            resultado.extend(sorted(faixa, key=lambda b: (b["coluna"], b["y0"])))
            resultado.append(bloco)
            faixa = []
        else:
            faixa.append(bloco)
    resultado.extend(sorted(faixa, key=lambda b: (b["coluna"], b["y0"])))
    return resultado


def abrir(caminho: Path) -> pymupdf.Document:
    """pymupdf.open com o limite de páginas: todo PDF vindo da internet passa por aqui (revisão 2, BAIXO-9)."""
    doc = pymupdf.open(caminho)
    if len(doc) > MAX_PAGINAS:
        doc.close()
        raise ValueError(f"PDF com {len(doc)} páginas (máximo {MAX_PAGINAS})")
    return doc


def ler(caminho: Path) -> Documento:
    with abrir(caminho) as doc:
        brutos = sum(len(p.get_text()) for p in doc)
        paginas = [_blocos_da_pagina(p) for p in doc]
    contagem = Counter(_chave_borda(b["texto"]) for blocos in paginas for b in blocos
                       if b["borda"] and not b["imagem"] and not _ROTULO_QUESTAO.match(sem_destaques(b["texto"])))
    limite = max(2, FRACAO_PAGINAS_REPETIDO * len(paginas))
    # "QUESTÃO 17" no topo da página parece cabeçalho repetido quando tiramos os números: nunca é
    repetidos = {chave for chave, n in contagem.items() if n >= limite and chave}
    textos = []
    for blocos in paginas:
        mantidos = [b for b in blocos if not (b["borda"] and (b["imagem"] or _chave_borda(b["texto"]) in repetidos))]
        textos.append("\n".join(b["texto"] for b in _ordenar(mantidos)))
    return Documento(texto="\n".join(textos), paginas=len(paginas), caracteres_texto=brutos)


_MARCAS_DESTAQUE = re.compile(r"⟦/?[biu]⟧")


def sem_destaques(texto: str) -> str:
    return _MARCAS_DESTAQUE.sub("", texto)


def tem_destaque(texto: str) -> bool:
    return bool(_MARCAS_DESTAQUE.search(texto))


def destaques_para_prompt(texto: str) -> str:
    """Converte os marcadores internos para a forma explicada ao modelo no prompt."""
    for interno, externo in (("b", "negrito"), ("i", "itálico"), ("u", "sublinhado")):
        texto = texto.replace(f"⟦{interno}⟧", f"[{externo}]").replace(f"⟦/{interno}⟧", f"[/{externo}]")
    return texto
