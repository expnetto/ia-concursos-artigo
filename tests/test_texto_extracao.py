from iac import disciplinas, extracao, pdf
from iac import texto as tx
from iac.bancas import fgv
from iac.bancas.base import MetadadosCaderno, TabelaGabarito, escolher_tabela


def test_norm_1a1_preserva_comprimento_e_posicoes():
    original = "Questão “crase” – À BEÇA ﬁm nº 3"
    norm = tx.norm_1a1(original)
    assert len(norm) == len(original)
    posicao = norm.find("crase")
    assert original[posicao: posicao + 5] == "crase"


def test_hash_ignora_ordem_mas_nao_simbolos_nem_texto_base():
    a = tx.hash_conteudo("Qual é?", {"A": "um", "B": "dois", "C": "três"})
    assert a == tx.hash_conteudo("Qual é?", {"A": "três", "B": "um", "C": "dois"})
    # regressão M3: "µ < 720" e "µ > 720" eram a mesma questão
    assert tx.hash_conteudo("H0?", {"A": "µ < 720", "B": "x"}) != tx.hash_conteudo("H0?", {"A": "µ > 720", "B": "x"})
    # regressão A4: mesmo enunciado com textos de apoio diferentes
    assert tx.hash_conteudo("No texto, o autor", None, "Texto A") != tx.hash_conteudo("No texto, o autor", None, "Texto B")


def test_marcadores_de_destaque_nao_colidem_com_codigo():
    assert pdf.sem_destaques("o termo ⟦b⟧⟦u⟧não⟦/u⟧⟦/b⟧ é ⟦i⟧itálico⟦/i⟧") == "o termo não é itálico"
    assert not pdf.tem_destaque("def __init__(self): return 2**3  # lacuna ____")
    assert pdf.destaques_para_prompt("⟦u⟧não⟦/u⟧") == "[sublinhado]não[/sublinhado]"


def test_simbolos_da_fonte_symbol_sao_traduzidos():
    assert pdf.traduzir_simbolos("CPF  Nome  3") == "CPF → Nome ≤ 3"
    assert pdf.MARCADOR_SIMBOLO in pdf.traduzir_simbolos("")


def test_disciplinas_casam_palavra_inteira():
    # regressão C3: substring dentro de outra palavra
    assert disciplinas.disciplina_da_secao("Infraestrutura Tecnológica") == "tecnologia_informacao"
    assert disciplinas.disciplina_da_secao("Noções de Sustentabilidade") != "saude"
    assert disciplinas.disciplina_da_secao("Segurança Cibernética e da Informação") == "tecnologia_informacao"
    assert disciplinas.disciplina_da_secao("Conhecimentos Específicos") is None
    assert disciplinas.eh_titulo_de_secao("Língua Portuguesa")
    assert not disciplinas.eh_titulo_de_secao("GROUP BY ano")
    assert not disciplinas.eh_titulo_de_secao("Text II")


CADERNO = """Conhecimentos Gerais
Língua Portuguesa
1
Assinale a opção correta sobre a frase em destaque: ⟦i⟧É preciso estar atento.⟦/i⟧
(A) primeira opção.
(B) segunda opção.
(C) terceira opção.
(D) quarta opção.
(E) quinta opção.
Língua Inglesa
Use the following TEXT to answer the next question.
This is a long English text about consulting careers and jobs in the industry of technology.
2
According to the text, the author
(A) alpha.
(B) beta.
(C) gamma.
(D) delta.
(E) epsilon.
3
Observe a figura e assinale.
[[IMAGEM]]
(A) 1.
(B) 2.
(C) 3.
(D) 4.
(E) 5.
Realização
"""


def test_segmentar_secoes_texto_base_imagem_e_rodape():
    resultado = extracao.segmentar(CADERNO)
    assert [i.numero for i in resultado.itens] == [1, 2, 3]
    q1, q2, q3 = resultado.itens
    assert q1.secao == "Língua Portuguesa"                 # o título mais específico vence
    assert q1.alternativas["E"] == "quinta opção."
    assert q1.problemas == []
    assert q2.secao == "Língua Inglesa"
    assert q2.texto_base and q2.texto_base.startswith("Use the following TEXT")
    assert extracao.P_IMAGEM in q3.problemas
    assert q3.alternativas["E"] == "5."                    # rodapé "Realização" não vaza


def test_numero_solto_numa_fracao_nao_trava_a_sequencia():
    # regressão C1: um "1" de fração logo depois do número da questão fazia sumir as seguintes
    caderno = CADERNO.replace("Observe a figura e assinale.", "1\n√5\nCalcule e assinale.")
    numeros = [i.numero for i in extracao.segmentar(caderno).itens]
    assert numeros == [1, 2, 3]


def test_contagem_da_capa_manda_para_quarentena():
    capa = "Além deste caderno contendo 70 (setenta) questões objetivas\n"
    resultado = extracao.segmentar(capa + CADERNO)
    assert resultado.esperado == 70 and resultado.status == "quarentena"
    assert extracao.esperado_da_capa("caderno contendo quarenta questões objetivas") == 40


