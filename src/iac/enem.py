"""Ingestão do ENEM como pontes (docs/PLANO-CONCLUSAO.md, item 1 do método).

Cada questão do ENEM com texto disponível vira uma `questao` ME5 numa banca/concurso próprios (banca 'inep',
concurso 'enem_<ano>'), no mesmo formato das questões de concurso: o executor a roda com `me_padrao` sem
nenhuma alteração. Os parâmetros oficiais (a, b, c), a área, o ano e a habilidade ficam em `item_param_inep`.

Casamento texto → CO_ITEM: pela posição no caderno. Para cada área, o caderno (CO_PROVA) escolhido é o que tem a
sequência de gabarito 100% igual à do texto, entre os cadernos SEM item adaptado. Quando cadernos diferentes
batem 100% com sequências de CO_ITEM diferentes (versões com troca de itens de mesmo gabarito), vale a
sequência da maioria (empate: menor CO_PROVA), e as posições em que eles discordam ficam marcadas em
`casamento_divergente`. Em LC, posições 1–5: só inglês (TP_LINGUA = 0).

Exclusões, cada uma contada (a primeira da lista é o motivo principal): sem_texto (2017: a fonte não tem a
questão), anulado, abandonado_ou_sem_b, adaptado, imagem_sem_descricao. Imagem COM descrição textual entra,
marcada em `com_descricao`.

Conjunto de teste (2023–2024): os itens ficam no banco, mas nenhuma análise lê o *b* deles; o único acesso é
`psicometria.avaliar_teste` (com trava). `b_inep_desenvolvimento` recusa o teste.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from iac import db as dbm
from iac import enem_fontes as fontes
from iac import pdf
from iac import texto as tx
from iac.disciplinas import popular as popular_disciplinas

BANCA = "inep"
VERSAO = "enem-1"
AREAS = ("LC", "CH", "MT")                 # CN fica fora do plano
ANOS_DEV, ANOS_TESTE = (2017, 2022), (2023, 2024)
AMOSTRA_DEV, AMOSTRA_TESTE, AMOSTRA_ANCORA = "enem_dev", "enem_teste", "concurso_ancora"
AMOSTRA_ORIGEM_ANCORA = "extensao_me"
SEMENTE = 20261019
MIN_POSICOES_CASAMENTO = 10                # abaixo disso um 100% poderia ser acaso
ITENS_POR_AREA = 45
# 1º dia: LC e CH; 2º dia: CN e MT (desde 2017). Datas oficiais da 1ª aplicação.
DATAS = {2017: ("2017-11-05", "2017-11-12"), 2022: ("2022-11-13", "2022-11-20"),
         2023: ("2023-11-05", "2023-11-12"), 2024: ("2024-11-03", "2024-11-10")}
DIA_DA_AREA = {"LC": 0, "CH": 0, "CN": 1, "MT": 1}
DESLOCAMENTO_2017 = {"LC": 0, "CH": 45, "CN": 90, "MT": 135}   # 2017: CO_POSICAO começa em 1 em cada área
# áreas do ENEM ↔ disciplinas do concurso
DISCIPLINAS_DA_AREA = {
    "LC": ("lingua_portuguesa", "lingua_inglesa", "lingua_espanhola"),
    "MT": ("matematica_rlm", "matematica_financeira", "estatistica"),
    "CH": ("historia_geografia", "atualidades"),
}
MOTIVOS = ("sem_texto", "anulado", "abandonado_ou_sem_b", "adaptado", "imagem_sem_descricao")

ESQUEMA_PARAM = """
CREATE TABLE IF NOT EXISTS item_param_inep (
    id                    INTEGER PRIMARY KEY,
    ano                   INTEGER NOT NULL,
    area                  TEXT NOT NULL CHECK (area IN ('LC','CH','MT','CN')),
    numero                INTEGER NOT NULL,           -- nº da questão no caderno de onde veio o texto (1–180)
    co_posicao            INTEGER NOT NULL,
    co_item               INTEGER NOT NULL,
    co_prova              INTEGER NOT NULL,
    tx_cor                TEXT,
    tp_lingua             INTEGER,
    co_habilidade         INTEGER,
    gabarito              TEXT,
    param_a               REAL,
    param_b               REAL,
    param_c               REAL,
    in_item_aban          INTEGER,
    in_item_adaptado      INTEGER,
    tem_imagem            INTEGER NOT NULL DEFAULT 0 CHECK (tem_imagem IN (0,1)),
    com_descricao         INTEGER NOT NULL DEFAULT 0 CHECK (com_descricao IN (0,1)),
    casamento_divergente  INTEGER NOT NULL DEFAULT 0 CHECK (casamento_divergente IN (0,1)),
    fonte_texto           TEXT,
    conjunto              TEXT NOT NULL CHECK (conjunto IN ('dev','teste')),
    motivos               TEXT,                       -- NULL = entra; senão, motivos separados por vírgula
    questao_id            INTEGER REFERENCES questao(id),
    versao                TEXT NOT NULL,
    UNIQUE (ano, area, numero)
);
CREATE INDEX IF NOT EXISTS ix_param_inep_questao ON item_param_inep(questao_id);
"""


def garantir_tabela(conexao: sqlite3.Connection) -> None:
    """Tabela aditiva, criada aqui e não por migração: subir PRAGMA user_version derrubaria o painel em execução
    (processo longo com a versão antiga do código carregada)."""
    conexao.executescript(ESQUEMA_PARAM)


def conjunto_do_ano(ano: int) -> str:
    if ano in ANOS_DEV:
        return "dev"
    if ano in ANOS_TESTE:
        return "teste"
    raise ValueError(f"ENEM {ano} fora do plano (dev {ANOS_DEV}, teste {ANOS_TESTE})")


# ------------------------------------------------------------------ casamento

def numero_da_questao(ano: int, area: str, co_posicao: int, tp_lingua: str) -> int | None:
    """Posição do INEP → nº da questão no caderno do texto. None = espanhol (fora)."""
    if ano == 2017:
        if area == "LC":
            if co_posicao <= 5:
                return co_posicao if tp_lingua == "0" else None
            if co_posicao <= 10:
                return None                                  # 2017: espanhol ocupa 6–10
            return co_posicao - 5
        return co_posicao + DESLOCAMENTO_2017[area]
    if area == "LC" and co_posicao <= 5 and tp_lingua != "0":
        return None
    return co_posicao


@dataclass
class Casamento:
    ano: int
    area: str
    co_prova: int
    tx_cor: str
    linhas: dict[int, dict]                     # nº da questão → linha do ITENS_PROVA
    divergentes: set[int] = field(default_factory=set)
    candidatos: list[int] = field(default_factory=list)
    posicoes_comparadas: int = 0


def _cadernos(itens: list[dict], ano: int, area: str) -> dict[int, dict[int, dict]]:
    cadernos: dict[int, dict[int, dict]] = defaultdict(dict)
    for linha in itens:
        if linha["SG_AREA"] != area:
            continue
        numero = numero_da_questao(ano, area, int(linha["CO_POSICAO"]), linha["TP_LINGUA"])
        if numero is not None:
            cadernos[int(linha["CO_PROVA"])][numero] = linha
    return cadernos


def casar(itens: list[dict], textos: dict[int, fontes.TextoQuestao], ano: int, area: str) -> Casamento:
    candidatos = []
    for co_prova, linhas in sorted(_cadernos(itens, ano, area).items()):
        if len(linhas) != ITENS_POR_AREA:
            continue
        comuns = [n for n in linhas if n in textos]
        if len(comuns) < MIN_POSICOES_CASAMENTO:
            continue
        bate = all(linhas[n]["TX_GABARITO"] == textos[n].gabarito for n in comuns)
        sem_adaptado = all(linhas[n]["IN_ITEM_ADAPTADO"] != "1" for n in linhas)
        if bate and sem_adaptado:
            candidatos.append((co_prova, linhas, len(comuns)))
    if not candidatos:
        raise RuntimeError(f"ENEM {ano} {area}: nenhum caderno sem adaptação bate 100% com o gabarito do texto")
    def sequencia(linhas):
        return tuple(linhas[n]["CO_ITEM"] for n in sorted(linhas))
    votos = Counter(sequencia(linhas) for _, linhas, _ in candidatos)
    maioria = max(votos.values())
    co_prova, linhas, comparadas = next(c for c in candidatos if votos[sequencia(c[1])] == maioria)
    divergentes = {n for _, outras, _ in candidatos for n in linhas if outras[n]["CO_ITEM"] != linhas[n]["CO_ITEM"]}
    return Casamento(ano, area, co_prova, linhas[min(linhas)]["TX_COR"], linhas, divergentes,
                     [c[0] for c in candidatos], comparadas)


def motivos_do_item(linha: dict, texto: fontes.TextoQuestao | None) -> list[str]:
    motivos = []
    if texto is None:
        motivos.append("sem_texto")
    if linha["TX_GABARITO"] == "X" or (texto is not None and texto.gabarito == "X"):
        motivos.append("anulado")
    if linha["IN_ITEM_ABAN"] == "1" or not linha["NU_PARAM_B"].strip():
        motivos.append("abandonado_ou_sem_b")
    if linha["IN_ITEM_ADAPTADO"] == "1":
        motivos.append("adaptado")
    if texto is not None and texto.tem_imagem and not texto.com_descricao:
        motivos.append("imagem_sem_descricao")
    return motivos


# ------------------------------------------------------------------ gravação

def _real(valor: str) -> float | None:
    valor = (valor or "").strip().replace(",", ".")
    return float(valor) if valor else None


def _inteiro(valor: str) -> int | None:
    valor = (valor or "").strip()
    return int(float(valor)) if valor else None


def _disciplina(area: str, numero: int) -> str:
    if area == "LC":
        return "lingua_inglesa" if numero <= 5 else "lingua_portuguesa"
    return {"MT": "matematica_rlm", "CH": "historia_geografia"}[area]


def _concurso(conexao, ano: int) -> int:
    banca_id = dbm.obter_ou_criar(conexao, "banca", {"sigla": BANCA}, {"nome": "Inep (ENEM)"})
    return dbm.obter_ou_criar(conexao, "concurso", {"banca_id": banca_id, "slug": f"enem_{ano}"}, {
        "titulo": f"ENEM {ano} (1ª aplicação)", "orgao": "Inep", "esfera": "federal", "poder": "executivo",
        "regiao": "nacional", "ano_edital": ano, "data_prova": DATAS[ano][0], "data_prova_fonte": "manual",
        "url_fonte": fontes.URL_INEP.format(ano=ano)})


def _caderno(conexao, concurso_id: int, cas: Casamento, n_itens: int) -> int:
    documento_id = dbm.obter_ou_criar(
        conexao, "documento", {"url": f"{fontes.URL_INEP.format(ano=cas.ano)}#{cas.area}:CO_PROVA={cas.co_prova}"},
        {"concurso_id": concurso_id, "nome_arquivo": f"ITENS_PROVA_{cas.ano}.csv", "tipo": "outro"})
    return conexao.execute(
        """INSERT INTO caderno (documento_id, concurso_id, nivel, tipo_caderno, formato, n_itens, n_esperado,
                                fonte_esperado, status, gabarito_status, versao_extrator)
           VALUES (?,?, 'medio', ?, 'ME5', ?, ?, 'sequencia', 'ok', 'casado', ?)""",
        (documento_id, concurso_id, f"{cas.area} {cas.tx_cor} (CO_PROVA {cas.co_prova})", n_itens,
         ITENS_POR_AREA, VERSAO)).lastrowid


def _covariaveis(texto: fontes.TextoQuestao) -> dict:
    from iac import extracao, preparo
    norm = tx.norm_1a1(texto.enunciado)
    alternativas = " ".join(texto.alternativas.values())
    return {
        "n_carac_enunciado": len(texto.enunciado), "n_alternativas": len(texto.alternativas),
        "tem_destaque": int(pdf.tem_destaque(texto.enunciado + alternativas)),
        "comando_negativo": int(bool(preparo._COMANDO_NEGATIVO.search(norm))),
        "cita_norma": int(bool(preparo._CITA_NORMA.search(norm + " " + tx.norm_1a1(alternativas)))),
        "tem_codigo": int(extracao.tem_codigo(texto.enunciado) or extracao.tem_codigo(alternativas)),
        "tem_afirmativas": int(bool(preparo._AFIRMATIVAS.search(texto.enunciado))),
        "permutavel": int(not preparo._REFERENCIAL.search(tx.norm_1a1(alternativas))),
    }


def _questao(conexao, caderno_id: int, cas: Casamento, numero: int, linha: dict, texto, motivos) -> int:
    hash_ = tx.sha256_texto(f"{BANCA}|{cas.ano}|{linha['CO_ITEM']}|"
                            f"{tx.hash_conteudo(texto.enunciado, texto.alternativas)}")
    gabarito = "X" if "anulado" in motivos else linha["TX_GABARITO"]
    item_id = conexao.execute(
        """INSERT INTO item (caderno_id, numero, secao, enunciado, alternativas_json, tem_imagem, hash_conteudo,
                             gabarito_preliminar, gabarito_definitivo) VALUES (?,?,?,?,?,?,?,?,?)""",
        (caderno_id, numero, f"ENEM {cas.area}", texto.enunciado, json.dumps(texto.alternativas, ensure_ascii=False),
         int(texto.tem_imagem), hash_, gabarito, gabarito)).lastrowid
    valores = {"hash_conteudo": hash_, "item_canonico_id": item_id, "formato": "ME5", "gabarito": gabarito,
               "gabarito_fonte": "definitivo", "disciplina": _disciplina(cas.area, numero), "disciplina_fonte": "secao",
               "elegivel": int(not motivos), "nivel_questao": "medio", "n_cargos": 0, "n_concursos": 1,
               "data_primeira_prova": DATAS[cas.ano][DIA_DA_AREA[cas.area]], **_covariaveis(texto)}
    colunas = ", ".join(valores)
    questao_id = conexao.execute(f"INSERT INTO questao ({colunas}) VALUES ({', '.join('?' for _ in valores)})",
                                 tuple(valores.values())).lastrowid
    conexao.execute("UPDATE item SET questao_id = ? WHERE id = ?", (questao_id, item_id))
    for motivo in motivos:
        conexao.execute(
            """INSERT INTO exclusao (etapa, entidade, entidade_id, motivo, detalhe, regra_versao, criada_em)
               VALUES ('extrair', 'item', ?, ?, ?, ?, ?)""",
            (item_id, f"enem_{motivo}", f"ENEM {cas.ano} {cas.area} q{numero} CO_ITEM {linha['CO_ITEM']}", VERSAO,
             dbm.agora()))
    return questao_id


def _param(conexao, cas: Casamento, numero: int, linha: dict, texto, motivos, questao_id) -> None:
    conexao.execute(
        """INSERT INTO item_param_inep (ano, area, numero, co_posicao, co_item, co_prova, tx_cor, tp_lingua,
               co_habilidade, gabarito, param_a, param_b, param_c, in_item_aban, in_item_adaptado, tem_imagem,
               com_descricao, casamento_divergente, fonte_texto, conjunto, motivos, questao_id, versao)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cas.ano, cas.area, numero, int(linha["CO_POSICAO"]), int(linha["CO_ITEM"]), cas.co_prova, linha["TX_COR"],
         _inteiro(linha["TP_LINGUA"]), _inteiro(linha["CO_HABILIDADE"]), linha["TX_GABARITO"],
         _real(linha["NU_PARAM_A"]), _real(linha["NU_PARAM_B"]), _real(linha["NU_PARAM_C"]),
         _inteiro(linha["IN_ITEM_ABAN"]), _inteiro(linha["IN_ITEM_ADAPTADO"]),
         int(bool(texto and texto.tem_imagem)), int(bool(texto and texto.com_descricao)),
         int(numero in cas.divergentes), texto.fonte if texto else None, conjunto_do_ano(cas.ano),
         ",".join(motivos) or None, questao_id, VERSAO))


