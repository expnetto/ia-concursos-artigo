"""Utilidades de texto.

Regra de ouro: o texto ORIGINAL nunca é alterado. Para casar/buscar usamos `norm_1a1`, que
troca cada caractere por exatamente UM caractere. Assim, uma posição encontrada no texto
normalizado é a mesma posição no texto original, e podemos recortar o original com acento.

(O extrator antigo do ~/dataprev2026 usava uma normalização que mudava o comprimento do texto;
quando isso acontecia ele caía silenciosamente no texto normalizado — por isso o banco antigo
ficou todo sem acento e em minúsculas.)
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Caracteres que o NFKD não reduz a uma letra base, mas que têm um equivalente óbvio.
_TROCAS_DIRETAS = {
    "–": "-", "—": "-", "−": "-", "‐": "-",
    "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "´": "'", "`": "'",
    "º": "o", "ª": "a", " ": " ", " ": " ", " ": " ",
}


def _base_do_caractere(caractere: str) -> str:
    """Devolve UM caractere: a letra-base minúscula sem acento, ou o próprio caractere."""
    if caractere in _TROCAS_DIRETAS:
        return _TROCAS_DIRETAS[caractere]
    decomposto = unicodedata.normalize("NFKD", caractere)
    # o primeiro caractere da decomposição é a letra-base ("ã" -> "a" + til combinante)
    base = decomposto[0] if decomposto else caractere
    # ligaduras como "ﬁ" decompõem em 2 letras; para manter 1:1 ficamos só com a primeira
    if unicodedata.combining(base):
        return " "
    return base.lower()


def norm_1a1(texto: str) -> str:
    """Minúsculo, sem acento, com o MESMO comprimento do texto de entrada."""
    resultado = "".join(_base_do_caractere(c) for c in texto)
    assert len(resultado) == len(texto)  # invariante de que o resto do pipeline depende
    return resultado


def colapsar_espacos(texto: str) -> str:
    """Junta quebras de linha e espaços múltiplos (o PDF quebra linha no meio da frase)."""
    return re.sub(r"\s+", " ", texto).strip()


def chave_comparacao(texto: str) -> str:
    """Forma canônica para deduplicação: sem acento/caixa, pontuação comum removida, espaços colapsados.

    Símbolos com significado (< > ≠ ≤ µ Σ % $ + = /) são PRESERVADOS: "µ < 720" e "µ > 720" não
    podem virar a mesma questão (achado da revisão de 15/10).
    """
    sem_pontuacao = re.sub(r"[.,;:!?\"'“”‘’()\[\]{}…–—\-_*]+", " ", norm_1a1(texto))
    return colapsar_espacos(sem_pontuacao)


def hash_conteudo(enunciado: str, alternativas: dict[str, str] | None, texto_base: str | None = None) -> str:
    """Identidade da questão, independente da ORDEM das alternativas.

    Tipos de caderno diferentes embaralham as alternativas: elas entram ordenadas pelo texto.
    O texto de apoio entra no hash (mesmo enunciado com textos diferentes = questões diferentes),
    e os marcadores de destaque também (mudar o termo sublinhado muda a questão).
    """
    partes = [chave_comparacao(enunciado), chave_comparacao(texto_base or "")]
    if alternativas:
        partes.extend(sorted(chave_comparacao(t) for t in alternativas.values()))
    return hashlib.sha256("\x1f".join(partes).encode("utf-8")).hexdigest()


def sha256_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()