def test_destaque_perdido_e_texto_ausente():
    caderno = CADERNO.replace("⟦i⟧É preciso estar atento.⟦/i⟧", "É preciso estar atento.")
    assert extracao.P_DESTAQUE_PERDIDO in extracao.segmentar(caderno).itens[0].problemas
    sem_texto = CADERNO.replace("Use the following TEXT to answer the next question.\n"
                                "This is a long English text about consulting careers and jobs in the industry of technology.\n", "")
    assert extracao.P_TEXTO_AUSENTE in extracao.segmentar(sem_texto).itens[1].problemas


def test_codigo_preserva_quebras_de_linha():
    bloco = "public class A {\n    int x;\n    void f() {\n        x = 1;\n    }\n}"
    assert "\n" in extracao.normalizar_bloco(bloco)
    assert extracao.normalizar_bloco("frase quebrada\nno meio") == "frase quebrada no meio"


def test_cabecalhos_de_gabarito_fgv():
    assert fgv._interpretar_cabecalho("Analista de TI da Fazenda Estadual – Prova 2 – Tipo 1")[1:3] == ("1", "2")
    assert fgv._interpretar_cabecalho("Consultor Legislativo - Assessoramento - 1 - Turno Manhã")[1:] == ("1", None, "manha")
    cargo, tipo, _, turno = fgv._interpretar_cabecalho("Analista de TI (Tarde) - Tipo 2")
    assert (cargo, tipo, turno) == ("Analista de TI", "2", "tarde")


def _tabela(cargo, tipo="1", respostas=None, **extra):
    return TabelaGabarito(cargo=cargo, tipo_caderno=tipo, respostas=respostas or {1: "A", 2: "B"},
                          versao="definitivo", **extra)


def test_casamento_recusa_empate_e_respeita_sessao():
    meta = MetadadosCaderno(cargo="ANALISTA DE SISTEMAS", nivel=None, tipo_caderno="1", turno="tarde",
                            texto_capa_norm="analista de sistemas")
    # regressão C2a: empate entre DUAS tabelas com respostas diferentes era resolvido pela primeira
    empate = [_tabela("Analista de Sistemas", respostas={1: "A", 2: "B"}),
              _tabela("Analista de Sistemas", respostas={1: "C", 2: "D"})]
    assert escolher_tabela(meta, empate, 2)[0] is None
    # a sessão (turno) separa as duas
    separadas = [_tabela("Analista de Sistemas", respostas={1: "A", 2: "B"}, turno="manha"),
                 _tabela("Analista de Sistemas", respostas={1: "C", 2: "D"}, turno="tarde")]
    assert escolher_tabela(meta, separadas, 2)[0].respostas[1] == "C"
    # regressão C2b: cargo de uma palavra ("Sistemas") não casa mais com qualquer capa
    assert escolher_tabela(meta, [_tabela("Sistemas")], 2)[0] is None
    # tamanho diferente nunca casa
    assert escolher_tabela(meta, [_tabela("Analista de Sistemas", respostas={1: "A"})], 2)[0] is None


def test_zero_solto_depois_nao_esconde_a_sequencia_do_um():
    # regressão (15/10): cadeia_mais_longa pulava o início 1 quando havia um candidato "0" mais adiante
    fortes = {1: [(10, 12)], 2: [(20, 22)], 3: [(30, 32)], 0: [(40, 42)]}
    aceitos, _ = extracao.cadeia_mais_longa(fortes, {})
    assert [n for _, _, n in aceitos] == [1, 2, 3]


def test_caderno_de_quatro_alternativas_nao_e_incompleto():
    # FGV nível médio/professor: (A) a (D). Antes, todas as questões viravam "alternativas_incompletas"
    partes = []
    for n in range(1, 13):
        partes += [str(n), f"Enunciado da questão {n}, assinale a opção correta.",
                   "(A) primeira.", "(B) segunda.", "(C) terceira.", "(D) quarta."]
    resultado = extracao.segmentar("\n".join(partes) + "\n")
    assert resultado.status == "ok"
    assert all(extracao.P_ALTERNATIVAS not in i.problemas for i in resultado.itens)
    assert resultado.itens[0].alternativas["D"] == "quarta."


def test_questao_com_tres_alternativas_num_caderno_de_quatro_continua_incompleta():
    partes = []
    for n in range(1, 13):
        letras = "ABC" if n == 5 else "ABCD"
        partes += [str(n), f"Enunciado {n}."] + [f"({l}) opção {l}." for l in letras]
    itens = {i.numero: i for i in extracao.segmentar("\n".join(partes) + "\n").itens}
    assert extracao.P_ALTERNATIVAS in itens[5].problemas and extracao.P_ALTERNATIVAS not in itens[6].problemas
