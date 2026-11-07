"""Etapas de preparo do banco de questões: extrair → gabaritar → consolidar.

Garantias (revisão de 15/10/2026):
- idempotentes; reprocessam o que foi extraído com versão antiga do extrator;
- ids de `questao` estáveis (upsert por hash_conteudo): amostra e execuções nunca ficam órfãs;
- toda exclusão vira linha em `exclusao` (fluxograma do artigo), com TODOS os motivos;
- gabarito só é aceito quando o casamento é inequívoco E passa em duas conferências:
  preliminar × definitivo (só podem divergir no que mudou em recurso) e a conferência cruzada
  entre cadernos que compartilham questões (a alternativa correta tem o mesmo texto em todos).
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import sqlite3
from collections import defaultdict

from iac import config
from iac import db as dbm
from iac import extracao, extracao_ce, pdf
from iac import texto as tx
from iac.bancas import ADAPTADORES
from iac.bancas.base import escolher_tabela
from iac.disciplinas import disciplina_da_secao, popular as popular_disciplinas
from iac.ingestao import caminho_no_acervo

LIMIAR_OCR = 2000                   # caracteres extraíveis no PDF inteiro; abaixo disso é escaneado
DIVERGENCIA_MAXIMA_RECURSO = 0.15   # recurso muda poucas questões; mais que isso = tabela trocada
CONCORDANCIA_MINIMA_CRUZADA = 0.9   # entre cadernos que compartilham questões
MIN_COMPARTILHADAS = 5
REGRA_ELEGIBILIDADE = "2.0"
# muda quando QUALQUER segmentador muda (revisão 2, BAIXO-8: antes a versão do C/E não disparava reprocessamento)
VERSAO_EXTRACAO = f"{extracao.VERSAO_EXTRATOR}+ce{extracao_ce.VERSAO_EXTRATOR_CE}"
# bancas cujas questões NÃO vêm de PDF (ingestão própria, ex.: ENEM em iac.enem): o preparo não as toca
BANCAS_EXTERNAS = ("inep",)
_SO_BANCAS_DE_PDF = f"(SELECT id FROM banca WHERE sigla IN ({','.join(repr(b) for b in BANCAS_EXTERNAS)}))"


def _adaptador(conexao: sqlite3.Connection, concurso_id: int):
    sigla = conexao.execute(
        "SELECT b.sigla FROM concurso c JOIN banca b ON b.id = c.banca_id WHERE c.id = ?", (concurso_id,)
    ).fetchone()[0]
    return ADAPTADORES[sigla]


def _excluir(conexao, etapa: str, entidade: str, entidade_id: int, motivo: str, detalhe: str | None = None) -> None:
    conexao.execute(
        """INSERT OR REPLACE INTO exclusao (etapa, entidade, entidade_id, motivo, detalhe, regra_versao, criada_em)
           VALUES (?,?,?,?,?,?,?)""",
        (etapa, entidade, entidade_id, motivo, (detalhe or "")[:500] or None, REGRA_ELEGIBILIDADE, dbm.agora()),
    )


# ------------------------------------------------------------------ cargo

_AREAS = [
    ("ti", r"tecnologia|informatica|sistemas|desenvolv|dados|redes|software|computa|\bti\b|suporte|web|ciberne"),
    ("juridica", r"juiz|promotor|defensor|procurador|advogad|oficial de justica|area judiciaria|juridic|area fim"),
    ("fiscal_controle", r"auditor|fiscal|controle|tributar|contador|contab|financas|tesouro|economista"),
    ("saude", r"medic|enferm|saude|farmac|odonto|psicolog|nutri|fisioter|assistente social"),
    ("educacao", r"professor|pedagog|educa|docente"),
    ("seguranca", r"policia|penitenciari|guarda|bombeiro|delegado|escrivao|investigador|seguranca publica"),
    ("engenharia", r"engenh|arquitet|geolog"),
    ("legislativa", r"legislativ|redator|taquigraf"),
    ("administrativa", r"administra|assistente|tecnico|analista|agente|auxiliar|secretari|gestao"),
]


def area_do_cargo(nome: str) -> str:
    norm = tx.norm_1a1(nome)
    return next((area for area, padrao in _AREAS if re.search(padrao, norm)), "outra")


def _cargo_id(conexao, nome: str | None) -> int | None:
    if not nome:
        return None
    chave = tx.chave_comparacao(nome)
    linha = conexao.execute("SELECT id FROM cargo WHERE nome_norm = ?", (chave,)).fetchone()
    if linha:
        return linha[0]
    return conexao.execute("INSERT INTO cargo (nome_norm, nome, area, area_fonte) VALUES (?,?,?, 'regra')",
                           (chave, nome, area_do_cargo(nome))).lastrowid


# ------------------------------------------------------------------ extrair

def extrair(conexao: sqlite3.Connection, refazer: bool = False, log=print) -> dict[str, int]:
    popular_disciplinas(conexao)
    conexao.commit()
    if refazer:
        _apagar_extracao(conexao)
    pendentes = conexao.execute(
        """SELECT d.id, d.concurso_id, d.sha256, d.nome_arquivo, c.id AS caderno_id FROM documento d
           LEFT JOIN caderno c ON c.documento_id = d.id
           WHERE d.tipo = 'prova' AND d.sha256 IS NOT NULL
             AND (c.id IS NULL OR c.versao_extrator <> ?)""", (VERSAO_EXTRACAO,)
    ).fetchall()
    contagem: dict[str, int] = defaultdict(int)
    for doc in pendentes:
        if doc["caderno_id"] and not _pode_reprocessar(conexao, doc["caderno_id"]):
            log(f"  caderno {doc['caderno_id']} tem execuções: mantido na versão antiga do extrator")
            contagem["mantido"] += 1
            continue
        contagem[_extrair_um(conexao, doc)] += 1
    log(f"extrair: {dict(contagem)}")
    return dict(contagem)


def _pode_reprocessar(conexao, caderno_id: int) -> bool:
    """Apaga a extração antiga de um caderno, preservando as questões (e seus ids) que outros cadernos sustentam.

    Questão cujo item canônico está neste caderno: passa a apontar para outra cópia dela (outro tipo de
    caderno); sem outra cópia, é apagada (a consolidação recria). Se a questão já foi usada em amostra ou
    execução, o caderno NÃO é reprocessado (os dados do experimento não podem mudar por baixo).
    """
    usado = conexao.execute(
        """SELECT 1 FROM item i WHERE i.caderno_id = ? AND i.questao_id IN
               (SELECT questao_id FROM execucao UNION SELECT questao_id FROM amostra_questao) LIMIT 1""",
        (caderno_id,)).fetchone()
    if usado:
        return False
    with dbm.transacao(conexao):
        for q in conexao.execute(
                """SELECT q.id FROM questao q JOIN item i ON i.id = q.item_canonico_id WHERE i.caderno_id = ?""",
                (caderno_id,)).fetchall():
            outro = conexao.execute("SELECT id FROM item WHERE questao_id = ? AND caderno_id <> ? ORDER BY id LIMIT 1",
                                    (q["id"], caderno_id)).fetchone()
            if outro:
                conexao.execute("UPDATE questao SET item_canonico_id = ? WHERE id = ?", (outro["id"], q["id"]))
            else:
                conexao.execute("UPDATE item SET questao_id = NULL WHERE questao_id = ?", (q["id"],))
                conexao.execute("DELETE FROM exclusao WHERE entidade = 'questao' AND entidade_id = ?", (q["id"],))
                conexao.execute("DELETE FROM questao WHERE id = ?", (q["id"],))
        conexao.execute("DELETE FROM exclusao WHERE entidade = 'item' AND entidade_id IN (SELECT id FROM item WHERE caderno_id = ?)", (caderno_id,))
        conexao.execute("DELETE FROM exclusao WHERE entidade = 'caderno' AND entidade_id = ?", (caderno_id,))
        conexao.execute("DELETE FROM item WHERE caderno_id = ?", (caderno_id,))
        conexao.execute("DELETE FROM texto_base WHERE caderno_id = ?", (caderno_id,))
        conexao.execute("DELETE FROM caderno WHERE id = ?", (caderno_id,))
    return True


def _extrair_um(conexao: sqlite3.Connection, doc: sqlite3.Row) -> str:
    adaptador = _adaptador(conexao, doc["concurso_id"])
    caminho = caminho_no_acervo(doc["sha256"])
    try:
        meta = adaptador.metadados_caderno(caminho, doc["nome_arquivo"])
        lido = pdf.ler(caminho)
        if lido.caracteres_texto < LIMIAR_OCR:
            resultado = extracao.CadernoExtraido([], None, "quarentena", "pdf_escaneado")
        elif hasattr(adaptador, "segmentar"):        # banca com mais de um formato (Cebraspe: C/E e ME)
            resultado = adaptador.segmentar(lido.texto)
        else:
            resultado = extracao.segmentar(lido.texto, adaptador.PERFIL)
    except Exception as erro:  # noqa: BLE001 — um PDF ruim não derruba o lote; vira quarentena registrada
        with dbm.transacao(conexao):
            caderno_id = conexao.execute(
                """INSERT INTO caderno (documento_id, concurso_id, n_itens, status, versao_extrator)
                   VALUES (?,?,0,'quarentena',?)""", (doc["id"], doc["concurso_id"], VERSAO_EXTRACAO)).lastrowid
            _excluir(conexao, "extrair", "caderno", caderno_id, "erro_leitura_pdf", f"{type(erro).__name__}: {erro}")
        return "quarentena"

    esperado = meta.esperado or resultado.esperado
    status, motivo = resultado.status, resultado.motivo
    if status == "ok" and esperado and len(resultado.itens) != esperado:
        status, motivo = "quarentena", f"{len(resultado.itens)} itens, capa diz {esperado}"
    if meta.avisos and status == "ok" and not meta.tipo_caderno:
        status, motivo = "quarentena", "; ".join(meta.avisos)

    with dbm.transacao(conexao):
        caderno_id = conexao.execute(
            """INSERT INTO caderno (documento_id, concurso_id, cargo_id, nivel, tipo_caderno, formato, n_itens,
                                    n_esperado, fonte_esperado, status, versao_extrator)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (doc["id"], doc["concurso_id"], _cargo_id(conexao, _nome_do_cargo(adaptador, meta)), meta.nivel,
             meta.tipo_caderno,
             _formato(resultado.itens), len(resultado.itens), esperado, "capa" if esperado else None,
             status, VERSAO_EXTRACAO),
        ).lastrowid
        if status == "quarentena":
            _excluir(conexao, "extrair", "caderno", caderno_id, "quarentena", motivo)
        textos: dict[str, int] = {}
        for item in resultado.itens:
            texto_base_id = None
            if item.texto_base:
                hash_tb = tx.sha256_texto(item.texto_base)
                if hash_tb not in textos:
                    textos[hash_tb] = conexao.execute(
                        "INSERT INTO texto_base (caderno_id, texto, hash) VALUES (?,?,?)",
                        (caderno_id, item.texto_base, hash_tb)).lastrowid
                texto_base_id = textos[hash_tb]
            item_id = conexao.execute(
                """INSERT INTO item (caderno_id, numero, secao, texto_base_id, enunciado, alternativas_json,
                                     tem_imagem, hash_conteudo) VALUES (?,?,?,?,?,?,?,?)""",
                (caderno_id, item.numero, item.secao, texto_base_id, item.enunciado,
                 json.dumps(item.alternativas, ensure_ascii=False) if item.alternativas else None,
                 int(item.tem_imagem), tx.hash_conteudo(item.enunciado, item.alternativas, item.texto_base)),
            ).lastrowid
            for problema in item.problemas:
                _excluir(conexao, "extrair", "item", item_id, problema)
    return status


