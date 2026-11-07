"""Interpreta a resposta bruta do modelo: letra, confiança, justificativa, acerto.

Regra central: NUNCA adivinhar. Só vale uma letra dita de forma explícita e inequívoca.
Casos que viram formato inválido (com o motivo gravado), e não acerto/erro aleatório:
- resposta cortada pelo limite de tokens (finish_reason=length) ou raciocínio <think> não fechado;
- JSON presente com "resposta" inválida (não caímos na regex sobre a justificativa);
- duas letras diferentes afirmadas; letra fora do mapa de permutação.
(Revisão de 15/10: "Resposta: A seguir explico" virava A; "a alternativa C parece..." virava C.)
"""

from __future__ import annotations

import json
import re
import sqlite3

from iac import db as dbm
from iac import texto as tx

VERSAO_AVALIADOR = "2.1"

_PENSAMENTO = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CERCA = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# Só frases afirmativas e com a letra MAIÚSCULA isolada; as palavras podem ter qualquer caixa.
_LETRA_ME = [
    re.compile(r"(?im)^\s*(?:resposta|alternativa correta|gabarito)(?:\s+final)?\s*[:=]\s*\(?([A-E])\)?\s*\.?\s*$"),
    re.compile(r"(?i:\bresposta\s+(?:correta\s+)?(?:é|e)\s+(?:a\s+)?(?:alternativa\s+|letra\s+)?)\(?([A-E])\)?(?![\w])"),
    re.compile(r"(?i:\balternativa\s+correta\s+(?:é|e)\s+(?:a\s+)?)\(?([A-E])\)?(?![\w])"),
]
_CE = {"CERTO": "C", "C": "C", "ERRADO": "E", "E": "E", "BRANCO": "B", "EM BRANCO": "B"}

LOTE = 500                      # avaliações por transação: não segura a trava do banco por minutos


def _limpar(texto: str) -> tuple[str, bool]:
    """Remove raciocínio e cercas de código. Devolve (texto, truncado)."""
    texto = _PENSAMENTO.sub("", texto or "")
    if re.search(r"<think>", texto, re.IGNORECASE):
        texto = re.split(r"<think>", texto, flags=re.IGNORECASE)[0]   # raciocínio aberto e nunca fechado
        return texto.strip(), True
    cerca = _CERCA.search(texto)
    return (cerca.group(1) if cerca else texto).strip(), False


def _primeiro_objeto_json(texto: str) -> str | None:
    for inicio in [m.start() for m in re.finditer(r"\{", texto)]:
        profundidade = 0
        for fim in range(inicio, len(texto)):
            if texto[fim] == "{":
                profundidade += 1
            elif texto[fim] == "}":
                profundidade -= 1
                if profundidade == 0:
                    trecho = texto[inicio: fim + 1]
                    if "resposta" in trecho.lower():
                        return trecho
                    break
    return None


def _confianca(valor) -> float | None:
    """0–100. Decimal ≤ 1 ("0.85", "1.0") é fração e vira percentual; fora de 0–100 é inválido."""
    bruto = str(valor).strip().rstrip("%")
    try:
        numero = float(bruto)
    except (TypeError, ValueError):
        return None
    if "." in bruto and 0 <= numero <= 1:
        numero *= 100
    return numero if 0 <= numero <= 100 else None


def interpretar(bruta: str, formato: str, finish_reason: str | None = None) -> dict:
    """formato_ok, metodo_extracao, motivo_invalido, resposta_exibida, confianca(_bruta), justificativa."""
    resultado = {"formato_ok": 0, "metodo_extracao": None, "motivo_invalido": None, "resposta_exibida": None,
                 "confianca": None, "confianca_bruta": None, "justificativa": None}
    if finish_reason == "length":
        resultado["motivo_invalido"] = "truncada"
        return resultado
    texto, truncado = _limpar(bruta)
    if truncado and not texto:
        resultado["motivo_invalido"] = "truncada"
        return resultado

    dados, metodo = None, None
    try:
        dados, metodo = json.loads(texto), "json"
    except (json.JSONDecodeError, TypeError):
        trecho = _primeiro_objeto_json(texto)
        if trecho:
            for candidato in (trecho, trecho.replace("'", '"')):
                try:
                    dados, metodo = json.loads(candidato), "json_reparado"
                    break
                except json.JSONDecodeError:
                    continue

    if isinstance(dados, dict):
        # chaves sem acento e em minúsculas: modelos escrevem "confiança", "Resposta" (15/10: a confiança do
        # Qwen 2.5 no certo/errado era descartada em silêncio por causa do "ç")
        dados = {tx.norm_1a1(str(k)).strip(): v for k, v in dados.items()}
        if dados.get("justificativa") is not None:
            resultado["justificativa"] = str(dados["justificativa"])[:3000]
        if dados.get("confianca") is not None:
            resultado["confianca_bruta"] = str(dados["confianca"])[:50]
            resultado["confianca"] = _confianca(dados["confianca"])
        resposta = _normalizar_resposta(str(dados.get("resposta", "")).strip().strip("().").upper(), formato)
        if resposta is None:
            resultado["motivo_invalido"] = "resposta_invalida"     # JSON existe: não adivinhamos pela justificativa
            return resultado
        return {**resultado, "formato_ok": 1, "metodo_extracao": metodo, "resposta_exibida": resposta}

    resposta, motivo = _por_regex(texto, formato)
    if resposta is None:
        resultado["motivo_invalido"] = motivo
        return resultado
    return {**resultado, "formato_ok": 1, "metodo_extracao": "regex", "resposta_exibida": resposta}


