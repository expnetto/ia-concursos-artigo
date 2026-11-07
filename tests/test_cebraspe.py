"""Cebraspe: segmentação C/E e múltipla escolha com o layout real das provas 2024–2026."""

from iac import extracao
from iac.bancas import cebraspe as cb
from iac.coleta.cebraspe import classificar


def _ce(inicio: int, n: int = 12) -> str:
    linhas = ["-- CONHECIMENTOS ESPECÍFICOS –", "Julgue os itens que se seguem, relativos à contabilidade geral no", "Brasil."]
    for k in range(inicio, inicio + n):
        linhas += [f"{k} Afirmação de número {k} sobre contabilidade, com texto", "suficiente para ser um item."]
    return "\n".join(linhas) + "\n"


def test_ce_comecando_em_51():
    # cadernos de "Conhecimentos Específicos" continuam a numeração do de "Básicos"
    resultado = cb.segmentar(_ce(51))
    assert [i.numero for i in resultado.itens] == list(range(51, 63))
    assert resultado.status == "ok"


def test_ce_contador_de_margem_nao_vira_item():
    # revisão 2, MÉDIO-1: "5" sozinho na margem do texto de apoio não é o item 5
    texto = _ce(1, 4) + "\n".join([
        "Texto 1A1-II", "Um texto de apoio longo que a banca numera de cinco em cinco linhas.",
        "5", "Continua o texto de apoio com mais uma linha de conteúdo.",
        "Julgue os itens a seguir, relativos ao texto 1A1-II.",
        "5 O autor defende a tese central do texto com argumentos.",
        "6 O texto apresenta linguagem predominantemente técnica.",
    ] + [f"{k} Afirmação {k} sobre o texto, com conteúdo suficiente." for k in range(7, 13)]) + "\n"
    itens = {i.numero: i for i in cb.segmentar(texto).itens}
    assert "Texto 1A1-II" not in itens[4].enunciado
    assert itens[5].enunciado.endswith("O autor defende a tese central do texto com argumentos.")


def _me(inicio: int, n: int = 6) -> str:
    partes = ["CONHECIMENTOS GERAIS"]
    for k in range(inicio, inicio + n):
        partes += [f"Questão {k}", "[[IMAGEM]]", "<PGE_RN196CG1A01E4497>",
                   f"A Lei citada no enunciado da questão {k} determina que",
                   "A a primeira opção, com texto que", "continua na linha seguinte.",
                   "B a segunda opção.", "C a terceira opção.", "D a quarta opção.",
                   "E a quinta opção, que termina aqui."]
    return "\n".join(partes) + "\n"


def test_me_com_letra_e_texto_na_mesma_linha():
    resultado = cb.segmentar(_me(1))
    assert [i.numero for i in resultado.itens] == list(range(1, 7))
    q = resultado.itens[0]
    assert q.enunciado.startswith("A Lei citada")             # "A Lei" no enunciado não é a alternativa A
    assert q.alternativas["A"] == "a primeira opção, com texto que continua na linha seguinte."
    assert q.alternativas["E"] == "a quinta opção, que termina aqui."


def test_me_icone_e_codigo_da_questao_nao_contam_como_imagem():
    q = cb.segmentar(_me(1)).itens[0]
    assert extracao.P_IMAGEM not in q.problemas
    assert "<PGE_RN196" not in q.enunciado


def test_me_comecando_em_31():
    assert [i.numero for i in cb.segmentar(_me(31)).itens] == list(range(31, 37))


def test_classificar_descricoes_reais():
    assert classificar("GABARITO DEFINITIVO – PROVA OBJETIVA – CARGO 5") == "gabarito_definitivo"
    assert classificar("Gabarito Retificado - Prova Objetiva") == "gabarito_retificado"
    assert classificar("PROVA OBJETIVA – ÁREA CORPORAL") == "prova"
    assert classificar("Gabarito definitivo – Prova objetiva e discursiva") == "gabarito_definitivo"
    assert classificar("PROVA DISCURSIVA") is None
    assert classificar("PROVA OBJETIVA PARA HABILITAÇÃO 34: LIBRAS – QUESTÃO 1 – OPÇÃO A") == "prova"


