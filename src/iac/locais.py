"""Metadados dos modelos locais (Ollama): quantização, digest e contexto realmente usados.

Gravados no banco a cada `iac modelos`, porque são variáveis de análise (modelo quantizado em 4
bits pode ir pior que o original) e porque um `ollama pull` novo pode trocar o pacote sem aviso.
"""

from __future__ import annotations

import sqlite3

import httpx

from iac import catalogo
from iac import db as dbm


def registrar_metadados(conexao: sqlite3.Connection) -> dict[str, str]:
    via = catalogo.vias().get("ollama")
    if via is None:
        return {}
    base = via.base_url.rsplit("/v1", 1)[0]
    resultado = {}
    try:
        instalados = {m["name"]: m for m in httpx.get(f"{base}/api/tags", timeout=10).json().get("models", [])}
    except (httpx.HTTPError, ValueError) as erro:
        return {"ollama": f"indisponível ({type(erro).__name__})"}
    with dbm.transacao(conexao):
        for modelo in conexao.execute("SELECT id, slug, nome_api FROM modelo WHERE via = 'ollama'").fetchall():
            pacote = instalados.get(modelo["nome_api"])
            if not pacote:
                resultado[modelo["slug"]] = "não baixado"
                continue
            detalhes = pacote.get("details") or {}
            conexao.execute("UPDATE modelo SET quantizacao = ?, digest = ?, versao = ? WHERE id = ?",
                            (detalhes.get("quantization_level"), pacote.get("digest"),
                             f"{modelo['nome_api']}@{(pacote.get('digest') or '')[:12]}", modelo["id"]))
            resultado[modelo["slug"]] = f"{detalhes.get('quantization_level')} {(pacote.get('digest') or '')[:12]}"
    return resultado
