"""Medida D, gêmeos de candidatos reais (docs/PREREGISTRO-ADENDO-ENEM.md, §7, emenda 3).

Um gêmeo é um candidato real do ENEM 2022: o modelo recebe a nota dele e, na condição `gemeo_perfil`, os acertos por
competência numa metade das pontes 2022 da área (a metade conhecida), e responde às pontes da outra metade (a
escondida). A resposta do gêmeo é comparada com a resposta real do candidato.

Este módulo faz três coisas:
- extração (`extrair`): lê os microdados de 2022 EM FLUXO de dentro do zip (o CSV de 1,5 GB não é extraído para o
  disco), sorteia 50 gêmeos por área (5 por decil de NU_NOTA entre os elegíveis), grava as respostas reais deles às
  pontes (ligadas por CO_ITEM), as metades e a taxa de acerto real de todos os elegíveis por decil e ponte (a curva
  real). Duas passadas no arquivo: a primeira conhece a distribuição das notas; a segunda tira só as linhas sorteadas;
- metades e perfil (`dividir_metades`, `perfil`, `texto_perfil`): o perfil que o executor põe no prompt;
- amostra do piloto (`criar_amostra_piloto`): as pontes escondidas da dobra 1 (metade B) de LC e MT.

Nada aqui lê o b do INEP. As tabelas são aditivas (criadas aqui, como `item_param_inep`), sem migração.
"""

from __future__ import annotations

import csv
import io
import zipfile
from array import array
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from iac import db as dbm
from iac import enem, turma

SEMENTE = 20261015
ANO = 2022
N_DECIS, POR_DECIL = 10, 5
N_GEMEOS = N_DECIS * POR_DECIL
AREAS = enem.AREAS
INDICE_DA_AREA = {"LC": 0, "CH": 1, "MT": 2}
INICIO_DA_POSICAO = {"LC": 1, "CH": 46, "MT": 136}     # CO_POSICAO da 1ª questão da área no ITENS_PROVA 2022
N_RESPOSTAS = 45
CONDICAO_PERFIL, CONDICAO_SO_THETA, CONDICAO_EMBARALHADO = "gemeo_perfil", "gemeo_so_theta", "gemeo_embaralhado"
CONDICOES = (CONDICAO_PERFIL, CONDICAO_SO_THETA, CONDICAO_EMBARALHADO)
AMOSTRA_PILOTO = "gemeos_piloto"
AREAS_PILOTO = ("LC", "MT")
METADE_ESCONDIDA_PILOTO = "B"              # dobra 1: conhecida = A, escondida = B
VERSAO = "gemeos-1"
MEMBRO_MICRODADOS = "DADOS/MICRODADOS_ENEM_2022.csv"
MEMBRO_ITENS = "DADOS/ITENS_PROVA_2022.csv"
BINS_NOTA = 10_001                         # NU_NOTA tem uma casa decimal, de 0 a 1000: bin = nota × 10
LOTE = 100_000

# Matriz de Referência do ENEM (matriz_referencia_enem.pdf, conferida em 22/10/2026): habilidades de cada competência
COMPETENCIAS = {
    "LC": ((1, 4), (5, 8), (9, 11), (12, 14), (15, 17), (18, 20), (21, 24), (25, 27), (28, 30)),
    "MT": ((1, 5), (6, 9), (10, 14), (15, 18), (19, 23), (24, 26), (27, 30)),
    "CH": ((1, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 30)),
}
ROTULOS = {   # abreviação dos títulos das competências da matriz (adendo §7.2)
    "LC": ("tecnologias da comunicação", "língua estrangeira", "linguagem corporal", "arte", "texto literário",
           "sistemas simbólicos das linguagens", "argumentação e pontos de vista", "língua portuguesa",
           "impacto das tecnologias"),
    "MT": ("números", "geometria", "grandezas e medidas", "variação de grandezas", "álgebra", "gráficos e tabelas",
           "estatística e probabilidade"),
    "CH": ("cultura e identidades", "espaço geográfico e poder", "instituições e movimentos sociais",
           "técnica e tecnologia", "cidadania e democracia", "sociedade e natureza"),
}
ABERTURA_PERFIL = " Em outras questões dessa mesma prova, por competência da Matriz de Referência do ENEM, você acertou: "

