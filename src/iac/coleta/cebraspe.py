"""Descoberta de documentos da Cebraspe pela API pública que alimenta o próprio site
(www.cebraspe.org.br/concursos). robots.txt do site: tudo permitido; a API e o CDN não têm robots.

Lista: GET apis.cebraspe.org.br/cebraspe/eventos/tipo/concursos/   (todas as fases)
Detalhe: GET apis.cebraspe.org.br/cebraspe/eventos/<eventoURL>       (arquivosGabarito = provas + gabaritos)
URL do arquivo (mesma regra do site): isGuid → cdn/<nome>; "_explorer" → cespe.unb.br/concursos/<id>/arquivos/<nome>;
senão → cdn/concursos/<id>/arquivos/<nome>.

A DESCRIÇÃO do arquivo vai para documento.nome_arquivo: é a chave de pareamento prova ↔ gabarito.
"""

from __future__ import annotations

import re
import sqlite3

from iac import config
from iac import db as dbm
from iac import texto as tx
from iac.coleta.http import ClienteEducado, Recusado, Transitorio

ANO_INICIAL = config.ANO_INICIO_CORPUS - 1   # edital de 2019 pode ter prova em 2020; o consolidar decide
API = "https://apis.cebraspe.org.br/cebraspe/eventos"
CDN = "https://cdn.cebraspe.org.br/"
HOSTS = {"apis.cebraspe.org.br", "cdn.cebraspe.org.br", "www.cespe.unb.br"}

_EXCLUIR = re.compile(r"\b(pratica|redacao|padrao|espelho|titulos|oral|aptidao|teste fisico|psicol\w*|"
                      r"heteroidentific\w*|biometr\w*|resultado|convocac\w*|edital|recurso|justificativa)\b",
                      re.IGNORECASE)


def classificar(descricao: str) -> str | None:
    """Tipo do arquivo pela descrição publicada. "Gabarito ... da prova objetiva" é GABARITO (testado antes)."""
    norm = tx.norm_1a1(descricao)
    if _EXCLUIR.search(norm):
        return None
    # "objetiva e discursiva" ainda tem parte objetiva; só discursiva pura é descartada
    if "discursiv" in norm and "objetiv" not in norm:
        return None
    if "gabarito" in norm:
        if "retific" in norm:
            return "gabarito_retificado"
        if "definitiv" in norm:
            return "gabarito_definitivo"
        return "gabarito_preliminar"
    if re.search(r"\bprovas? objetivas?\b", norm) or norm.startswith("caderno"):
        return "prova"
    return None


_IDENTIFICADOR = re.compile(r"^[A-Za-z0-9_\-]{2,80}$")
_NOME_PERIGOSO = re.compile(r"[/?#\\]|\.\.")


def url_do_arquivo(identificador: str, arquivo: dict) -> str:
    """URL do arquivo pela regra do site. O nome vem da API: com "/", "?", "#", "\\" ou ".." a URL apontaria para
    outro lugar, então é recusado (ValueError) em vez de concatenado (revisão 2, MÉDIO-8). Os nomes normais
    entram como estão, para casar com as URLs já registradas."""
    nome = str(arquivo["nomeArquivo"])
    if _NOME_PERIGOSO.search(nome) or not _IDENTIFICADOR.match(identificador):
        raise ValueError(f"nome de arquivo recusado: {nome[:80]!r}")
    pasta = identificador
    if arquivo.get("isGuid"):
        return CDN + nome
    if arquivo.get("tipoExtensaoArquivo") == "_explorer":
        return f"http://www.cespe.unb.br/concursos/{pasta}/arquivos/{nome}"
    return f"{CDN}concursos/{pasta}/arquivos/{nome}"


