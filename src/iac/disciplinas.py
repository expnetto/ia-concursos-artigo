"""Taxonomia de disciplinas comum a todas as bancas.

Primeiro classificamos pelo título de seção do caderno, que é grátis e auditável. Seções genéricas
("Conhecimentos Específicos") ficam sem disciplina e vão para a classificação por LLM.

As regras casam PALAVRAS INTEIRAS (`\\b`): sem isso, "tecnoLÓGICA" virava lógica,
"ciberNÉTICA" virava ética e "SUStentabilidade" virava saúde (achado da revisão de 15/10).
A ordem importa: a primeira regra que casar vence, então as específicas vêm antes das gerais.
A tabela final seção → disciplina é revisada à mão (`uv run iac secoes`).
"""

from __future__ import annotations

import re

from iac import texto as tx

# slug -> macroarea (macroárea é o nível usado nas análises de calibração por área)
DISCIPLINAS: dict[str, str] = {
    "lingua_portuguesa": "portugues", "redacao_oficial": "portugues",
    "lingua_inglesa": "linguas", "lingua_espanhola": "linguas",
    "matematica_rlm": "matematica", "matematica_financeira": "matematica", "estatistica": "matematica",
    "direito_constitucional": "direito", "direito_administrativo": "direito", "direito_penal": "direito",
    "direito_processual_penal": "direito", "direito_civil": "direito", "direito_processual_civil": "direito",
    "direito_tributario": "direito", "direito_trabalho": "direito", "direito_processual_trabalho": "direito",
    "direito_previdenciario": "direito", "direito_empresarial": "direito", "direito_ambiental": "direito",
    "direito_eleitoral": "direito", "direitos_humanos": "direito", "direito_outros": "direito",
    "legislacao_especifica": "direito", "etica_servico_publico": "direito",
    "informatica": "ti", "tecnologia_informacao": "ti",
    "administracao": "gestao", "administracao_publica": "gestao", "gestao_pessoas": "gestao",
    "arquivologia": "gestao", "contabilidade": "gestao", "auditoria_controle": "gestao",
    "economia_financas": "gestao", "afo": "gestao",
    "atualidades": "outras", "historia_geografia": "outras", "saude": "outras", "educacao": "outras",
    "engenharia_exatas": "outras", "seguranca_publica": "outras", "outras": "outras",
}

_REGRAS: list[tuple[str, str]] = [
    (r"redacao oficial", "redacao_oficial"),
    (r"portugues[ae]?|lingua patria|interpretacao de textos?", "lingua_portuguesa"),
    (r"ingles[ae]?|english", "lingua_inglesa"),
    (r"espanhol[ae]?", "lingua_espanhola"),
    (r"matematica financeira", "matematica_financeira"),
    (r"estatistica", "estatistica"),
    (r"raciocinio|matematica|logica|logico", "matematica_rlm"),
    (r"processual penal|processo penal", "direito_processual_penal"),
    (r"processual civil|processo civil", "direito_processual_civil"),
    (r"processual do trabalho|processo do trabalho", "direito_processual_trabalho"),
    (r"constitucional", "direito_constitucional"),
    (r"direito administrativo|administrativo", "direito_administrativo"),
    (r"direito penal|penal", "direito_penal"),
    (r"direito civil|codigo civil", "direito_civil"),
    (r"tributario|tributaria", "direito_tributario"),
    (r"direito do trabalho|trabalhista", "direito_trabalho"),
    (r"previdenciario|previdenciaria|seguridade social", "direito_previdenciario"),
    (r"empresarial|comercial", "direito_empresarial"),
    (r"ambiental", "direito_ambiental"),
    (r"eleitoral", "direito_eleitoral"),
    (r"direitos humanos", "direitos_humanos"),
    (r"etica", "etica_servico_publico"),
    (r"legislacao|lei organica|estatuto|regimento interno|protecao de dados|lgpd", "legislacao_especifica"),
    (r"direito|juridic[ao]s?", "direito_outros"),
    (r"nocoes de informatica|informatica basica|informatica", "informatica"),
    (r"tecnologia da informacao|desenvolvimento de (software|sistemas)|banco de dados|redes de computadores|"
     r"seguranca da informacao|seguranca cibernetica|engenharia de software|infraestrutura|ciencia de dados|"
     r"governanca de ti|sistemas de informacao|computacao|sistemas operacionais|arquitetura de computadores|"
     r"arquitetura de (sistemas|software)|redes|programacao|algoritmos|nuvem|devops|inteligencia artificial",
     "tecnologia_informacao"),
    (r"administracao financeira e orcamentaria|afo|orcamento publico", "afo"),
    (r"contabilidade|contabil|custos", "contabilidade"),
    (r"auditoria|controle externo|controle interno", "auditoria_controle"),
    (r"economia|financas publicas|financas", "economia_financas"),
    (r"arquivologia|gestao documental", "arquivologia"),
    (r"gestao de pessoas|recursos humanos", "gestao_pessoas"),
    (r"administracao publica|gestao publica", "administracao_publica"),
    (r"administracao|gestao", "administracao"),
    (r"atualidades|realidade (brasileira|regional|local)|conhecimentos gerais sobre", "atualidades"),
    (r"historia|geografia|conhecimentos regionais|conhecimentos sobre o (estado|municipio)", "historia_geografia"),
    (r"saude|sus|enfermagem|medicina|farmacia|odontologia|nutricao", "saude"),
    (r"educacao|pedagogi[ac]|didatica", "educacao"),
    (r"engenharia(?! de software)|fisica|quimica|arquitetura(?! de (sistemas|software|computadores))",
     "engenharia_exatas"),
    (r"seguranca publica|criminologia", "seguranca_publica"),
]
_REGRAS_COMPILADAS = [(re.compile(rf"\b(?:{padrao})\b"), slug) for padrao, slug in _REGRAS]