ESQUEMA = """
CREATE TABLE IF NOT EXISTS gemeo_ponte (
    questao_id   INTEGER PRIMARY KEY REFERENCES questao(id),
    area         TEXT NOT NULL CHECK (area IN ('LC','CH','MT')),
    co_item      INTEGER NOT NULL,
    tp_lingua    INTEGER,                         -- 0 = inglês (LC, posições 1–5); NULL = comum
    habilidade   INTEGER NOT NULL,
    competencia  INTEGER NOT NULL,
    metade       TEXT NOT NULL CHECK (metade IN ('A','B')),
    versao       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gemeo_decil (
    area         TEXT NOT NULL,
    decil        INTEGER NOT NULL CHECK (decil BETWEEN 1 AND 10),
    nota_min     REAL NOT NULL,                   -- limite inferior (exclusivo, menos no decil 1)
    nota_max     REAL NOT NULL,                   -- limite superior (inclusivo)
    n_elegiveis  INTEGER NOT NULL,
    PRIMARY KEY (area, decil)
);
CREATE TABLE IF NOT EXISTS gemeo (
    id            INTEGER PRIMARY KEY,
    area          TEXT NOT NULL CHECK (area IN ('LC','CH','MT')),
    indice        INTEGER NOT NULL CHECK (indice BETWEEN 1 AND 50),   -- = repetição da condição
    decil         INTEGER NOT NULL CHECK (decil BETWEEN 1 AND 10),
    par_indice    INTEGER NOT NULL,               -- de quem vem o perfil em gemeo_embaralhado (mesmo decil)
    nu_inscricao  TEXT NOT NULL,
    linha_csv     INTEGER NOT NULL,
    nu_nota       REAL NOT NULL,
    co_prova      INTEGER NOT NULL,
    tp_lingua     INTEGER,
    versao        TEXT NOT NULL,
    UNIQUE (area, indice)
);
CREATE TABLE IF NOT EXISTS gemeo_resposta (
    gemeo_id     INTEGER NOT NULL REFERENCES gemeo(id),
    questao_id   INTEGER NOT NULL REFERENCES questao(id),
    letra        TEXT NOT NULL,                   -- o caractere de TX_RESPOSTAS (branco/dupla marcação = erro)
    acertou      INTEGER NOT NULL CHECK (acertou IN (0,1)),
    PRIMARY KEY (gemeo_id, questao_id)
);
CREATE TABLE IF NOT EXISTS gemeo_curva (
    area         TEXT NOT NULL,
    decil        INTEGER NOT NULL,
    questao_id   INTEGER NOT NULL REFERENCES questao(id),
    n            INTEGER NOT NULL,                -- elegíveis do decil que fizeram o item
    acertos      INTEGER NOT NULL,
    PRIMARY KEY (area, decil, questao_id)
);
CREATE TABLE IF NOT EXISTS gemeo_fonte (
    chave        TEXT PRIMARY KEY,
    valor        TEXT NOT NULL
);
"""


def garantir_tabelas(conexao) -> None:
    conexao.executescript(ESQUEMA)


# ------------------------------------------------------------------ regras puras (testadas sem microdados)

def competencia(area: str, habilidade: int) -> int:
    for numero, (inicio, fim) in enumerate(COMPETENCIAS[area], start=1):
        if inicio <= habilidade <= fim:
            return numero
    raise ValueError(f"habilidade {habilidade} fora da matriz de {area}")


def dividir_metades(pontes: list[tuple[int, int]], area: str) -> dict[int, str]:
    """Metades A e B estratificadas por competência (adendo §7.2). `pontes`: (questao_id, competência)."""
    gerador = np.random.default_rng([SEMENTE, 100 + INDICE_DA_AREA[area]])
    ordem: list[int] = []
    for comp in sorted({c for _, c in pontes}):
        ids = sorted(q for q, c in pontes if c == comp)
        ordem += [ids[k] for k in gerador.permutation(len(ids))]
    return {q: "AB"[k % 2] for k, q in enumerate(ordem)}


