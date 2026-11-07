"""Classificação de disciplina por LLM local: só lista fechada, rastreável e validável às cegas."""

import json

import pytest

from iac import catalogo, classificacao
from iac import db as dbm
from iac.disciplinas import popular
from iac.provedores import Resposta


@pytest.fixture
def banco(tmp_path):
    conexao = dbm.conectar(tmp_path / "t.sqlite")
    popular(conexao)
    conexao.execute("INSERT INTO banca (sigla, nome) VALUES ('fgv', 'FGV')")
    conexao.execute("INSERT INTO concurso (banca_id, slug) VALUES (1, 'x24')")
    conexao.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (1, 'u', 'p.pdf', 'prova')")
    conexao.execute("""INSERT INTO caderno (documento_id, concurso_id, status, versao_extrator, formato, n_itens)
                       VALUES (1, 1, 'ok', 't', 'ME5', 4)""")
    for n in range(1, 5):
        conexao.execute("""INSERT INTO item (caderno_id, numero, enunciado, alternativas_json, hash_conteudo)
                           VALUES (1, ?, ?, ?, ?)""", (n, f"Questão {n} sobre o tema", json.dumps({"A": "a", "B": "b"}), f"h{n}"))
        disciplina = "lingua_portuguesa" if n == 4 else None
        conexao.execute("""INSERT INTO questao (hash_conteudo, item_canonico_id, formato, gabarito, elegivel, disciplina,
                           disciplina_fonte) VALUES (?, ?, 'ME5', 'A', 1, ?, ?)""",
                        (f"h{n}", n, disciplina, "secao" if disciplina else None))
    conexao.commit()
    catalogo.sincronizar(conexao)
    return conexao


class Falso:
    def __init__(self, respostas):
        self.respostas = respostas if isinstance(respostas, dict) else list(respostas)
        self.corpos = []

    def __call__(self, via):
        return self

    def completar(self, corpo):
        self.corpos.append(corpo)
        if isinstance(self.respostas, dict):          # resposta pela questão: a ordem de classificação é aleatória
            numero = int(corpo["messages"][-1]["content"].split("Questão ")[1].split()[0])
            item = self.respostas[numero]
        else:
            item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return Resposta(texto=item, modelo_retornado="falso", tokens_in=300, tokens_out=8, latencia_ms=5,
                        finish_reason="stop")

    def fechar(self):
        pass


def test_classifica_so_o_que_falta_e_registra_a_origem(banco):
    falso = Falso({1: '{"disciplina": "direito_administrativo"}', 2: 'Resposta: "informatica"', 3: '{"disciplina": "astrologia"}'})
    contagem = classificacao.classificar(banco, fabrica_cliente=falso, log=lambda m: None)
    assert contagem == {"classificadas": 2, "invalidas": 1, "erros": 0}
    linhas = dict(banco.execute("SELECT id, disciplina || '/' || COALESCE(disciplina_fonte, '-') FROM questao"))
    assert linhas == {1: "direito_administrativo/llm", 2: "informatica/llm", 3: None, 4: "lingua_portuguesa/secao"}
    origem = banco.execute("SELECT detalhe FROM auditoria WHERE entidade_id = 1 AND evento = 'disciplina_llm'").fetchone()[0]
    assert origem.startswith("qwen2.5-7b@" + classificacao.VERSAO_CLASSIFICADOR)
    assert len(falso.corpos) == 3 and falso.corpos[0]["temperature"] == 0
    assert "direito_administrativo" in falso.corpos[0]["messages"][-1]["content"]   # a lista fechada vai no prompt


def test_rodar_de_novo_nao_reclassifica(banco):
    classificacao.classificar(banco, fabrica_cliente=Falso(['{"disciplina": "afo"}'] * 3), log=lambda m: None)
    falso = Falso([])
    assert classificacao.classificar(banco, fabrica_cliente=falso, log=lambda m: None)["classificadas"] == 0
    assert falso.corpos == []


def test_erros_seguidos_param_a_rodada(banco):
    falso = Falso([ConnectionError("ollama fora")] * 3)
    contagem = classificacao.classificar(banco, fabrica_cliente=falso, log=lambda m: None, max_erros_seguidos=2)
    assert contagem["erros"] == 2 and len(falso.corpos) == 2


def test_validacao_as_cegas_exporta_sem_o_rotulo_e_mede_concordancia(banco, tmp_path):
    classificacao.classificar(banco, fabrica_cliente=Falso({1: '{"disciplina": "afo"}', 2: '{"disciplina": "informatica"}',
                                                            3: '{"disciplina": "afo"}'}), log=lambda m: None)
    planilha, chave = classificacao.exportar_validacao(banco, tmp_path / "v.xlsx", n=3, semente=1,
                                                          chave=tmp_path / "chave.json")
    from openpyxl import load_workbook
    aba = load_workbook(planilha)["Questões"]
    assert not any(c.value in ("afo", "informatica") for c in aba["C"][1:])   # coluna da resposta vem vazia
    ids = [linha[0].value for linha in aba.iter_rows(min_row=2)]
    for i, linha in enumerate(aba.iter_rows(min_row=2), start=0):
        linha[2].value = "afo" if ids[i] in (1, 3) else "tecnologia_informacao"   # humano discorda na questão 2
    load = aba.parent
    load.save(planilha)
    relatorio = classificacao.importar_validacao(banco, planilha, chave)
    assert relatorio["n"] == 3 and relatorio["concordancia"] == pytest.approx(2 / 3)
    assert banco.execute("SELECT disciplina, disciplina_fonte FROM questao WHERE id = 2").fetchone()[:] == \
        ("tecnologia_informacao", "humano")
