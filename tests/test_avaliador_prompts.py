import pytest

from iac import avaliador, prompts
from iac.provedores import ocultar_chaves, probabilidades_da_letra


@pytest.mark.parametrize("bruta, esperado, metodo", [
    ('{"justificativa": "x", "resposta": "C", "confianca": 80}', "C", "json"),
    ('```json\n{"justificativa": "y", "resposta": "(b)", "confianca": 0.9}\n```', "B", "json"),
    ('<think>talvez A... ou D</think>{"justificativa": "z", "resposta": "D", "confianca": 55}', "D", "json"),
    ("Claro! {'justificativa': 'w', 'resposta': 'E', 'confianca': 70} espero ter ajudado", "E", "json_reparado"),
    ("Pensei bastante.\nResposta: A", "A", "regex"),
    ("Logo, a resposta correta é a alternativa (C).", "C", "regex"),
])
def test_interpretar_me(bruta, esperado, metodo):
    leitura = avaliador.interpretar(bruta, "ME")
    assert (leitura["resposta_exibida"], leitura["metodo_extracao"], leitura["formato_ok"]) == (esperado, metodo, 1)


@pytest.mark.parametrize("bruta, motivo", [
    ("Não sei responder.", "sem_letra"),
    ("Resposta: A seguir explico o raciocínio", "sem_letra"),             # regressão C3
    ("A alternativa e a letra mais citada", "sem_letra"),                  # regressão: IGNORECASE na letra
    ("Descarta-se a alternativa C, pois está errada.", "sem_letra"),       # menção de passagem
    ("<think>A alternativa C parece certa, mas", "truncada"),              # raciocínio truncado
    ('{"justificativa": "ERRADO por isso", "resposta": "N/A"}', "resposta_invalida"),
    ("A resposta correta é a alternativa A. Não, a resposta correta é a alternativa B.", "ambigua"),
])
def test_interpretar_nunca_chuta(bruta, motivo):
    leitura = avaliador.interpretar(bruta, "ME")
    assert leitura["formato_ok"] == 0 and leitura["motivo_invalido"] == motivo


def test_finish_reason_length_e_truncada_mesmo_com_json():
    assert avaliador.interpretar('{"resposta": "A"}', "ME", "length")["motivo_invalido"] == "truncada"


def test_confianca():
    conf = lambda v: avaliador.interpretar(f'{{"resposta": "B", "confianca": {v}}}', "ME")["confianca"]
    assert conf(0.9) == pytest.approx(90) and conf("1.0") == pytest.approx(100) and conf(85) == 85
    assert conf(150) is None


def test_ce_e_pontuacao_com_penalidade():
    assert avaliador.interpretar('{"resposta": "ERRADO", "confianca": 60}', "CE")["resposta_exibida"] == "E"
    assert avaliador.pontuar("B", "C", "CE") == (1, None, 0.0)
    assert avaliador.pontuar("E", "C", "CE") == (0, 0, -1.0)
    assert avaliador.pontuar("C", "C", "CE") == (0, 1, 1.0)


QUESTAO = {"id": 7, "gabarito": "B", "formato": "ME5", "enunciado": "Quanto é 1+1?", "texto_base": None,
           "alternativas": {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"}}
TEMPLATE = "{instrucao_destaque}<questao>{apoio}{enunciado}\n{alternativas}</questao>"


def test_permutacao_tira_o_gabarito_da_letra_original_e_e_deterministica():
    for rep in range(1, 30):
        montado = prompts.montar(TEMPLATE, QUESTAO, embaralhar=True, repeticao=rep)
        assert montado.permutacao["B"] != "B"
        exibida_correta = next(l for l, orig in montado.permutacao.items() if orig == "B")
        assert f"({exibida_correta}) 2" in montado.texto
        assert prompts.montar(TEMPLATE, QUESTAO, True, rep) == montado


def test_destaques_vao_para_o_prompt_na_forma_explicada():
    com = {**QUESTAO, "enunciado": "O termo ⟦u⟧não⟦/u⟧ é advérbio?"}
    texto = prompts.montar(TEMPLATE, com, False, 1).texto
    assert "[sublinhado]não[/sublinhado]" in texto and "destaques da prova" in texto
    assert "destaques" not in prompts.montar(TEMPLATE, QUESTAO, False, 1).texto


def test_probabilidade_interna_da_letra():
    tokens = [{"token": '{"justificativa": "x", "resposta": "', "top_logprobs": []},
              {"token": "C", "top_logprobs": [{"token": "C", "logprob": -0.1}, {"token": "B", "logprob": -2.5}]},
              {"token": '"}', "top_logprobs": []}]
    probs = probabilidades_da_letra(tokens)
    assert probs["C"] == pytest.approx(0.905, abs=0.01) and probs["B"] < 0.1


def test_chaves_sao_ocultadas_em_mensagens_de_erro():
    assert "sk-proj" not in ocultar_chaves("Incorrect API key provided: sk-proj-abcdef123456")
    assert "gsk_" not in ocultar_chaves("invalid gsk_abcdefghijk")


def test_chave_confianca_com_cedilha_e_maiuscula_e_aceita():
    leitura = avaliador.interpretar('{"Justificativa": "x", "Resposta": "CERTO", "confiança": 95}', "CE", "stop")
    assert leitura["confianca"] == 95 and leitura["resposta_exibida"] == "C"