def cortes_decis(notas) -> np.ndarray:
    return np.quantile(np.asarray(notas, float), np.linspace(0, 1, N_DECIS + 1))


def decil(notas, cortes) -> np.ndarray:
    """Decil k = (q_{k−1}, q_k]; o primeiro inclui o mínimo."""
    return np.searchsorted(np.asarray(cortes)[1:-1], np.asarray(notas, float), side="left") + 1


def sortear(decis: np.ndarray, area: str) -> list[tuple[int, int]]:
    """5 posições por decil, sem reposição, na ordem do sorteio: [(decil, posição no vetor de elegíveis)]."""
    gerador = np.random.default_rng([SEMENTE, INDICE_DA_AREA[area]])
    escolhidos = []
    for d in range(1, N_DECIS + 1):
        posicoes = np.flatnonzero(decis == d)
        if posicoes.size < POR_DECIL:
            raise ValueError(f"{area}: decil {d} com {posicoes.size} elegíveis (< {POR_DECIL})")
        escolhidos += [(d, int(p)) for p in gerador.choice(posicoes, POR_DECIL, replace=False)]
    return escolhidos


def pares_embaralhados(area: str) -> dict[int, int]:
    """Índice do gêmeo → índice de quem empresta o perfil em gemeo_embaralhado (mesmo decil, nunca o próprio)."""
    gerador = np.random.default_rng([SEMENTE, 200 + INDICE_DA_AREA[area]])
    par = {}
    for d in range(N_DECIS):
        indices = [d * POR_DECIL + k + 1 for k in range(POR_DECIL)]
        ordem = [indices[k] for k in gerador.permutation(POR_DECIL)]
        par.update({ordem[k]: ordem[(k + 1) % POR_DECIL] for k in range(POR_DECIL)})
    return par


def nota_inteira(nota: float) -> int:
    return int(np.floor(nota + 0.5))


def texto_perfil(area: str, contagens: dict[int, tuple[int, int]]) -> str:
    """`contagens`: competência → (acertos, total) na metade conhecida. Competência sem item conhecido fica de fora."""
    partes = [f"{ROTULOS[area][c - 1]}, {a} de {t}" for c, (a, t) in sorted(contagens.items()) if t > 0]
    return ABERTURA_PERFIL + "; ".join(partes) + "." if partes else ""


# ------------------------------------------------------------------ pontes e cadernos

@dataclass(frozen=True)
class Ponte:
    questao_id: int
    area: str
    co_item: int
    tp_lingua: int | None
    habilidade: int


def pontes_2022(conexao) -> dict[str, list[Ponte]]:
    """Pontes 2022 do `enem_dev`, por área, em ordem de questao_id. Não lê parâmetro nenhum."""
    linhas = conexao.execute(
        """SELECT p.questao_id, p.area, p.co_item, p.tp_lingua, p.co_habilidade FROM item_param_inep p
           JOIN amostra_questao aq ON aq.questao_id = p.questao_id JOIN amostra a ON a.id = aq.amostra_id
           WHERE a.nome = ? AND p.ano = ? ORDER BY p.questao_id""", (enem.AMOSTRA_DEV, ANO)).fetchall()
    pontes: dict[str, list[Ponte]] = defaultdict(list)
    for q, area, co_item, lingua, hab in linhas:
        if area in AREAS:
            pontes[area].append(Ponte(q, area, int(co_item), None if lingua in (None, "") else int(lingua), int(hab)))
    return dict(pontes)


@dataclass(frozen=True)
class Gabarito:
    colunas: np.ndarray     # índice da ponte (na lista da área)
    posicoes: np.ndarray    # índice em TX_RESPOSTAS (0..44)
    letras: np.ndarray      # gabarito (uint8)