def ingerir_ano(conexao: sqlite3.Connection, raiz: Path, ano: int) -> dict:
    """Grava um ano inteiro numa transação. Ano já ingerido não é tocado (ids estáveis)."""
    garantir_tabela(conexao)
    popular_disciplinas(conexao)
    if conexao.execute("SELECT 1 FROM item_param_inep WHERE ano = ? LIMIT 1", (ano,)).fetchone():
        return {"ano": ano, "situacao": "ja_ingerido"}
    itens = fontes.ler_itens_inep(raiz / "inep" / str(ano) / f"ITENS_PROVA_{ano}.csv")
    textos = fontes.ler_textos(raiz, ano)
    casamentos = [casar(itens, textos, ano, area) for area in AREAS]
    resumo: dict = {"ano": ano, "conjunto": conjunto_do_ano(ano), "situacao": "ingerido", "areas": {}}
    with dbm.transacao(conexao):
        concurso_id = _concurso(conexao, ano)
        for cas in casamentos:
            com_texto = [n for n in cas.linhas if n in textos]
            caderno_id = _caderno(conexao, concurso_id, cas, len(com_texto))
            contagem: Counter = Counter()
            for numero in sorted(cas.linhas):
                linha, texto = cas.linhas[numero], textos.get(numero)
                motivos = motivos_do_item(linha, texto)
                questao_id = _questao(conexao, caderno_id, cas, numero, linha, texto, motivos) if texto else None
                _param(conexao, cas, numero, linha, texto, motivos, questao_id)
                contagem[motivos[0] if motivos else "entra"] += 1
                contagem["com_descricao"] += int(not motivos and texto.com_descricao)
            resumo["areas"][cas.area] = {"co_prova": cas.co_prova, "cor": cas.tx_cor, "candidatos": cas.candidatos,
                                         "posicoes_comparadas": cas.posicoes_comparadas,
                                         "divergentes": sorted(cas.divergentes), **dict(contagem)}
        conexao.execute(
            "INSERT INTO auditoria (entidade, entidade_id, evento, detalhe, criada_em) VALUES (?,?,?,?,?)",
            ("ingestao_enem", ano, VERSAO, json.dumps({"resumo": resumo, "fontes": fontes.impressoes(raiz)}),
             dbm.agora()))
    return resumo