def _formato(itens) -> str | None:
    tamanhos = [len(i.alternativas) for i in itens if i.alternativas]
    if not tamanhos:
        return "CE" if itens else None
    moda = max(set(tamanhos), key=tamanhos.count)
    return {5: "ME5", 4: "ME4"}.get(moda)


def _apagar_extracao(conexao: sqlite3.Connection) -> None:
    with dbm.transacao(conexao):
        for tabela in ("execucao", "amostra_questao"):
            if conexao.execute(f"SELECT 1 FROM {tabela} LIMIT 1").fetchone():
                raise RuntimeError(f"há dados em {tabela}: refazer tudo apagaria resultados. Use extração incremental.")
        for tabela in ("exclusao", "questao", "item", "texto_base", "caderno"):
            conexao.execute(f"DELETE FROM {tabela}")


# ------------------------------------------------------------------ gabaritar

_QUARENTENA_DO_GABARITAR = """SELECT entidade_id FROM exclusao
                               WHERE etapa = 'gabaritar' AND entidade = 'caderno' AND motivo = 'quarentena'"""


def gabaritar(conexao: sqlite3.Connection, log=print) -> dict[str, int]:
    """Recálculo completo e determinístico, um concurso por transação (revisão 3, A3).

    Cada concurso é zerado e recalculado na MESMA transação: uma queda no meio deixa os outros concursos
    com o resultado anterior, nunca com gabarito nulo. Cadernos que o próprio gabaritar pôs em quarentena
    ("contagem não confirmada") voltam a ser avaliados, então a exclusão que os explica nunca some sozinha.
    """
    contagem: dict[str, int] = defaultdict(int)
    concursos = conexao.execute(
        f"""SELECT DISTINCT ca.concurso_id FROM caderno ca JOIN concurso co ON co.id = ca.concurso_id
            WHERE (ca.status = 'ok' OR ca.id IN ({_QUARENTENA_DO_GABARITAR})) AND co.banca_id NOT IN {_SO_BANCAS_DE_PDF}"""
    ).fetchall()
    for (concurso_id,) in concursos:
        adaptador = _adaptador(conexao, concurso_id)
        tabelas, ilegiveis = [], []
        for g in conexao.execute(
            "SELECT id, sha256, tipo, nome_arquivo FROM documento WHERE concurso_id = ? AND tipo LIKE 'gabarito%' AND sha256 IS NOT NULL",
            (concurso_id,),
        ).fetchall():
            try:
                tabelas.extend(adaptador.ler_gabarito(caminho_no_acervo(g["sha256"]), g["tipo"].split("_", 1)[1],
                                                      g["id"], g["nome_arquivo"]))
            except Exception as erro:  # noqa: BLE001 — registrado como exclusão do documento
                ilegiveis.append((g["id"], str(erro)))
        with dbm.transacao(conexao):
            _zerar_gabaritos_do_concurso(conexao, concurso_id)
            for documento_id, erro in ilegiveis:
                _excluir(conexao, "gabaritar", "documento", documento_id, "gabarito_ilegivel", erro)
            contagem["gabarito_ilegivel"] += len(ilegiveis)
            datas = sorted({t.data_prova for t in tabelas if t.data_prova})
            if datas:
                conexao.execute("""UPDATE concurso SET data_prova = ?, data_prova_fonte = 'gabarito'
                                   WHERE id = ? AND (data_prova IS NULL OR data_prova_fonte = 'gabarito')""",
                                (datas[0], concurso_id))
            for caderno in conexao.execute(
                """SELECT ca.id, ca.n_itens, ca.n_esperado, d.sha256, d.nome_arquivo FROM caderno ca
                   JOIN documento d ON d.id = ca.documento_id WHERE ca.concurso_id = ? AND ca.status = 'ok'""",
                (concurso_id,),
            ).fetchall():
                contagem[_gabaritar_caderno(conexao, adaptador, caderno, tabelas)] += 1
    log(f"gabaritar: {dict(contagem)}")
    return dict(contagem)


