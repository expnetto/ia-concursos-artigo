"""Executor de ponta a ponta com banco temporário e fornecedor falso (sem rede)."""

import dataclasses
import json

import pytest

from iac import amostragem, avaliador, catalogo, executor
from iac import db as dbm
from iac.catalogo import Via
from iac.disciplinas import popular
from iac.provedores import CotaEsgotada, Resposta

VIA_LOCAL = Via("ollama", "http://localhost:11434/v1", "", 0, 0, True, False)


@pytest.fixture
def banco(tmp_path, monkeypatch):
    conexao = dbm.conectar(tmp_path / "t.sqlite")
    popular(conexao)
    conexao.execute("INSERT INTO banca (sigla, nome) VALUES ('fgv', 'FGV')")
    conexao.execute("INSERT INTO concurso (banca_id, slug, ano_edital, data_prova) VALUES (1, 'x24', 2024, '2024-05-12')")
    conexao.execute("INSERT INTO documento (concurso_id, url, nome_arquivo, tipo) VALUES (1, 'u', 'p.pdf', 'prova')")
    conexao.execute("""INSERT INTO caderno (documento_id, concurso_id, status, versao_extrator, formato, n_itens)
                       VALUES (1, 1, 'ok', 't', 'ME5', 5)""")
    for n in range(1, 6):
        alternativas = json.dumps({l: f"opção {l}{n}" for l in "ABCDE"})
        conexao.execute("""INSERT INTO item (caderno_id, numero, enunciado, alternativas_json, hash_conteudo,
                           gabarito_preliminar) VALUES (1, ?, ?, ?, ?, 'C')""", (n, f"Questão {n}?", alternativas, f"h{n}"))
        conexao.execute("""INSERT INTO questao (hash_conteudo, item_canonico_id, formato, gabarito, elegivel, disciplina,
                           disciplina_fonte) VALUES (?, ?, 'ME5', 'C', 1, 'lingua_portuguesa', 'secao')""", (f"h{n}", n))
    conexao.commit()
    catalogo.sincronizar(conexao)
    amostragem.criar(conexao, "t", 5, 1)
    return conexao


class Falso:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.corpos = []

    def __call__(self, via):
        return self

    def completar(self, corpo):
        self.corpos.append(corpo)
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Resposta):
            return item
        return Resposta(texto=item, modelo_retornado="falso", tokens_in=500, tokens_out=20, latencia_ms=5,
                        finish_reason="stop")

    def fechar(self):
        pass


def _rodar(banco, modelo="llama3.1-8b", condicao="me_padrao"):
    return executor.executar(banco, "t", [modelo], condicao, log=lambda *_: None)


def test_executa_avalia_e_e_idempotente(banco, monkeypatch):
    falso = Falso(['{"justificativa": "x", "resposta": "C", "confianca": 90}'] * 3 +
                  ['{"justificativa": "y", "resposta": "A", "confianca": 95}'] * 2)
    monkeypatch.setattr(executor, "Cliente", falso)
    assert _rodar(banco) == {"ok": 5, "erro": 0}
    assert _rodar(banco) == {"ok": 0, "erro": 0}
    avaliador.avaliar_pendentes(banco)
    assert tuple(banco.execute("SELECT SUM(acertou), COUNT(*) FROM avaliacao").fetchone()) == (3, 5)
    assert "Authorization" not in json.dumps(falso.corpos)
    assert all("response_format" not in c for c in falso.corpos)          # política única: sem JSON mode
    linha = banco.execute("SELECT * FROM v_resultado LIMIT 1").fetchone()
    assert linha["pos_corte"] is not None and linha["macroarea"] == "portugues"


def test_permutada_traduz_letra_para_ordem_original(banco, monkeypatch):
    falso = Falso([])
    def responde_correta(corpo):
        texto = corpo["messages"][1]["content"]
        letra = next(l for l in "ABCDE" if f"({l}) opção C" in texto)
        return Resposta(texto=json.dumps({"justificativa": "z", "resposta": letra, "confianca": 50}),
                        modelo_retornado=None, tokens_in=500, tokens_out=1, latencia_ms=1, finish_reason="stop")
    falso.completar = responde_correta
    monkeypatch.setattr(executor, "Cliente", falso)
    _rodar(banco, condicao="me_permutada")
    avaliador.avaliar_pendentes(banco)
    linhas = banco.execute("SELECT resposta_exibida, resposta, acertou FROM avaliacao").fetchall()
    assert len(linhas) == 5
    assert all(l["resposta"] == "C" and l["acertou"] == 1 and l["resposta_exibida"] != "C" for l in linhas)