# ------------------------------------------------------------------ amostras congeladas

def _congelar(conexao, nome: str, criterio: str, estratos: dict[str, list[int]]) -> dict:
    if conexao.execute("SELECT 1 FROM amostra WHERE nome = ?", (nome,)).fetchone():
        return {"amostra": nome, "situacao": "ja_existe"}
    estratos = {e: ids for e, ids in estratos.items() if ids}
    if not estratos:
        raise ValueError(f"amostra {nome}: nenhuma questão")
    from iac.amostragem import _versao_codigo
    total = sum(len(v) for v in estratos.values())
    with dbm.transacao(conexao):
        amostra_id = conexao.execute(
            "INSERT INTO amostra (nome, semente, criterio, n_pedido, versao_codigo, criada_em) VALUES (?,?,?,?,?,?)",
            (nome, SEMENTE, criterio, total, _versao_codigo(), dbm.agora())).lastrowid
        conexao.executemany("INSERT INTO amostra_estrato (amostra_id, estrato, n_populacao, n_amostra) VALUES (?,?,?,?)",
                            [(amostra_id, e, len(ids), len(ids)) for e, ids in sorted(estratos.items())])
        conexao.executemany("INSERT INTO amostra_questao (amostra_id, questao_id, estrato) VALUES (?,?,?)",
                            [(amostra_id, q, e) for e, ids in sorted(estratos.items()) for q in sorted(ids)])
    return {"amostra": nome, "situacao": "criada", "questoes": total,
            "estratos": {e: len(v) for e, v in sorted(estratos.items())}}


