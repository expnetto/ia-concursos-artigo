"""Leitura de config/modelos.toml e config/condicoes.toml e sincronização com o banco.

Proteções (revisão de 15/10): o sha de uma condição cobre TODOS os campos, e nem condição nem
modelo podem mudar de definição depois de executados (temperatura, via, nome da API...). Via sem
https fora de localhost é recusada (a chave iria em texto claro).
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from iac import config
from iac import db as dbm
from iac import texto as tx

CAMPOS_MODELO = ("familia", "fornecedor", "pesos", "parametros_b", "parametros_ativos_b", "data_lancamento",
                 "corte_conhecimento", "corte_fonte", "acesso_consumidor", "acesso_verificado_em", "via", "nome_api",
                 "versao", "quantizacao", "contexto_max", "raciocinio", "preco_in_usd_mtok", "preco_out_usd_mtok")
CAMPOS_IMUTAVEIS_MODELO = ("via", "nome_api", "versao", "quantizacao")
_CAMPOS_DO_OLLAMA = ("versao", "quantizacao")
CAMPOS_CONDICAO = ("formato", "temperatura", "repeticoes", "embaralhar", "json_mode", "template")


@dataclass(frozen=True)
class Via:
    nome: str
    base_url: str
    chave_env: str
    rpm: int
    rpd: int
    json_mode: bool
    paga: bool
    teto_usd: float = 0.0
    treina_com_dados: bool | None = None
    logprobs: bool = False
    tpd: int = 0               # teto de tokens por dia (0 = sem)
    aceita_seed: bool = True   # Gemini (endpoint OpenAI-compatível) recusa o campo com HTTP 400


def _ler(caminho: Path) -> dict:
    with open(caminho, "rb") as arquivo:
        return tomllib.load(arquivo)


def _validar_via(via: Via) -> None:
    url = urlparse(via.base_url)
    local = url.hostname in ("localhost", "127.0.0.1")
    if url.scheme != "https" and not local:
        raise ValueError(f"via {via.nome}: base_url precisa ser https (a chave iria em texto claro)")
    if via.paga and via.teto_usd <= 0:
        raise ValueError(f"via {via.nome}: via paga sem teto_usd")


def vias(caminho: Path | None = None) -> dict[str, Via]:
    dados = _ler(caminho or config.CATALOGO_MODELOS)
    resultado = {nome: Via(nome=nome, **valores) for nome, valores in dados.get("vias", {}).items()}
    for via in resultado.values():
        _validar_via(via)
    return resultado


def teto_global(caminho: Path | None = None) -> float:
    """Teto da soma de todas as vias pagas. Falha FECHADO: com via paga no catálogo, ausente ou ≤ 0 é erro
    (antes, apagar a seção [orcamento] desligava o teto global sem aviso — revisão 2, BAIXO-5)."""
    caminho = caminho or config.CATALOGO_MODELOS
    valor = float(_ler(caminho).get("orcamento", {}).get("teto_global_usd", 0.0))
    if valor <= 0 and any(v.paga for v in vias(caminho).values()):
        raise ValueError("config/modelos.toml: [orcamento] teto_global_usd precisa ser > 0 quando há via paga")
    return valor


def sha_condicao(c: dict) -> str:
    return tx.sha256_texto(json.dumps({k: c.get(k) for k in CAMPOS_CONDICAO}, sort_keys=True, ensure_ascii=False))


def _executado(conexao, coluna: str, id_: int) -> bool:
    return bool(conexao.execute(f"SELECT 1 FROM execucao WHERE {coluna} = ? LIMIT 1", (id_,)).fetchone())


def sincronizar(conexao: sqlite3.Connection, modelos: Path | None = None, condicoes: Path | None = None) -> dict:
    dados_modelos = _ler(modelos or config.CATALOGO_MODELOS)
    dados_condicoes = _ler(condicoes or config.CATALOGO_CONDICOES)
    todas_vias = vias(modelos)
    with dbm.transacao(conexao):
        for m in dados_modelos.get("modelo", []):
            via = todas_vias.get(m["via"])
            if via is None:
                raise ValueError(f"modelo {m['slug']}: via '{m['via']}' não existe")
            if not via.paga and m.get("preco_in_usd_mtok"):
                raise ValueError(f"modelo {m['slug']}: tem preço mas a via '{via.nome}' está marcada como gratuita")
            valores = {c: (m.get(c) if m.get(c) != "" else None) for c in CAMPOS_MODELO}
            valores["raciocinio"] = int(bool(valores["raciocinio"]))
            valores["treina_com_dados"] = None if via.treina_com_dados is None else int(via.treina_com_dados)
            linha = conexao.execute("SELECT * FROM modelo WHERE slug = ?", (m["slug"],)).fetchone()
            if linha:
                for c in _CAMPOS_DO_OLLAMA:   # vêm de `locais.registrar_metadados`, não do catálogo: não apagar
                    if valores[c] is None:
                        valores[c] = linha[c]
                if _executado(conexao, "modelo_id", linha["id"]):
                    mudou = [c for c in CAMPOS_IMUTAVEIS_MODELO if linha[c] != valores[c]]
                    if mudou:
                        raise ValueError(f"modelo '{m['slug']}' já executado: {mudou} não podem mudar (crie outro slug)")
                sets = ", ".join(f"{c} = ?" for c in valores)
                conexao.execute(f"UPDATE modelo SET {sets} WHERE id = ?", (*valores.values(), linha["id"]))
            else:
                colunas = ", ".join(["slug", *valores])
                marcas = ", ".join("?" for _ in range(len(valores) + 1))
                conexao.execute(f"INSERT INTO modelo ({colunas}) VALUES ({marcas})", (m["slug"], *valores.values()))
        for c in dados_condicoes.get("condicao", []):
            c.setdefault("json_mode", False)
            sha = sha_condicao(c)
            linha = conexao.execute("SELECT id, config_sha FROM condicao WHERE slug = ?", (c["slug"],)).fetchone()
            if linha and linha["config_sha"] != sha and _executado(conexao, "condicao_id", linha["id"]):
                raise ValueError(f"condição '{c['slug']}' já tem execuções: crie um slug novo em vez de editar")
            campos = (c["formato"], c["temperatura"], c["repeticoes"], int(c["embaralhar"]), int(c["json_mode"]),
                      c["template"], sha)
            if linha:
                conexao.execute("""UPDATE condicao SET formato=?, temperatura=?, repeticoes=?, embaralhar=?, json_mode=?,
                                   template=?, config_sha=? WHERE id=?""", (*campos, linha["id"]))
            else:
                conexao.execute("""INSERT INTO condicao (slug, formato, temperatura, repeticoes, embaralhar, json_mode,
                                   template, config_sha) VALUES (?,?,?,?,?,?,?,?)""", (c["slug"], *campos))
    return {"modelos": len(dados_modelos.get("modelo", [])), "condicoes": len(dados_condicoes.get("condicao", []))}