def _via_paga(teto):
    return {"openai": Via("openai", "https://x", "", 0, 0, True, True, teto)}


def test_teto_de_gasto_reserva_antes_e_impede_chamada(banco, monkeypatch):
    falso = Falso(['{"resposta": "C"}'] * 5)
    monkeypatch.setattr(executor, "Cliente", falso)
    monkeypatch.setattr(catalogo, "vias", lambda: _via_paga(0.01))
    _rodar(banco, modelo="gpt-4-0613")
    assert falso.corpos == []    # o pior caso de UMA chamada já passa do teto: nenhuma requisição sai


def test_uso_ausente_em_via_paga_cobra_o_pior_caso(banco, monkeypatch):
    sem_uso = Resposta(texto='{"resposta": "C"}', modelo_retornado=None, tokens_in=None, tokens_out=None,
                       latencia_ms=1, finish_reason="stop")
    falso = Falso([sem_uso] * 5)
    monkeypatch.setattr(executor, "Cliente", falso)
    monkeypatch.setattr(catalogo, "vias", lambda: _via_paga(100))
    _rodar(banco, modelo="gpt-4-0613")
    custos = [r[0] for r in banco.execute("SELECT custo_usd FROM execucao")]
    assert len(custos) == 5 and all(c > 0.01 for c in custos)


def test_erro_em_via_paga_mantem_a_reserva(banco, monkeypatch):
    falso = Falso([RuntimeError("timeout depois do envio")] * 5)
    monkeypatch.setattr(executor, "Cliente", falso)
    monkeypatch.setattr(catalogo, "vias", lambda: _via_paga(100))
    _rodar(banco, modelo="gpt-4-0613")
    assert banco.execute("SELECT MIN(custo_usd) FROM execucao").fetchone()[0] > 0


def test_cota_esgotada_para(banco, monkeypatch):
    falso = Falso([CotaEsgotada("daily")])
    monkeypatch.setattr(executor, "Cliente", falso)
    _rodar(banco)
    assert banco.execute("SELECT COUNT(*) FROM execucao WHERE status = 'ok'").fetchone()[0] == 0
    assert len(falso.corpos) == 1


def test_erros_seguidos_interrompem_e_sao_refeitos_depois(banco, monkeypatch):
    monkeypatch.setattr(executor, "Cliente", Falso([RuntimeError("404 model not found")] * 5))
    assert _rodar(banco) == {"ok": 0, "erro": 5}
    monkeypatch.setattr(executor, "Cliente", Falso(['{"resposta": "C"}'] * 5))
    assert _rodar(banco)["ok"] == 5


def test_prompt_truncado_pela_via_vira_erro(banco, monkeypatch):
    cortado = Resposta(texto='{"resposta": "C"}', modelo_retornado=None, tokens_in=10, tokens_out=5,
                       latencia_ms=1, finish_reason="stop")
    monkeypatch.setattr(executor, "Cliente", Falso([cortado] * 5))
    assert _rodar(banco) == {"ok": 0, "erro": 5}


def test_condicao_executada_nao_pode_mudar(banco, monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "Cliente", Falso(['{"resposta": "C"}'] * 5))
    _rodar(banco)
    from iac import config
    dados = open(config.CATALOGO_CONDICOES, encoding="utf-8").read().replace("temperatura = 0.0", "temperatura = 0.1", 1)
    alterado = tmp_path / "c.toml"
    alterado.write_text(dados, encoding="utf-8")
    with pytest.raises(ValueError, match="já tem execuções"):
        catalogo.sincronizar(banco, condicoes=alterado)


def test_reserva_nao_reabre_execucao_concluida(banco, monkeypatch):
    # revisão 2, MÉDIO-2: dois processos calculam os pendentes antes de o outro gravar
    monkeypatch.setattr(executor, "Cliente", Falso(['{"resposta": "C"}'] * 5))
    _rodar(banco)
    avaliador.avaliar_pendentes(banco)
    pendentes_velhos = [(q, 1) for q in range(1, 6)]
    monkeypatch.setattr(executor, "_pendentes", lambda *a: iter(pendentes_velhos))
    falso = Falso(['{"resposta": "A"}'] * 5)
    monkeypatch.setattr(executor, "Cliente", falso)
    assert _rodar(banco) == {"ok": 0, "erro": 0, "em_andamento": 5}
    assert falso.corpos == []
    assert banco.execute("SELECT COUNT(*) FROM execucao WHERE status = 'ok'").fetchone()[0] == 5
    assert banco.execute("SELECT COUNT(*) FROM avaliacao").fetchone()[0] == 5