def _pagina(inicio: int, letras: str, cabecalho: str = "Aplicação: 13/04/2025") -> str:
    numeros = "\n".join(str(n) for n in range(inicio, inicio + len(letras)))
    return f"{cabecalho}\n{numeros}\n" + "\n".join(letras) + "\n" + "0\n" * 20


def test_gabarito_que_comeca_no_21():
    tabelas = cb.tabelas_das_paginas([_pagina(21, "EBDXDECAED")], "conhecimentos especificos cargo 1", "definitivo")
    assert len(tabelas) == 1 and tabelas[0].respostas == dict(zip(range(21, 31), "EBDXDECAED"))


def test_gabarito_em_duas_paginas_vira_uma_tabela():
    # revisão 2, MÉDIO-10: a tabela que continua na página seguinte não pode sumir
    paginas = [_pagina(1, "CECECECECE"), _pagina(11, "EEEEECCCCC", cabecalho="")]
    tabelas = cb.tabelas_das_paginas(paginas, "cargo 1", "definitivo")
    assert len(tabelas) == 1 and sorted(tabelas[0].respostas) == list(range(1, 21))


def test_gabarito_com_numeros_fora_de_sequencia_e_descartado():
    assert cb._grade("1\n2\n4\nC\nE\nC\n") == {}


def test_gabarito_item_e_resposta_juntos():
    pagina = ("GABARITO DEFINITIVO\n<<N. Cargo/>>\n1\nCARGO 1: ASSISTENTE DE PROJETO\n"
              "Julgue o item a seguir.\n1\nB0702343 - ERRADO | A gestão de custos ocorre somente após.\n"
              "Julgue o item a seguir.\n2\nB0702344 - CORRETO | Marcos de fase são pontos de controle.\n"
              "3\nB0702345 - ANULADO | Item anulado pela banca.\n")
    tabelas = cb.tabelas_das_paginas([pagina], "conhecimentos especificos", "definitivo")
    assert len(tabelas) == 1 and tabelas[0].respostas == {1: "E", 2: "C", 3: "X"}


class _ApiFalsa:
    def __init__(self, detalhes):
        self.detalhes = detalhes

    def obter_json(self, url):
        if url.endswith("/tipo/concursos/"):
            return [{"eventos": [{"eventoAno": 2024, "eventoURL": k} for k in self.detalhes]
                     + [{"eventoAno": 2015, "eventoURL": "VELHO_15"}, {"eventoAno": 2999, "eventoURL": "X_99"},
                        {"eventoAno": 2024}, "lixo"]}]
        resposta = self.detalhes[url.rsplit("/", 1)[1]]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def test_descobrir_aguenta_json_malformado_e_nome_perigoso(tmp_path):
    from iac import db as dbm
    from iac.coleta import cebraspe as coleta
    banco = dbm.conectar(tmp_path / "t.sqlite")
    api = _ApiFalsa({
        "BOM_24": {"eventoNomeCompleto": "Órgão", "arquivosGabarito": [
            {"nomeArquivo": "ABC.PDF", "descricaoArquivo": "PROVA OBJETIVA – CARGO 1"},
            {"nomeArquivo": "../../segredo", "descricaoArquivo": "GABARITO DEFINITIVO – CARGO 1"},
            {"nomeArquivo": 123, "descricaoArquivo": "GABARITO DEFINITIVO – CARGO 2"},
            "não é objeto"]},
        "RUIM_24": ["não", "é", "objeto"],
        "QUEBRA_24": ValueError("JSON inválido"),
    })
    contagem = coleta.descobrir(banco, log=lambda m: None, cliente=api)
    assert contagem["concursos"] == 1 and contagem["documentos"] == 1
    assert contagem["fora_do_periodo"] == 2 and contagem["erros"] >= 3
    urls = [r[0] for r in banco.execute("SELECT url FROM documento")]
    assert urls == ["https://cdn.cebraspe.org.br/concursos/BOM_24/arquivos/ABC.PDF"]


