"""Fontes do ENEM: baixar (reprodutível) e ler os itens do INEP e os textos das questões.

- Itens e parâmetros: `ITENS_PROVA_<ano>.csv` dentro de `microdados_enem_<ano>.zip` do INEP. O ZIP tem
  vários GB; lemos só o índice e o membro pequeno por requisições Range (`ZipRemoto`), sem baixar o resto.
  O certificado do gov.br exige o repositório de certificados do sistema (`ssl.create_default_context()`).
- Textos 2022–2024: Hugging Face `maritaca-ai/enem` (Apache-2.0), um JSONL por ano.
- Texto 2017: Hugging Face `eduagarcia/enem_challenge` (parquet; só questões sem imagem).

Layout em disco (o mesmo para o que foi baixado e para uma pasta já pronta):
    <raiz>/inep/<ano>/ITENS_PROVA_<ano>.csv
    <raiz>/textos/maritaca_<ano>.jsonl
    <raiz>/textos/enem_challenge.parquet
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
import ssl
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from iac import config

URL_INEP = "https://download.inep.gov.br/microdados/microdados_enem_{ano}.zip"
URL_MARITACA = "https://huggingface.co/datasets/maritaca-ai/enem/resolve/main/{ano}.jsonl"
URL_CHALLENGE = "https://huggingface.co/datasets/eduagarcia/enem_challenge/resolve/main/data/train-00000-of-00001.parquet"
ANOS_MARITACA = (2022, 2023, 2024)
ANO_CHALLENGE = 2017
MARCADOR_IMAGEM = "[[placeholder]]"
# ENEM Challenge (2017) não traz as imagens nem as descreve, e algumas questões dependem delas ("conforme indica a
# figura", "Figura 1 Disponível em:"). Conferido à mão nas 116 questões de 2017: casa 70, 113, 146 e 164, e não
# casa usos sem imagem ("a figura do alferes", "o gráfico da equação", "a tabela periódica").
_REFERENCIA_A_IMAGEM = re.compile(
    r"(?i)\b(?:conforme|como)\s+(?:indica|mostra|ilustra|apresenta)\w*\s+(?:n?a\s+)?"
    r"(?:figura|imagem|gr[aá]fico|ilustra[çc][ãa]o)"
    r"|\bfigura\s+\d"
    r"|\bn?a\s+figura\s+(?:indica|mostra|apresenta|ilustra|a\s+seguir|abaixo)")
LETRAS = "ABCDE"
BLOCO_LEITURA = 1 << 20
TENTATIVAS_HTTP = 4                 # o servidor do INEP derruba conexões de vez em quando (WinError 10054)


def _get(cliente: httpx.Client, url: str, **kwargs) -> httpx.Response:
    for tentativa in range(TENTATIVAS_HTTP):
        try:
            resposta = cliente.get(url, **kwargs)
            resposta.raise_for_status()
            return resposta
        except httpx.TransportError:
            if tentativa + 1 == TENTATIVAS_HTTP:
                raise
            time.sleep(2 ** tentativa)
    raise AssertionError("inalcançável")



# ------------------------------------------------------------------ baixar

class ZipRemoto(io.RawIOBase):
    """Arquivo remoto com seek, lido por requisições HTTP Range (para `zipfile`)."""

    def __init__(self, url: str, cliente: httpx.Client):
        self.url, self.cliente, self.pos = url, cliente, 0
        resposta = cliente.head(url)
        resposta.raise_for_status()
        self.tamanho = int(resposta.headers["content-length"])
        self.lidos = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, deslocamento: int, origem: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.tamanho}[origem]
        self.pos = base + deslocamento
        return self.pos

    def readinto(self, destino) -> int:
        n = min(len(destino), self.tamanho - self.pos)
        if n <= 0:
            return 0
        resposta = _get(self.cliente, self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        dados = resposta.content[:n]
        destino[:len(dados)] = dados
        self.pos += len(dados)
        self.lidos += len(dados)
        return len(dados)


def _cliente() -> httpx.Client:
    return httpx.Client(timeout=config.TIMEOUT_HTTP_S, follow_redirects=True, verify=ssl.create_default_context(),
                        headers={"User-Agent": config.USER_AGENT})


def baixar_itens_inep(ano: int, raiz: Path, cliente: httpx.Client | None = None) -> Path:
    """Extrai só `ITENS_PROVA_<ano>.csv` do ZIP remoto do INEP. Não baixa de novo se já existe."""
    destino = raiz / "inep" / str(ano) / f"ITENS_PROVA_{ano}.csv"
    if destino.exists():
        return destino
    proprio = cliente is None
    cliente = cliente or _cliente()
    try:
        remoto = ZipRemoto(URL_INEP.format(ano=ano), cliente)
        with zipfile.ZipFile(io.BufferedReader(remoto, BLOCO_LEITURA)) as pacote:
            membros = [i for i in pacote.infolist()
                       if "ITENS_PROVA" in i.filename.upper() and i.filename.upper().endswith(".CSV")]
            if len(membros) != 1:
                raise RuntimeError(f"ENEM {ano}: esperava 1 ITENS_PROVA no ZIP, achei {len(membros)}")
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(pacote.read(membros[0]))
    finally:
        if proprio:
            cliente.close()
    return destino


def _baixar_arquivo(url: str, destino: Path, cliente: httpx.Client) -> Path:
    if destino.exists():
        return destino
    resposta = _get(cliente, url)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(resposta.content)
    return destino


def baixar(raiz: Path, anos: tuple[int, ...]) -> dict[str, str]:
    """Baixa tudo o que a ingestão precisa para `anos`. Devolve caminho relativo → sha256."""
    with _cliente() as cliente:
        for ano in anos:
            baixar_itens_inep(ano, raiz, cliente)
            if ano in ANOS_MARITACA:
                _baixar_arquivo(URL_MARITACA.format(ano=ano), raiz / "textos" / f"maritaca_{ano}.jsonl", cliente)
            elif ano == ANO_CHALLENGE:
                _baixar_arquivo(URL_CHALLENGE, raiz / "textos" / "enem_challenge.parquet", cliente)
            else:
                raise ValueError(f"ENEM {ano}: sem fonte de texto no plano")
    return impressoes(raiz)


def impressoes(raiz: Path) -> dict[str, str]:
    """sha256 de cada arquivo de fonte presente (vai para o registro da ingestão)."""
    return {str(p.relative_to(raiz).as_posix()): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(raiz.rglob("*")) if p.is_file() and p.suffix.lower() in (".csv", ".jsonl", ".parquet")}


# ------------------------------------------------------------------ ler

def ler_itens_inep(caminho: Path) -> list[dict[str, str]]:
    """CSV do INEP: separador `;`, UTF-8 em alguns anos e latin1 em outros."""
    bruto = caminho.read_bytes()
    try:
        texto = bruto.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = bruto.decode("latin1")
    linhas = list(csv.DictReader(io.StringIO(texto), delimiter=";"))
    obrigatorias = {"CO_POSICAO", "SG_AREA", "CO_ITEM", "TX_GABARITO", "CO_HABILIDADE", "IN_ITEM_ABAN",
                    "NU_PARAM_A", "NU_PARAM_B", "NU_PARAM_C", "TX_COR", "CO_PROVA", "TP_LINGUA", "IN_ITEM_ADAPTADO"}
    faltam = obrigatorias - set(linhas[0] if linhas else ())
    if faltam:
        raise ValueError(f"{caminho.name}: colunas ausentes {sorted(faltam)}")
    return linhas


@dataclass(frozen=True)
class TextoQuestao:
    numero: int                    # 1–180, numeração do caderno de onde o texto veio
    enunciado: str
    alternativas: dict[str, str]
    gabarito: str                  # 'A'–'E' ou 'X' (anulada)
    tem_imagem: bool
    com_descricao: bool            # imagem substituída pela descrição textual da fonte
    fonte: str


def _substituir_imagens(texto: str, descricoes: list[str]) -> tuple[str, list[str]]:
    while MARCADOR_IMAGEM in texto and descricoes:
        texto = texto.replace(MARCADOR_IMAGEM, f"[{descricoes[0].strip()}]", 1)
        descricoes = descricoes[1:]
    return texto, descricoes


def _texto_maritaca(registro: dict) -> TextoQuestao:
    descricoes = list(registro.get("description") or [])
    enunciado, descricoes = _substituir_imagens(registro["question"], descricoes)
    alternativas = {}
    for letra, alternativa in zip(LETRAS, registro["alternatives"]):
        alternativas[letra], descricoes = _substituir_imagens(alternativa, descricoes)
    if len(alternativas) != 5:
        raise ValueError(f"{registro['id']}: {len(alternativas)} alternativas")
    sobrou_marcador = MARCADOR_IMAGEM in enunciado or any(MARCADOR_IMAGEM in a for a in alternativas.values())
    tem_imagem = bool(registro.get("IU")) or sobrou_marcador
    rotulo = registro["label"]
    return TextoQuestao(
        numero=int(registro["id"].split("_")[1]), enunciado=enunciado.strip(),
        alternativas={k: v.strip() for k, v in alternativas.items()},
        gabarito="X" if rotulo == "Anulado" else rotulo, tem_imagem=tem_imagem,
        com_descricao=tem_imagem and not sobrou_marcador, fonte="maritaca-ai/enem")


def ler_maritaca(caminho: Path) -> dict[int, TextoQuestao]:
    registros = [json.loads(linha) for linha in caminho.read_text(encoding="utf-8").splitlines() if linha.strip()]
    textos = {t.numero: t for t in map(_texto_maritaca, registros)}
    if len(textos) != len(registros):
        raise ValueError(f"{caminho.name}: números de questão repetidos")
    return textos


def _escolhas(valor) -> dict[str, str]:
    if isinstance(valor, str):
        valor = ast.literal_eval(valor)
    return {rotulo: texto.strip() for rotulo, texto in zip(valor["label"], valor["text"])}


def ler_challenge(caminho: Path, ano: int = ANO_CHALLENGE) -> dict[int, TextoQuestao]:
    import pyarrow.parquet as pq
    textos = {}
    for registro in pq.read_table(caminho).to_pylist():
        if str(registro["exam_year"]) != str(ano):
            continue
        anulada = str(registro.get("nullified")) not in ("None", "False", "", "0")
        alternativas = _escolhas(registro["choices"])
        if sorted(alternativas) != list(LETRAS):
            raise ValueError(f"{registro['id']}: alternativas {sorted(alternativas)}")
        enunciado = registro["question"].strip()
        tem_imagem = MARCADOR_IMAGEM in enunciado or bool(_REFERENCIA_A_IMAGEM.search(
            " ".join([enunciado, *alternativas.values()])))
        numero = int(registro["question_number"])
        textos[numero] = TextoQuestao(numero, enunciado, alternativas, "X" if anulada else registro["answerKey"],
                                      tem_imagem, False, "eduagarcia/enem_challenge")
    return textos


def ler_textos(raiz: Path, ano: int) -> dict[int, TextoQuestao]:
    if ano in ANOS_MARITACA:
        return ler_maritaca(raiz / "textos" / f"maritaca_{ano}.jsonl")
    if ano == ANO_CHALLENGE:
        return ler_challenge(raiz / "textos" / "enem_challenge.parquet", ano)
    raise ValueError(f"ENEM {ano}: sem fonte de texto no plano")