_GENERICAS = re.compile(r"^(conhecimentos? (especificos?|gerais?|basicos?|complementares?)|"
                        r"prova (objetiva|de conhecimentos)|modulo [ivx\d]+|bloco [ivx\d]+|parte [ivx\d]+)\b")
_NAO_TITULO = re.compile(r"\b(text|texto|select|from|where|group by|foreign|table|figura|questao|questoes)\b")


def disciplina_da_secao(secao: str | None) -> str | None:
    if not secao:
        return None
    norm = tx.chave_comparacao(secao)
    if _GENERICAS.match(norm):
        return None
    achadas = [slug for padrao, slug in _REGRAS_COMPILADAS if padrao.search(norm)]
    if not achadas:
        return None
    # seção que junta disciplinas de macroáreas diferentes ("Arquitetura de Computadores e Redes",
    # "Legislação Especial e Ética") não decide sozinha: a questão vai para classificação individual
    if len({DISCIPLINAS[s] for s in achadas}) > 1:
        return None
    return achadas[0]


def eh_titulo_de_secao(linha: str) -> bool:
    """'Língua Portuguesa', 'CONHECIMENTOS ESPECÍFICOS', 'Noções de Direito Administrativo'.

    Exige forma de título (curto, sem pontuação final, palavras capitalizadas) E conteúdo de
    título (uma disciplina reconhecida ou um cabeçalho genérico de prova). A segunda condição é
    o que impede que "GROUP BY ano" ou "Marketing Manager" virem seção.
    """
    limpa = linha.strip().strip("*_").strip()
    if not (3 <= len(limpa) <= 110) or limpa[-1] in ".:;,?!\"”" or "[[" in limpa:
        return False
    if re.match(r"^\(?[A-Ea-e]\)", limpa) or re.search(r"\d", limpa) or not limpa[0].isupper():
        return False
    palavras = [p for p in re.split(r"[\s\-–/]+", limpa) if len(p) > 3]
    if not palavras or sum(p[0].isupper() for p in palavras) / len(palavras) < 0.75:
        return False
    norm = tx.chave_comparacao(limpa)
    if _NAO_TITULO.search(norm):
        return False
    return bool(_GENERICAS.match(norm)) or disciplina_da_secao(limpa) is not None


def popular(conexao) -> None:
    conexao.executemany("INSERT OR IGNORE INTO disciplina (slug, macroarea) VALUES (?, ?)", DISCIPLINAS.items())
