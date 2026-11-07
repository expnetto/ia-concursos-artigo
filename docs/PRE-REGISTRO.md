# Pré-registro das hipóteses H1 a H6

Cópia literal da seção 10 do documento de arquitetura do projeto, aprovada em 15/10/2026 às 19h23 (commit `5384b32` do repositório de desenvolvimento). O sorteio do núcleo ocorreu em 16/10/2026 às 00h00.
Os nomes P1 a P8 citados na tabela são perguntas derivadas do mesmo documento.

---

## 10. Pré-registro (aprovado pelo usuário em 15/10/2026, antes do núcleo-1)

Fixado antes de qualquer execução da amostra-núcleo. Análises que não estiverem aqui são exploratórias.
O usuário pode revisar as hipóteses depois; cada revisão fica registrada no histórico do git. Nível de significância 0,05; correção de Holm dentro de cada família.

| # | Hipótese (direção prevista) | Medida | Teste | Amostra |
|---|---|---|---|---|
| H1 (P1) | Todo modelo acerta acima do chute, e o acerto varia por macroárea (pior em matemática e direito do que em português) | acurácia | IC de Wilson × 1/5 (1/4); regressão logística mista `acertou ~ modelo + macroarea + (1\|questao)` | núcleo + extensão |
| H2 (P2) | Os modelos são **sobreconfiantes**: confiança média > acurácia, e a taxa de erro entre respostas com confiança ≥ 90 é > 10% | ECE, Brier, P(erro \| conf ≥ 90) | IC por bootstrap (2.000 reamostragens por questão) | núcleo |
| H3 (P4) | O modelo pago de 2023 (GPT-4-0613) acerta mais que o gratuito da mesma época (GPT-3.5) | acurácia pareada | McNemar exato | núcleo-1 |
| H4 (P5) | Modelos mais novos acertam mais **e** erram com convicção menos vezes | acurácia e erro convicto × data de lançamento | regressão mista com efeito da data (por família) | núcleo + extensão |
| H5 (P7) | Acerto maior em provas **anteriores** ao corte de conhecimento do modelo (memorização) | acurácia pré × pós-corte | termo `pos_corte` na regressão mista; queda sob permutação das alternativas | núcleo + `me_permutada` |
| H6 (P8) | No certo/errado com penalidade, os modelos raramente se abstêm onde erram | abstenção útil, nota líquida | proporção com IC de Wilson | Cebraspe, `ce_abstencao` |

Regras fixadas junto: (1) resposta fora do formato conta como erro na nota de "candidato"
(`acertou_candidato`) e é reportada à parte; (2) análise principal só com gabarito definitivo, com
sensibilidade incluindo os preliminares; (3) questões em `auditoria` (gabarito mudou depois do uso)
são reportadas à parte; (4) amostra-núcleo estratificada banca × macroárea × faixa de ano, semente 20261015.