def _zerar_gabaritos_do_concurso(conexao, concurso_id: int) -> None:
    conexao.execute(f"UPDATE caderno SET status = 'ok' WHERE concurso_id = ? AND id IN ({_QUARENTENA_DO_GABARITAR})",
                    (concurso_id,))
    conexao.execute("""UPDATE item SET gabarito_preliminar = NULL, gabarito_definitivo = NULL
                       WHERE caderno_id IN (SELECT id FROM caderno WHERE concurso_id = ?)""", (concurso_id,))
    conexao.execute("""UPDATE caderno SET gabarito_status = NULL, gab_prelim_doc = NULL, gab_defin_doc = NULL
                       WHERE concurso_id = ?""", (concurso_id,))
    conexao.execute("""DELETE FROM exclusao WHERE etapa = 'gabaritar' AND (
                           (entidade = 'caderno' AND entidade_id IN (SELECT id FROM caderno WHERE concurso_id = ?))
                           OR (entidade = 'documento' AND entidade_id IN (SELECT id FROM documento WHERE concurso_id = ?)))""",
                    (concurso_id, concurso_id))


def _diagnosticar_numeracao(meta, tabelas: list, numeros: set[int]) -> str | None:
    """A tabela do cargo existe, mas com outros números (caderno truncado ou rótulo perdido)? Vira 'tamanho...',
    que mantém a quarentena "contagem não confirmada" quando não há capa (revisão 3, M1)."""
    tabela, _ = escolher_tabela(dataclasses.replace(meta, esperado=None), tabelas, None)
    if tabela and set(tabela.respostas) != numeros:
        return (f"tamanho/numeração diferente: tabela {min(tabela.respostas)}–{max(tabela.respostas)}, "
                f"itens {min(numeros, default=0)}–{max(numeros, default=0)}")
    return None


