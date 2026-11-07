"""Ingestão do ENEM: casamento por gabarito, exclusões, compatibilidade com o executor e isolamento do preparo."""

import csv
import json

import pytest

from iac import amostragem, enem, preparo, prompts
from iac import db as dbm
from iac import enem_fontes as fontes
from iac.disciplinas import popular

COLUNAS = ["CO_POSICAO", "SG_AREA", "CO_ITEM", "TX_GABARITO", "CO_HABILIDADE", "IN_ITEM_ABAN", "TX_MOTIVO_ABAN",
           "NU_PARAM_A", "NU_PARAM_B", "NU_PARAM_C", "TX_COR", "CO_PROVA", "TP_LINGUA", "IN_ITEM_ADAPTADO"]
TEMPLATE = "{instrucao_destaque}<questao>\n{apoio}{enunciado}\n\n{alternativas}\n</questao>"


def _gab(numero):
    return "ABCDE"[(numero * 7 + numero // 3) % 5]


def _linha(pos, area, item, gab, prova, cor="AZUL", lingua="", aban="0", b="0.5", adaptado="0"):
    return {"CO_POSICAO": str(pos), "SG_AREA": area, "CO_ITEM": str(item), "TX_GABARITO": gab, "CO_HABILIDADE": "3",
            "IN_ITEM_ABAN": aban, "TX_MOTIVO_ABAN": "", "NU_PARAM_A": "1.2", "NU_PARAM_B": b if aban == "0" else "",
            "NU_PARAM_C": "0.15", "TX_COR": cor, "CO_PROVA": str(prova), "TP_LINGUA": lingua, "IN_ITEM_ADAPTADO": adaptado}


def _area(area, inicio, prova, base_item, cor="AZUL", adaptado="0", trocar=None, embaralhar=False):
    linhas = []
    numeros = list(range(inicio, inicio + 45))
    ordem = numeros[::-1] if embaralhar else numeros
    for pos, numero in zip(numeros, ordem):
        item = base_item + numero + (100000 if trocar and numero in trocar else 0)
        if area == "LC" and pos <= 5:
            linhas.append(_linha(pos, area, item, _gab(numero), prova, cor, "0", adaptado=adaptado))
            linhas.append(_linha(pos, area, item + 50000, "E", prova, cor, "1", adaptado=adaptado))
        else:
            linhas.append(_linha(pos, area, item, _gab(numero), prova, cor, adaptado=adaptado))
    return linhas


def _ano_sintetico(raiz, ano=2022, extras=()):
    linhas = (_area("LC", 1, 10, 1000) + _area("LC", 1, 11, 1000, embaralhar=True)
              + _area("CH", 46, 20, 2000) + _area("MT", 136, 30, 3000) + _area("MT", 136, 31, 3000, "CINZA")
              + _area("MT", 136, 32, 3000, "LARANJA", trocar={140}) + _area("MT", 136, 33, 3000, "LARANJA", "1")
              + list(extras))
    pasta = raiz / "inep" / str(ano)
    pasta.mkdir(parents=True)
    with open(pasta / f"ITENS_PROVA_{ano}.csv", "w", encoding="latin1", newline="") as arquivo:
        escritor = csv.DictWriter(arquivo, COLUNAS, delimiter=";")
        escritor.writeheader()
        escritor.writerows(linhas)
    registros = []
    for numero in list(range(1, 91)) + list(range(136, 181)):
        registro = {"id": f"questao_{numero:02d}", "IU": False, "question": f"Enunciado {numero} da prova {ano}",
                    "description": [], "alternatives": [f"opção {l} {numero}" for l in "ABCDE"],
                    "label": _gab(numero), "figures": []}
        if numero == 50:
            registro.update(IU=True, question="[[placeholder]]\nVeja a tirinha.",
                            description=["Descrição da imagem: tirinha."])
        if numero == 51:
            registro.update(IU=True, question="[[placeholder]]\nVeja o mapa.", description=[])
        registros.append(registro)
    (raiz / "textos").mkdir(exist_ok=True)
    (raiz / "textos" / f"maritaca_{ano}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in registros), encoding="utf-8")


@pytest.fixture
def banco(tmp_path):
    conexao = dbm.conectar(tmp_path / "t.sqlite")
    popular(conexao)
    conexao.commit()
    return conexao


def test_numero_da_questao_2017_e_lingua():
    assert enem.numero_da_questao(2017, "LC", 3, "0") == 3
    assert enem.numero_da_questao(2017, "LC", 8, "1") is None          # espanhol 6–10
    assert enem.numero_da_questao(2017, "LC", 11, "") == 6
    assert enem.numero_da_questao(2017, "MT", 1, "") == 136
    assert enem.numero_da_questao(2022, "LC", 2, "1") is None
    assert enem.numero_da_questao(2022, "CH", 46, "") == 46


def test_casamento_escolhe_caderno_que_bate_e_sem_adaptacao(tmp_path):
    _ano_sintetico(tmp_path)
    itens = fontes.ler_itens_inep(tmp_path / "inep" / "2022" / "ITENS_PROVA_2022.csv")
    textos = fontes.ler_textos(tmp_path, 2022)
    lc = enem.casar(itens, textos, 2022, "LC")
    assert lc.co_prova == 10 and lc.linhas[1]["TP_LINGUA"] == "0"     # caderno 11 tem outra ordem
    mt = enem.casar(itens, textos, 2022, "MT")
    assert mt.co_prova == 30 and 33 not in mt.candidatos               # 33 é adaptado
    assert mt.divergentes == {140}                                     # 32 troca um item de mesmo gabarito


def test_imagem_com_descricao_entra_e_sem_descricao_sai(tmp_path):
    _ano_sintetico(tmp_path)
    textos = fontes.ler_textos(tmp_path, 2022)
    assert textos[50].com_descricao and "[Descrição da imagem: tirinha.]" in textos[50].enunciado
    assert textos[51].tem_imagem and not textos[51].com_descricao
    linha = _linha(51, "CH", 1, "A", 1)
    assert enem.motivos_do_item(linha, textos[51]) == ["imagem_sem_descricao"]
    assert enem.motivos_do_item(_linha(1, "CH", 1, "X", 1, aban="1"), None) == [
        "sem_texto", "anulado", "abandonado_ou_sem_b"]
    assert enem.motivos_do_item(_linha(1, "CH", 1, "A", 1, adaptado="1"), textos[1]) == ["adaptado"]


def _ingerir(banco, tmp_path, anos=(2022,)):
    resumo = [enem.ingerir_ano(banco, tmp_path, ano) for ano in anos]
    return resumo


def test_ingestao_grava_pontes_compativeis_com_o_executor(banco, tmp_path):
    _ano_sintetico(tmp_path)
    resumo = _ingerir(banco, tmp_path)[0]
    assert resumo["areas"]["CH"]["entra"] == 44 and resumo["areas"]["CH"]["imagem_sem_descricao"] == 1
    assert resumo["areas"]["CH"]["com_descricao"] == 1
    assert banco.execute("SELECT COUNT(*) FROM item_param_inep").fetchone()[0] == 135
    questao_id, b = banco.execute(
        "SELECT questao_id, param_b FROM item_param_inep WHERE area = 'CH' AND numero = 46").fetchone()
    assert b == 0.5
    q = prompts.carregar_questao(banco, questao_id)
    assert q["formato"] == "ME5" and q["gabarito"] == _gab(46) and sorted(q["alternativas"]) == list("ABCDE")
    assert "(E) opção E 46" in prompts.montar(TEMPLATE, q, False, 1).texto
    assert banco.execute("SELECT elegivel, nivel_questao, disciplina FROM questao WHERE id = ?",
                         (questao_id,)).fetchone()[:] == (1, "medio", "historia_geografia")
    assert _ingerir(banco, tmp_path)[0]["situacao"] == "ja_ingerido"   # idempotente


def test_preparo_e_amostragem_nao_tocam_no_enem(banco, tmp_path):
    _ano_sintetico(tmp_path)
    _ingerir(banco, tmp_path)
    antes = banco.execute("SELECT COUNT(*), SUM(elegivel) FROM questao").fetchone()[:]
    preparo.gabaritar(banco, log=lambda m: None)
    preparo.consolidar(banco, log=lambda m: None)
    assert banco.execute("SELECT COUNT(*), SUM(elegivel) FROM questao").fetchone()[:] == antes
    with pytest.raises(ValueError, match="nenhuma questão"):
        amostragem.criar(banco, "x", 10, 1)                           # sem bancas: inep fica de fora


def test_amostras_congeladas_e_b_do_teste_lacrado(banco, tmp_path):
    _ano_sintetico(tmp_path, 2022)
    _ingerir(banco, tmp_path)
    banco.execute("INSERT INTO amostra (nome, semente, criterio, n_pedido, criada_em) VALUES ('extensao_me',1,'',0,'')")
    banco.commit()
    with pytest.raises(ValueError, match="nada foi criado"):
        enem.criar_amostras(banco)              # teste e âncora vazios no banco sintético
    assert not banco.execute("SELECT 1 FROM amostra WHERE nome = 'enem_dev'").fetchone()
    resultado = enem._congelar(banco, enem.AMOSTRA_DEV, "t", enem._pontes(banco, enem.ANOS_DEV))
    assert resultado["questoes"] == 45 + 44 + 45     # LC, CH (uma imagem sem descrição), MT
    assert enem._congelar(banco, enem.AMOSTRA_DEV, "t", {})["situacao"] == "ja_existe"
    assert len(enem.b_inep_desenvolvimento(banco)) == resultado["questoes"]
    with pytest.raises(PermissionError):
        enem.b_inep_desenvolvimento(banco, enem.AMOSTRA_TESTE)


def test_conjunto_do_ano():
    assert enem.conjunto_do_ano(2017) == "dev" and enem.conjunto_do_ano(2024) == "teste"
    with pytest.raises(ValueError):
        enem.conjunto_do_ano(2019)


def test_challenge_marca_referencia_a_figura_ausente(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    escolhas = {"text": [f"op {l} " for l in "ABCDE"], "label": list("ABCDE")}
    registros = [
        {"id": "2017_1", "question_number": 1, "exam_year": "2017", "nullified": "None", "answerKey": "A",
         "question": "Um raio forma um ângulo x, conforme indica a figura. Qual é x?", "choices": escolhas},
        {"id": "2017_2", "question_number": 2, "exam_year": "2017", "nullified": "None", "answerKey": "B",
         "question": "O texto fez da figura do alferes o principal dos inconfidentes.", "choices": escolhas},
        {"id": "2016_2", "question_number": 2, "exam_year": "2016", "nullified": "None", "answerKey": "B",
         "question": "outro ano", "choices": escolhas},
    ]
    caminho = tmp_path / "c.parquet"
    pq.write_table(pa.Table.from_pylist(registros), caminho)
    textos = fontes.ler_challenge(caminho)
    assert sorted(textos) == [1, 2] and textos[1].tem_imagem and not textos[2].tem_imagem
    assert textos[2].alternativas["E"] == "op E" and textos[2].gabarito == "B"