def _pontes(conexao, anos: tuple[int, ...]) -> dict[str, list[int]]:
    estratos: dict[str, list[int]] = defaultdict(list)
    for area, ano, questao_id in conexao.execute(
            f"""SELECT p.area, p.ano, p.questao_id FROM item_param_inep p JOIN questao q ON q.id = p.questao_id
                WHERE p.motivos IS NULL AND q.elegivel = 1 AND p.ano IN ({','.join('?' for _ in anos)})""", anos):
        estratos[f"{BANCA}|{area}|{ano}"].append(questao_id)
    return estratos


def disciplinas_ancora() -> tuple[str, ...]:
    return tuple(d for area in ("LC", "MT", "CH") for d in DISCIPLINAS_DA_AREA[area])


def ancora_por_disciplina(conexao) -> list[sqlite3.Row]:
    """Questões da extensao_me nas disciplinas que o ENEM cobre, com e sem o filtro de nível médio."""
    marcas = ",".join("?" for _ in disciplinas_ancora())
    return conexao.execute(
        f"""SELECT q.disciplina, b.sigla, COUNT(*) AS todas, SUM(q.nivel_questao = 'medio') AS medio,
                   SUM(q.nivel_questao IS NULL) AS sem_nivel
            FROM amostra_questao aq JOIN amostra a ON a.id = aq.amostra_id JOIN questao q ON q.id = aq.questao_id
            JOIN item i ON i.id = q.item_canonico_id JOIN caderno ca ON ca.id = i.caderno_id
            JOIN concurso co ON co.id = ca.concurso_id JOIN banca b ON b.id = co.banca_id
            WHERE a.nome = ? AND q.elegivel = 1 AND q.disciplina IN ({marcas})
            GROUP BY 1, 2 ORDER BY 1, 2""", (AMOSTRA_ORIGEM_ANCORA, *disciplinas_ancora())).fetchall()


