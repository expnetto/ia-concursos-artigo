"""Caminhos e configuração. Nada de caminho fixo da máquina: tudo relativo à raiz do projeto."""

from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DADOS = Path(os.environ.get("IAC_DADOS", RAIZ / "data"))
BANCO = DADOS / "iac.sqlite"
PDFS = DADOS / "raw"                      # PDFs endereçados por sha256: raw/ab/abcdef....pdf
CATALOGO_MODELOS = RAIZ / "config" / "modelos.toml"
CATALOGO_CONDICOES = RAIZ / "config" / "condicoes.toml"

USER_AGENT = "ia-concursos/0.1 (pesquisa academica TCC Unipe)"
INTERVALO_MINIMO_POR_HOST_S = 1.0         # cortesia com os sites das bancas
TAMANHO_MAXIMO_PDF = 60 * 1024 * 1024     # PDF maior que isso é suspeito: recusamos
TIMEOUT_HTTP_S = 60.0

ANO_INICIO_CORPUS = 2020                  # recorte do TCC: provas aplicadas de 2020 a 2026
ANO_FIM_CORPUS = 2026


def carregar_env(caminho: Path | None = None) -> None:
    """Lê `.env` (CHAVE=valor) para os.environ sem sobrescrever o que já existe.

    Implementação mínima de propósito: evita dependência e nunca imprime valores.
    """
    caminho = caminho or RAIZ / ".env"
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))
