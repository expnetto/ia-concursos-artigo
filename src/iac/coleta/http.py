"""Cliente HTTP "educado" para coletar PDFs dos sites oficiais das bancas.

Requisitos de segurança e cortesia (revisão de 15/10/2026):
- só hosts da lista permitida (sem SSRF: nada de IP privado, nada de host fora da banca);
- robots.txt respeitado e cacheado por host; robots que proíbe ou responde 401/403 = NÃO permitido;
- 1 requisição por segundo por host, User-Agent identificado;
- redirecionamentos revalidados (máximo 3, inclusive no robots.txt), download em streaming com limite
  de bytes e prazo total, conteúdo precisa começar com %PDF-; o nome vindo do servidor nunca vira caminho.

Dois tipos de falha (revisão 2, ALTO-2):
- `Recusado`: decisão de política, permanente (host, robots, 4xx, tipo, tamanho);
- `Transitorio`: rede, DNS, 429/5xx, robots indisponível. Nunca fica em cache; a próxima rodada tenta de novo.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

from iac import config

MAX_REDIRECIONAMENTOS = 3
TENTATIVAS_TRANSITORIAS = 3              # 429/5xx: tenta até 3 vezes antes de desistir nesta rodada
ESPERA_MAXIMA_RETRY_S = 60.0
PRAZO_TOTAL_S = 300.0                    # um servidor lento não segura a conexão para sempre
TAMANHO_MAXIMO_ROBOTS = 512 * 1024
_REDIRECIONA = (301, 302, 303, 307, 308)


class Recusado(Exception):
    """Download recusado por política (host, robots, tipo, tamanho, 4xx). Permanente."""


class Transitorio(Exception):
    """Falha passageira (rede, DNS, 429/5xx, robots indisponível). Tentar de novo depois."""


def _espera_retry(resposta: httpx.Response, tentativa: int) -> float:
    try:
        segundos = float(resposta.headers.get("retry-after", ""))
    except ValueError:
        segundos = float("nan")
    if not 0 <= segundos <= ESPERA_MAXIMA_RETRY_S:      # ausente, negativo, NaN ou longo demais
        segundos = min(2.0 ** tentativa * 5, ESPERA_MAXIMA_RETRY_S)
    return segundos


class ClienteEducado:
    def __init__(self, hosts_permitidos: set[str], intervalo_s: float = config.INTERVALO_MINIMO_POR_HOST_S,
                 transporte: httpx.BaseTransport | None = None):
        self.hosts = {h.lower() for h in hosts_permitidos}
        self.intervalo = intervalo_s
        self._ultimo: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._http = httpx.Client(headers={"User-Agent": config.USER_AGENT}, timeout=config.TIMEOUT_HTTP_S,
                                  follow_redirects=False, transport=transporte)

    def fechar(self) -> None:
        self._http.close()

    def _validar_host(self, url: str) -> str:
        partes = urlparse(url)
        host = (partes.hostname or "").lower()
        if partes.scheme not in ("http", "https") or host not in self.hosts:
            raise Recusado(f"host fora da lista: {host or url}")
        try:
            enderecos = socket.getaddrinfo(host, None)
        except socket.gaierror as erro:
            raise Transitorio(f"host não resolve: {host}") from erro
        for info in enderecos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise Recusado(f"host resolve para endereço interno: {host}")
        return host

    def _esperar(self, host: str) -> None:
        espera = self._ultimo.get(host, 0.0) + self.intervalo - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        self._ultimo[host] = time.monotonic()

    def _get(self, url: str, max_bytes: int, checar_robots: bool = True) -> tuple[int, bytes]:
        """GET com host revalidado a cada redirecionamento, retry em 429/5xx, limite de bytes e prazo total.

        Devolve (status, corpo) para 200 e 4xx (o chamador decide); 429/5xx/rede viram Transitorio.
        """
        for _ in range(MAX_REDIRECIONAMENTOS + 1):
            host = self._validar_host(url)
            if checar_robots and not self._pode(url, host):
                raise Recusado(f"robots.txt proíbe: {url}")   # texto novo: o antigo marcava falhas de rede
            status, corpo, destino = self._get_um(url, host, max_bytes)
            if destino is None:
                return status, corpo
            url = destino
        raise Recusado("redirecionamentos demais")

    def _get_um(self, url: str, host: str, max_bytes: int) -> tuple[int, bytes, str | None]:
        """Uma URL (sem seguir redirecionamento): (status, corpo, destino_do_redirecionamento | None)."""
        for tentativa in range(TENTATIVAS_TRANSITORIAS):
            self._esperar(host)
            inicio = time.monotonic()
            try:
                with self._http.stream("GET", url) as resposta:
                    if resposta.status_code in _REDIRECIONA:
                        return resposta.status_code, b"", urljoin(url, resposta.headers.get("location", ""))
                    if resposta.status_code == 429 or resposta.status_code >= 500:
                        if tentativa + 1 < TENTATIVAS_TRANSITORIAS:
                            time.sleep(_espera_retry(resposta, tentativa))
                            continue
                        raise Transitorio(f"HTTP {resposta.status_code}")
                    if resposta.status_code != 200:
                        return resposta.status_code, b"", None
                    return 200, self._ler_corpo(resposta, max_bytes, inicio), None
            except httpx.RequestError as erro:     # rede, timeout, gzip corrompido (DecodingError)...
                raise Transitorio(f"{type(erro).__name__}: {erro}") from erro
        raise Transitorio("tentativas esgotadas")       # inalcançável; mantém o contrato explícito

    @staticmethod
    def _ler_corpo(resposta: httpx.Response, max_bytes: int, inicio: float) -> bytes:
        if int(resposta.headers.get("content-length") or 0) > max_bytes:
            raise Recusado(f"arquivo grande demais ({resposta.headers['content-length']} bytes)")
        dados = bytearray()
        for pedaco in resposta.iter_bytes():
            dados.extend(pedaco)
            if len(dados) > max_bytes:
                raise Recusado("arquivo grande demais (streaming)")
            if time.monotonic() - inicio > PRAZO_TOTAL_S:
                raise Transitorio("prazo total do download estourado")
        return bytes(dados)

    def _pode(self, url: str, host: str) -> bool:
        if host not in self._robots:
            base = f"{urlparse(url).scheme}://{host}/robots.txt"
            status, corpo = self._get(base, TAMANHO_MAXIMO_ROBOTS, checar_robots=False)   # Transitorio sobe sem cache
            robots: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            if status == 200:
                robots.parse(corpo.decode("utf-8", errors="replace").splitlines())
            elif status in (401, 403):
                robots = None                    # acesso negado ao robots = não permitido (política)
            else:
                robots.parse([])                 # 404/410 e outros 4xx: sem robots = tudo permitido
            self._robots[host] = robots
        robots = self._robots[host]
        return bool(robots and robots.can_fetch(config.USER_AGENT, url))

    def obter_json(self, url: str, max_bytes: int = 20 * 1024 * 1024):
        """GET de uma API pública da banca (mesmas regras de host, robots, ritmo e tamanho)."""
        status, corpo = self._get(url, max_bytes)
        if status != 200:
            raise Recusado(f"HTTP {status}")
        return httpx.Response(200, content=corpo).json()

    def baixar_pdf(self, url: str) -> bytes:
        status, corpo = self._get(url, config.TAMANHO_MAXIMO_PDF)
        if status != 200:
            raise Recusado(f"HTTP {status}")
        if not corpo.startswith(b"%PDF-"):
            raise Recusado("conteúdo não é PDF")
        return corpo
