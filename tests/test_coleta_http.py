"""Cliente HTTP da coleta: recusa por política (permanente) × falha transitória (tentar de novo)."""

import socket

import httpx
import pytest

from iac.coleta import http as hmod
from iac.coleta.http import ClienteEducado, Recusado, Transitorio

PDF = b"%PDF-1.7 conteudo"


@pytest.fixture(autouse=True)
def dns_publico(monkeypatch):
    monkeypatch.setattr(hmod.socket, "getaddrinfo", lambda host, porta: [(None, None, None, None, ("200.1.2.3", 0))])
    monkeypatch.setattr(hmod.time, "sleep", lambda s: None)


def _cliente(rotas: dict, hosts=("a.org",)):
    """rotas: caminho -> Response, ou lista de Responses consumidas em ordem."""
    chamadas = []

    def tratar(req: httpx.Request):
        chamadas.append(str(req.url))
        alvo = rotas.get(req.url.path, httpx.Response(404))
        if isinstance(alvo, list):
            alvo = alvo.pop(0) if len(alvo) > 1 else alvo[0]
        if isinstance(alvo, Exception):
            raise alvo
        return alvo

    cliente = ClienteEducado(set(hosts), intervalo_s=0, transporte=httpx.MockTransport(tratar))
    return cliente, chamadas


def test_pdf_ok_sem_robots():
    cliente, _ = _cliente({"/x.pdf": httpx.Response(200, content=PDF)})
    assert cliente.baixar_pdf("https://a.org/x.pdf") == PDF


def test_host_fora_da_lista_e_recusa():
    cliente, _ = _cliente({})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://b.org/x.pdf")


def test_ip_privado_e_recusa(monkeypatch):
    monkeypatch.setattr(hmod.socket, "getaddrinfo", lambda h, p: [(None, None, None, None, ("10.0.0.1", 0))])
    cliente, _ = _cliente({})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_dns_falhando_e_transitorio(monkeypatch):
    def falha(h, p):
        raise socket.gaierror("sem rede")
    monkeypatch.setattr(hmod.socket, "getaddrinfo", falha)
    cliente, _ = _cliente({})
    with pytest.raises(Transitorio):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_robots_que_proibe_e_recusa():
    cliente, _ = _cliente({"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /")})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_robots_503_e_transitorio_e_nao_fica_em_cache():
    rotas = {"/robots.txt": [httpx.Response(503), httpx.Response(503), httpx.Response(503), httpx.Response(404)],
             "/x.pdf": httpx.Response(200, content=PDF)}
    cliente, _ = _cliente(rotas)
    with pytest.raises(Transitorio):
        cliente.baixar_pdf("https://a.org/x.pdf")
    assert cliente.baixar_pdf("https://a.org/x.pdf") == PDF     # a próxima tentativa relê o robots


def test_robots_timeout_e_transitorio():
    cliente, _ = _cliente({"/robots.txt": httpx.ReadTimeout("lento")})
    with pytest.raises(Transitorio):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_robots_403_e_recusa():
    cliente, _ = _cliente({"/robots.txt": httpx.Response(403)})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_robots_redireciona_para_fora_da_lista_nao_e_seguido():
    cliente, chamadas = _cliente({"/robots.txt": httpx.Response(301, headers={"location": "https://mal.org/robots.txt"})})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")
    assert not any("mal.org" in c for c in chamadas)


def test_503_no_pdf_tenta_de_novo_e_consegue():
    cliente, chamadas = _cliente({"/x.pdf": [httpx.Response(503, headers={"retry-after": "1"}),
                                             httpx.Response(200, content=PDF)]})
    assert cliente.baixar_pdf("https://a.org/x.pdf") == PDF
    assert sum(c.endswith("/x.pdf") for c in chamadas) == 2


def test_429_persistente_e_transitorio():
    cliente, _ = _cliente({"/x.pdf": [httpx.Response(429)]})
    with pytest.raises(Transitorio):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_404_e_recusa():
    cliente, _ = _cliente({})
    with pytest.raises(Recusado, match="404"):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_erro_de_conexao_no_pdf_e_transitorio():
    cliente, _ = _cliente({"/x.pdf": httpx.ConnectError("caiu")})
    with pytest.raises(Transitorio):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_conteudo_que_nao_e_pdf_e_recusa():
    cliente, _ = _cliente({"/x.pdf": httpx.Response(200, content=b"<html>")})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_redirecionamento_para_fora_da_lista_e_recusa():
    cliente, _ = _cliente({"/x.pdf": httpx.Response(302, headers={"location": "https://mal.org/x.pdf"})})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_pdf_grande_demais_e_recusa(monkeypatch):
    monkeypatch.setattr(hmod.config, "TAMANHO_MAXIMO_PDF", 10)
    cliente, _ = _cliente({"/x.pdf": httpx.Response(200, content=PDF * 5)})
    with pytest.raises(Recusado):
        cliente.baixar_pdf("https://a.org/x.pdf")


def test_json_grande_demais_e_recusa_sem_ler_tudo():
    cliente, _ = _cliente({"/api": httpx.Response(200, content=b"[" + b"1," * 100 + b"1]")})
    with pytest.raises(Recusado):
        cliente.obter_json("https://a.org/api", max_bytes=50)


def test_json_ok():
    cliente, _ = _cliente({"/api": httpx.Response(200, json={"a": 1})})
    assert cliente.obter_json("https://a.org/api") == {"a": 1}
