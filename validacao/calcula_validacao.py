"""Calcula as medidas do protocolo de validação a partir de validacao/*.csv (sem texto de prova).

Uso, na raiz do repositório:  python validacao/calcula_validacao.py
Grava validacao/resultado.json.
"""
import collections, csv, json, math, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
R = Path(__file__).resolve().parents[1]
MACRO = {r["disciplina"]: r["macroarea"] for r in csv.DictReader(open(R / "dados" / "disciplinas.csv", encoding="utf-8"))}


def kappa(x, y):
    n = len(x); po = sum(a == b for a, b in zip(x, y)) / n
    ca, cb = collections.Counter(x), collections.Counter(y)
    pe = sum(ca[k] * cb[k] for k in ca) / n / n
    return round(100 * po, 1), round((po - pe) / (1 - pe), 4)


def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(100 * p, 1), round(100 * (c - m) / d, 1), round(100 * (c + m) / d, 1), n]


assert kappa(list("aabb"), list("aabb"))[1] == 1.0 and abs(wilson(50, 100)[1] - 40.4) < 0.1
d = list(csv.DictReader(open(R / "validacao" / "disciplinas_400.csv", encoding="utf-8")))
assert len(d) == 400
for r in d:
    assert r["consenso"] == (r["avaliador_A"] if r["avaliador_A"] == r["avaliador_B"] else r["desempate"]), r["questao_id"]
mac = lambda col: [MACRO[r[col]] for r in d]
assert mac("banco_disciplina") == [r["banco_grupo"] for r in d]
res = {
    "n": len(d),
    "divergencias_A_B": sum(r["avaliador_A"] != r["avaliador_B"] for r in d),
    "A_x_B_disciplina": kappa([r["avaliador_A"] for r in d], [r["avaliador_B"] for r in d]),
    "A_x_B_macroarea": kappa(mac("avaliador_A"), mac("avaliador_B")),
    "consenso_x_modelo_disciplina": kappa([r["consenso"] for r in d], [r["banco_disciplina"] for r in d]),
    "consenso_x_modelo_macroarea": kappa(mac("consenso"), mac("banco_disciplina")),
    "acerto_modelo_disciplina_ic95": wilson(sum(r["consenso"] == r["banco_disciplina"] for r in d), 400),
    "acerto_modelo_macroarea_ic95": wilson(sum(a == b for a, b in zip(mac("consenso"), mac("banco_disciplina"))), 400),
    "acerto_por_macroarea_do_modelo_ic95": {},
    "criterio_kappa_macroarea_0_61": None,
}
for m in sorted(set(mac("banco_disciplina"))):
    sub = [r for r in d if r["banco_grupo"] == m]
    res["acerto_por_macroarea_do_modelo_ic95"][m] = wilson(sum(MACRO[r["consenso"]] == m for r in sub), len(sub))
x, y = mac("consenso"), mac("banco_disciplina")
po = sum(a == b for a, b in zip(x, y)) / 400
pe = sum(collections.Counter(x)[k] * collections.Counter(y)[k] for k in set(x)) / 400 / 400
res["criterio_kappa_macroarea_0_61"] = (po - pe) / (1 - pe) >= 0.61  # sem arredondar: 0,6099 não atinge 0,61
e = list(csv.DictReader(open(R / "validacao" / "extracao_120.csv", encoding="utf-8")))
res["extracao"] = {"n": len(e), "divergencias": sum(any(r[c].strip().lower() not in ("sim", "s") for c in ("texto_igual", "alternativas_iguais", "gabarito_igual")) for r in e)}
print(json.dumps(res, ensure_ascii=False, indent=1))
(R / "validacao" / "resultado.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
