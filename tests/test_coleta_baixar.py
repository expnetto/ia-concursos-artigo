"""Laço de download: o que é recusa permanente, o que volta para a fila."""

import pytest

from iac import coleta
from iac import db as dbm
from iac.coleta.http import Recusado, Transitorio

PDF = b"%PDF-1.7 x"


@pytest.fixture
def banco(tmp_path, monkeypatch):
    monkeypatch.setattr(coleta, "guardar_pdf", lambda dados: ("ab" * 32, len(dados)))
    conexao = dbm.conectar(tmp_path / "t.sqlite")
    conexao.execute("INSERT INTO banca (sigla, nome) VALUES ('fgv', 'FGV')")
    conexao.execute("INSERT INTO concurso (banca_id, slug) VALUES (1, 'x')")
    for n in range(1, 4):
        conexao.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (1, ?, 'p.pdf', 'prova')",
                        (f"https://conhecimento.fgv.br/{n}.pdf",))
    conexao.commit()
    return conexao


class Falso:
    def __init__(self, efeitos):
        self.efeitos = efeitos
        self.fechado = False

    def baixar_pdf(self, url):
        efeito = self.efeitos[url.rsplit("/", 1)[1]]
        if isinstance(efeito, Exception):
            raise efeito
        return efeito

    def fechar(self):
        self.fechado = True


def _erros(banco):
    return {r["id"]: r["erro"] for r in banco.execute("SELECT id, erro FROM documento ORDER BY id")}


def test_transitorio_volta_para_a_fila_e_recusa_nao(banco):
    falso = Falso({"1.pdf": PDF, "2.pdf": Transitorio("HTTP 503"), "3.pdf": Recusado("HTTP 404")})
    criados = []
    contagem = coleta.baixar(banco, log=lambda m: None, fabrica_cliente=lambda s: criados.append(s) or falso)
    assert contagem == {"baixado": 1, "transitorio": 1, "recusado": 1}
    assert criados == ["fgv"]                         # um cliente por banca, não um por documento
    assert falso.fechado
    erros = _erros(banco)
    assert erros[2].startswith("transitorio:") and erros[3].startswith("recusado:")

    falso.efeitos["2.pdf"] = PDF
    assert coleta.baixar(banco, log=lambda m: None, fabrica_cliente=lambda s: falso) == {"baixado": 1}


def test_recusas_antigas_que_eram_de_rede_sao_liberadas(banco):
    banco.execute("UPDATE documento SET erro = 'recusado: host não resolve: conhecimento.fgv.br' WHERE id = 1")
    banco.execute("UPDATE documento SET erro = 'recusado: HTTP 503' WHERE id = 2")
    banco.execute("UPDATE documento SET erro = 'recusado: HTTP 404' WHERE id = 3")
    banco.commit()
    assert coleta.liberar_recusas_transitorias(banco) == 2
    assert _erros(banco) == {1: None, 2: None, 3: "recusado: HTTP 404"}


def test_banca_desconhecida_e_recusa_e_nao_derruba_o_laco(banco):
    banco.execute("INSERT INTO banca (sigla, nome) VALUES ('nova', 'Nova')")
    banco.execute("UPDATE concurso SET banca_id = 2")
    banco.commit()
    contagem = coleta.baixar(banco, log=lambda m: None)
    assert contagem == {"recusado": 3}
