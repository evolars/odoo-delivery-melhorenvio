"""Cliente HTTP da API do Melhor Envio.

Sem acoplamento com registros do Odoo: recebe o token explicitamente e devolve
dicionários, para poder ser testado sem banco. O `env` é opcional e só serve
para traduzir as mensagens fora de uma requisição HTTP.

Autenticação é `Authorization: Bearer <token>`. O token pode ser o de um
aplicativo OAuth ou o gerado no painel (Integrações → Permissões de acesso);
os dois são JWT e trazem a validade no campo `exp`. Toda chamada leva o
`User-Agent` com o nome da aplicação e um e-mail de contato técnico: o Melhor
Envio exige. Limite de 250 requisições por minuto por usuário.

Compra de etiqueta: carrinho (`/me/cart`), pagamento com o saldo da carteira
(`/me/shipment/checkout`), geração (`/me/shipment/generate`), PDF
(`/me/imprimir/pdf/<id>`). Rastreio pelo status da etiqueta
(`/me/shipment/tracking`) e cancelamento (`/me/shipment/cancel`).

Referência: https://docs.melhorenvio.com.br (cálculo, carrinho, compra,
geração, impressão, status e cancelamento atualizados em 18/06/2026,
conferidos em 05/10/2026).
"""
import base64
import json
import logging
import time
from datetime import datetime, timezone

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PRODUCTION_URL = "https://melhorenvio.com.br"
SANDBOX_URL = "https://sandbox.melhorenvio.com.br"
DEFAULT_TIMEOUT = 30
# A cotação roda no checkout, com o comprador esperando: melhor desistir cedo e
# deixar os outros métodos de entrega aparecerem.
QUOTE_TIMEOUT = 10

# O checkout cota cada método separadamente, e cada mudança no carrinho cota de
# novo. Dois métodos do Melhor Envio (econômico e expresso) mandam o mesmo
# pedido: a resposta traz todos os serviços, então a segunda cotação sai daqui.
# Bairro pelo CEP, quando o endereço não tem: o checkout não pede bairro, e a
# compra da etiqueta exige.
VIACEP_URL = "https://viacep.com.br/ws/%s/json/"

# Motivo de cancelamento: a documentação manda usar sempre 2.
CANCEL_REASON = "2"

QUOTE_CACHE_TTL_S = 600
QUOTE_CACHE_MAX = 256
_QUOTE_CACHE = {}


