"""Sensibilidade da Tabela 5: diferença de acerto jurídico x demais áreas só com questões cuja disciplina veio do cabeçalho do caderno.

Uso, na raiz do repositório:  python analises/sensibilidade_cabecalho.py
Motivo: a validação humana (validacao/) mostrou acerto moderado na disciplina atribuída pelo modelo.
"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
R = Path(__file__).resolve().parents[1]
B, SEMENTE = 10000, 20261106
lc = lambda n: pd.read_csv(R / "dados" / n)
am, qu, mo, di = lc("amostras.csv"), lc("questoes.csv"), lc("modelos.csv"), lc("disciplinas.csv")
qu["macroarea"] = qu.disciplina.map(di.set_index("disciplina").macroarea)
rs = lc("respostas_me.csv").merge(mo, on="modelo").merge(qu, on="questao_id")
rs["y"] = (rs.formato_ok == 1) & (rs.acertou == 1)
rs["jur"] = rs.macroarea == "direito"
nuc = rs[rs.questao_id.isin(set(am[am.amostra == "nucleo_me"].questao_id)) & (rs.gabarito_fonte == "definitivo") & (rs.disciplina_fonte == "secao")]


def dif(x, rng):
    j, y = x.jur.values, x.y.values
    ij, id_ = np.where(j)[0], np.where(~j)[0]
    v = 100 * (y[rng.choice(ij, (B, len(ij)))].mean(1) - y[rng.choice(id_, (B, len(id_)))].mean(1))
    return [round(100 * float(y[ij].mean() - y[id_].mean()), 1), *np.round(np.percentile(v, [2.5, 97.5]), 1).tolist(), int(len(ij)), int(len(id_))]


out = {}
for s in ("gemini-flash", "gpt-oss-120b", "qwen3.8-27b"):
    rng = np.random.default_rng(SEMENTE)
    x = nuc[nuc.modelo == s]
    out[s] = {"cabecalho_definitivo": dif(x, rng), "cabecalho_FGV": dif(x[x.banca == "fgv"], rng), "cabecalho_Cebraspe": dif(x[x.banca == "cebraspe"], rng)}
print(json.dumps(out, indent=1))
(R / "resultados" / "sensibilidade_cabecalho.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
