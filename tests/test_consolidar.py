"""Consolidação: período 2020–2026, gabarito C/E e duplicatas conflitantes (revisão 2: ALTO-3, ALTO-4, MÉDIO-6)."""

import json

import pytest

from iac import db as dbm
from iac import preparo
from iac.disciplinas import popular


@pytest.fixture
def banco(tmp_path):
    conexao = dbm.conectar(tmp_path / "t.sqlite")
    popular(conexao)
    conexao.execute("INSERT INTO banca (sigla, nome) VALUES ('fgv', 'FGV')")
    conexao.commit()
    return conexao


def _concurso(banco, slug, ano_edital=None, data_prova=None):
    cid = banco.execute("INSERT INTO concurso (banca_id, slug, ano_edital, data_prova, data_prova_fonte) VALUES (1,?,?,?,?)",
                        (slug, ano_edital, data_prova, "gabarito" if data_prova else None)).lastrowid
    did = banco.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (?, ?, 'p.pdf', 'prova')",
                        (cid, f"u-{slug}")).lastrowid
    return cid, did


def _caderno(banco, slug, formato="ME5", itens=(("h1", "B"),), **concurso):
    cid, did = _concurso(banco, slug, **concurso)
    caderno = banco.execute("""INSERT INTO caderno (documento_id, concurso_id, status, versao_extrator, formato, n_itens)
                               VALUES (?, ?, 'ok', 't', ?, ?)""", (did, cid, formato, len(itens))).lastrowid
    for n, (hash_, gabarito) in enumerate(itens, 1):
        alternativas = None if formato == "CE" else json.dumps({l: f"opção {l} de {hash_}" for l in "ABCDE"})
        banco.execute("""INSERT INTO item (caderno_id, numero, enunciado, alternativas_json, hash_conteudo, gabarito_definitivo)
                         VALUES (?, ?, ?, ?, ?, ?)""", (caderno, n, f"Enunciado {hash_}", alternativas, hash_, gabarito))
    banco.commit()
    return caderno


def _motivos(banco, hash_):
    q = banco.execute("SELECT id, elegivel FROM questao WHERE hash_conteudo = ?", (hash_,)).fetchone()
    motivos = {r[0] for r in banco.execute(
        "SELECT motivo FROM exclusao WHERE etapa='consolidar' AND entidade='questao' AND entidade_id=?", (q["id"],))}
    return q["elegivel"], motivos


def test_prova_dentro_do_periodo_e_elegivel(banco):
    _caderno(banco, "a24", data_prova="2024-03-10", ano_edital=2023)
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (1, set())


def test_prova_antes_de_2020_fica_fora(banco):
    _caderno(banco, "a18", data_prova="2018-06-01")
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (0, {"fora_do_periodo"})


def test_questao_repetida_vale_a_primeira_ocorrencia(banco):
    _caderno(banco, "a19", data_prova="2019-11-01")
    _caderno(banco, "a23", data_prova="2023-02-01")
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (0, {"fora_do_periodo"})


def test_sem_data_mas_edital_de_2021_e_elegivel(banco):
    _caderno(banco, "a21", ano_edital=2021)
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (1, set())


def test_sem_data_e_edital_de_2019_e_sem_data(banco):
    _caderno(banco, "a19", ano_edital=2019)             # edital de 2019 pode ter prova em 2020: precisa da data
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (0, {"sem_data"})


def test_sem_data_e_sem_ano_e_sem_data(banco):
    _caderno(banco, "alba")
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (0, {"sem_data"})


def test_edital_antigo_sem_data_fica_fora(banco):
    _caderno(banco, "a15", ano_edital=2015)
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "h1") == (0, {"fora_do_periodo"})


def test_caderno_ce_com_tabela_de_letras_e_inteiro_duvidoso(banco):
    # tabela A–E pareada por engano: os itens com "C" e "E" passariam como certo/errado
    _caderno(banco, "c22", formato="CE", itens=(("k1", "C"), ("k2", "A"), ("k3", "E")), data_prova="2022-01-01")
    preparo.consolidar(banco, log=lambda m: None)            # não pode estourar o CHECK nem derrubar o lote
    for hash_ in ("k1", "k2", "k3"):
        elegivel, motivos = _motivos(banco, hash_)
        assert elegivel == 0 and "gabarito_duvidoso" in motivos
    assert banco.execute("SELECT gabarito_status FROM caderno").fetchone()[0] == "duvidoso"


def test_duplicata_ce_com_gabaritos_diferentes_e_inconsistente(banco):
    _caderno(banco, "c22", formato="CE", itens=(("k1", "C"),), data_prova="2022-01-01")
    _caderno(banco, "c24", formato="CE", itens=(("k1", "E"),), data_prova="2024-01-01")
    preparo.consolidar(banco, log=lambda m: None)
    assert _motivos(banco, "k1") == (0, {"gabarito_inconsistente"})


