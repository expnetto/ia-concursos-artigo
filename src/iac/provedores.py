"""Cliente único para qualquer via que fale o protocolo OpenAI (chat/completions).

Ollama, Groq, Gemini (endpoint compatível), OpenRouter, Mistral e OpenAI usam o mesmo formato;
muda só a URL e a chave. A chave NUNCA sai daqui: não é logada, não é devolvida, e qualquer
pedaço dela que apareça numa mensagem de erro do provedor é apagado antes de gravar.

Além do texto da resposta, devolvemos tudo o que a via informa e que vira variável de análise:
raciocínio exposto, motivo de parada (truncamento), probabilidade por letra (logprobs), provedor
real (OpenRouter roteia), impressão digital do modelo (troca silenciosa de versão).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from iac.catalogo import Via

TIMEOUT_S = 300.0
MAX_TENTATIVAS = 5
ESPERA_INICIAL_S = 4.0
MAX_BYTES_RESPOSTA = 512 * 1024     # resposta maior que isso é anômala (loop, proxy): recusada
_PADRAO_CHAVE = re.compile(r"\b(sk-[\w-]{6,}|gsk_[\w-]{6,}|AIza[\w-]{6,}|sk-or-[\w-]{6,})")


class CotaEsgotada(Exception):
    """A via disse que a cota (diária) acabou: parar de chamar essa via nesta rodada."""


class ErroDefinitivo(Exception):
    """Erro que não adianta repetir (400, 401, 404, corpo inválido...)."""


@dataclass(frozen=True)
class Resposta:
    texto: str
    modelo_retornado: str | None
    tokens_in: int | None
    tokens_out: int | None
    latencia_ms: int
    finish_reason: str | None = None
    raciocinio: str | None = None
    tokens_raciocinio: int | None = None
    prob_letras: dict[str, float] | None = None
    provedor_real: str | None = None
    system_fingerprint: str | None = None
    tentativas: int = 1
    extras: dict = field(default_factory=dict)


def ocultar_chaves(texto: str) -> str:
    texto = _PADRAO_CHAVE.sub("[CHAVE-OCULTA]", texto)
    for nome, valor in os.environ.items():
        if valor and len(valor) >= 12 and ("KEY" in nome or "TOKEN" in nome) and valor in texto:
            texto = texto.replace(valor, "[CHAVE-OCULTA]")
    return texto


def probabilidades_da_letra(conteudo_logprobs: list[dict] | None, campo: str = "resposta") -> dict[str, float] | None:
    """Probabilidade que o modelo deu a cada letra no ponto em que escreveu o valor de "resposta".

    É o sinal "de dentro" do modelo, independente da confiança que ele declara: o análogo mais
    próximo, num LLM, do mapa de ativação que o Grad-CAM mostrava na rede convolucional.
    """
    if not conteudo_logprobs:
        return None
    texto, posicoes = "", []
    for token in conteudo_logprobs:
        posicoes.append(len(texto))
        texto += token.get("token", "")
    m = re.search(rf'"{campo}"\s*:\s*"\(?', texto)
    if not m:
        return None
    for indice, inicio in enumerate(posicoes):
        fim = inicio + len(conteudo_logprobs[indice].get("token", ""))
        if fim > m.end():
            alternativas = conteudo_logprobs[indice].get("top_logprobs") or []
            probs: dict[str, float] = {}
            for alt in alternativas:
                letra = alt.get("token", "").strip().strip("(").upper()
                if re.fullmatch(r"[A-E]|CERTO|ERRADO|BRANCO|C|E|B", letra):
                    probs[letra] = probs.get(letra, 0.0) + 2.718281828 ** alt.get("logprob", -99)
            return probs or None
    return None


class Cliente:
    def __init__(self, via: Via):
        self.via = via
        cabecalhos = {"Content-Type": "application/json"}
        if via.chave_env:
            chave = os.environ.get(via.chave_env)
            if not chave:
                raise ErroDefinitivo(f"variável {via.chave_env} não definida (ver .env.example)")
            cabecalhos["Authorization"] = f"Bearer {chave}"
        self._http = httpx.Client(base_url=via.base_url.rstrip("/") + "/", headers=cabecalhos,
                                  timeout=TIMEOUT_S, follow_redirects=False)
        self._ultima_chamada = 0.0
        self.tentativas_ultima_chamada = 0   # requisições HTTP reais da última `completar` (cota diária)

    def fechar(self) -> None:
        self._http.close()

    def _respeitar_rpm(self) -> None:
        if self.via.rpm <= 0:
            return
        espera = self._ultima_chamada + 60.0 / self.via.rpm - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        self._ultima_chamada = time.monotonic()

    def listar_modelos(self) -> list[str]:
        resposta = self._http.get("models")
        resposta.raise_for_status()
        return sorted(m["id"] for m in resposta.json().get("data", []))

    def _post(self, corpo: dict) -> tuple[int, bytes, httpx.Headers]:
        with self._http.stream("POST", "chat/completions", json=corpo) as http:
            dados = bytearray()
            for pedaco in http.iter_bytes():
                dados.extend(pedaco)
                if len(dados) > MAX_BYTES_RESPOSTA:
                    raise ErroDefinitivo(f"resposta maior que {MAX_BYTES_RESPOSTA} bytes")
            return http.status_code, bytes(dados), http.headers

    def completar(self, corpo: dict) -> Resposta:
        """POST chat/completions com novas tentativas para 429/5xx/timeout."""
        espera = ESPERA_INICIAL_S
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            self.tentativas_ultima_chamada = tentativa
            self._respeitar_rpm()
            inicio = time.monotonic()
            try:
                status, bruto, cabecalhos = self._post(corpo)
            except (httpx.TimeoutException, httpx.TransportError):
                if tentativa == MAX_TENTATIVAS:
                    raise
                time.sleep(espera)
                espera *= 2
                continue
            latencia = int((time.monotonic() - inicio) * 1000)
            texto_erro = ocultar_chaves(bruto[:400].decode("utf-8", "replace"))
            if status == 429:
                if _cota_diaria(texto_erro):
                    raise CotaEsgotada(f"HTTP 429: {texto_erro}")
                time.sleep(_retry_after(cabecalhos) or espera)
                espera *= 2
                continue
            if status >= 500:
                if tentativa == MAX_TENTATIVAS:
                    raise ErroDefinitivo(f"HTTP {status}: {texto_erro}")
                time.sleep(espera)
                espera *= 2
                continue
            if status >= 400:
                raise ErroDefinitivo(f"HTTP {status}: {texto_erro}")
            return _interpretar(bruto, latencia, tentativa)
        raise CotaEsgotada("limite de requisições persistente")


def _interpretar(bruto: bytes, latencia: int, tentativas: int) -> Resposta:
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as erro:
        raise ErroDefinitivo(f"corpo não é JSON: {erro}") from erro
    if not isinstance(dados, dict):
        raise ErroDefinitivo("corpo JSON não é objeto")
    if dados.get("error"):   # alguns provedores devolvem erro com HTTP 200
        raise ErroDefinitivo(ocultar_chaves(f"erro no corpo: {json.dumps(dados['error'])[:300]}"))
    escolhas = dados.get("choices")
    if not isinstance(escolhas, list) or not escolhas or not isinstance(escolhas[0], dict):
        raise ErroDefinitivo("resposta sem choices")
    escolha = escolhas[0]
    mensagem = escolha.get("message") or {}
    conteudo = mensagem.get("content")
    raciocinio = mensagem.get("reasoning_content") or mensagem.get("reasoning") or None
    if conteudo is None and not raciocinio:
        raise ErroDefinitivo(f"mensagem vazia (finish_reason={escolha.get('finish_reason')})")
    uso = dados.get("usage") or {}
    detalhes = uso.get("completion_tokens_details") or {}
    logprobs = (escolha.get("logprobs") or {}).get("content")
    return Resposta(
        texto=conteudo or "",
        modelo_retornado=dados.get("model"),
        tokens_in=_inteiro(uso.get("prompt_tokens")),
        tokens_out=_inteiro(uso.get("completion_tokens")),
        latencia_ms=latencia,
        finish_reason=escolha.get("finish_reason"),
        raciocinio=raciocinio if isinstance(raciocinio, str) else None,
        tokens_raciocinio=_inteiro(detalhes.get("reasoning_tokens")),
        prob_letras=probabilidades_da_letra(logprobs if isinstance(logprobs, list) else None),
        provedor_real=dados.get("provider") if isinstance(dados.get("provider"), str) else None,
        system_fingerprint=dados.get("system_fingerprint"),
        tentativas=tentativas,
    )


def _inteiro(valor) -> int | None:
    return valor if isinstance(valor, int) and valor >= 0 else None


def _retry_after(cabecalhos: httpx.Headers) -> float | None:
    valor = cabecalhos.get("retry-after")
    try:
        return min(float(valor), 120.0) if valor else None
    except ValueError:
        return None


def _cota_diaria(texto: str) -> bool:
    texto = texto.lower()
    return any(p in texto for p in ("per day", "daily", "requests per day", "rpd", "quota", "exhausted",
                                    "insufficient_quota", "credit"))
