"""Segmentação de um caderno de prova em itens (questões), com validação.

Comum a todas as bancas; o que muda por banca fica em `PerfilExtracao`.

Filosofia: preferimos PERDER uma questão a guardar uma questão errada. Cada item carrega a lista
de `problemas` detectados, e o caderno inteiro vai para quarentena se a estrutura não fechar
(em especial: nº de itens diferente do nº informado na capa).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from iac import texto as tx
from iac.disciplinas import eh_titulo_de_secao
from iac.pdf import MARCADOR_IMAGEM, MARCADOR_SIMBOLO, sem_destaques, tem_destaque

VERSAO_EXTRATOR = "2.4"

# Problemas de item (cada um torna a questão inelegível)
P_IMAGEM = "imagem"
P_ALTERNATIVAS = "alternativas_incompletas"
P_DESTAQUE_PERDIDO = "destaque_perdido"
P_TEXTO_AUSENTE = "texto_ausente"
P_ALTERNATIVA_LONGA = "alternativa_suspeita"
P_SIMBOLO = "simbolo_desconhecido"
P_ENUNCIADO_VAZIO = "enunciado_vazio"   # rede de segurança: a divisão engoliu o enunciado
P_LIXO_PAGINA = "lixo_de_pagina"          # rodapé/rascunho vazou para o texto

_REFERE_DESTAQUE = re.compile(r"\b(destacad|em destaque|sublinhad|grifad|negrit|italic|underlined|in bold)")
_REFERE_FIGURA = re.compile(r"\b(figura|imagem|grafico|ilustra|mapa|diagrama|charge|tirinha|cartum)")
# Referência FORTE a um texto de apoio externo ("no texto", "o texto 2", "3º parágrafo").
# "o texto", "os textos" sozinhos ficam de fora: costumam ser genéricos ("assinale o texto que...").
_REFERE_TEXTO = re.compile(
    r"\b(do texto|no texto|ao texto|pelo texto|o texto (acima|anterior|[ivx\d]+)\b|texto [ivx\d]+\b|textos [ivx\d]+ e|"
    r"do trecho do texto|o fragmento do texto|o autor do texto|paragrafo|linhas? \d+|situacao hipotetica|"
    r"texto [a-z0-9]+-[a-z0-9]+|"
    r"the text|in the text|text [ivx\d]+\b|the passage|the author|paragraph)"
)
# Conteúdo embutido no próprio enunciado: "o código exibido a seguir", "o trecho seguinte"
_EMBUTIDO = re.compile(r"\b(texto|trecho|fragmento|passagem|poema|excerto|codigo|frase|periodo)\b.{0,40}?"
                       r"\b(a seguir|seguinte|abaixo)\b|\b(seguinte|following)\s+(texto|trecho|fragmento|text|passage)\b")
_CITACAO_LONGA = re.compile(r"[“\"][^”\"]{80,}[”\"]")
MIN_CONTEUDO_EMBUTIDO = 150   # caracteres depois da expressão para aceitar que o texto está embutido
MIN_ENUNCIADO_COM_TEXTO = 500  # enunciado desse tamanho já traz o próprio texto ("Na reunião... no texto")

# Início de texto de apoio (só no começo de linha e depois de frase terminada)
_INICIO_TEXTO_BASE = re.compile(
    r"^\s*(texto\s*[ivx\d]+\b|texto\s*$|leia o texto|leia os textos|leia o fragmento|leia o trecho|"
    r"leia a (charge|tirinha|cronica|noticia|reportagem|letra|carta)|leia o poema|considere o texto|"
    r"o texto a seguir|os textos a seguir|as questoes de n|para responder [aàs]s? quest|"
    r"use the following|read the (following )?text|text\s*[ivx\d]+\s*$|"
    r"texto\s+[a-z0-9]+(-[a-z0-9]+)+\s*$|situacao hipotetica)"   # Cebraspe: "Texto 1A3-I", "Situação hipotética 1A3-I"
)
_ROTULO_APOIO = re.compile(r"^\s*(texto\s+[a-z0-9]+(-[a-z0-9]+)+\s*$|situacao hipotetica\b|texto\s*[ivx\d]+\s*$|"
                           r"use the following|read the (following )?text|leia o texto|leia os textos|leia o fragmento)")
# Lixo de página: a LINHA inteira é rodapé ("Realização", "RASCUNHO", "Redação 1 5 10 15").
# Só a linha isolada: "realização" no meio de uma frase é palavra comum em prova.
_LIXO_PAGINA = re.compile(r"^\s*(realizacao|rascunho\b.*|folha de respostas\b.*|redacao(\s+\d+)+|nao destaque\b.*)\s*$")
_QTD_NA_CAPA = re.compile(r"contendo\s+(?:as\s+)?(\d{1,3}|[a-z ]{4,30}?)\s*(?:\([a-z \-]+\)\s*)?quest(?:oes|ao)")
_FIM_DE_FRASE = (".", "?", "!", ";", ")", "”", '"', ":")
_CODIGO = re.compile(r"(;\s*$|[{}]\s*$|^\s*(def|class|public|private|return|import|for|while|if)\b|"
                     r"^\s*(select|insert|update|delete|create)\s|</?\w+[^>]*>|^\s{2,}\S)", re.MULTILINE | re.IGNORECASE)

_NUMEROS = {"dez": 10, "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
            "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100, "cento": 100, "um": 1, "dois": 2,
            "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9, "duzentas": 200,
            "duzentos": 200, "cento e vinte": 120}


def numero_por_extenso(texto: str) -> int | None:
    texto = texto.strip()
    if texto.isdigit():
        return int(texto)
    total = 0
    for parte in re.split(r"\s+e\s+", texto):
        valor = _NUMEROS.get(parte.strip())
        if valor is None:
            return None
        total += valor
    return total or None


@dataclass(frozen=True)
class PerfilExtracao:
    """Como uma banca escreve suas provas."""
    inicio_questao: re.Pattern = re.compile(r"(?:^|\n)[ \t]*(\d{1,3})[ \t]*(?=\n)")
    alternativa: re.Pattern = re.compile(r"(?:^|\n)[ \t]*\(([a-e])\)[ \t]*")
    letras: str = "ABCDE"
    texto_minimo_entre_numeros: int = 12
    janela_primeira_alternativa: int = 4000
    # True: `alternativa` casa no texto ORIGINAL (maiúsculas importam: "A a opção" × "a" no meio da frase)
    alternativa_no_original: bool = False


@dataclass
class ItemExtraido:
    numero: int
    enunciado: str
    alternativas: dict[str, str]
    secao: str | None = None
    texto_base: str | None = None
    problemas: list[str] = field(default_factory=list)

    @property
    def tem_imagem(self) -> bool:
        return P_IMAGEM in self.problemas


@dataclass
class CadernoExtraido:
    itens: list[ItemExtraido]
    esperado: int | None
    status: str           # 'ok' | 'quarentena'
    motivo: str | None
    avisos: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ utilidades

def normalizar_bloco(texto: str) -> str:
    """Junta as quebras de linha do PDF, EXCETO em código (indentação e linhas importam)."""
    if len(_CODIGO.findall(texto)) >= 2:
        return "\n".join(l.rstrip() for l in texto.strip("\n").splitlines() if l.strip())
    return tx.colapsar_espacos(texto)


def tem_codigo(texto: str) -> bool:
    return len(_CODIGO.findall(texto)) >= 2


def esperado_da_capa(texto: str) -> int | None:
    m = _QTD_NA_CAPA.search(tx.norm_1a1(texto[:8000]))
    return numero_por_extenso(m.group(1)) if m else None


# ------------------------------------------------------------------ segmentação

Posicoes = dict[int, list[tuple[int, int]]]


def _cadeia_a_partir(inicio: int, fortes: Posicoes, fracos: Posicoes) -> tuple[list[tuple[int, int, int]], list[str]]:
    aceitos, avisos = [], []
    posicao_minima, numero = 0, inicio
    while True:
        escolha = next((p for p in fortes.get(numero, []) if p[0] >= posicao_minima), None)
        if escolha is None:
            escolha = next((p for p in fracos.get(numero, []) if p[0] >= posicao_minima), None)
            if escolha is not None:
                avisos.append(f"questão {numero} aceita por candidato fraco")
        if escolha is None:
            return aceitos, avisos
        aceitos.append((escolha[0], escolha[1], numero))
        posicao_minima = escolha[1]
        numero += 1


def cadeia_mais_longa(fortes: Posicoes, fracos: Posicoes | None = None) -> tuple[list[tuple[int, int, int]], list[str]]:
    """A sequência n, n+1, n+2... mais longa (empate: a que começa no menor n).

    Não exige começar no 1: o caderno de "Conhecimentos Específicos" da Cebraspe continua a numeração
    do de "Básicos" (itens 51 a 120). Em cada número usa o primeiro candidato forte depois do anterior;
    sem forte, o primeiro fraco.
    """
    fracos = fracos or {}
    melhor: tuple[list, list] = ([], [])
    # sem atalho "pular n se existe n-1": a posição importa (um "0" solto no fim do caderno não começa nada)
    for inicio in sorted(n for n in set(fortes) | set(fracos) if n >= 1):
        cadeia = _cadeia_a_partir(inicio, fortes, fracos)
        if len(cadeia[0]) > len(melhor[0]):
            melhor = cadeia
    aceitos, avisos = melhor
    if aceitos and aceitos[0][2] > 1:
        # começar no 51 é normal (Específicos); começar no 3 com uma "questão 1" logo antes é rótulo perdido
        # (revisão 3, M2): os itens iniciais sumiriam sem registro. Só olha os 3 números imediatamente anteriores,
        # porque a capa tem instruções numeradas (1, 2, 3...) bem antes do item 51.
        inicio, posicao = aceitos[0][2], aceitos[0][0]
        anteriores = [n for n in range(max(1, inicio - 3), inicio)
                      if any(p[0] < posicao for p in fortes.get(n, []) + fracos.get(n, []))]
        if anteriores:
            avisos = [*avisos, f"{AVISO_BURACO_INICIAL}: começa em {inicio}, mas há {anteriores} antes"]
    return aceitos, avisos


AVISO_BURACO_INICIAL = "sequência com buraco no início"


def _posicoes_de_questoes(norm: str, perfil: PerfilExtracao,
                          texto: str | None = None) -> tuple[list[tuple[int, int, int]], list[str]]:
    """Posições (início, fim, número) das questões, na sequência contínua mais longa.

    Dois níveis de candidato: "forte" (passa em todos os filtros) e "fraco" (tem alternativa logo
    à frente, mas parece coluna de tabela). Para cada número usamos o primeiro forte depois da
    questão anterior; sem forte, o primeiro fraco. Assim um "1" solto numa fração não trava a
    sequência e não faz sumir todas as questões seguintes.
    """
    alvo_alternativas = texto if perfil.alternativa_no_original and texto is not None else norm
    # o início vale a partir do RÓTULO inteiro ("⟦u⟧QUESTÃO 19"), não do número: senão o rótulo da
    # questão seguinte sobra no fim da última alternativa da anterior
    candidatos = [(m.start() + (norm[m.start()] == "\n"), m.end(), int(m.group(1)))
                  for m in perfil.inicio_questao.finditer(norm)]
    fortes: dict[int, list[tuple[int, int]]] = {}
    fracos: dict[int, list[tuple[int, int]]] = {}
    for i, (ini, fim, numero) in enumerate(candidatos):
        if not perfil.alternativa.search(alvo_alternativas[fim: fim + perfil.janela_primeira_alternativa]):
            continue
        proximo = candidatos[i + 1][0] if i + 1 < len(candidatos) else None
        colado = proximo is not None and len(re.sub(r"\s", "", norm[fim:proximo])) < perfil.texto_minimo_entre_numeros
        (fracos if colado else fortes).setdefault(numero, []).append((ini, fim))
    return cadeia_mais_longa(fortes, fracos)


def _cortar_ultima_alternativa(trecho: str) -> tuple[str, str]:
    """A última alternativa termina na primeira linha que é título, texto-base, imagem ou lixo."""
    linhas = trecho.split("\n")
    anterior = linhas[0].rstrip()
    for i in range(1, len(linhas)):
        linha = linhas[i]
        if not linha.strip():
            continue
        norm = tx.norm_1a1(sem_destaques(linha))
        frase_terminou = sem_destaques(anterior).rstrip().endswith(_FIM_DE_FRASE)
        # rótulo explícito de texto de apoio ("Situação hipotética 1A3-I", "Texto CG1A1-I") corta sempre:
        # a Cebraspe não termina alternativa com ponto, então não dá para exigir frase terminada
        if MARCADOR_IMAGEM in linha or _LIXO_PAGINA.search(norm) or _ROTULO_APOIO.match(norm):
            return "\n".join(linhas[:i]), "\n".join(linhas[i:])
        if frase_terminou and (_INICIO_TEXTO_BASE.match(norm) or _titulo_de_linhas(linhas[i:i + 3])[0]):
            return "\n".join(linhas[:i]), "\n".join(linhas[i:])
        anterior = linha.rstrip()
    return trecho, ""


_CONECTIVO_FINAL = re.compile(r"(\b(e|de|da|do|das|dos|em|para)|,)$", re.IGNORECASE)


def _titulo_de_linhas(linhas: list[str]) -> tuple[str | None, int]:
    """Título de seção em 1–3 linhas seguidas. Devolve (título mais específico, nº de linhas usadas).

    Uma linha só continua na seguinte quando termina em conectivo ("... Deficiência e"). Fora
    disso, cada linha é um título: em "CONHECIMENTOS GERAIS / Língua Portuguesa" fica o último,
    o mais específico.
    """
    titulos, usadas, i = [], 0, 0
    while i < len(linhas) and i < 3:
        atual = sem_destaques(linhas[i]).strip()
        j = i
        while _CONECTIVO_FINAL.search(atual) and j + 1 < len(linhas) and j + 1 < 3:
            j += 1
            atual = f"{atual} {sem_destaques(linhas[j]).strip()}"
        if not atual or not eh_titulo_de_secao(atual):
            break
        titulos.append(atual)
        usadas = j + 1
        i = j + 1
    return (titulos[-1], usadas) if titulos else (None, 0)


def _escolher_primeira_alternativa(corpo: str, marcas: list, posicoes: dict, perfil: PerfilExtracao) -> bool:
    """Com letra e texto na mesma linha, três coisas parecem a alternativa A: o artigo que abre o enunciado
    ("A respeito de...", "A biossegurança é..."), uma linha quebrada da própria alternativa ("...sem armas,\\nA lei
    regula...") e a alternativa A de verdade. Regra estrutural (a maiúscula não decide: o artigo é seguido de
    minúscula, e a alternativa às vezes de maiúscula — achado no banco real em 15/10, 1.145 questões):
      - antes da alternativa A tem que haver ENUNCIADO (texto com letras);
      - a linha anterior não termina em vírgula ou ponto e vírgula (senão é continuação de frase).
    Uma candidata só: vale ela. Várias: vale a última e a questão fica suspeita. Nenhuma: suspeita.
    Devolve True se a escolha é suspeita."""
    primeira, segunda = perfil.letras[0], perfil.letras[1]
    candidatas = [m for m in marcas if m.group(1).upper() == primeira and m.start() < posicoes[segunda][0]]
    validas = []
    for m in candidatas:
        antes = sem_destaques(corpo[:m.start()]).rstrip()
        if re.search(r"[^\W\d_]", antes) and not antes.endswith((",", ";")):
            validas.append(m)
    escolha = validas[-1] if validas else candidatas[-1]
    posicoes[primeira] = (escolha.start(), escolha.end())
    return len(validas) != 1


def _dividir_alternativas(corpo: str, perfil: PerfilExtracao) -> tuple[str, dict[str, str], str, bool]:
    """(enunciado, alternativas, resto depois da última alternativa, divisão suspeita)."""
    alvo = corpo if perfil.alternativa_no_original else tx.norm_1a1(corpo)
    marcas = list(perfil.alternativa.finditer(alvo))
    posicoes: dict[str, tuple[int, int]] = {}
    proxima = 0
    for m in marcas:
        if proxima >= len(perfil.letras):
            break
        if m.group(1).upper() == perfil.letras[proxima]:
            posicoes[perfil.letras[proxima]] = (m.start(), m.end())
            proxima += 1
    suspeita = False
    if perfil.alternativa_no_original and len(posicoes) > 1:
        suspeita = _escolher_primeira_alternativa(corpo, marcas, posicoes, perfil)
    if not posicoes:
        return normalizar_bloco(corpo), {}, "", False
    letras = list(posicoes)
    enunciado = corpo[: posicoes[letras[0]][0]]
    alternativas: dict[str, str] = {}
    resto = ""
    for i, letra in enumerate(letras):
        ini = posicoes[letra][1]
        fim = posicoes[letras[i + 1]][0] if i + 1 < len(letras) else len(corpo)
        trecho = corpo[ini:fim]
        if i + 1 == len(letras):
            trecho, resto = _cortar_ultima_alternativa(trecho)
        alternativas[letra] = normalizar_bloco(trecho)
    # sobra de marcador do rótulo ("⟦u⟧QUESTÃO 16⟦/u⟧" deixa um "⟦/u⟧" no começo do enunciado)
    enunciado = re.sub(r"^(\s*⟦/[biu]⟧)+", "", enunciado)
    return normalizar_bloco(enunciado), alternativas, resto, suspeita


def _separar_titulos(resto: str) -> tuple[str | None, str]:
    """Do trecho entre questões: título de seção (no começo) e o conteúdo restante (texto-base)."""
    linhas = [l for l in resto.splitlines() if l.strip()]
    titulo = None
    i = 0
    while i < len(linhas):
        achado, usadas = _titulo_de_linhas(linhas[i:i + 3])
        if not achado:
            break
        titulo, i = achado, i + usadas
    conteudo = [l for l in linhas[i:] if not _LIXO_PAGINA.search(tx.norm_1a1(l))]
    return titulo, "\n".join(conteudo).strip()


def _preambulo(preambulo: str) -> tuple[str | None, str | None]:
    """Seção e texto-base antes da questão 1. A capa é ruído; só aproveitamos marcadores explícitos."""
    linhas = [l for l in preambulo.splitlines() if l.strip()]
    inicio_texto = None
    for i in range(len(linhas) - 1, -1, -1):
        if _INICIO_TEXTO_BASE.match(tx.norm_1a1(sem_destaques(linhas[i]))):
            inicio_texto = i
            break
    fim_titulos = inicio_texto if inicio_texto is not None else len(linhas)
    secao = None
    for inicio in range(max(0, fim_titulos - 3), fim_titulos):
        achado, usadas = _titulo_de_linhas(linhas[inicio:fim_titulos])
        if achado and inicio + usadas == fim_titulos:
            secao = achado
            break
    texto_base = "\n".join(linhas[inicio_texto:]).strip() if inicio_texto is not None else None
    return secao, texto_base


def segmentar(texto: str, perfil: PerfilExtracao = PerfilExtracao()) -> CadernoExtraido:
    norm = tx.norm_1a1(texto)
    esperado = esperado_da_capa(texto)
    posicoes, avisos = _posicoes_de_questoes(norm, perfil, texto)
    if not posicoes:
        return CadernoExtraido([], esperado, "quarentena", "nenhuma questão reconhecida", avisos)

    secao, texto_base = _preambulo(texto[: posicoes[0][0]])
    escopo = _escopo_declarado(texto_base, 1)
    itens: list[ItemExtraido] = []
    for i, (_ini, fim, numero) in enumerate(posicoes):
        fim_corpo = posicoes[i + 1][0] if i + 1 < len(posicoes) else len(texto)
        enunciado, alternativas, resto, suspeita = _dividir_alternativas(texto[fim:fim_corpo], perfil)
        item = ItemExtraido(numero=numero, enunciado=enunciado, alternativas=alternativas, secao=secao)
        _diagnosticar(item, perfil, texto_base, obrigatorio=numero in escopo)
        if suspeita and P_ALTERNATIVA_LONGA not in item.problemas:
            item.problemas.append(P_ALTERNATIVA_LONGA)
        if not enunciado_tem_texto(enunciado):
            item.problemas.append(P_ENUNCIADO_VAZIO)
        itens.append(item)
        if resto:
            novo_titulo, novo_texto = _separar_titulos(resto)
            if novo_titulo:
                secao, texto_base, escopo = novo_titulo, None, set()
            if len(novo_texto) > 80:
                texto_base = novo_texto
                escopo = _escopo_declarado(texto_base, numero + 1)
    _conferir_numero_de_alternativas(itens, perfil)
    _alternativa_final_longa(itens)
    return _validar(itens, esperado, avisos)


def enunciado_tem_texto(enunciado: str) -> bool:
    return bool(re.search(r"[^\W\d_]{2,}", sem_destaques(enunciado or "")))


def _conferir_numero_de_alternativas(itens: list[ItemExtraido], perfil: PerfilExtracao) -> None:
    """O caderno define se a prova tem 5 ou 4 alternativas (moda das questões), não o perfil da banca.

    Antes, um caderno A–D inteiro (FGV nível médio, professores) virava "alternativas_incompletas".
    """
    tamanhos = [len(i.alternativas) for i in itens if i.alternativas]
    if not tamanhos:
        return
    moda = max(set(tamanhos), key=tamanhos.count)
    if moda not in (len(perfil.letras), len(perfil.letras) - 1):
        return
    esperadas = list(perfil.letras[:moda])
    for item in itens:
        completo = list(item.alternativas) == esperadas and all(item.alternativas.values())
        if completo and P_ALTERNATIVAS in item.problemas:
            item.problemas.remove(P_ALTERNATIVAS)
        elif not completo and P_ALTERNATIVAS not in item.problemas:
            item.problemas.append(P_ALTERNATIVAS)


_NUMEROS_EN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
               "nine": 9, "ten": 10}
_ESCOPO_PROXIMAS = re.compile(r"\b(?:next|following|proximas|seguintes)\s+(\w+)\s+(?:questions|questoes)\b")
_ESCOPO_INTERVALO = re.compile(r"\bquest(?:oes|ions)\s+(?:de\s+|from\s+)?(\d{1,3})\s+(?:a|to|e|and)\s+(\d{1,3})\b")


def _escopo_declarado(texto_base: str | None, proxima: int) -> set[int]:
    """Questões que a própria prova diz que usam o texto ("the next six questions", "questões de 5 a 8")."""
    if not texto_base:
        return set()
    norm = tx.norm_1a1(texto_base[:400])
    intervalo = _ESCOPO_INTERVALO.search(norm)
    if intervalo:
        return set(range(int(intervalo.group(1)), int(intervalo.group(2)) + 1))
    proximas = _ESCOPO_PROXIMAS.search(norm)
    if proximas:
        n = _NUMEROS_EN.get(proximas.group(1)) or numero_por_extenso(proximas.group(1))
        if n:
            return set(range(proxima, proxima + n))
    return set()


def _diagnosticar(item: ItemExtraido, perfil: PerfilExtracao, texto_base: str | None, obrigatorio: bool = False) -> None:
    conteudo = item.enunciado + " " + " ".join(item.alternativas.values())
    norm_enunciado = tx.norm_1a1(sem_destaques(item.enunciado))
    # dentro do escopo declarado pela prova, o texto é obrigatório mesmo sem citação explícita
    referencia_texto = obrigatorio or bool(_REFERE_TEXTO.search(norm_enunciado))
    embutido = _EMBUTIDO.search(norm_enunciado)
    texto_embutido = ((bool(embutido) and len(norm_enunciado) - embutido.end() >= MIN_CONTEUDO_EMBUTIDO)
                      or bool(_CITACAO_LONGA.search(sem_destaques(item.enunciado)))
                      or len(norm_enunciado) >= MIN_ENUNCIADO_COM_TEXTO)
    if obrigatorio:
        texto_embutido = False
    if referencia_texto and not texto_embutido and texto_base:
        item.texto_base = texto_base
    apoio = item.texto_base or ""

    if MARCADOR_IMAGEM in conteudo or MARCADOR_IMAGEM in apoio or (
            _REFERE_FIGURA.search(norm_enunciado) and not texto_embutido and MARCADOR_IMAGEM in (texto_base or "")):
        item.problemas.append(P_IMAGEM)
    if len(item.alternativas) != len(perfil.letras) or any(not t for t in item.alternativas.values()):
        item.problemas.append(P_ALTERNATIVAS)
    if _REFERE_DESTAQUE.search(norm_enunciado) and not tem_destaque(conteudo + apoio):
        item.problemas.append(P_DESTAQUE_PERDIDO)
    if referencia_texto and not texto_embutido and not item.texto_base:
        item.problemas.append(P_TEXTO_AUSENTE)
    if MARCADOR_SIMBOLO in conteudo + apoio:
        item.problemas.append(P_SIMBOLO)
    ultima = list(item.alternativas.values())[-1] if item.alternativas else ""
    if re.search(r"\b(realizacao|rascunho)\s*$", tx.norm_1a1(sem_destaques(ultima))):
        item.problemas.append(P_LIXO_PAGINA)


def _alternativa_final_longa(itens: list[ItemExtraido]) -> None:
    for item in itens:
        if len(item.alternativas) < 3:
            continue
        valores = list(item.alternativas.values())
        outras = median(len(v) for v in valores[:-1]) or 1
        if len(valores[-1]) > 400 and len(valores[-1]) > 4 * outras:
            item.problemas.append(P_ALTERNATIVA_LONGA)


def _validar(itens: list[ItemExtraido], esperado: int | None, avisos: list[str]) -> CadernoExtraido:
    buraco = next((a for a in avisos if a.startswith(AVISO_BURACO_INICIAL)), None)
    if buraco:
        return CadernoExtraido(itens, esperado, "quarentena", buraco, avisos)
    if esperado and len(itens) != esperado:
        return CadernoExtraido(itens, esperado, "quarentena", f"{len(itens)} itens, capa diz {esperado}", avisos)
    completos = sum(P_ALTERNATIVAS not in i.problemas for i in itens)
    if completos < 0.9 * len(itens):
        return CadernoExtraido(itens, esperado, "quarentena", f"só {completos}/{len(itens)} com alternativas completas", avisos)
    if len(itens) < 10:
        return CadernoExtraido(itens, esperado, "quarentena", f"apenas {len(itens)} itens", avisos)
    # sem capa legível, o caderno fica 'ok' provisoriamente: a etapa gabaritar confirma o total
    # pelo tamanho da tabela do gabarito e coloca em quarentena se não bater
    return CadernoExtraido(itens, esperado, "ok", None, avisos)