def _ancora(conexao) -> dict[str, list[int]]:
    marcas = ",".join("?" for _ in disciplinas_ancora())
    estratos: dict[str, list[int]] = defaultdict(list)
    for sigla, disciplina, questao_id in conexao.execute(
            f"""SELECT b.sigla, q.disciplina, q.id
                FROM amostra_questao aq JOIN amostra a ON a.id = aq.amostra_id JOIN questao q ON q.id = aq.questao_id
                JOIN item i ON i.id = q.item_canonico_id JOIN caderno ca ON ca.id = i.caderno_id
                JOIN concurso co ON co.id = ca.concurso_id JOIN banca b ON b.id = co.banca_id
                WHERE a.nome = ? AND q.elegivel = 1 AND q.nivel_questao = 'medio' AND q.disciplina IN ({marcas})""",
            (AMOSTRA_ORIGEM_ANCORA, *disciplinas_ancora())):
        estratos[f"{sigla}|{disciplina}"].append(questao_id)
    return estratos


def criar_amostras(conexao: sqlite3.Connection) -> list[dict]:
    """As três amostras são calculadas antes de gravar qualquer uma: ou todas têm questões, ou nenhuma é criada."""
    pedidos = [
        (AMOSTRA_DEV, f"pontes ENEM {ANOS_DEV} LC/CH/MT sem exclusão ({VERSAO}); censo", _pontes(conexao, ANOS_DEV)),
        (AMOSTRA_TESTE, f"pontes ENEM {ANOS_TESTE} LC/CH/MT sem exclusão ({VERSAO}); censo; b do INEP lacrado até a "
                        "trava", _pontes(conexao, ANOS_TESTE)),
        (AMOSTRA_ANCORA, f"{AMOSTRA_ORIGEM_ANCORA} ∩ nivel_questao='medio' ∩ disciplina em "
                         f"{list(disciplinas_ancora())}; censo", _ancora(conexao)),
    ]
    vazias = [nome for nome, _, estratos in pedidos if not any(estratos.values())
              and not conexao.execute("SELECT 1 FROM amostra WHERE nome = ?", (nome,)).fetchone()]
    if vazias:
        raise ValueError(f"amostras sem nenhuma questão: {vazias} (nada foi criado)")
    return [_congelar(conexao, nome, criterio, estratos) for nome, criterio, estratos in pedidos]