class MelhorEnvioError(UserError):
    """O Melhor Envio recusou a operação, ou não deu para falar com ele."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _untranslated(source, *args, **kwargs):
    if args or kwargs:
        return source % (args or kwargs)
    return source


def digits(value):
    return "".join(char for char in (value or "") if char.isdigit())


def token_expiry(token):
    """Validade do token (datetime UTC sem fuso, como o Odoo guarda), ou None.

    Lê o `exp` do JWT sem conferir a assinatura: serve só para avisar quando
    vence, quem valida é o Melhor Envio.
    """
    partes = (token or "").strip().split(".")
    if len(partes) != 3:
        return None
    corpo = partes[1] + "=" * (-len(partes[1]) % 4)
    try:
        exp = json.loads(base64.urlsafe_b64decode(corpo)).get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError):
        return None


def district_for_postal_code(postal_code, timeout=10):
    """Bairro do CEP pelo ViaCEP, ou "" (CEP geral de cidade pequena não tem)."""
    cep = digits(postal_code)
    if len(cep) != 8:
        return ""
    try:
        data = requests.get(VIACEP_URL % cep, timeout=timeout).json()
    except (requests.RequestException, ValueError):
        _logger.info("Melhor Envio: ViaCEP não respondeu para %s", cep)
        return ""
    if not isinstance(data, dict) or data.get("erro"):
        return ""
    return (data.get("bairro") or "").strip()


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_options(response):
    """Os serviços que o Melhor Envio cotou, com preço, prazo e transportadora.

    Usa os campos `custom_*`: são os valores com os ajustes que a conta
    configurou (taxa ou desconto, prazo adicional). Serviço que não atende o
    trecho vem com `error` e sem preço, e fica de fora.
    """
    opcoes = []
    for servico in response if isinstance(response, list) else []:
        if not isinstance(servico, dict) or servico.get("error"):
            continue
        preco = _to_float(servico.get("custom_price"))
        if preco is None:
            preco = _to_float(servico.get("price"))
        if preco is None:
            continue
        prazo = servico.get("custom_delivery_time")
        if prazo is None:
            prazo = servico.get("delivery_time")
        opcoes.append({
            "id": servico.get("id"),
            "name": servico.get("name") or "",
            "company": (servico.get("company") or {}).get("name") or "",
            "price": preco,
            "days": prazo,
        })
    return opcoes


class MelhorEnvioClient:
    def __init__(self, token, user_agent, sandbox=False, timeout=DEFAULT_TIMEOUT, env=None):
        self.token = (token or "").strip()
        self.user_agent = user_agent
        self.sandbox = sandbox
        self.timeout = timeout
        self.base_url = SANDBOX_URL if sandbox else PRODUCTION_URL
        self._ = env._ if env is not None else _untranslated

    @staticmethod
    def clear_quote_cache():
        _QUOTE_CACHE.clear()

    # ------------------------------------------------------------------ #
    # Transporte                                                          #
    # ------------------------------------------------------------------ #

    def _request_json(self, method, path, payload=None, timeout=None):
        _ = self._
        if not self.token:
            raise MelhorEnvioError(_(
                "Configure o token do Melhor Envio (painel do Melhor Envio → "
                "Integrações → Permissões de acesso)."
            ))
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % self.token,
            "User-Agent": self.user_agent,
        }
        url = "%s/%s" % (self.base_url, path.lstrip("/"))
        try:
            response = requests.request(
                method, url, headers=headers, json=payload, timeout=timeout or self.timeout,
            )
        except requests.RequestException as error:
            _logger.warning("Melhor Envio: falha de conexão em %s %s: %s", method, path, error)
            raise MelhorEnvioError(_("Não foi possível conectar ao Melhor Envio.")) from error

        try:
            data = response.json() if response.content else {}
        except ValueError:
            # o PDF da etiqueta vem como o endereço do arquivo, às vezes sem aspas
            data = (response.text or "").strip()

        if response.status_code == 401:
            raise MelhorEnvioError(
                _("O Melhor Envio recusou o token: ele está errado ou venceu. Gere outro "
                  "no painel (Integrações → Permissões de acesso)."),
                status_code=401, payload=data,
            )
        if not response.ok:
            detail = self._error_detail(data) or response.reason
            _logger.warning("Melhor Envio: recusa status=%s em %s %s: %s",
                            response.status_code, method, path, detail)
            raise MelhorEnvioError(
                _("O Melhor Envio recusou a operação: %s", detail),
                status_code=response.status_code, payload=data,
            )
        return data

    @staticmethod
    def _flatten(value):
        if isinstance(value, dict):
            return [texto for item in value.values() for texto in MelhorEnvioClient._flatten(item)]
        if isinstance(value, (list, tuple)):
            return [texto for item in value for texto in MelhorEnvioClient._flatten(item)]
        return [str(value)] if value not in (None, "") else []

    @staticmethod
    def _error_detail(data):
        """Texto do erro. A validação (422) vem no formato do Laravel: `message`
        genérico e `errors` com a lista de mensagens por campo. O carrinho usa
        `error`, com listas aninhadas por volume."""
        if not isinstance(data, dict):
            return ""
        erros = MelhorEnvioClient._flatten(data.get("errors")) or (
            MelhorEnvioClient._flatten(data.get("error"))
            if isinstance(data.get("error"), (dict, list)) else []
        )
        if erros:
            return "; ".join(erros)
        return data.get("message") or data.get("error") or ""

    # ------------------------------------------------------------------ #
    # Operações                                                           #
    # ------------------------------------------------------------------ #

    def quote(self, from_postal_code, to_postal_code, products):
        """Cotação de todos os serviços para um par de CEPs. Devolve a resposta
        crua: uma lista com um item por serviço.

        `products` vai no formato "por produtos" da API. O Melhor Envio monta os
        volumes dentro dos limites de cada serviço; mandando a caixa já
        fechada como um produto, ele cota a caixa como ela é.
        """
        _ = self._
        origem, destino = digits(from_postal_code), digits(to_postal_code)
        if len(origem) != 8:
            raise MelhorEnvioError(_(
                "O endereço de onde o pacote sai está sem CEP válido. Corrija o endereço "
                "do depósito (ou da empresa)."
            ))
        if len(destino) != 8:
            raise MelhorEnvioError(_("Informe um CEP válido no endereço de entrega."))

        payload = {
            "from": {"postal_code": origem},
            "to": {"postal_code": destino},
            "products": products,
        }
        chave = (self.base_url, self.token, json.dumps(payload, sort_keys=True))
        guardado = _QUOTE_CACHE.get(chave)
        if guardado and time.time() < guardado[0]:
            return guardado[1]

        response = self._request_json("POST", "/api/v2/me/shipment/calculate",
                                      payload=payload, timeout=QUOTE_TIMEOUT)
        if len(_QUOTE_CACHE) >= QUOTE_CACHE_MAX:
            _QUOTE_CACHE.clear()
        _QUOTE_CACHE[chave] = (time.time() + QUOTE_CACHE_TTL_S, response)
        return response

    def add_to_cart(self, payload):
        """Põe o envio no carrinho. Devolve a etiqueta criada, com o `id` que as
        demais operações usam. Fica no carrinho 7 dias; sem pagamento, some."""
        return self._request_json("POST", "/api/v2/me/cart", payload=payload)

    def checkout(self, order_ids):
        """Paga as etiquetas com o saldo da carteira."""
        return self._request_json("POST", "/api/v2/me/shipment/checkout",
                                  payload={"orders": list(order_ids)})

    def generate(self, order_ids):
        """Gera as etiquetas pagas: a transportadora é avisada e, na Loggi
        Coleta, a coleta é agendada. Devolve {id: {status, message}}."""
        return self._request_json("POST", "/api/v2/me/shipment/generate",
                                  payload={"orders": list(order_ids)})

    def order(self, order_id):
        return self._request_json("GET", "/api/v2/me/orders/%s" % order_id)

    def label_pdf(self, order_id):
        """O PDF da etiqueta. A API devolve o endereço do arquivo, que se baixa
        sem autenticação."""
        _ = self._
        data = self._request_json("GET", "/api/v2/me/imprimir/pdf/%s" % order_id)
        candidatos = []
        if isinstance(data, str):
            candidatos = [data]
        elif isinstance(data, dict):
            candidatos = [data.get("url")] + list(data.values()) + list(data.keys())
        elif isinstance(data, list):
            candidatos = data
        url = next((str(item).strip().strip('"') for item in candidatos
                    if isinstance(item, str) and item.strip().strip('"').startswith("http")), None)
        if not url:
            raise MelhorEnvioError(_("O Melhor Envio não devolveu o arquivo da etiqueta."))
        try:
            response = requests.get(url, timeout=self.timeout)
        except requests.RequestException as error:
            raise MelhorEnvioError(_("Não foi possível baixar a etiqueta.")) from error
        if not response.ok or not response.content.startswith(b"%PDF"):
            raise MelhorEnvioError(_("O arquivo da etiqueta não é um PDF."))
        return response.content

    def tracking(self, order_ids):
        """Status de cada etiqueta: {id: {status, tracking, *_at}}."""
        return self._request_json("POST", "/api/v2/me/shipment/tracking",
                                  payload={"orders": list(order_ids)})

    def cancellable(self, order_ids):
        return self._request_json("POST", "/api/v2/me/shipment/cancellable",
                                  payload={"orders": list(order_ids)})

    def cancel(self, order_id, description):
        """Cancela a etiqueta. Já gerada, o estorno cai na carteira em até 12 horas."""
        return self._request_json("POST", "/api/v2/me/shipment/cancel", payload={
            "order": {"id": order_id, "reason_id": CANCEL_REASON,
                      "description": (description or "")[:255]},
        })
