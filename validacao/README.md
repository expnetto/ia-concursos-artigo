# Conferências humanas

Situação: **concluídas em 07/11/2026**. O protocolo das disciplinas (`protocolo_validacao.md`) foi fixado antes de qualquer rótulo humano, depois da coleta. Os resultados estão relatados como saíram.

Para recalcular: `python validacao/calcula_validacao.py`. O resultado fica em `resultado.json`.

## Disciplinas (400 questões)

- **Quem fez:** avaliador A, Victor M. Q. B. Cavalcanti; avaliador B, Luiz A. S. Santino; desempate, Eraldo X. P. Netto. A e B trabalharam às cegas e de forma independente. No desempate, a ordem das opções foi sorteada, sem indicar de quem era cada uma.
- **Arquivo:** `disciplinas_400.csv`, com id, rótulo do modelo, escolhas de A e B, desempate e consenso. O texto das questões não é publicado.
- **A x B:** 52,5% de concordância na disciplina (kappa 0,498, moderada) e 74,0% na macroárea (kappa 0,665, substancial); 190 divergências.
- **Consenso x modelo:** acerto de 54,5% (49,6 a 59,3) na disciplina e 68,5% (63,8 a 72,9) na macroárea. O kappa na macroárea foi 0,6099, **abaixo do critério de 0,61** fixado no protocolo.
- **Por macroárea do modelo:** o bloco jurídico teve acerto de 51,3% (115 questões), e parte das demais era de saúde. Por isso, `analises/sensibilidade_cabecalho.py` repete a comparação do bloco jurídico só com questões cuja disciplina veio do cabeçalho do caderno; o resultado na FGV se mantém.

## Extração (120 questões)

- **Quem fez:** Eraldo X. P. Netto.
- **Amostra:** 120 questões usadas no estudo, sorteadas por banca e formato (semente 20261108), comparadas à prova e ao gabarito oficiais pelos links em `extracao_120.csv`.
- **Resultado:** nenhuma divergência de texto, alternativas ou gabarito.

## Comparação com usuários (seção 5.4)

As 30 questões foram conferidas à mão no site da plataforma por Eraldo X. P. Netto. Os percentuais de usuários pertencem à plataforma e não são publicados aqui.