def ingerir(conexao: sqlite3.Connection, raiz: Path, anos: tuple[int, ...] = ANOS_DEV + ANOS_TESTE,
            baixar: bool = False) -> dict:
    if baixar:
        fontes.baixar(raiz, anos)
    anos_resumo = [ingerir_ano(conexao, raiz, ano) for ano in anos]
    return {"anos": anos_resumo, "amostras": criar_amostras(conexao)}


# ------------------------------------------------------------------ leitura (só desenvolvimento)

def b_inep_desenvolvimento(conexao: sqlite3.Connection, amostra: str = AMOSTRA_DEV) -> list[sqlite3.Row]:
    """Parâmetros do INEP das pontes de DESENVOLVIMENTO. O teste é recusado (só `psicometria.avaliar_teste`)."""
    if amostra != AMOSTRA_DEV:
        raise PermissionError("o b do INEP do conjunto de teste só é lido por psicometria.avaliar_teste (com trava)")
    return conexao.execute(
        """SELECT p.questao_id, p.area, p.ano, p.param_a, p.param_b, p.param_c FROM item_param_inep p
           JOIN amostra_questao aq ON aq.questao_id = p.questao_id JOIN amostra a ON a.id = aq.amostra_id
           WHERE a.nome = ? AND p.conjunto = 'dev' ORDER BY p.questao_id""", (AMOSTRA_DEV,)).fetchall()


def resumo(conexao: sqlite3.Connection) -> dict:
    """Contagens por ano, conjunto, área e motivo (nenhum valor de b é lido)."""
    garantir_tabela(conexao)
    tabela: dict = defaultdict(lambda: defaultdict(Counter))
    for ano, conjunto, area, motivos, com_descricao, divergente in conexao.execute(
            "SELECT ano, conjunto, area, motivos, com_descricao, casamento_divergente FROM item_param_inep"):
        celula = tabela[f"{ano} ({conjunto})"][area]
        celula["posicoes"] += 1
        celula[(motivos or "entra").split(",")[0]] += 1
        celula["com_descricao_entra"] += int(bool(com_descricao) and not motivos)
        celula["casamento_divergente_entra"] += int(bool(divergente) and not motivos)
    return {k: {a: dict(c) for a, c in sorted(v.items())} for k, v in sorted(tabela.items())}