def _nome_do_cargo(adaptador, meta) -> str | None:
    return meta.cargo_oficial if getattr(adaptador, "CARGO_E_CHAVE_DE_PAREAMENTO", False) else meta.cargo


def _gabaritar_caderno(conexao, adaptador, caderno, tabelas) -> str:
    meta = adaptador.metadados_caderno(caminho_no_acervo(caderno["sha256"]), caderno["nome_arquivo"])
    # a tabela tem que ter OS MESMOS números dos itens, não só o mesmo tamanho: um caderno 51–120 casado
    # com uma tabela 1–70 ficava "casado" sem nenhum item preenchido
    numeros = {r[0] for r in conexao.execute("SELECT numero FROM item WHERE caderno_id = ?", (caderno["id"],))}
    compativeis = [t for t in tabelas if not numeros or set(t.respostas) == numeros]
    escolhidas, motivos = {}, []
    grupos = {"preliminar": ["preliminar"], "definitivo": ["retificado", "definitivo"]}  # retificação vence
    for coluna, versoes in grupos.items():
        for versao in versoes:
            tabela, motivo = escolher_tabela(meta, [t for t in compativeis if t.versao == versao], caderno["n_itens"])
            if tabela:
                escolhidas[coluna] = tabela
                break
            motivos.append(_diagnosticar_numeracao(meta, [t for t in tabelas if t.versao == versao], numeros) or motivo)

    if not escolhidas:
        status = ("ambiguo" if any(m.startswith("ambiguo") for m in motivos)
                  else "tamanho_diferente" if any(m.startswith("tamanho") for m in motivos) and not caderno["n_esperado"]
                  else "sem_tabela")   # com a contagem confirmada pela capa, é só a tabela do cargo que falta
        conexao.execute("UPDATE caderno SET gabarito_status = ? WHERE id = ?", (status, caderno["id"]))
        _excluir(conexao, "gabaritar", "caderno", caderno["id"], f"gabarito_{status}", "; ".join(motivos))
        if status == "tamanho_diferente" and not caderno["n_esperado"]:
            # sem capa legível e sem tabela do mesmo tamanho: o caderno provavelmente foi truncado
            conexao.execute("UPDATE caderno SET status = 'quarentena' WHERE id = ?", (caderno["id"],))
            _excluir(conexao, "gabaritar", "caderno", caderno["id"], "quarentena", "contagem não confirmada")
        return status

    pre, defi = escolhidas.get("preliminar"), escolhidas.get("definitivo")
    if pre and defi:
        divergentes = sum(pre.respostas[n] != defi.respostas.get(n) and defi.respostas.get(n) != "X" for n in pre.respostas)
        if divergentes / len(pre.respostas) > DIVERGENCIA_MAXIMA_RECURSO:
            # um dos dois é de outro cargo/sessão e não dá para saber qual: descarta o caderno
            conexao.execute("UPDATE caderno SET gabarito_status = 'duvidoso' WHERE id = ?", (caderno["id"],))
            _excluir(conexao, "gabaritar", "caderno", caderno["id"], "gabarito_duvidoso",
                     f"preliminar x definitivo divergem em {divergentes}/{len(pre.respostas)}")
            return "duvidoso"

    for coluna, tabela in (("gabarito_preliminar", pre), ("gabarito_definitivo", defi)):
        if tabela:
            conexao.executemany(f"UPDATE item SET {coluna} = ? WHERE caderno_id = ? AND numero = ?",
                                [(letra, caderno["id"], n) for n, letra in tabela.respostas.items()])
    conexao.execute(
        """UPDATE caderno SET gabarito_status = 'casado', gab_prelim_doc = ?, gab_defin_doc = ?,
               n_esperado = COALESCE(n_esperado, ?), fonte_esperado = COALESCE(fonte_esperado, 'gabarito')
           WHERE id = ?""",
        (pre.documento_id if pre else None, defi.documento_id if defi else None, caderno["n_itens"], caderno["id"]))
    return "casado"


