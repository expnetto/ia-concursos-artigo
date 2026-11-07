"""Linha de comando: `uv run iac <etapa>`. Cada etapa é idempotente e pode ser repetida."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iac import config
from iac import db as dbm


def _status(conexao) -> None:
    consultas = {
        "fluxograma de exclusões": "SELECT etapa, n FROM v_funil ORDER BY ordem",
        "cadernos por gabarito": "SELECT COALESCE(gabarito_status, '(quarentena)'), COUNT(*) FROM caderno GROUP BY 1 ORDER BY 2 DESC",
        "exclusões por etapa e motivo": "SELECT etapa, entidade, motivo, COUNT(*) FROM exclusao GROUP BY 1,2,3 ORDER BY 1, 4 DESC",
        "elegíveis por disciplina": "SELECT COALESCE(disciplina,'(a classificar)'), COUNT(*) FROM questao WHERE elegivel = 1 GROUP BY 1 ORDER BY 2 DESC",
        "execuções": "SELECT m.slug, c.slug, COUNT(*), SUM(e.status='erro'), ROUND(SUM(e.custo_usd),4) FROM execucao e JOIN modelo m ON m.id=e.modelo_id JOIN condicao c ON c.id=e.condicao_id GROUP BY 1,2",
    }
    for rotulo, sql in consultas.items():
        print(f"{rotulo}:")
        for linha in conexao.execute(sql).fetchall():
            print("   ", " | ".join(str(v) for v in linha))


def _secoes(conexao) -> None:
    """Tabela seção → disciplina para revisão manual (achado E2/E3 da revisão)."""
    from iac.disciplinas import disciplina_da_secao
    for secao, n in conexao.execute(
            "SELECT secao, COUNT(*) FROM item GROUP BY secao ORDER BY 2 DESC").fetchall():
        print(f"{n:5d} | {secao!s:70.70} | {disciplina_da_secao(secao)}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    config.carregar_env()
    parser = argparse.ArgumentParser(prog="iac", description=__doc__)
    sub = parser.add_subparsers(dest="etapa", required=True)

    p = sub.add_parser("importar-legado", help="importa PDFs já coletados em ~/dataprev2026")
    p.add_argument("--raiz", type=Path, default=Path.home() / "dataprev2026")
    p = sub.add_parser("descobrir", help="registra URLs de provas e gabaritos de uma banca")
    p.add_argument("banca", choices=["fgv", "cebraspe"])
    p.add_argument("--raiz", type=Path, default=Path.home() / "dataprev2026")
    p = sub.add_parser("baixar", help="baixa os documentos pendentes (respeita robots.txt, 1 req/s)")
    p.add_argument("--banca")
    p.add_argument("--limite", type=int)
    p = sub.add_parser("extrair", help="segmenta cadernos de prova em itens")
    p.add_argument("--refazer", action="store_true", help="apaga e refaz toda a extração")
    sub.add_parser("gabaritar", help="casa gabaritos com os cadernos")
    sub.add_parser("consolidar", help="deduplica itens em questões e aplica elegibilidade")
    sub.add_parser("preparar", help="extrair + gabaritar + consolidar")
    sub.add_parser("status", help="resumo do banco")
    sub.add_parser("secoes", help="tabela seção → disciplina para revisão")
    sub.add_parser("datas-pendentes", help="concursos sem data da prova que impedem questões de entrar no corpus")
    p = sub.add_parser("definir-data", help="informa a data da prova de um concurso (do edital ou à mão)")
    p.add_argument("banca", choices=["fgv", "cebraspe"])
    p.add_argument("slug")
    p.add_argument("data", help="AAAA-MM-DD")
    p.add_argument("--fonte", choices=["manual", "edital"], default="manual")
    p = sub.add_parser("classificar", help="disciplina das questões sem seção específica, por LLM local")
    p.add_argument("--modelo", default="qwen2.5-7b")
    p.add_argument("--limite", type=int)
    p = sub.add_parser("validar-disciplinas", help="planilha às cegas (--exportar) ou concordância (--importar)")
    p.add_argument("--exportar", type=Path)
    p.add_argument("--importar", type=Path)
    p.add_argument("--n", type=int, default=100)
    p = sub.add_parser("backup", help="cópia consistente do banco")
    p.add_argument("destino", type=Path)

    p = sub.add_parser("modelos", help="sincroniza config/modelos.toml e condicoes.toml com o banco")
    p = sub.add_parser("amostrar", help="cria amostra estratificada congelada")
    p.add_argument("--nome", required=True)
    p.add_argument("--tamanho", type=int, required=True)
    p.add_argument("--semente", type=int, default=20261015)
    p.add_argument("--banca", action="append", help="restringe a estas bancas (onda); pode repetir")
    p.add_argument("--excluir", action="append", help="exclui questões destas amostras (ex.: piloto)")
    p.add_argument("--formato", choices=["ME", "CE"])
    p.add_argument("--so-classificadas", action="store_true", help="só questões com disciplina (estrato real)")
    p = sub.add_parser("executar", help="roda modelos sobre uma amostra")
    p.add_argument("--amostra", required=True)
    p.add_argument("--modelo", action="append", required=True, help="slug; pode repetir")
    p.add_argument("--condicao", default="me_padrao")
    p.add_argument("--limite", type=int, default=None, help="máximo de execuções nesta rodada")
    sub.add_parser("avaliar", help="interpreta respostas brutas ainda não avaliadas")
    p = sub.add_parser("contexto", help="janela de contexto local: equivalência e execuções que encostaram no limite")
    p.add_argument("acao", choices=["equivalencia", "marcar"])
    p.add_argument("--modelo", required=True)
    p.add_argument("--limite", type=int, help="marcar: contexto usado (ex.: 4096)")
    p.add_argument("--desde", help="marcar: ISO UTC do início da rodada com o contexto menor")
    p.add_argument("--condicao", default="me_padrao")
    p.add_argument("--amostra", default="extensao_me")
    p.add_argument("--n", type=int, default=150)
    p.add_argument("--saida", type=Path, help="equivalencia: grava o resultado em JSON")
    p = sub.add_parser("enem", help="pontes do ENEM: ingerir (itens do INEP + textos) e resumir")
    p.add_argument("acao", choices=["ingerir", "resumo", "ancora"])
    p.add_argument("--fontes", type=Path, default=None, help="pasta com inep/<ano>/ e textos/ (padrão: data/enem)")
    p.add_argument("--baixar", action="store_true", help="baixa do INEP e do Hugging Face o que faltar")
    p.add_argument("--anos", type=int, nargs="+", default=[2017, 2022, 2023, 2024])
    p = sub.add_parser("vias", help="lista os modelos que cada via oferece hoje")
    p.add_argument("via", nargs="?", help="só esta via")

    args = parser.parse_args(argv)
    conexao = dbm.conectar()

    if args.etapa == "descobrir":
        if args.banca == "fgv":
            from iac.coleta import fgv as coleta_fgv
            print(coleta_fgv.descobrir_pelo_indice_legado(conexao, args.raiz))
        else:
            from iac.coleta import cebraspe as coleta_cebraspe
            coleta_cebraspe.descobrir(conexao)
    elif args.etapa == "baixar":
        from iac import coleta
        coleta.baixar(conexao, args.banca, args.limite)
    elif args.etapa == "importar-legado":
        from iac.ingestao import importar_legado
        print(importar_legado(conexao, args.raiz))
    elif args.etapa in ("extrair", "preparar"):
        from iac import preparo
        preparo.extrair(conexao, refazer=getattr(args, "refazer", False))
        if args.etapa == "preparar":
            preparo.gabaritar(conexao)
            preparo.consolidar(conexao)
    elif args.etapa == "gabaritar":
        from iac import preparo
        preparo.gabaritar(conexao)
    elif args.etapa == "consolidar":
        from iac import preparo
        preparo.consolidar(conexao)
    elif args.etapa == "status":
        _status(conexao)
    elif args.etapa == "secoes":
        _secoes(conexao)
    elif args.etapa == "datas-pendentes":
        from iac import preparo
        pendentes = preparo.concursos_sem_data(conexao)
        for linha in pendentes:
            print(f"{linha['sigla']:8} {linha['slug']:30} edital={linha['ano_edital'] or '?':4} "
                  f"cadernos={linha['cadernos']:3} {linha['titulo'] or ''} {linha['url_fonte'] or ''}")
        print(f"{len(pendentes)} concursos; preencha com: iac definir-data <banca> <slug> AAAA-MM-DD --fonte edital")
    elif args.etapa == "definir-data":
        from iac import preparo
        preparo.definir_data(conexao, args.banca, args.slug, args.data, args.fonte)
        print(f"{args.banca}/{args.slug}: {args.data} ({args.fonte}); rode `iac consolidar` para aplicar")
    elif args.etapa == "classificar":
        from iac import classificacao
        classificacao.classificar(conexao, args.modelo, args.limite)
    elif args.etapa == "validar-disciplinas":
        from iac import classificacao
        chave = config.DADOS / "validacao_disciplina_chave.json"
        if args.exportar:
            print(classificacao.exportar_validacao(conexao, args.exportar, args.n, chave=chave))
        elif args.importar:
            print(classificacao.importar_validacao(conexao, args.importar, chave))
    elif args.etapa == "backup":
        print(dbm.backup(conexao, args.destino))
    elif args.etapa == "modelos":
        from iac import catalogo, locais
        print(catalogo.sincronizar(conexao))
        print(locais.registrar_metadados(conexao))
    elif args.etapa == "amostrar":
        from iac import amostragem
        print(amostragem.criar(conexao, args.nome, args.tamanho, args.semente, args.banca, args.excluir,
                               args.formato, args.so_classificadas))
    elif args.etapa == "executar":
        from iac import executor
        totais = executor.executar(conexao, args.amostra, args.modelo, args.condicao, args.limite)
        print(totais)
        if totais.get("em_andamento") or totais.get("descartadas"):
            return 3                     # há execuções por concluir: quem automatiza não deve tratar como fim
    elif args.etapa == "vias":
        from iac import catalogo
        from iac.provedores import Cliente
        for nome, via in catalogo.vias().items():
            if args.via and nome != args.via:
                continue
            try:
                cliente = Cliente(via)
                print(f"{nome}: {', '.join(cliente.listar_modelos())}")
                cliente.fechar()
            except Exception as erro:  # noqa: BLE001 — diagnóstico para o usuário
                print(f"{nome}: indisponível ({type(erro).__name__}: {str(erro)[:120]})")
    elif args.etapa == "enem":
        import json
        from iac import enem
        if args.acao == "ingerir":
            resultado = enem.ingerir(conexao, args.fontes or config.DADOS / "enem", tuple(args.anos), args.baixar)
            print(json.dumps(resultado, ensure_ascii=False, indent=1))
        elif args.acao == "resumo":
            print(json.dumps(enem.resumo(conexao), ensure_ascii=False, indent=1))
        else:
            for linha in enem.ancora_por_disciplina(conexao):
                print(f"{linha['disciplina']:24} {linha['sigla']:9} todas={linha['todas']:5} medio={linha['medio'] or 0:4} "
                      f"sem_nivel={linha['sem_nivel'] or 0:5}")
    elif args.etapa == "avaliar":
        from iac import avaliador
        print(avaliador.avaliar_pendentes(conexao))
    elif args.etapa == "contexto":
        import json
        from iac import contexto
        if args.acao == "marcar":
            if not (args.limite and args.desde):
                parser.error("contexto marcar exige --limite e --desde")
            print({"marcadas_para_refazer": contexto.marcar_insuficientes(conexao, args.modelo, args.desde, args.limite)})
        else:
            resultado = contexto.equivalencia(conexao, args.modelo, args.condicao, args.amostra, args.n)
            print(json.dumps({k: v for k, v in resultado.items() if k != "divergentes"}))
            if args.saida:
                args.saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