def descobrir(conexao: sqlite3.Connection, log=print, cliente: ClienteEducado | None = None) -> dict[str, int]:
    proprio = cliente is None
    cliente = cliente or ClienteEducado(HOSTS)
    contagem = {"concursos": 0, "documentos": 0, "reclassificados": 0, "fora_do_periodo": 0, "erros": 0}
    try:
        fases = cliente.obter_json(f"{API}/tipo/concursos/")
        eventos = [e for fase in fases if isinstance(fase, dict) for e in fase.get("eventos") or []
                   if isinstance(e, dict)]
        with dbm.transacao(conexao):
            banca_id = dbm.obter_ou_criar(conexao, "banca", {"sigla": "cebraspe"}, {"nome": "Cebraspe"})
        for i, evento in enumerate(eventos, 1):
            situacao = _registrar_evento(conexao, cliente, banca_id, evento, contagem, log)
            if situacao != "ok":
                contagem[situacao] += 1
            if i % 25 == 0:
                log(f"descobrir cebraspe: {i}/{len(eventos)} {contagem}")
    finally:
        if proprio:
            cliente.fechar()
    log(f"descobrir cebraspe: fim {contagem}")
    return contagem


def _registrar_evento(conexao, cliente, banca_id: int, evento: dict, contagem: dict, log) -> str:
    """Um concurso e seus arquivos. Um evento malformado é registrado e pulado; não derruba a descoberta."""
    ano, identificador = evento.get("eventoAno"), evento.get("eventoURL")
    if not isinstance(ano, int) or not ANO_INICIAL <= ano <= config.ANO_FIM_CORPUS:
        return "fora_do_periodo"
    if not isinstance(identificador, str) or not _IDENTIFICADOR.match(identificador):
        log(f"  evento com identificador inválido: {identificador!r}")
        return "erros"
    try:
        detalhe = cliente.obter_json(f"{API}/{identificador}")
        if not isinstance(detalhe, dict):
            raise ValueError("detalhe não é objeto JSON")
        with dbm.transacao(conexao):
            concurso_id = dbm.obter_ou_criar(
                conexao, "concurso", {"banca_id": banca_id, "slug": identificador},
                {"titulo": _texto(evento.get("eventoNomeAbreviado")), "orgao": _texto(detalhe.get("eventoNomeCompleto")),
                 "ano_edital": ano, "url_fonte": f"https://www.cebraspe.org.br/concursos/{identificador}"})
            contagem["concursos"] += 1
            for arquivo in detalhe.get("arquivosGabarito") or []:
                _registrar_arquivo(conexao, concurso_id, identificador, arquivo, contagem)
    except (Recusado, Transitorio, ValueError, KeyError, TypeError, AttributeError, sqlite3.IntegrityError) as erro:
        log(f"  {identificador}: {type(erro).__name__}: {erro}")
        return "erros"
    return "ok"


def _registrar_arquivo(conexao, concurso_id: int, identificador: str, arquivo, contagem: dict) -> None:
    if not isinstance(arquivo, dict) or not isinstance(arquivo.get("nomeArquivo"), str):
        return
    descricao = _texto(arquivo.get("descricaoArquivo")) or ""
    tipo = classificar(descricao)
    if tipo is None:
        return
    try:
        url = url_do_arquivo(identificador, arquivo)
    except ValueError:                  # nome perigoso: só este arquivo fica de fora, não o concurso inteiro
        contagem["erros"] += 1
        return
    existente = conexao.execute("SELECT id, tipo FROM documento WHERE url = ?", (url,)).fetchone()
    if existente:
        if existente["tipo"] != tipo:   # o classificador melhorou: corrige o tipo já registrado
            conexao.execute("UPDATE documento SET tipo = ? WHERE id = ?", (tipo, existente["id"]))
            contagem["reclassificados"] += 1
        return
    conexao.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (?,?,?,?)",
                    (concurso_id, url, descricao, tipo))
    contagem["documentos"] += 1


def _texto(valor) -> str | None:
    return valor.strip() if isinstance(valor, str) and valor.strip() else None