def test_descobrir_corrige_tipo_de_documento_ja_registrado(tmp_path):
    from iac import db as dbm
    from iac.coleta import cebraspe as coleta
    banco = dbm.conectar(tmp_path / "t.sqlite")
    api = _ApiFalsa({"BOM_24": {"arquivosGabarito": [
        {"nomeArquivo": "G.PDF", "descricaoArquivo": "GABARITO DEFINITIVO – PROVA OBJETIVA"}]}})
    coleta.descobrir(banco, log=lambda m: None, cliente=api)
    banco.execute("UPDATE documento SET tipo = 'prova'")       # como o classificador antigo gravava
    banco.commit()
    assert coleta.descobrir(banco, log=lambda m: None, cliente=api)["reclassificados"] == 1
    assert banco.execute("SELECT tipo FROM documento").fetchone()[0] == "gabarito_definitivo"


def test_data_invalida_no_gabarito_vira_nula():
    from iac.bancas.base import data_iso
    assert data_iso("13", "04", "2025") == "2025-04-13"
    assert data_iso("99", "99", "2024") is None


def test_linha_quebrada_da_alternativa_a_com_maiuscula_nao_corta_a_alternativa():
    # revisão 3, A1: "A lei regula..." é continuação da alternativa A, não o começo dela
    texto = _me(1).replace("A a primeira opção, com texto que\ncontinua na linha seguinte.",
                           "A a primeira opção prevê o direito de reunião sem armas,\nA lei regula o exercício.")
    q = cb.segmentar(texto).itens[0]
    assert q.alternativas["A"].startswith("a primeira opção") or extracao.P_ALTERNATIVA_LONGA in q.problemas


def test_rotulo_perdido_no_inicio_vai_para_quarentena():
    # revisão 3, M2: sem o rótulo "Questão 2", a cadeia 3..12 venceria e 1–2 sumiriam sem registro
    texto = _me(1, 12).replace("Questão 2\n", "Questao dois\n")
    resultado = cb.segmentar(texto)
    assert resultado.status == "quarentena" and "buraco" in resultado.motivo


def test_ce_rotulo_perdido_no_inicio_vai_para_quarentena():
    texto = _ce(1, 14).replace("\n2 Afirmação", "\nAfirmação")
    assert cb.segmentar(texto).status == "quarentena"


def _me_artigo(n=12):
    partes = []
    for k in range(1, n + 1):
        partes += [f"Questão {k}", "A respeito do gerenciamento de resíduos em atividades forenses, assinale",
                   "a opção correta.", "A O acondicionamento adequado é obrigatório.", "B A segregação é opcional.",
                   "C O descarte é livre.", "D Não há norma.", "E Nenhuma das anteriores."]
    return "\n".join(partes) + "\n"


def test_enunciado_que_comeca_com_artigo_a_nao_vira_alternativa():
    # 15/10 no banco real: 1.145 questões com enunciado vazio e alternativas deslocadas uma letra
    q = cb.segmentar(_me_artigo()).itens[0]
    assert q.enunciado.startswith("A respeito do gerenciamento")
    assert q.alternativas["A"] == "O acondicionamento adequado é obrigatório."
    assert q.alternativas["B"] == "A segregação é opcional."
    assert extracao.P_ALTERNATIVA_LONGA not in q.problemas


def test_enunciado_vazio_sempre_exclui():
    texto = "\n".join(sum(([f"Questão {k}", "A primeira.", "B segunda.", "C terceira.", "D quarta.", "E quinta."]
                           for k in range(1, 13)), [])) + "\n"
    q = cb.segmentar(texto).itens[0]
    assert extracao.P_ENUNCIADO_VAZIO in q.problemas