def test_reserva_recente_de_outro_processo_e_pulada_e_antiga_e_retomada(banco, monkeypatch):
    monkeypatch.setattr(executor, "Cliente", Falso([RuntimeError("x")] * 2))
    executor.executar(banco, "t", ["llama3.1-8b"], "me_padrao", limite=2, log=lambda *_: None)
    banco.execute("UPDATE execucao SET status = 'reservada', criada_em = ? WHERE questao_id = 1", (executor.dbm.agora(),))
    banco.execute("UPDATE execucao SET status = 'reservada', criada_em = '2026-01-01T00:00:00+00:00' WHERE questao_id = 2")
    banco.commit()
    falso = Falso(['{"resposta": "C"}'] * 5)
    monkeypatch.setattr(executor, "Cliente", falso)
    assert _rodar(banco) == {"ok": 4, "erro": 0, "em_andamento": 1}   # a 1 está noutro processo; a 2 foi abandonada
    status = dict(banco.execute("SELECT questao_id, status FROM execucao"))
    assert status[1] == "reservada" and status[2] == "ok"


def test_teto_diario_conta_tentativas_http_reais(banco, monkeypatch):
    # revisão 2, MÉDIO-3: cada chamada que falhou gastou 3 requisições HTTP da cota diária, não 1
    class ComRetentativas(Falso):
        def completar(self, corpo):
            self.corpos.append(corpo)
            self.tentativas_ultima_chamada = 3
            raise RuntimeError("HTTP 503")
    falso = ComRetentativas([])
    monkeypatch.setattr(executor, "Cliente", falso)
    monkeypatch.setattr(catalogo, "vias", lambda: {"ollama": dataclasses.replace(VIA_LOCAL, rpd=5)})
    _rodar(banco)
    assert len(falso.corpos) == 2                      # 0+3 < 5, 3+3 >= 5: a terceira não sai
    assert banco.execute("SELECT SUM(tentativas) FROM execucao").fetchone()[0] == 6


def test_chave_ausente_pula_o_modelo_e_segue_para_o_proximo(banco, monkeypatch):
    from iac.provedores import ErroDefinitivo
    chamados = []

    def fabrica(via):
        chamados.append(via.nome)
        if len(chamados) == 1:
            raise ErroDefinitivo("variável OPENAI_API_KEY não definida")
        return Falso(['{"resposta": "C"}'] * 5)
    monkeypatch.setattr(executor, "Cliente", fabrica)
    totais = executor.executar(banco, "t", ["llama3.1-8b", "llama3.1-8b"], "me_padrao", log=lambda *_: None)
    assert totais["ok"] == 5 and len(chamados) == 2


def test_avaliacao_registra_o_gabarito_e_e_refeita_se_ele_mudar(banco, monkeypatch):
    monkeypatch.setattr(executor, "Cliente", Falso(['{"resposta": "C"}'] * 5))
    _rodar(banco)
    avaliador.avaliar_pendentes(banco)
    assert banco.execute("SELECT SUM(acertou) FROM avaliacao").fetchone()[0] == 5
    banco.execute("UPDATE questao SET gabarito = 'A' WHERE id = 1")      # correção manual do gabarito
    banco.commit()
    assert avaliador.avaliar_pendentes(banco)["avaliadas"] == 1
    linha = banco.execute("SELECT acertou, gabarito_usado FROM avaliacao WHERE execucao_id = "
                          "(SELECT id FROM execucao WHERE questao_id = 1)").fetchone()
    assert tuple(linha) == (0, "A")


def test_teto_global_ausente_com_via_paga_e_erro(tmp_path):
    from iac import config
    dados = open(config.CATALOGO_MODELOS, encoding="utf-8").read().replace("teto_global_usd = 30.0", "teto_global_usd = 0")
    alterado = tmp_path / "m.toml"
    alterado.write_text(dados, encoding="utf-8")
    with pytest.raises(ValueError, match="teto_global_usd"):
        catalogo.teto_global(alterado)
    assert catalogo.teto_global() > 0



def test_conclusao_de_reserva_retomada_por_outro_processo_e_descartada(banco, monkeypatch):
    # revisão 3, M5: a chamada demorou mais que o prazo de abandono e outro processo retomou a chave
    class Lento(Falso):
        def completar(self, corpo):
            banco.execute("UPDATE execucao SET criada_em = '2099-01-01T00:00:00+00:00' WHERE status = 'reservada'")
            banco.commit()                               # outro processo "retomou" a reserva
            return super().completar(corpo)
    monkeypatch.setattr(executor, "Cliente", Lento(['{"resposta": "C"}'] * 5))
    assert _rodar(banco) == {"ok": 0, "erro": 0, "descartadas": 5}
    assert banco.execute("SELECT COUNT(*) FROM execucao WHERE status = 'reservada'").fetchone()[0] == 5