def mapa_cadernos(itens: list[dict], pontes: list[Ponte], area: str) -> dict[tuple[int, str], Gabarito]:
    """(CO_PROVA, TP_LINGUA do candidato) → onde está cada ponte no caderno. Só cadernos que contêm, sem item
    adaptado, todas as pontes da área (adendo §7.1). Quem fez espanhol não tem as pontes de inglês."""
    por_prova: dict[int, dict[int, dict]] = defaultdict(dict)
    for linha in itens:
        if linha["SG_AREA"] == area:
            por_prova[int(linha["CO_PROVA"])][int(linha["CO_ITEM"])] = linha
    mapa = {}
    for prova, por_item in por_prova.items():
        if any(p.co_item not in por_item or por_item[p.co_item]["IN_ITEM_ADAPTADO"] == "1" for p in pontes):
            continue
        for lingua in ("0", "1"):     # o item vale para quem fez a língua da linha dele (vazio = comum a todos)
            usa = [k for k, p in enumerate(pontes) if por_item[p.co_item]["TP_LINGUA"] in ("", lingua)]
            linhas = [por_item[pontes[k].co_item] for k in usa]
            mapa[(prova, lingua)] = Gabarito(
                np.array(usa, dtype=int),
                np.array([int(l["CO_POSICAO"]) - INICIO_DA_POSICAO[area] for l in linhas], dtype=int),
                np.frombuffer("".join(l["TX_GABARITO"] for l in linhas).encode("latin1"), dtype=np.uint8).copy())
    return mapa


# ------------------------------------------------------------------ leitura em fluxo

def _ler_itens(arquivo_zip: zipfile.ZipFile) -> list[dict]:
    with arquivo_zip.open(MEMBRO_ITENS) as bruto:
        return list(csv.DictReader(io.TextIOWrapper(bruto, encoding="latin1", newline=""), delimiter=";"))


def _linhas(arquivo_zip: zipfile.ZipFile):
    """Linhas do CSV dos microdados, lidas em fluxo de dentro do zip (nada é extraído para o disco)."""
    with arquivo_zip.open(MEMBRO_MICRODADOS) as bruto:
        leitor = csv.reader(io.TextIOWrapper(bruto, encoding="latin1", newline=""), delimiter=";")
        cabecalho = next(leitor)
        yield {nome: i for i, nome in enumerate(cabecalho)}
        yield from leitor


@dataclass
class _Area:
    """Estado da primeira passada numa área."""
    mapa: dict
    k: int
    linhas: array
    notas: array
    n: np.ndarray
    acertos: np.ndarray
    lote: list

    @classmethod
    def nova(cls, mapa, k):
        return cls(mapa, k, array("q"), array("d"), np.zeros((BINS_NOTA, k), np.int64),
                   np.zeros((BINS_NOTA, k), np.int64), [])


def _campos(col: dict, linha: list[str], area: str) -> tuple | None:
    """(nota, co_prova, lingua, respostas) se a pessoa é elegível na área (sem olhar o caderno), senão None."""
    if linha[col[f"TP_PRESENCA_{area}"]] != "1":
        return None
    nota, prova, respostas = (linha[col[f"NU_NOTA_{area}"]], linha[col[f"CO_PROVA_{area}"]],
                              linha[col[f"TX_RESPOSTAS_{area}"]])
    if not nota or not prova or len(respostas) != N_RESPOSTAS:
        return None
    return float(nota), int(prova), linha[col["TP_LINGUA"]] if area == "LC" else "0", respostas


def _acumular(estado: _Area) -> None:
    """Soma, por bin de nota e ponte, quantos elegíveis fizeram o item e quantos acertaram (vetorizado)."""
    grupos: dict = defaultdict(list)
    for bin_, prova, lingua, respostas in estado.lote:
        grupos[(prova, lingua)].append((bin_, respostas))
    for chave, membros in grupos.items():
        gab = estado.mapa[chave]
        bins = np.fromiter((b for b, _ in membros), dtype=np.int64, count=len(membros))
        r = np.frombuffer("".join(s for _, s in membros).encode("latin1"), dtype=np.uint8).reshape(-1, N_RESPOSTAS)
        acerto = (r[:, gab.posicoes] == gab.letras[None, :]).astype(np.int64)
        indice = (bins[:, None], gab.colunas[None, :])
        np.add.at(estado.n, indice, 1)
        np.add.at(estado.acertos, indice, acerto)
    estado.lote.clear()


