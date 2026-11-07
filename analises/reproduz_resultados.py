"""Reproduz os resultados principais do artigo a partir de dados/*.csv (sem o banco e sem texto de prova).

Uso, na raiz do repositório:  python analises/reproduz_resultados.py
Grava resultados/resultados.json e imprime a Tabela 4. A análise principal usa só gabarito definitivo
(regra do pré-registro); a base com todas as questões entra como sensibilidade.
Estimador das regressões: quase-verossimilhança penalizada (BRESLOW; CLAYTON, 1993), com autoteste em dados simulados.
"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd, scipy.sparse as sp
from scipy.stats import norm, rankdata
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
R = Path(__file__).resolve().parents[1]
ORDEM = ["gemini-flash", "gpt-oss-120b", "qwen3.8-27b", "qwen3-8b", "gemma2-9b", "qwen2.5-7b", "llama3-8b", "llama3.1-8b", "gemma3-4b", "mistral-7b-v0.3", "llama2-7b"]
TOP = ORDEM[:3]
B = 10000
SEMENTE = 20261106


def pql(x, y, q, iters=30):
    u_cat = pd.Categorical(q).codes; m = u_cat.max() + 1; n = len(y)
    z = sp.csr_matrix((np.ones(n), (np.arange(n), u_cat)), shape=(n, m))
    b, u, s2 = np.zeros(x.shape[1]), np.zeros(m), 1.0
    for _ in range(iters):
        for _ in range(8):
            eta = x @ b + z @ u
            p = np.clip(1 / (1 + np.exp(-eta)), 1e-6, 1 - 1e-6); w = p * (1 - p)
            g_b = x.T @ (y - p); g_u = z.T @ (y - p) - u / s2
            d = np.asarray(z.T @ sp.diags(w) @ z @ np.ones(m)).ravel() + 1 / s2
            xwz = x.T @ sp.diags(w) @ z
            xwx = x.T @ (x * w[:, None])
            sch = xwx - (xwz @ sp.diags(1 / d) @ xwz.T)
            sch = np.asarray(sch.todense()) if sp.issparse(sch) else sch
            db = np.linalg.solve(sch, g_b - xwz @ (g_u / d))
            du = (g_u - xwz.T @ db) / d
            b += db; u += du
            if abs(db).max() < 1e-7:
                break
        novo = (u @ u + (1 / d).sum()) / m
        if abs(novo - s2) < 1e-4:
            s2 = novo; break
        s2 = novo
    return b, np.sqrt(np.diag(np.linalg.inv(sch))), s2


r0 = np.random.default_rng(20261105)
mq, nq = 1500, 10
qs = np.repeat(np.arange(mq), nq); xs = r0.integers(0, 2, mq * nq).astype(float); us = r0.normal(0, 1.2, mq)[qs]
ys = (r0.random(mq * nq) < 1 / (1 + np.exp(-(0.3 - 0.8 * xs + us)))).astype(float)
b_, ep_, s_ = pql(np.c_[np.ones(mq * nq), xs], ys, qs)
assert abs(b_[1] + 0.8) < 3 * ep_[1] + 0.1 and 0.8 < s_ < 1.8, "autoteste do estimador"


def auroc(cf, y):
    p, n = y.sum(), (~y).sum()
    return float((rankdata(cf)[y].sum() - p * (p + 1) / 2) / (p * n))


assert abs(auroc(np.array([1., 2, 3, 4]), np.array([False, False, True, True])) - 1) < 1e-9


def ece(p, y):
    k = np.minimum((p * 10).astype(int), 9)
    return float(sum(abs(p[k == i].mean() - y[k == i].mean()) * (k == i).mean() for i in range(10) if (k == i).any()))


lc = lambda n: pd.read_csv(R / "dados" / n)
am, qu, mo, di, es = lc("amostras.csv"), lc("questoes.csv"), lc("modelos.csv"), lc("disciplinas.csv"), lc("estratos.csv")
qu["macroarea"] = qu.disciplina.map(di.set_index("disciplina").macroarea)
rs = lc("respostas_me.csv").merge(mo, on="modelo").merge(qu, on="questao_id")
rs["y"] = (rs.formato_ok == 1) & (rs.acertou == 1)
rs["jur"] = rs.macroarea == "direito"
rs = rs.rename(columns={"questao_id": "q", "confianca": "cf", "data_lancamento": "dt", "gabarito_fonte": "gf", "macroarea": "area"})
nu_ids = set(am[am.amostra == "nucleo_me"].questao_id)
est = am[am.amostra == "nucleo_me"].set_index("questao_id").estrato
pop = es[es.amostra == "nucleo_me"].set_index("estrato").n_populacao
nuc = rs[rs.q.isin(nu_ids)].copy()
nuc["estrato"] = nuc.q.map(est)


def tabela(n):
    rng = np.random.default_rng(SEMENTE)
    o = {}
    for s in ORDEM:
        x = n[n.modelo == s].sort_values("q")
        N = len(x); y = x.y.values; cf = x.cf.astype(float).values
        tem = ~np.isnan(cf); alta = tem & (cf >= 90); erros = tem & ~y
        i = rng.integers(0, N, (B, N))
        al, yy = alta[i], y[i]
        ac = 100 * yy.mean(1); er = 100 * ((al & ~yy).sum(1) / al.sum(1))
        o[s] = dict(n=N, acerto=100 * y.mean(), acerto_ic=np.percentile(ac, [2.5, 97.5]).tolist(), confianca=cf[tem].mean(), respostas_90=100 * alta.sum() / tem.sum(),
                    erro_90=100 * (1 - y[alta].mean()), erro_90_ic=np.percentile(er, [2.5, 97.5]).tolist(), erros_com_90=100 * alta[erros].mean(), auroc=auroc(cf[tem], y[tem]),
                    ece=100 * ece(cf[tem] / 100, y[tem].astype(float)), brier=float(np.mean((cf[tem] / 100 - y[tem]) ** 2)))
    return o


def regs(d):
    d = d[d.area.notna()].reset_index(drop=True).copy()
    d["yf"] = d.y.astype(float)
    A = pd.get_dummies(d.area).astype(float).drop(columns="portugues")
    M = pd.get_dummies(d.modelo).astype(float).drop(columns="gemini-flash")
    x = pd.concat([pd.Series(1.0, index=d.index, name="const"), A, M], axis=1)
    b, ep, _ = pql(x.values, d.yf.values, d.q.values)
    ou = lambda b, e: [float(np.exp(b)), float(np.exp(b - 1.96 * e)), float(np.exp(b + 1.96 * e))]
    r = {"linhas": len(d)}; ps = {}
    for i, nome in enumerate(x.columns):
        if nome in ("direito", "matematica"):
            r["H1_" + nome] = ou(b[i], ep[i]); ps["H1_" + nome] = float(2 * norm.sf(abs(b[i] / ep[i])))
    d2 = d[d.dt.notna()].reset_index(drop=True)
    t = pd.to_datetime(d2.dt)
    d2["an"] = t.dt.year - 2023 + (t.dt.month - 1) / 12
    d2["conv"] = ((d2.yf == 0) & (d2.cf >= 90)).astype(float)
    x2 = pd.concat([pd.Series(1.0, index=d2.index, name="const"), d2.an, pd.get_dummies(d2.familia).astype(float).iloc[:, 1:]], axis=1)
    for alvo, rot in (("yf", "H4_acerto"), ("conv", "H4_erro_entre_todas")):
        b, ep, _ = pql(x2.values, d2[alvo].values, d2.q.values)
        r[rot] = ou(b[1], ep[1]); ps[rot] = float(2 * norm.sf(abs(b[1] / ep[1])))
    d3 = d2[d2.cf >= 90].reset_index(drop=True)
    x3 = pd.concat([pd.Series(1.0, index=d3.index, name="const"), d3.an, pd.get_dummies(d3.familia).astype(float).iloc[:, 1:]], axis=1)
    b, ep, _ = pql(x3.values, (1 - d3.yf).values, d3.q.values)
    r["H4_erro_entre_90"] = ou(b[1], ep[1]); ps["H4_erro_entre_90"] = float(2 * norm.sf(abs(b[1] / ep[1])))
    r["p"] = ps
    return r


def dif_juridico(x, rng):
    j, y = x.jur.values, x.y.values
    ij, id_ = np.where(j)[0], np.where(~j)[0]
    v = 100 * (y[rng.choice(ij, (B, len(ij)))].mean(1) - y[rng.choice(id_, (B, len(id_)))].mean(1))
    return [100 * float(y[ij].mean() - y[id_].mean()), *np.percentile(v, [2.5, 97.5]).tolist()]


def tabela5(n):
    rng = np.random.default_rng(SEMENTE)
    o = {}
    for s in TOP:
        x = n[n.modelo == s]
        d = x[x.gf == "definitivo"]
        o[s] = {"definitivo": dif_juridico(d, rng), "FGV_definitivo": dif_juridico(d[d.banca == "fgv"], rng), "Cebraspe_definitivo": dif_juridico(d[d.banca == "cebraspe"], rng), "todas": dif_juridico(x, rng)}
    return o


def certo_errado(so_def):
    c = lc("respostas_ce.csv").merge(qu[["questao_id", "gabarito_fonte"]], on="questao_id")
    if so_def:
        c = c[c.gabarito_fonte == "definitivo"]
    o = {}
    for s, g in c.groupby("modelo"):
        ok = g[(g.absteve == 0) & (g.formato_ok == 1)]
        o[s] = dict(n=len(g), abstencao=100 * float(g.absteve.mean()), acerto_respondidas=100 * float(ok.acertou.mean()))
    return o


def areas(n):
    """Seção 5.3: bloco jurídico contra as demais áreas, nos três melhores."""
    import statsmodels.api as sm
    rng = np.random.default_rng(SEMENTE)
    o = {}
    for s in TOP:
        x = n[n.modelo == s].sort_values("q")
        j, y, cf = x.jur.values, x.y.values, x.cf.astype(float).values
        tem = ~np.isnan(cf)
        r = {"n": [int(j.sum()), int((~j).sum())], "acerto": [100 * y[j].mean(), 100 * y[~j].mean()], "confianca": [cf[j & tem].mean(), cf[~j & tem].mean()],
             "valor_mais_comum_juridico": [int(pd.Series(cf[j & tem]).value_counts().iloc[0]), int((j & tem).sum())]}
        so = lambda a, b: (cf[a].mean() - 100 * y[a].mean()) - (cf[b].mean() - 100 * y[b].mean())
        ij, id_ = np.where(j & tem)[0], np.where(~j & tem)[0]
        v = [so(rng.choice(ij, len(ij)), rng.choice(id_, len(id_))) for _ in range(B)]
        r["sobreconfianca"] = [float(so(ij, id_)), *np.percentile(v, [2.5, 97.5]).tolist()]
        Z = np.c_[np.ones(len(x)), j, x.banca == "fgv", x.comando_negativo.fillna(0), x.tem_afirmativas.fillna(0), x.cita_norma.fillna(0), np.log(x.n_carac_enunciado.astype(float).clip(lower=1))].astype(float)
        fit = sm.Logit(y.astype(float), Z).fit(disp=0)
        lo, hi = fit.conf_int()[1]
        r["razao_chances"] = [float(np.exp(fit.params[1])), float(np.exp(lo)), float(np.exp(hi))]
        o[s] = r
    return o


def locais(d):
    rng = np.random.default_rng(SEMENTE)
    o = {}
    for s in ORDEM[3:]:
        x = d[d.modelo == s]
        j, y, cf = x.jur.values, x.y.values, x.cf.astype(float).values
        ij, id_ = np.where(j)[0], np.where(~j)[0]
        v = [100 * (y[rng.choice(ij, len(ij))].mean() - y[rng.choice(id_, len(id_))].mean()) for _ in range(2000)]
        tem = ~np.isnan(cf)
        o[s] = dict(n=len(x), dif=100 * float(y[ij].mean() - y[id_].mean()), ic=np.percentile(v, [2.5, 97.5]).tolist(), dif_confianca=float(cf[j & tem].mean() - cf[~j & tem].mean()))
    return o


def sensibilidade_questoes(n):
    """Questões sem texto ou dado (confiança 0 no Gemini) e questões com alternativa suspeita."""
    base = tabela(n)
    sem = set(n[(n.modelo == "gemini-flash") & (n.cf == 0)].q)
    sus = set(lc("questoes_alternativa_suspeita.csv").questao_id)
    o = {}
    for nome, ids in (("sem_texto", sem), ("alternativa_suspeita", sus)):
        t = tabela(n[~n.q.isin(ids)])
        o[nome] = dict(n=len(ids & set(n.q)), max_acerto=max(abs(base[s]["acerto"] - t[s]["acerto"]) for s in t), max_erro_90=max(abs(base[s]["erro_90"] - t[s]["erro_90"]) for s in t),
                       erros_com_90={s: [base[s]["erros_com_90"], t[s]["erros_com_90"]] for s in t}, auroc_gemini=[base["gemini-flash"]["auroc"], t["gemini-flash"]["auroc"]])
    return o


ponderado = lambda n: {s: 100 * float((m := n[n.modelo == s].groupby("estrato").y.mean()).mul(pop.reindex(m.index)).sum() / pop.reindex(m.index).sum()) for s in TOP}
d_n, d_a = nuc[nuc.gf == "definitivo"], rs[rs.gf == "definitivo"]
print("base principal:", d_n.q.nunique(), "questões;", d_a.q.nunique(), "com a extensão")
res = {"principal": dict(tabela4=tabela(d_n), ponderado=ponderado(d_n), reg_comuns=regs(d_n), reg_extensao=regs(d_a), certo_errado=certo_errado(True), areas=areas(d_n), locais=locais(d_a), sensibilidade=sensibilidade_questoes(d_n)),
       "sensibilidade_todas": dict(tabela4=tabela(nuc), reg_comuns=regs(nuc), reg_extensao=regs(rs), certo_errado=certo_errado(False)), "tabela5": tabela5(nuc)}
(R / "resultados").mkdir(exist_ok=True)
json.dump(res, open(R / "resultados" / "resultados.json", "w", encoding="utf-8"), indent=1)
f = lambda v, d=1: f"{v:.{d}f}".replace(".", ",")
for s, t in res["principal"]["tabela4"].items():
    print(f"{s:16} {f(t['acerto'])} ({f(t['acerto_ic'][0])}–{f(t['acerto_ic'][1])}) | {f(t['confianca'])} | {f(t['respostas_90'])} | {f(t['erro_90'])} ({f(t['erro_90_ic'][0])}–{f(t['erro_90_ic'][1])}) | {f(t['erros_com_90'])} | {f(t['auroc'], 2)}")
for s, t in res["tabela5"].items():
    print(s, {k: [f(x) for x in v] for k, v in t.items()})
print({k: v for k, v in res["principal"]["reg_extensao"].items() if k != "p"})