# ------------------------------------------------------------------ consolidar

LETRAS_ME = set("ABCDE")
_COMANDO_NEGATIVO = re.compile(r"\b(exceto|incorret[ao]s?|errad[ao]s?|fals[ao]s?)\b|"
                               r"\bnao\s+(e|esta|estao|constitui|corresponde|apresenta|se aplica|pode)\b")
_CITA_NORMA = re.compile(r"\b(lei|decreto|sumula|art\.?|artigo|constituicao|resolucao|portaria|lc)\s*"
                         r"(complementar\s*)?(n[o°º.]*\s*)?\d")
_AFIRMATIVAS = re.compile(r"(?:^|\s)(I|II|III|IV|V)\s*[.\-–)]\s")
_REFERENCIAL = re.compile(r"\b(todas as (anteriores|alternativas|opcoes)|nenhuma das|ambas|"
                          r"(alternativas|opcoes|itens|letras) [a-e] e [a-e]|apenas [a-e] e [a-e])\b")


def _resposta(item) -> str | None:
    return item["gabarito_definitivo"] or item["gabarito_preliminar"]


def _texto_correto(item) -> str | None:
    letra = _resposta(item)
    alt = json.loads(item["alternativas_json"]) if item["alternativas_json"] else {}
    if letra in alt:
        return tx.chave_comparacao(pdf.sem_destaques(alt[letra]))
    return None


def _motivo_periodo(grupo: list) -> str | None:
    """Corpus = provas de ANO_INICIO a ANO_FIM, pela PRIMEIRA ocorrência conhecida da questão (revisão 2, ALTO-4).

    Sem data da prova, o ano do edital decide só quando não há dúvida: edital ≥ ANO_INICIO garante prova no
    período; edital de ANO_INICIO-1 pode ter prova no ano seguinte, então exige a data (`sem_data`).
    """
    anos_prova = [int(i["data_prova"][:4]) for i in grupo if i["data_prova"]]
    anos_edital = [i["ano_edital"] for i in grupo if not i["data_prova"] and i["ano_edital"]]
    if not anos_prova and not anos_edital:
        return "sem_data"
    primeiro = min(anos_prova + anos_edital)
    if primeiro < config.ANO_INICIO_CORPUS - 1 or (anos_prova and min(anos_prova) < config.ANO_INICIO_CORPUS):
        return "fora_do_periodo"
    if primeiro == config.ANO_INICIO_CORPUS - 1:
        return "sem_data"
    if max(anos_prova + anos_edital) > config.ANO_FIM_CORPUS:
        return "fora_do_periodo"
    return None


def _cadernos_ce_com_letras(itens: list) -> dict[int, str]:
    """Caderno certo/errado com gabarito A/B/D em algum item: a tabela pareada é de múltipla escolha.

    Os itens com "C" e "E" passariam como certo/errado; por isso o caderno INTEIRO fica duvidoso.
    """
    letras: dict[int, set[str]] = defaultdict(set)
    for item in itens:
        if item["formato_caderno"] == "CE" and _resposta(item):
            letras[item["caderno_id"]].add(_resposta(item))
    return {c: f"caderno C/E com gabarito {''.join(sorted(l))}" for c, l in letras.items() if l & {"A", "B", "D"}}


def definir_data(conexao: sqlite3.Connection, banca: str, slug: str, data: str, fonte: str = "manual") -> None:
    """Data da prova informada à mão (ou lida do edital) para um concurso sem data."""
    datetime.date.fromisoformat(data)                       # ValueError se a data for inválida
    if fonte not in ("manual", "edital"):
        raise ValueError(f"fonte inválida: {fonte}")
    with dbm.transacao(conexao):
        cursor = conexao.execute(
            """UPDATE concurso SET data_prova = ?, data_prova_fonte = ?
               WHERE slug = ? AND banca_id = (SELECT id FROM banca WHERE sigla = ?)""", (data, fonte, slug, banca))
        if cursor.rowcount != 1:
            raise ValueError(f"concurso não encontrado: {banca}/{slug}")