def _primeira_passada(arquivo_zip, mapas: dict[str, dict], pontes: dict[str, list[Ponte]]) -> dict[str, _Area]:
    estados = {a: _Area.nova(mapas[a], len(pontes[a])) for a in mapas}
    linhas = _linhas(arquivo_zip)
    col = next(linhas)
    for numero, linha in enumerate(linhas, start=1):
        for area, estado in estados.items():
            campos = _campos(col, linha, area)
            if campos is None or (campos[1], campos[2]) not in estado.mapa:
                continue
            nota, prova, lingua, respostas = campos
            estado.linhas.append(numero)
            estado.notas.append(nota)
            estado.lote.append((int(round(nota * 10)), prova, lingua, respostas))
            if len(estado.lote) >= LOTE:
                _acumular(estado)
    for estado in estados.values():
        _acumular(estado)
    return estados


def _segunda_passada(arquivo_zip, escolhidas: dict[int, list[tuple[str, int]]]) -> dict[tuple[str, int], dict]:
    """Só as linhas sorteadas: (área, índice do gêmeo) → campos da pessoa."""
    achadas = {}
    linhas = _linhas(arquivo_zip)
    col = next(linhas)
    for numero, linha in enumerate(linhas, start=1):
        for area, indice in escolhidas.get(numero, ()):
            nota, prova, lingua, respostas = _campos(col, linha, area)
            achadas[(area, indice)] = {"nu_inscricao": linha[col["NU_INSCRICAO"]], "linha_csv": numero, "nu_nota": nota,
                                       "co_prova": prova, "tp_lingua": int(linha[col["TP_LINGUA"]] or -1),
                                       "lingua": lingua, "respostas": respostas}
    return achadas


# ------------------------------------------------------------------ extração