def _normalizar_resposta(valor: str, formato: str) -> str | None:
    if formato == "CE":
        return _CE.get(valor)
    return valor if re.fullmatch(r"[A-E]", valor) else None


def _por_regex(texto: str, formato: str) -> tuple[str | None, str]:
    if formato == "CE":
        achados = {_CE[p] for p in re.findall(r"\b(CERTO|ERRADO|EM BRANCO|BRANCO)\b", texto)}
    else:
        achados = {m.group(1) for padrao in _LETRA_ME for m in padrao.finditer(texto)}
    if len(achados) == 1:
        return achados.pop(), ""
    return None, ("ambigua" if achados else "sem_letra")


def pontuar(resposta: str | None, gabarito: str, formato: str) -> tuple[int, int | None, float | None]:
    """(absteve, acertou, pontos)."""
    if resposta is None:
        return 0, None, None
    if formato == "CE":
        if resposta == "B":
            return 1, None, 0.0
        acertou = int(resposta == gabarito)
        return 0, acertou, 1.0 if acertou else -1.0
    acertou = int(resposta == gabarito)
    return 0, acertou, float(acertou)


def avaliar_pendentes(conexao: sqlite3.Connection) -> dict[str, int]:
    """Avalia execuções 'ok' sem avaliação, com avaliação de versão antiga do avaliador, ou cujo gabarito mudou
    desde a avaliação (correção manual). Grava em lotes para não segurar a trava do banco (revisão 2, MÉDIO-4)."""
    linhas = conexao.execute(
        """SELECT e.id, e.resposta_bruta, e.permutacao_json, e.finish_reason, e.logprobs_json, q.gabarito,
                  c.formato AS formato_condicao
           FROM execucao e JOIN questao q ON q.id = e.questao_id JOIN condicao c ON c.id = e.condicao_id
           LEFT JOIN avaliacao a ON a.execucao_id = e.id
           WHERE e.status = 'ok' AND (a.execucao_id IS NULL OR a.versao_avaliador <> ?
                                      OR a.gabarito_usado IS NOT q.gabarito)""", (VERSAO_AVALIADOR,)
    ).fetchall()
    contagem = {"avaliadas": 0, "formato_invalido": 0}
    for inicio in range(0, len(linhas), LOTE):
        with dbm.transacao(conexao):
            for linha in linhas[inicio:inicio + LOTE]:
                formato_ok = _avaliar_uma(conexao, linha)
                contagem["avaliadas"] += 1
                contagem["formato_invalido"] += 1 - formato_ok
    return contagem



def _avaliar_uma(conexao: sqlite3.Connection, linha) -> int:
    formato = "CE" if linha["formato_condicao"] == "CE" else "ME"
    leitura = interpretar(linha["resposta_bruta"], formato, linha["finish_reason"])
    exibida = leitura["resposta_exibida"]
    resposta = exibida
    if exibida and linha["permutacao_json"] and formato == "ME":
        resposta = json.loads(linha["permutacao_json"]).get(exibida)
        if resposta is None:
            leitura = {**leitura, "formato_ok": 0, "motivo_invalido": "fora_do_mapa"}
    if not leitura["formato_ok"]:
        resposta = None
    probs = json.loads(linha["logprobs_json"]) if linha["logprobs_json"] else {}
    prob = probs.get(exibida) if exibida else None
    absteve, acertou, pontos = pontuar(resposta, linha["gabarito"], formato)
    conexao.execute(
        """INSERT OR REPLACE INTO avaliacao (execucao_id, formato_ok, metodo_extracao, motivo_invalido,
               resposta_exibida, resposta, absteve, confianca_bruta, confianca, prob_resposta, justificativa,
               acertou, pontos, versao_avaliador, gabarito_usado) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (linha["id"], leitura["formato_ok"], leitura["metodo_extracao"] if leitura["formato_ok"] else None,
         leitura["motivo_invalido"], exibida, resposta, absteve, leitura["confianca_bruta"],
         leitura["confianca"], prob, leitura["justificativa"], acertou, pontos, VERSAO_AVALIADOR, linha["gabarito"]),
    )
    return leitura["formato_ok"]
