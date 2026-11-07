"""Montagem do prompt de uma questão para uma condição.

Também decide a permutação das alternativas (condição `me_permutada`). A permutação é
determinística (semente = questão + repetição) e SEMPRE tira a alternativa correta da letra
original: é isso que permite detectar memorização da letra do gabarito.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from iac.pdf import destaques_para_prompt, tem_destaque

MARCA_USUARIO = "<<<USUARIO>>>"   # no template: o que vem antes é a mensagem de sistema da condição

INSTRUCAO_DESTAQUE = (
    "Os destaques da prova original foram preservados assim: [negrito]...[/negrito], "
    "[itálico]...[/itálico] e [sublinhado]...[/sublinhado].\n"
)


@dataclass(frozen=True)
class PromptMontado:
    texto: str
    permutacao: dict[str, str] | None   # letra exibida -> letra original
    sistema: str | None = None          # mensagem de sistema própria da condição (turma simulada); None = a padrão
    perfil: dict | None = None          # aluno simulado (turma simulada): aluno, nota, área

    def sha_texto(self) -> str:
        """Texto coberto pelo prompt_sha: só o do usuário nas condições antigas (sha inalterado)."""
        return self.texto if self.sistema is None else f"{self.sistema}\n{MARCA_USUARIO}\n{self.texto}"


def _permutar(alternativas: dict[str, str], gabarito: str | None, semente: str) -> dict[str, str]:
    letras = sorted(alternativas)
    gerador = random.Random(semente)
    for _ in range(100):
        ordem = letras[:]
        gerador.shuffle(ordem)
        mapa = dict(zip(letras, ordem))       # exibida -> original
        if gabarito is None or mapa[gabarito] != gabarito:
            return mapa
    raise RuntimeError("não foi possível permutar")  # impossível com 4+ alternativas


def montar(template: str, questao: dict, embaralhar: bool, repeticao: int,
           perfil: dict | None = None) -> PromptMontado:
    """`questao` precisa ter: id, enunciado, alternativas (dict ou None), texto_base, gabarito.

    `perfil` (turma simulada): campos extras do template ({nota}, {area_nome}). Se o template tiver
    `MARCA_USUARIO`, o trecho antes dela vira a mensagem de sistema.
    """
    alternativas: dict[str, str] | None = questao["alternativas"]
    permutacao = None
    if alternativas and embaralhar:
        permutacao = _permutar(alternativas, questao["gabarito"], f"{questao['id']}-{repeticao}")
        pares = [(exibida, alternativas[original]) for exibida, original in sorted(permutacao.items())]
    else:
        pares = sorted((alternativas or {}).items())
    linhas = [f"({letra}) {destaques_para_prompt(texto)}" for letra, texto in pares]
    conteudo = " ".join([questao["enunciado"], questao.get("texto_base") or "", *(alternativas or {}).values()])
    apoio = f"Texto de apoio:\n{destaques_para_prompt(questao['texto_base'])}\n\n" if questao.get("texto_base") else ""
    campos = {
        "apoio": apoio,
        "enunciado": destaques_para_prompt(questao["enunciado"]),
        "alternativas": "\n".join(linhas),
        "instrucao_destaque": INSTRUCAO_DESTAQUE if tem_destaque(conteudo) else "",
    }
    sistema = None
    if MARCA_USUARIO in template:
        modelo_sistema, template = template.split(MARCA_USUARIO, 1)
        sistema = modelo_sistema.strip().format(**(perfil or {}))
        template = template.lstrip("\n")
    texto = template.format(**campos, **(perfil or {}))
    return PromptMontado(texto=texto, permutacao=permutacao, sistema=sistema, perfil=perfil)


def carregar_questao(conexao, questao_id: int) -> dict:
    linha = conexao.execute(
        """SELECT q.id, q.gabarito, q.formato, q.permutavel, i.enunciado, i.alternativas_json, tb.texto AS texto_base
           FROM questao q JOIN item i ON i.id = q.item_canonico_id
           LEFT JOIN texto_base tb ON tb.id = i.texto_base_id WHERE q.id = ?""", (questao_id,)
    ).fetchone()
    return {
        "id": linha["id"], "gabarito": linha["gabarito"], "formato": linha["formato"],
        "permutavel": bool(linha["permutavel"]), "enunciado": linha["enunciado"], "texto_base": linha["texto_base"],
        "alternativas": json.loads(linha["alternativas_json"]) if linha["alternativas_json"] else None,
    }