def _curva(estado: _Area, cortes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Agrega os bins de nota em decis: (10 × pontes) de n e de acertos."""
    d = decil(np.arange(BINS_NOTA) / 10, cortes) - 1
    n, acertos = np.zeros((N_DECIS, estado.k), np.int64), np.zeros((N_DECIS, estado.k), np.int64)
    np.add.at(n, d, estado.n)
    np.add.at(acertos, d, estado.acertos)
    return n, acertos


def _sortear_areas(estados: dict[str, _Area]) -> tuple[dict, dict, dict]:
    cortes, decis_por_area, escolhidas = {}, {}, defaultdict(list)
    for area, estado in estados.items():
        notas = np.frombuffer(estado.notas, dtype=np.float64)
        cortes[area] = cortes_decis(notas)
        decis_por_area[area] = decil(notas, cortes[area])
        for indice, (_, posicao) in enumerate(sortear(decis_por_area[area], area), start=1):
            escolhidas[estado.linhas[posicao]].append((area, indice))
    return cortes, decis_por_area, escolhidas


def extrair(conexao, caminho_zip: Path, areas: tuple[str, ...] = AREAS, log=print) -> dict:
    """Sorteia e grava os gêmeos (adendo §7.1). Idempotente: se já houver gêmeos, não refaz nada."""
    garantir_tabelas(conexao)
    if conexao.execute("SELECT 1 FROM gemeo LIMIT 1").fetchone():
        return {"situacao": "ja_extraido"}
    pontes = pontes_2022(conexao)
    with zipfile.ZipFile(caminho_zip) as arquivo_zip:
        itens = _ler_itens(arquivo_zip)
        mapas = {a: mapa_cadernos(itens, pontes[a], a) for a in areas}
        info = arquivo_zip.getinfo(MEMBRO_MICRODADOS)
        log("gemeos: primeira passada (distribuição das notas e curva real)")
        estados = _primeira_passada(arquivo_zip, mapas, pontes)
        cortes, decis_por_area, escolhidas = _sortear_areas(estados)
        log("gemeos: segunda passada (linhas sorteadas)")
        achadas = _segunda_passada(arquivo_zip, escolhidas)
    resumo = _gravar(conexao, areas, pontes, mapas, estados, cortes, decis_por_area, achadas)
    with dbm.transacao(conexao):
        conexao.executemany("INSERT OR REPLACE INTO gemeo_fonte (chave, valor) VALUES (?, ?)", [
            ("zip", Path(caminho_zip).name), ("membro", MEMBRO_MICRODADOS), ("crc32", f"{info.CRC:08x}"),
            ("tamanho", str(info.file_size)), ("versao", VERSAO), ("extraido_em", dbm.agora())])
    return resumo


def _gravar(conexao, areas, pontes, mapas, estados, cortes, decis_por_area, achadas) -> dict:
    resumo = {"situacao": "extraido", "areas": {}}
    with dbm.transacao(conexao):
        for area in areas:
            lista, estado = pontes[area], estados[area]
            comp = {p.questao_id: competencia(area, p.habilidade) for p in lista}
            metade = dividir_metades(list(comp.items()), area)
            conexao.executemany(
                """INSERT INTO gemeo_ponte (questao_id, area, co_item, tp_lingua, habilidade, competencia, metade,
                   versao) VALUES (?,?,?,?,?,?,?,?)""",
                [(p.questao_id, area, p.co_item, p.tp_lingua, p.habilidade, comp[p.questao_id], metade[p.questao_id],
                  VERSAO) for p in lista])
            c = cortes[area]
            contagem = np.bincount(decis_por_area[area], minlength=N_DECIS + 1)[1:]
            conexao.executemany("INSERT INTO gemeo_decil VALUES (?,?,?,?,?)",
                                [(area, d + 1, float(c[d]), float(c[d + 1]), int(contagem[d])) for d in range(N_DECIS)])
            n, acertos = _curva(estado, c)
            conexao.executemany("INSERT INTO gemeo_curva VALUES (?,?,?,?,?)",
                                [(area, d + 1, p.questao_id, int(n[d, j]), int(acertos[d, j]))
                                 for d in range(N_DECIS) for j, p in enumerate(lista)])
            par = pares_embaralhados(area)
            for indice in range(1, N_GEMEOS + 1):
                g = achadas[(area, indice)]
                d = (indice - 1) // POR_DECIL + 1
                if decil([g["nu_nota"]], c)[0] != d:
                    raise RuntimeError(f"{area} gêmeo {indice}: a nota da segunda passada não cai no decil {d}")
                gemeo_id = conexao.execute(
                    """INSERT INTO gemeo (area, indice, decil, par_indice, nu_inscricao, linha_csv, nu_nota, co_prova,
                       tp_lingua, versao) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (area, indice, d, par[indice], g["nu_inscricao"], g["linha_csv"], g["nu_nota"], g["co_prova"],
                     g["tp_lingua"], VERSAO)).lastrowid
                gab = mapas[area][(g["co_prova"], g["lingua"])]
                conexao.executemany(
                    "INSERT INTO gemeo_resposta (gemeo_id, questao_id, letra, acertou) VALUES (?,?,?,?)",
                    [(gemeo_id, lista[j].questao_id, g["respostas"][pos], int(g["respostas"][pos] == chr(letra)))
                     for j, pos, letra in zip(gab.colunas, gab.posicoes, gab.letras)])
            resumo["areas"][area] = {"elegiveis": int(contagem.sum()), "pontes": len(lista),
                                     "metades": {m: sum(1 for v in metade.values() if v == m) for m in "AB"},
                                     "cortes": [round(float(x), 1) for x in c]}
    return resumo


# ------------------------------------------------------------------ amostra do piloto

