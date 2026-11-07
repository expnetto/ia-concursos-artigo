"""Adaptadores de banca. Cada módulo expõe PERFIL, metadados_caderno(), ler_gabarito() e, se a
banca tiver mais de um formato de prova, segmentar()."""

from iac.bancas import cebraspe, fgv

ADAPTADORES = {"fgv": fgv, "cebraspe": cebraspe}