def concursos_sem_data(conexao: sqlite3.Connection) -> list[sqlite3.Row]:
    """Concursos com caderno extraído e sem data da prova, com o ano do edital quando há (para preencher à mão)."""
    return conexao.execute(
        """SELECT b.sigla, co.slug, co.titulo, co.ano_edital, co.url_fonte, COUNT(ca.id) AS cadernos
           FROM concurso co JOIN banca b ON b.id = co.banca_id JOIN caderno ca ON ca.concurso_id = co.id
           WHERE co.data_prova IS NULL AND ca.status = 'ok'
             AND (co.ano_edital IS NULL OR co.ano_edital = ?)
           GROUP BY co.id ORDER BY b.sigla, co.slug""", (config.ANO_INICIO_CORPUS - 1,)).fetchall()


def _cadernos_duvidosos(grupos: dict[str, list]) -> dict[int, str]:
    """Conferência cruzada: caderno cujas respostas discordam dos outros nas questões compartilhadas."""
    concorda: dict[int, int] = defaultdict(int)
    total: dict[int, int] = defaultdict(int)
    for grupo in grupos.values():
        textos = [(i["caderno_id"], _texto_correto(i)) for i in grupo if _resposta(i) not in (None, "X")]
        textos = [(c, t) for c, t in textos if t]
        if len({c for c, _ in textos}) < 2:
            continue
        for caderno_id, texto in textos:
            outros = [t for c, t in textos if c != caderno_id]
            maioria = max(set(outros), key=outros.count)
            total[caderno_id] += 1
            concorda[caderno_id] += int(texto == maioria)
    return {c: f"concorda em {concorda[c]}/{total[c]} questões compartilhadas"
            for c in total if total[c] >= MIN_COMPARTILHADAS and concorda[c] / total[c] < CONCORDANCIA_MINIMA_CRUZADA}


def consolidar(conexao: sqlite3.Connection, log=print) -> dict[str, int]:
    itens = conexao.execute(
        f"""SELECT i.*, ca.concurso_id, ca.cargo_id, ca.nivel, ca.formato AS formato_caderno, cg.area, co.data_prova,
                  co.ano_edital, tb.texto AS texto_base
           FROM item i JOIN caderno ca ON ca.id = i.caderno_id JOIN concurso co ON co.id = ca.concurso_id
           LEFT JOIN cargo cg ON cg.id = ca.cargo_id LEFT JOIN texto_base tb ON tb.id = i.texto_base_id
           WHERE ca.status = 'ok' AND co.banca_id NOT IN {_SO_BANCAS_DE_PDF} ORDER BY i.id"""
    ).fetchall()
    problemas: dict[int, list[str]] = defaultdict(list)
    for linha in conexao.execute("SELECT entidade_id, motivo FROM exclusao WHERE etapa = 'extrair' AND entidade = 'item'"):
        problemas[linha[0]].append(linha[1])

    grupos: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for item in itens:
        grupos[item["hash_conteudo"]].append(item)
    duvidosos = {**_cadernos_ce_com_letras(itens), **_cadernos_duvidosos(grupos)}

    contagem: dict[str, int] = defaultdict(int)
    with dbm.transacao(conexao):
        conexao.execute("DELETE FROM exclusao WHERE etapa = 'consolidar'")
        for caderno_id, detalhe in duvidosos.items():
            conexao.execute("UPDATE caderno SET gabarito_status = 'duvidoso' WHERE id = ?", (caderno_id,))
            _excluir(conexao, "consolidar", "caderno", caderno_id, "gabarito_duvidoso_cruzado", detalhe)
        vistos = set()
        for hash_, grupo in grupos.items():
            dados = _congelar_questao_usada(conexao, hash_, _consolidar_grupo(grupo, problemas, duvidosos))
            questao_id = _upsert_questao(conexao, hash_, dados)
            vistos.add(questao_id)
            conexao.executemany("UPDATE item SET questao_id = ? WHERE id = ?", [(questao_id, i["id"]) for i in grupo])
            for motivo in dados["motivos"]:
                _excluir(conexao, "consolidar", "questao", questao_id, motivo)
            contagem["elegivel" if not dados["motivos"] else dados["motivos"][0]] += 1
        contagem["orfas"] = _tratar_orfas(conexao, vistos)
    log(f"consolidar: {dict(contagem)} | cadernos com gabarito duvidoso na conferência cruzada: {len(duvidosos)}")
    return dict(contagem)


