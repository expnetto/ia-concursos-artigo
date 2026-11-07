# Limites da confiabilidade de modelos de linguagem na resolução de questões de concursos públicos

Repositório de apoio ao artigo de TCC em Ciência da Computação (UNIPÊ, 2026), de Eraldo X. P. Netto, Luiz A. S. Santino e Victor M. Q. B. Cavalcanti, com orientação de Jose R. B. Andrade.

**Este repositório é um recorte.** Ele reúne o necessário para conferir as hipóteses, os procedimentos e os resultados do artigo. Não é o repositório de desenvolvimento, que tem o histórico completo e partes do projeto que não entraram no artigo. Por isso, os commits daqui não comprovam as datas do projeto: as datas estão em [docs/HISTORICO.md](docs/HISTORICO.md), com os commits de origem, e o histórico original pode ser consultado pelo orientador.

## O que há aqui

| Pasta | Conteúdo |
|---|---|
| `docs/PRE-REGISTRO.md` | Hipóteses H1 a H6 e regras, em cópia literal |
| `docs/HISTORICO.md` | Linha do tempo do que importa para o artigo |
| `docs/DESVIOS-E-LIMITACOES.md` | O que não seguiu o previsto e o que limita os resultados |
| `src/`, `config/`, `tests/` | Sistema: coleta das provas oficiais, extração, amostragem, execução e correção |
| `dados/` | Amostras, metadados e respostas dos modelos, sem texto de prova |
| `analises/` | Script que reproduz os resultados a partir de `dados/` |
| `resultados/` | Saída do script |
| `validacao/` | Protocolo e resultados das conferências humanas |

## Como reproduzir os resultados

```bash
pip install numpy pandas scipy statsmodels
python analises/reproduz_resultados.py
```

O script usa só `dados/`, leva poucos minutos e grava `resultados/resultados.json`. Ele reproduz a Tabela 4, a Tabela 5, H1, H2, H4, H6 e os números da seção 5.3 do artigo. A análise principal usa as 694 questões comuns com gabarito definitivo, conforme a regra registrada, e guarda a base com todas as questões como sensibilidade.

## Sistema

```bash
uv sync
uv run pytest
uv run iac --help
```

Reproduzir a coleta do zero exige baixar os documentos oficiais, os modelos locais e chaves de API próprias (`.env.example`). O banco não é distribuído, porque contém o texto das provas; `dados/questoes.csv` traz o link oficial de cada prova.

## O que ficou fora

- Texto das provas, que pertence às bancas.
- O banco SQLite e as respostas brutas dos modelos.
- O teste complementar do projeto sobre substituir dados humanos, que não atingiu seu critério e ficou fora do artigo, como declara a Conclusão.
- As análises exploratórias secundárias da seção 5.4: os scripts e os dados externos não fazem parte deste recorte.

## Diferenças em relação ao código de desenvolvimento

O código foi copiado sem alteração, com três exceções: `src/iac/cli.py` perdeu os comandos de partes fora do artigo, `src/iac/config.py` perdeu o e-mail de contato e `pyproject.toml` perdeu os dados do autor. O executor e o esquema do banco ainda citam condições experimentais fora do artigo, porque o código é o mesmo que rodou.

O projeto de desenvolvimento tem 278 testes automatizados, número citado no artigo. Este recorte mantém os 121 que cobrem as partes incluídas.

## Uso de inteligência artificial

Foi utilizada a ferramenta Claude (Anthropic) como apoio ao desenvolvimento do código, à análise de dados e à redação. As decisões de pesquisa, a revisão e a responsabilidade pelo conteúdo são dos autores.
