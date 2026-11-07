"""Acesso ao SQLite. Um banco só; esquema em esquema.sql; versão em PRAGMA user_version."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

from iac import config

VERSAO_ESQUEMA = 3


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    caminho = caminho or config.BANCO
    if str(caminho) != ":memory:":
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(caminho, timeout=30)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    conexao.execute("PRAGMA journal_mode = WAL")   # leitura (análise) enquanto o executor grava
    conexao.execute("PRAGMA busy_timeout = 30000")
    conexao.execute("PRAGMA synchronous = FULL")    # queda de energia não perde a reserva de uma chamada paga
    _migrar(conexao)
    return conexao


VERSAO_BASE = 2      # esquema.sql cria a versão 2; as seguintes são migrações aditivas abaixo

_MIGRACOES = {
    # revisão 2, MÉDIO-7: gabarito usado em cada avaliação + trilha de mudanças que não podem ser aplicadas
    3: """ALTER TABLE avaliacao ADD COLUMN gabarito_usado TEXT;
          CREATE TABLE IF NOT EXISTS auditoria (
              id           INTEGER PRIMARY KEY,
              entidade     TEXT NOT NULL,
              entidade_id  INTEGER NOT NULL,
              evento       TEXT NOT NULL,
              detalhe      TEXT,
              criada_em    TEXT NOT NULL,
              UNIQUE (entidade, entidade_id, evento)
          );""",
}


def _migrar(conexao: sqlite3.Connection) -> None:
    versao = conexao.execute("PRAGMA user_version").fetchone()[0]
    if versao > VERSAO_ESQUEMA:
        raise RuntimeError(f"banco na versão {versao}, código conhece até {VERSAO_ESQUEMA}")
    if 0 < versao < VERSAO_BASE:
        raise RuntimeError(f"banco na versão {versao}: faça backup (iac backup) e recrie com a versão {VERSAO_ESQUEMA}")
    if versao == 0:
        sql = resources.files("iac").joinpath("esquema.sql").read_text(encoding="utf-8")
        conexao.executescript(sql)
        conexao.execute(f"PRAGMA user_version = {VERSAO_BASE}")
        conexao.commit()
        versao = VERSAO_BASE
    for alvo in range(versao + 1, VERSAO_ESQUEMA + 1):   # nunca editar esquema.sql de forma destrutiva
        conexao.execute("BEGIN IMMEDIATE")                 # outro processo pode ter migrado enquanto isso
        if conexao.execute("PRAGMA user_version").fetchone()[0] >= alvo:
            conexao.rollback()
            continue
        conexao.rollback()
        conexao.executescript(f"BEGIN IMMEDIATE; {_MIGRACOES[alvo]} PRAGMA user_version = {alvo}; COMMIT;")


@contextmanager
def transacao(conexao: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit se tudo der certo; rollback se qualquer coisa falhar no meio."""
    try:
        yield conexao
        conexao.commit()
    except BaseException:
        conexao.rollback()
        raise


TENTATIVAS_BANCO_OCUPADO = 5


def gravar_com_retentativa(conexao: sqlite3.Connection, sql: str, parametros: tuple = ()) -> None:
    """Uma escrita curta que sobrevive a "database is locked" (outro processo segurando o lock por > 30 s)."""
    for tentativa in range(TENTATIVAS_BANCO_OCUPADO):
        try:
            with transacao(conexao):
                conexao.execute(sql, parametros)
            return
        except sqlite3.OperationalError as erro:
            if "locked" not in str(erro) or tentativa + 1 == TENTATIVAS_BANCO_OCUPADO:
                raise
            time.sleep(2 ** tentativa)


def obter_ou_criar(conexao: sqlite3.Connection, tabela: str, chaves: dict, extras: dict | None = None) -> int:
    """Upsert simples por chave natural. Devolve o id. `tabela` vem só do nosso código."""
    onde = " AND ".join(f"{k} = ?" for k in chaves)
    linha = conexao.execute(f"SELECT id FROM {tabela} WHERE {onde}", tuple(chaves.values())).fetchone()
    if linha:
        if extras:
            sets = ", ".join(f"{k} = COALESCE(?, {k})" for k in extras)
            conexao.execute(f"UPDATE {tabela} SET {sets} WHERE id = ?", (*extras.values(), linha[0]))
        return linha[0]
    campos = {**chaves, **(extras or {})}
    colunas = ", ".join(campos)
    marcas = ", ".join("?" for _ in campos)
    cursor = conexao.execute(f"INSERT INTO {tabela} ({colunas}) VALUES ({marcas})", tuple(campos.values()))
    return cursor.lastrowid


def backup(conexao: sqlite3.Connection, destino: Path) -> Path:
    """Cópia consistente do banco (funciona com o banco em uso)."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    conexao.execute("VACUUM INTO ?", (str(destino),))
    return destino