def test_sincronizar_nao_apaga_versao_registrada_pelo_ollama(banco, monkeypatch):
    # 15/10: `iac modelos` recusava sincronizar porque o catálogo (sem versão) "mudava" a versão do Ollama
    monkeypatch.setattr(executor, "Cliente", Falso(['{"resposta": "C"}'] * 5))
    _rodar(banco)
    banco.execute("UPDATE modelo SET versao = 'llama3.1:8b@abc', quantizacao = 'Q4_K_M' WHERE slug = 'llama3.1-8b'")
    banco.commit()
    catalogo.sincronizar(banco)
    assert tuple(banco.execute("SELECT versao, quantizacao FROM modelo WHERE slug = 'llama3.1-8b'").fetchone()) == \
        ("llama3.1:8b@abc", "Q4_K_M")


def test_amostra_por_formato_e_so_classificadas(banco):
    banco.execute("UPDATE questao SET disciplina = NULL WHERE id = 5")
    banco.commit()
    r = amostragem.criar(banco, "so_class", 10, 1, formato="ME", so_classificadas=True)
    ids = {x[0] for x in banco.execute(
        "SELECT questao_id FROM amostra_questao aq JOIN amostra a ON a.id = aq.amostra_id WHERE a.nome = 'so_class'")}
    assert r["questoes"] == 4 and 5 not in ids
    with pytest.raises(ValueError):
        amostragem.criar(banco, "ce_vazia", 10, 1, formato="CE")


def test_via_que_recusa_seed_nao_recebe_seed():
    via = dataclasses.replace(VIA_LOCAL, aceita_seed=False)
    corpo = executor._corpo({"nome_api": "m", "raciocinio": 0}, via,
                            {"temperatura": 0, "json_mode": 0}, "prompt")
    assert "seed" not in corpo
    assert "seed" in executor._corpo({"nome_api": "m", "raciocinio": 0}, VIA_LOCAL, {"temperatura": 0, "json_mode": 0}, "p")


def test_contexto_marca_so_o_que_encostou_no_limite_e_executor_refaz(banco, monkeypatch):
    from iac import contexto
    curtas = ['{"justificativa": "x", "resposta": "C", "confianca": 90}'] * 4
    longa = Resposta(texto='{"justificativa": "x", "resposta": "C", "confianca": 90}', modelo_retornado="falso",
                     tokens_in=3900, tokens_out=100, latencia_ms=5, finish_reason="stop")
    monkeypatch.setattr(executor, "Cliente", Falso(curtas[:2] + [longa] + curtas[2:]))
    desde = dbm.agora()
    assert _rodar(banco) == {"ok": 5, "erro": 0}
    assert contexto.insuficientes(banco, "llama3.1-8b", desde, 4096) == [3]
    assert contexto.insuficientes(banco, "llama3.1-8b", "2999-01-01T00:00:00+00:00", 4096) == []   # fora da janela
    assert contexto.marcar_insuficientes(banco, "llama3.1-8b", desde, 4096) == 1
    assert banco.execute("SELECT status FROM execucao WHERE id = 3").fetchone()[0] == "erro"
    assert banco.execute("SELECT COUNT(*) FROM auditoria WHERE evento = 'contexto_insuficiente'").fetchone()[0] == 1
    refazer = Falso(curtas[:1])
    monkeypatch.setattr(executor, "Cliente", refazer)
    assert _rodar(banco) == {"ok": 1, "erro": 0}                 # só a marcada é refeita
    assert len(refazer.corpos) == 1


def test_equivalencia_compara_letra_e_texto_sem_gravar(banco, monkeypatch):
    from iac import contexto
    resposta = '{"justificativa": "x", "resposta": "C", "confianca": 90}'
    monkeypatch.setattr(executor, "Cliente", Falso([resposta] * 5))
    _rodar(banco)
    monkeypatch.setattr(contexto, "Cliente", Falso([resposta] * 2 + ['{"justificativa": "y", "resposta": "A"}']))
    antes = banco.execute("SELECT COUNT(*), MAX(criada_em) FROM execucao").fetchone()
    r = contexto.equivalencia(banco, "llama3.1-8b", "me_padrao", "t", 3, log=lambda *_: None)
    assert (r["n"], r["texto_igual"], r["letra_igual"], len(r["divergentes"])) == (3, 2, 2, 1)
    assert r["divergentes"][0]["antes"] == "C" and r["divergentes"][0]["depois"] == "A"
    assert tuple(banco.execute("SELECT COUNT(*), MAX(criada_em) FROM execucao").fetchone()) == tuple(antes)
