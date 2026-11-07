"""Conjunto de referência: conferência HUMANA da extração e do gabarito contra o PDF original.

Gera uma planilha para o usuário conferir, à mão, uma amostra de questões de cadernos sorteados
(bancas e concursos diferentes), com o PDF de cada caderno ao lado. É a "régua" do extrator: a
taxa de concordância vai para o artigo (validade da medida) e qualquer discordância vira correção.
"""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from iac import pdf
from iac.ingestao import caminho_no_acervo

INSTRUCOES = [
    "CONFERÊNCIA MANUAL — conjunto de referência do extrator",
    "",
    "Para cada linha da aba 'Questões': abra o PDF indicado (pasta 'pdfs'), vá até a questão de número indicado e confira:",
    "  1. TEXTO: o enunciado e as alternativas estão completos e iguais ao PDF? (pequenas diferenças de espaço não importam)",
    "     Os destaques do PDF aparecem como [negrito]...[/negrito], [itálico]...[/itálico], [sublinhado]...[/sublinhado].",
    "  2. GABARITO: abra o PDF do gabarito indicado e confira se a letra é a mesma.",
    "  3. ELEGÍVEL: se o sistema marcou um problema (coluna 'Problemas'), ele faz sentido?",
    "Responda S (sim) ou N (não) nas colunas amarelas. Em caso de N, escreva o que está errado em 'Observação'.",
    "",
    "Na aba 'Cadernos', confira também: o número de questões do caderno bate com o PDF?",
    "Tempo estimado: 1 a 2 horas. Não precisa fazer tudo de uma vez.",
]


def gerar(conexao: sqlite3.Connection, destino: Path, n_cadernos: int = 10, por_caderno: int = 10,
          semente: int = 20261016) -> Path:
    gerador = random.Random(semente)
    cadernos = conexao.execute(
        """SELECT ca.id, ca.n_itens, ca.n_esperado, b.sigla, co.slug, d.sha256, d.nome_arquivo,
                  dp.sha256 AS sha_pre, dp.nome_arquivo AS nome_pre, dd.sha256 AS sha_def, dd.nome_arquivo AS nome_def
           FROM caderno ca JOIN documento d ON d.id = ca.documento_id JOIN concurso co ON co.id = ca.concurso_id
           JOIN banca b ON b.id = co.banca_id
           LEFT JOIN documento dp ON dp.id = ca.gab_prelim_doc LEFT JOIN documento dd ON dd.id = ca.gab_defin_doc
           WHERE ca.status = 'ok' AND ca.gabarito_status = 'casado' ORDER BY ca.id""").fetchall()
    # um caderno por concurso, alternando bancas, para cobrir layouts diferentes
    por_concurso: dict[str, sqlite3.Row] = {}
    for c in gerador.sample(list(cadernos), len(cadernos)):
        por_concurso.setdefault(f"{c['sigla']}|{c['slug']}", c)
    escolhidos = sorted(por_concurso.values(), key=lambda c: (c["sigla"], c["slug"]))
    por_banca: dict[str, list] = {}
    for c in escolhidos:
        por_banca.setdefault(c["sigla"], []).append(c)
    selecao = []
    while len(selecao) < n_cadernos and any(por_banca.values()):
        for lista in por_banca.values():
            if lista and len(selecao) < n_cadernos:
                selecao.append(lista.pop(gerador.randrange(len(lista))))

    pasta_pdfs = destino / "pdfs"
    pasta_pdfs.mkdir(parents=True, exist_ok=True)
    livro = Workbook()
    aba_instr = livro.active
    aba_instr.title = "Instruções"
    for linha in INSTRUCOES:
        aba_instr.append([linha])
    aba_instr["A1"].font = Font(bold=True, size=13)
    aba_instr.column_dimensions["A"].width = 140

    aba_cad = livro.create_sheet("Cadernos")
    aba_cad.append(["caderno_id", "banca", "concurso", "PDF da prova", "PDF gabarito", "questões extraídas",
                    "nº na capa", "Contagem confere? (S/N)", "Observação"])
    aba_q = livro.create_sheet("Questões")
    aba_q.append(["caderno_id", "PDF da prova", "nº da questão", "Enunciado (início)", "Alternativas",
                  "Gabarito do sistema", "Problemas detectados", "Texto confere? (S/N)", "Gabarito confere? (S/N)",
                  "Problema faz sentido? (S/N)", "Observação"])
    for c in selecao:
        nome_prova = f"{c['sigla']}_{c['slug']}_caderno{c['id']}.pdf"
        shutil.copyfile(caminho_no_acervo(c["sha256"]), pasta_pdfs / nome_prova)
        nome_gab = None
        if c["sha_def"] or c["sha_pre"]:
            nome_gab = f"{c['sigla']}_{c['slug']}_gabarito_{'definitivo' if c['sha_def'] else 'preliminar'}_{c['id']}.pdf"
            shutil.copyfile(caminho_no_acervo(c["sha_def"] or c["sha_pre"]), pasta_pdfs / nome_gab)
        aba_cad.append([c["id"], c["sigla"], c["slug"], nome_prova, nome_gab, c["n_itens"], c["n_esperado"], "", ""])
        itens = conexao.execute(
            """SELECT i.id, i.numero, i.enunciado, i.alternativas_json, i.gabarito_preliminar, i.gabarito_definitivo
               FROM item i WHERE i.caderno_id = ? ORDER BY i.numero""", (c["id"],)).fetchall()
        for item in sorted(gerador.sample(list(itens), min(por_caderno, len(itens))), key=lambda i: i["numero"]):
            problemas = [r[0] for r in conexao.execute(
                "SELECT motivo FROM exclusao WHERE entidade = 'item' AND entidade_id = ?", (item["id"],))]
            alternativas = json.loads(item["alternativas_json"]) if item["alternativas_json"] else {}
            texto_alt = "\n".join(f"({l}) {pdf.destaques_para_prompt(t)}" for l, t in sorted(alternativas.items()))
            aba_q.append([c["id"], nome_prova, item["numero"], pdf.destaques_para_prompt(item["enunciado"])[:1500],
                          texto_alt[:3000], item["gabarito_definitivo"] or item["gabarito_preliminar"],
                          ", ".join(problemas), "", "", "", ""])
    _formatar(aba_cad, colunas_resposta=("H",), larguras={"C": 18, "D": 40, "E": 40, "I": 40})
    _formatar(aba_q, colunas_resposta=("H", "I", "J"),
              larguras={"B": 30, "D": 70, "E": 60, "G": 22, "K": 40})
    arquivo = destino / "conferencia_referencia.xlsx"
    livro.save(arquivo)
    return arquivo


def _formatar(aba, colunas_resposta: tuple[str, ...], larguras: dict[str, int]) -> None:
    amarelo = PatternFill("solid", fgColor="FFF2CC")
    for celula in aba[1]:
        celula.font = Font(bold=True)
    validacao = DataValidation(type="list", formula1='"S,N"', allow_blank=True)
    aba.add_data_validation(validacao)
    for coluna in colunas_resposta:
        validacao.add(f"{coluna}2:{coluna}{aba.max_row}")
        for linha in range(2, aba.max_row + 1):
            aba[f"{coluna}{linha}"].fill = amarelo
    for coluna, largura in larguras.items():
        aba.column_dimensions[coluna].width = largura
    for linha in aba.iter_rows(min_row=2):
        for celula in linha:
            celula.alignment = Alignment(wrap_text=True, vertical="top")
    aba.freeze_panes = "A2"
