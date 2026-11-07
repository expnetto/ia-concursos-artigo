"""Turma simulada da medida B (docs/PLANO-CONCLUSAO.md, item 3; adendo §6, esclarecimentos da medida B).

Procedimento de Acquaye, Huang, Carpuat e Rudinger (Findings of ACL 2026, "Take Out Your Calculators"), adaptado ao
plano: um modelo faz o papel de 30 alunos, cada um definido SÓ pela nota na área do ENEM (sem nome, sem dado
demográfico, sem histórico de respostas reais). A nota do aluno é a da repetição: repetição r (1..30) = aluno r, com
nota `NOTAS[r - 1]`, 30 notas igualmente espaçadas de 400 a 850 (arredondadas ao inteiro).

O template da condição tem duas partes separadas por `MARCA_USUARIO`: antes dela, a mensagem de sistema (o papel do
aluno, como o prompt A.3 do artigo); depois, a mensagem do usuário (a questão). Os campos `{nota}` e `{area_nome}`
vêm daqui; o resto do template é preenchido por `prompts.montar` como nas demais condições.
"""

from __future__ import annotations

import sqlite3

from iac import enem
from iac.prompts import MARCA_USUARIO  # noqa: F401 — reexportado: a marca faz parte do desenho da condição

N_ALUNOS = 30
NOTA_MIN, NOTA_MAX = 400, 850
NOTAS = tuple(int(round(NOTA_MIN + k * (NOTA_MAX - NOTA_MIN) / (N_ALUNOS - 1))) for k in range(N_ALUNOS))
CAMPO_NOTA = "{nota}"
NOME_DA_AREA = {"LC": "Linguagens, Códigos e suas Tecnologias", "CH": "Ciências Humanas e suas Tecnologias",
                "MT": "Matemática e suas Tecnologias"}
AREA_DA_DISCIPLINA = {d: area for area, ds in enem.DISCIPLINAS_DA_AREA.items() for d in ds}


def usa_perfil(template: str) -> bool:
    """A condição é de turma simulada quando o template pede a nota do aluno."""
    return CAMPO_NOTA in template


def nota_da_repeticao(repeticao: int) -> int:
    if not 1 <= repeticao <= N_ALUNOS:
        raise ValueError(f"turma simulada: repetição {repeticao} fora de 1..{N_ALUNOS}")
    return NOTAS[repeticao - 1]


def area_da_questao(conexao: sqlite3.Connection, questao_id: int) -> str:
    """Área do ENEM da questão: a do INEP nas pontes; a da disciplina nas questões de concurso."""
    tem_tabela = conexao.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_param_inep'").fetchone()
    linha = tem_tabela and conexao.execute(
        "SELECT area FROM item_param_inep WHERE questao_id = ?", (questao_id,)).fetchone()
    if linha and linha[0] in NOME_DA_AREA:
        return linha[0]
    linha = conexao.execute("SELECT disciplina FROM questao WHERE id = ?", (questao_id,)).fetchone()
    area = AREA_DA_DISCIPLINA.get(linha[0]) if linha else None
    if area is None:
        raise ValueError(f"turma simulada: questão {questao_id} fora das áreas LC, CH e MT")
    return area


def perfil(conexao: sqlite3.Connection, questao_id: int, repeticao: int) -> dict:
    """Campos do aluno simulado: nota (pelo número da repetição) e área (pela questão)."""
    area = area_da_questao(conexao, questao_id)
    return {"aluno": repeticao, "nota": nota_da_repeticao(repeticao), "area": area, "area_nome": NOME_DA_AREA[area]}