def test_definir_data_manual(banco):
    _caderno(banco, "alba")
    preparo.definir_data(banco, "fgv", "alba", "2022-05-08")
    linha = banco.execute("SELECT data_prova, data_prova_fonte, ano_edital FROM concurso").fetchone()
    assert tuple(linha) == ("2022-05-08", "manual", None)
    with pytest.raises(ValueError):
        preparo.definir_data(banco, "fgv", "alba", "2022-13-40")
    with pytest.raises(ValueError):
        preparo.definir_data(banco, "fgv", "nao-existe", "2022-05-08")


def test_gabarito_de_questao_ja_usada_nao_muda_e_fica_auditado(banco):
    # revisão 2, MÉDIO-7: o definitivo chega depois do experimento
    _caderno(banco, "a24", data_prova="2024-03-10")
    preparo.consolidar(banco, log=lambda m: None)
    qid = banco.execute("SELECT id FROM questao").fetchone()[0]
    banco.execute("INSERT INTO amostra (nome, semente, criterio, n_pedido, versao_codigo, criada_em) VALUES ('a',1,'x',1,'v','t')")
    banco.execute("INSERT INTO amostra_estrato (amostra_id, estrato, n_populacao, n_amostra) VALUES (1,'e',1,1)")
    banco.execute("INSERT INTO amostra_questao (amostra_id, questao_id, estrato) VALUES (1, ?, 'e')", (qid,))
    banco.execute("UPDATE item SET gabarito_definitivo = 'D'")
    banco.commit()
    preparo.consolidar(banco, log=lambda m: None)
    assert banco.execute("SELECT gabarito FROM questao").fetchone()[0] == "B"
    auditoria = banco.execute("SELECT entidade, entidade_id, evento, detalhe FROM auditoria").fetchall()
    assert [tuple(a) for a in auditoria] == [("questao", qid, "gabarito_mudou_apos_uso", "B -> D")]
    q = banco.execute("SELECT gabarito_fonte, alterada_recurso, elegivel FROM questao").fetchone()
    assert tuple(q) == ("definitivo", None, 1)          # revisão 3, A2: a fonte acompanha o valor congelado
    banco.execute("UPDATE item SET gabarito_definitivo = 'C'")
    banco.commit()
    preparo.consolidar(banco, log=lambda m: None)
    preparo.consolidar(banco, log=lambda m: None)       # rodar de novo não duplica o histórico
    detalhe = banco.execute("SELECT detalhe FROM auditoria WHERE evento = 'gabarito_mudou_apos_uso'").fetchone()[0]
    assert detalhe == "B -> D | B -> C"


def test_questao_usada_que_ficaria_inelegivel_continua_no_experimento(banco):
    # revisão 3, B5: anulada depois do uso sumiria dos pendentes dos outros modelos e quebraria o pareamento
    _caderno(banco, "a24", data_prova="2024-03-10")
    preparo.consolidar(banco, log=lambda m: None)
    qid = banco.execute("SELECT id FROM questao").fetchone()[0]
    banco.execute("INSERT INTO amostra (nome, semente, criterio, n_pedido, versao_codigo, criada_em) VALUES ('a',1,'x',1,'v','t')")
    banco.execute("INSERT INTO amostra_estrato (amostra_id, estrato, n_populacao, n_amostra) VALUES (1,'e',1,1)")
    banco.execute("INSERT INTO amostra_questao (amostra_id, questao_id, estrato) VALUES (1, ?, 'e')", (qid,))
    banco.execute("UPDATE item SET gabarito_definitivo = 'X'")
    banco.commit()
    preparo.consolidar(banco, log=lambda m: None)
    assert tuple(banco.execute("SELECT gabarito, elegivel FROM questao").fetchone()) == ("B", 1)
    eventos = {r[0]: r[1] for r in banco.execute("SELECT evento, detalhe FROM auditoria")}
    assert eventos["gabarito_mudou_apos_uso"] == "B -> X" and eventos["ficaria_inelegivel_apos_uso"] == "anulada"


def test_gabarito_de_questao_nao_usada_acompanha_o_definitivo(banco):
    _caderno(banco, "a24", data_prova="2024-03-10")
    preparo.consolidar(banco, log=lambda m: None)
    banco.execute("UPDATE item SET gabarito_definitivo = 'D'")
    banco.commit()
    preparo.consolidar(banco, log=lambda m: None)
    assert banco.execute("SELECT gabarito FROM questao").fetchone()[0] == "D"