def criar_amostra_piloto(conexao) -> dict:
    """Pontes escondidas da dobra 1 (metade B) de LC e MT. Congelada: se já existe, nada muda."""
    if conexao.execute("SELECT 1 FROM amostra WHERE nome = ?", (AMOSTRA_PILOTO,)).fetchone():
        return {"amostra": AMOSTRA_PILOTO, "situacao": "ja_existe"}
    estratos = {a: [q for (q,) in conexao.execute(
        "SELECT questao_id FROM gemeo_ponte WHERE area = ? AND metade = ? ORDER BY questao_id",
        (a, METADE_ESCONDIDA_PILOTO))] for a in AREAS_PILOTO}
    if not all(estratos.values()):
        raise ValueError("gemeos: rode a extração antes de criar a amostra do piloto")
    from iac.amostragem import _versao_codigo
    with dbm.transacao(conexao):
        amostra_id = conexao.execute(
            "INSERT INTO amostra (nome, semente, criterio, n_pedido, versao_codigo, criada_em) VALUES (?,?,?,?,?,?)",
            (AMOSTRA_PILOTO, SEMENTE, "medida D, piloto: pontes 2022 da metade B (escondida na dobra 1), LC e MT",
             sum(map(len, estratos.values())), _versao_codigo(), dbm.agora())).lastrowid
        conexao.executemany("INSERT INTO amostra_estrato VALUES (?,?,?,?)",
                            [(amostra_id, a, len(ids), len(ids)) for a, ids in estratos.items()])
        conexao.executemany("INSERT INTO amostra_questao (amostra_id, questao_id, estrato) VALUES (?,?,?)",
                            [(amostra_id, q, a) for a, ids in estratos.items() for q in ids])
    return {"amostra": AMOSTRA_PILOTO, "situacao": "criada", "questoes": {a: len(v) for a, v in estratos.items()}}


# ------------------------------------------------------------------ perfil (usado pelo executor)

def e_condicao_de_gemeo(slug: str) -> bool:
    return slug in CONDICOES


def _contagens(conexao, gemeo_id: int, conhecidas: str, area: str) -> dict[int, tuple[int, int]]:
    filtro, args = ("AND p.metade = ?", (conhecidas,)) if conhecidas in ("A", "B") else ("", ())
    linhas = conexao.execute(
        f"""SELECT p.competencia, SUM(r.acertou), COUNT(*) FROM gemeo_resposta r
            JOIN gemeo_ponte p ON p.questao_id = r.questao_id
            WHERE r.gemeo_id = ? AND p.area = ? {filtro} GROUP BY 1""", (gemeo_id, area, *args)).fetchall()
    return {c: (int(a), int(t)) for c, a, t in linhas}


def perfil(conexao, condicao: str, questao_id: int, repeticao: int) -> dict | None:
    """Campos do prompt do gêmeo `repeticao` para a questão. None = a pessoa não fez esse item (espanhol em LC):
    a questão não é perguntada ao gêmeo dela. Ponte 2022: perfil na OUTRA metade; fora de 2022 (estudo completo,
    teste), perfil em todas as pontes 2022 da área (adendo §7.4)."""
    if condicao not in CONDICOES:
        raise ValueError(f"condição {condicao} não é de gêmeo")
    ponte = conexao.execute("SELECT area, metade FROM gemeo_ponte WHERE questao_id = ?", (questao_id,)).fetchone()
    area = ponte[0] if ponte else turma.area_da_questao(conexao, questao_id)
    gemeo = conexao.execute("SELECT id, nu_nota, par_indice FROM gemeo WHERE area = ? AND indice = ?",
                            (area, repeticao)).fetchone()
    if gemeo is None:
        raise ValueError(f"gêmeo {repeticao} de {area} não existe (rode `iac gemeos extrair`)")
    if ponte and not conexao.execute("SELECT 1 FROM gemeo_resposta WHERE gemeo_id = ? AND questao_id = ?",
                                     (gemeo[0], questao_id)).fetchone():
        return None
    conhecidas = ("B" if ponte[1] == "A" else "A") if ponte else "todas"
    fonte = gemeo[0]
    if condicao == CONDICAO_EMBARALHADO:
        fonte = conexao.execute("SELECT id FROM gemeo WHERE area = ? AND indice = ?", (area, gemeo[2])).fetchone()[0]
    texto = "" if condicao == CONDICAO_SO_THETA else texto_perfil(area, _contagens(conexao, fonte, conhecidas, area))
    return {"gemeo": repeticao, "area": area, "area_nome": turma.NOME_DA_AREA[area], "nota": nota_inteira(gemeo[1]),
            "perfil": texto, "conhecidas": conhecidas,
            "perfil_de": repeticao if condicao != CONDICAO_EMBARALHADO else gemeo[2]}