def _consolidar_grupo(grupo: list, problemas: dict[int, list[str]], duvidosos: dict[int, str]) -> dict:
    confiaveis = [i for i in grupo if _resposta(i) and i["caderno_id"] not in duvidosos]
    canonico = confiaveis[0] if confiaveis else grupo[0]
    alternativas = json.loads(canonico["alternativas_json"]) if canonico["alternativas_json"] else {}
    # formato vem do CADERNO: questão de múltipla escolha cuja segmentação falhou continua ME
    formato = canonico["formato_caderno"] or ("CE" if not alternativas else "ME5")
    gabarito = _resposta(canonico) if confiaveis else None
    pre, defi = canonico["gabarito_preliminar"], canonico["gabarito_definitivo"]

    motivos = list(dict.fromkeys(problemas.get(canonico["id"], [])))
    if any(extracao.P_IMAGEM in problemas.get(i["id"], []) for i in grupo) and extracao.P_IMAGEM not in motivos:
        motivos.append(extracao.P_IMAGEM)
    if not gabarito:
        motivos.append("gabarito_duvidoso" if any(i["caderno_id"] in duvidosos for i in grupo) else "sem_gabarito")
    elif gabarito == "X" or any(_resposta(i) == "X" for i in confiaveis):
        motivos.append("anulada")
    elif formato == "CE" and gabarito not in ("C", "E"):
        motivos.append("gabarito_invalido")       # tabela A–E pareada com caderno C/E: não derruba o lote
        gabarito = None
    elif formato != "CE" and (gabarito not in LETRAS_ME or gabarito not in alternativas):
        motivos.append("gabarito_invalido")
    elif formato == "CE" and len({_resposta(i) for i in confiaveis}) > 1:
        motivos.append("gabarito_inconsistente")   # mesmo item, C num concurso e E noutro (ex.: lei mudou)
    elif len({_texto_correto(i) for i in confiaveis if _texto_correto(i)}) > 1:
        motivos.append("gabarito_inconsistente")
    periodo = _motivo_periodo(grupo)
    if periodo:
        motivos.insert(0, periodo)    # precedência no funil: fora do corpus vem antes de qualidade (revisão 3, M3)

    enunciado = pdf.sem_destaques(canonico["enunciado"])
    norm = tx.norm_1a1(enunciado)
    alternativas_texto = " ".join(alternativas.values())
    niveis = {i["nivel"] for i in grupo if i["nivel"]}
    areas = {i["area"] for i in grupo if i["area"]}
    datas = sorted(i["data_prova"] for i in grupo if i["data_prova"])
    return {
        "canonico": canonico, "formato": formato,
        "gabarito": gabarito if gabarito != "X" else "X",
        "gabarito_fonte": ("definitivo" if defi else "preliminar") if gabarito else None,
        "alterada": (int(pre != defi) if pre and defi else None),
        "disciplina": disciplina_da_secao(canonico["secao"]),
        "motivos": motivos,
        "nivel_questao": niveis.pop() if len(niveis) == 1 else ("misto" if niveis else None),
        "area_questao": areas.pop() if len(areas) == 1 else ("multipla" if areas else None),
        "n_cargos": len({i["cargo_id"] for i in grupo if i["cargo_id"]}),
        "n_concursos": len({i["concurso_id"] for i in grupo}),
        "data_primeira_prova": datas[0] if datas else None,
        "n_carac_enunciado": len(enunciado),
        "n_carac_texto_base": len(canonico["texto_base"] or ""),
        "n_alternativas": len(alternativas),
        "tem_texto_base": int(bool(canonico["texto_base"])),
        "tem_destaque": int(pdf.tem_destaque(canonico["enunciado"] + alternativas_texto + (canonico["texto_base"] or ""))),
        "comando_negativo": int(bool(_COMANDO_NEGATIVO.search(norm))),
        "cita_norma": int(bool(_CITA_NORMA.search(norm + " " + tx.norm_1a1(alternativas_texto)))),
        "tem_codigo": int(extracao.tem_codigo(canonico["enunciado"]) or extracao.tem_codigo(alternativas_texto)),
        "tem_afirmativas": int(bool(_AFIRMATIVAS.search(enunciado))),
        "permutavel": int(not _REFERENCIAL.search(tx.norm_1a1(alternativas_texto))),
    }


_COLUNAS_QUESTAO = ("item_canonico_id", "formato", "gabarito", "gabarito_fonte", "alterada_recurso", "elegivel",
                    "nivel_questao", "area_questao", "n_cargos", "n_concursos", "data_primeira_prova",
                    "n_carac_enunciado", "n_carac_texto_base", "n_alternativas", "tem_texto_base", "tem_destaque",
                    "comando_negativo", "cita_norma", "tem_codigo", "tem_afirmativas", "permutavel")


_CONGELADOS = (("gabarito", "gabarito"), ("gabarito_fonte", "gabarito_fonte"), ("alterada_recurso", "alterada"))


