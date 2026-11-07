# Protocolo de validação das disciplinas

Fixado em 07/11/2026, antes de qualquer rótulo humano.

- **Universo:** 101851 questões elegíveis com disciplina atribuída pelo Qwen 2.5 7B.
- **Amostra:** 400 questões, sorteio estratificado pela macroárea atribuída (semente 20261107), proporcional, com mínimo de 30 por macroárea: {'gestao': 63, 'matematica': 34, 'ti': 31, 'linguas': 30, 'portugues': 30, 'outras': 97, 'direito': 115}.
- **Avaliação:** duas pessoas, às cegas e de forma independente, sem ver o rótulo do modelo nem a outra planilha. Uma terceira pessoa decide as divergências.
- **Medidas:** kappa de Cohen entre as duas pessoas; concordância e kappa entre o consenso humano e o modelo, em disciplina e macroárea; concordância por macroárea com intervalo de 95%.
- **Critério:** kappa de pelo menos 0,61 (LANDIS; KOCH, 1977) em macroárea, entre consenso e modelo. Este critério foi fixado antes dos dados.
- **Relato:** o resultado será relatado como sair, inclusive abaixo do critério, e o banco e os resultados do artigo não serão alterados por ele.
- **Dados públicos:** apenas ids, rótulos e kappas; o texto das provas não é publicado.