def _congelar_questao_usada(conexao, hash_: str, dados: dict) -> dict:
    """Questão já em amostra ou execução não muda por baixo do experimento (revisões 2 e 3: MÉDIO-7, A2, B5).

    Ficam congelados o gabarito, a fonte do gabarito, `alterada_recurso` e a elegibilidade: senão a questão
    sairia dos pendentes dos modelos que ainda não a responderam e quebraria o pareamento. Cada mudança que
    deixou de ser aplicada vai para `auditoria` (histórico acumulado, sem sobrescrever o anterior) para decisão
    manual; aplicar uma correção é um UPDATE explícito em `questao`, e o avaliador reavalia o que mudou.
    """
    atual = conexao.execute(
        """SELECT q.id, q.gabarito, q.gabarito_fonte, q.alterada_recurso, q.elegivel FROM questao q
           WHERE q.hash_conteudo = ? AND (EXISTS (SELECT 1 FROM amostra_questao aq WHERE aq.questao_id = q.id)
                                          OR EXISTS (SELECT 1 FROM execucao e WHERE e.questao_id = q.id))""",
        (hash_,)).fetchone()
    if not atual:
        return dados
    for coluna, chave in _CONGELADOS:
        if atual[coluna] != dados[chave]:
            _auditar(conexao, atual["id"], f"{coluna}_mudou_apos_uso", f"{atual[coluna]} -> {dados[chave]}")
    novos_motivos = dados["motivos"]
    if atual["elegivel"] and novos_motivos:
        _auditar(conexao, atual["id"], "ficaria_inelegivel_apos_uso", ",".join(novos_motivos))
    motivos = [] if atual["elegivel"] else (novos_motivos or ["congelada_inelegivel"])
    return {**dados, "gabarito": atual["gabarito"], "gabarito_fonte": atual["gabarito_fonte"],
            "alterada": atual["alterada_recurso"], "motivos": motivos}


def _auditar(conexao, questao_id: int, evento: str, detalhe: str) -> None:
    """Acrescenta ao histórico do evento (B -> D e depois B -> C ficam os dois), sem repetir o mesmo registro."""
    conexao.execute(
        """INSERT INTO auditoria (entidade, entidade_id, evento, detalhe, criada_em) VALUES ('questao', ?, ?, ?, ?)
           ON CONFLICT(entidade, entidade_id, evento) DO UPDATE SET
               detalhe = CASE WHEN instr(auditoria.detalhe, excluded.detalhe) > 0 THEN auditoria.detalhe
                              ELSE auditoria.detalhe || ' | ' || excluded.detalhe END,
               criada_em = excluded.criada_em""",
        (questao_id, evento, detalhe, dbm.agora()))


def _upsert_questao(conexao, hash_: str, dados: dict) -> int:
    valores = {
        "item_canonico_id": dados["canonico"]["id"], "formato": dados["formato"], "gabarito": dados["gabarito"],
        "gabarito_fonte": dados["gabarito_fonte"], "alterada_recurso": dados["alterada"],
        "elegivel": int(not dados["motivos"]),
        **{c: dados[c] for c in _COLUNAS_QUESTAO[6:]},
    }
    colunas = ", ".join(_COLUNAS_QUESTAO)
    marcas = ", ".join(f":{c}" for c in _COLUNAS_QUESTAO)
    atualiza = ", ".join(f"{c} = excluded.{c}" for c in _COLUNAS_QUESTAO)
    conexao.execute(
        f"""INSERT INTO questao (hash_conteudo, {colunas}, disciplina, disciplina_fonte)
            VALUES (:hash, {marcas}, :disciplina, :disciplina_fonte)
            ON CONFLICT(hash_conteudo) DO UPDATE SET {atualiza},
                disciplina = CASE WHEN questao.disciplina_fonte IN ('llm','humano') THEN questao.disciplina
                                  ELSE excluded.disciplina END,
                disciplina_fonte = CASE WHEN questao.disciplina_fonte IN ('llm','humano') THEN questao.disciplina_fonte
                                        ELSE excluded.disciplina_fonte END""",
        {"hash": hash_, **valores, "disciplina": dados["disciplina"],
         "disciplina_fonte": "secao" if dados["disciplina"] else None},
    )
    return conexao.execute("SELECT id FROM questao WHERE hash_conteudo = ?", (hash_,)).fetchone()[0]


def _tratar_orfas(conexao, vistos: set[int]) -> int:
    """Questões que não correspondem mais a nenhum item: apaga se ninguém usa; senão, inelegível."""
    orfas = [r[0] for r in conexao.execute(
        f"""SELECT q.id FROM questao q JOIN item i ON i.id = q.item_canonico_id JOIN caderno ca ON ca.id = i.caderno_id
            JOIN concurso co ON co.id = ca.concurso_id WHERE co.banca_id NOT IN {_SO_BANCAS_DE_PDF}""") if r[0] not in vistos]
    for questao_id in orfas:
        usada = conexao.execute(
            """SELECT 1 FROM execucao WHERE questao_id = ? UNION SELECT 1 FROM amostra_questao WHERE questao_id = ? LIMIT 1""",
            (questao_id, questao_id)).fetchone()
        if usada:
            conexao.execute("UPDATE questao SET elegivel = 0 WHERE id = ?", (questao_id,))
            _excluir(conexao, "consolidar", "questao", questao_id, "orfa", "item de origem mudou após uso")
        else:
            conexao.execute("UPDATE item SET questao_id = NULL WHERE questao_id = ?", (questao_id,))
            conexao.execute("DELETE FROM questao WHERE id = ?", (questao_id,))
    return len(orfas)
