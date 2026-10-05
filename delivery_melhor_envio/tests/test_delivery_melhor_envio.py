import base64
import json
from datetime import datetime, time, timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.delivery_melhor_envio.models.melhor_envio_client import (
    PRODUCTION_URL,
    SANDBOX_URL,
    MelhorEnvioClient,
    MelhorEnvioError,
    extract_options,
    token_expiry,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, reason="OK"):
        self.status_code = status_code
        self.reason = reason
        self._payload = payload if payload is not None else []
        self.content = json.dumps(self._payload).encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def service(service_id, name, price, days, company="Correios", custom_price=None):
    """Um item da resposta da cotação, como na referência da API."""
    return {
        "id": service_id, "name": name,
        "price": "%.2f" % price,
        "custom_price": "%.2f" % (custom_price if custom_price is not None else price),
        "discount": "0.00", "currency": "R$",
        "delivery_time": days, "custom_delivery_time": days,
        "company": {"id": 1, "name": company},
    }


UNAVAILABLE = {"id": 17, "name": "Mini Envios", "error": "Serviço indisponível para o trecho.",
               "company": {"id": 1, "name": "Correios"}}

QUOTE = [
    service(1, "PAC", 22.10, 6),
    service(2, "SEDEX", 41.50, 2),
    service(3, ".Package", 19.90, 5, company="Jadlog"),
    UNAVAILABLE,
]


def jwt(exp):
    def parte(dados):
        return base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")
    return "%s.%s.assinatura" % (parte({"alg": "RS256"}), parte({"exp": exp}))


@tagged("post_install", "-at_install", "delivery_melhor_envio")
class TestMelhorEnvioClient(TransactionCase):

    def setUp(self):
        super().setUp()
        MelhorEnvioClient.clear_quote_cache()

    def test_environment(self):
        self.assertEqual(MelhorEnvioClient("t", "ua", sandbox=True).base_url, SANDBOX_URL)
        self.assertEqual(MelhorEnvioClient("t", "ua", sandbox=False).base_url, PRODUCTION_URL)

    def test_quote_sends_bearer_user_agent_and_bare_postal_codes(self):
        with patch.object(requests, "request", return_value=FakeResponse(payload=QUOTE)) as call:
            MelhorEnvioClient("tok", "Editora - Odoo (ti@editora.com)").quote(
                "88010-000", "01310-100", [{"id": "volume-1"}])
        args, kwargs = call.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], PRODUCTION_URL + "/api/v2/me/shipment/calculate")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["headers"]["User-Agent"], "Editora - Odoo (ti@editora.com)")
        self.assertEqual(kwargs["json"]["from"], {"postal_code": "88010000"})
        self.assertEqual(kwargs["json"]["to"], {"postal_code": "01310100"})

    def test_same_quote_is_asked_once(self):
        """Econômico e expresso cotam o mesmo pedido: uma chamada só."""
        client = MelhorEnvioClient("tok", "ua")
        with patch.object(requests, "request", return_value=FakeResponse(payload=QUOTE)) as call:
            client.quote("88010000", "01310100", [{"id": "volume-1"}])
            client.quote("88010000", "01310100", [{"id": "volume-1"}])
            client.quote("88010000", "20040002", [{"id": "volume-1"}])
        self.assertEqual(call.call_count, 2)

    def test_missing_token_or_postal_code_fails_before_the_network(self):
        with patch.object(requests, "request") as call:
            with self.assertRaises(MelhorEnvioError):
                MelhorEnvioClient("", "ua").quote("88010000", "01310100", [])
            with self.assertRaises(MelhorEnvioError):
                MelhorEnvioClient("t", "ua").quote("88010000", "0131", [])
            with self.assertRaises(MelhorEnvioError):
                MelhorEnvioClient("t", "ua").quote("", "01310100", [])
        call.assert_not_called()

    def test_validation_error_lists_the_fields(self):
        payload = {"message": "The given data was invalid.",
                   "errors": {"to.postal_code": ["O campo to.postal code é obrigatório."]}}
        with patch.object(requests, "request", return_value=FakeResponse(422, payload)):
            with self.assertRaises(MelhorEnvioError) as caught:
                MelhorEnvioClient("t", "ua").quote("88010000", "01310100", [])
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("to.postal code", str(caught.exception))

    def test_rejected_token_says_what_to_do(self):
        with patch.object(requests, "request",
                          return_value=FakeResponse(401, {"message": "Unauthenticated."})):
            with self.assertRaises(MelhorEnvioError) as caught:
                MelhorEnvioClient("t", "ua").quote("88010000", "01310100", [])
        self.assertIn("Permissões de acesso", str(caught.exception))

    def test_connection_failure_becomes_melhor_envio_error(self):
        with patch.object(requests, "request", side_effect=requests.ConnectionError("x")):
            with self.assertRaises(MelhorEnvioError):
                MelhorEnvioClient("t", "ua").quote("88010000", "01310100", [])

    def test_options_use_the_custom_values_and_drop_unavailable(self):
        opcoes = extract_options([service(1, "PAC", 30.0, 6, custom_price=27.5), UNAVAILABLE])
        self.assertEqual(len(opcoes), 1)
        self.assertEqual(opcoes[0], {"id": 1, "name": "PAC", "company": "Correios",
                                     "price": 27.5, "days": 6})

    def test_token_expiry_comes_from_the_jwt(self):
        validade = datetime(2027, 3, 1, 12, 0, 0)
        exp = int((validade - datetime(1970, 1, 1)).total_seconds())
        self.assertEqual(token_expiry(jwt(exp)), validade)
        self.assertIsNone(token_expiry("nao-e-jwt"))
        self.assertIsNone(token_expiry(False))


class MelhorEnvioCarrierCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        br = cls.env.ref("base.br")
        cls.env.company.write({
            "street": "Rua Felipe Schmidt, 10", "city": "Florianópolis", "zip": "88010-000",
            "state_id": cls.env.ref("base.state_br_sc").id, "country_id": br.id,
            "email": "ti@editora.com.br",
        })
        brl = cls.env.ref("base.BRL")
        brl.active = True
        cls.env.company.currency_id = brl
        cls.box_small = cls.env["stock.package.type"].create({
            "name": "Caixa P", "packaging_length": 240, "width": 170, "height": 50,
            "base_weight": 0.08, "max_weight": 1,
        })
        cls.box_large = cls.env["stock.package.type"].create({
            "name": "Caixa G", "packaging_length": 400, "width": 300, "height": 250,
            "base_weight": 0.35, "max_weight": 15,
        })
        cls.book = cls.env["product.product"].create({
            "name": "Livro", "type": "consu", "list_price": 50.0, "weight": 0.3,
        })
        cls.buyer = cls.env["res.partner"].create({
            "name": "Leitora", "street": "Av. Paulista, 1000", "city": "São Paulo",
            "zip": "01310-100", "state_id": cls.env.ref("base.state_br_sp").id,
            "country_id": br.id,
        })
        cls.foreign_buyer = cls.env["res.partner"].create({
            "name": "Ana Lisboa", "street": "Rua Augusta, 100", "city": "Lisboa",
            "zip": "1100-053", "country_id": cls.env.ref("base.pt").id,
        })
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "Correios econômico", "delivery_type": "melhor_envio",
            "integration_level": "rate", "prod_environment": True,
            "product_id": cls.env["product.product"].create({
                "name": "Frete", "type": "service",
            }).id,
            "melhor_envio_token": "tok",
            "melhor_envio_services": "1,17",
            "melhor_envio_package_type_ids": [(6, 0, [cls.box_small.id, cls.box_large.id])],
            "melhor_envio_default_package_type_id": cls.box_large.id,
        })

    def setUp(self):
        super().setUp()
        MelhorEnvioClient.clear_quote_cache()

    def _order(self, qty=2, partner=None):
        return self.env["sale.order"].create({
            "partner_id": (partner or self.buyer).id,
            "order_line": [(0, 0, {"product_id": self.book.id, "product_uom_qty": qty,
                                   "tax_id": [(5, 0, 0)]})],
        })

    def _rate(self, order, payload=QUOTE, carrier=None):
        with patch.object(requests, "request", return_value=FakeResponse(payload=payload)) as call:
            return (carrier or self.carrier).rate_shipment(order), call


@tagged("post_install", "-at_install", "delivery_melhor_envio")
class TestMelhorEnvioRating(MelhorEnvioCarrierCase):

    def test_cheapest_allowed_service_wins(self):
        """O Jadlog é mais barato, mas o método só aceita PAC e Mini Envios."""
        result, _call = self._rate(self._order())
        self.assertTrue(result["success"])
        self.assertAlmostEqual(result["price"], 22.10, 2)
        self.assertIn("PAC", result["warning_message"])
        self.assertIn("6 dias", result["warning_message"])

    def test_empty_services_means_any(self):
        self.carrier.melhor_envio_services = False
        result, _call = self._rate(self._order())
        self.assertAlmostEqual(result["price"], 19.90, 2)

    def test_fastest(self):
        self.carrier.write({"melhor_envio_services": "1,2", "melhor_envio_choice": "fastest"})
        result, _call = self._rate(self._order())
        self.assertAlmostEqual(result["price"], 41.50, 2)

    def test_payload_is_the_closed_box(self):
        _result, call = self._rate(self._order(qty=2))
        corpo = call.call_args[1]["json"]
        self.assertEqual(corpo["from"], {"postal_code": "88010000"})
        self.assertEqual(corpo["to"], {"postal_code": "01310100"})
        # 2 livros de 0,3 kg + caixa P de 0,08 kg; 240 × 170 × 50 mm vira 24 × 17 × 5 cm
        self.assertEqual(corpo["products"], [{
            "id": "volume-1", "width": 17, "height": 5, "length": 24,
            "weight": 0.68, "insurance_value": 100.0, "quantity": 1,
        }])

    def test_heavy_order_goes_in_the_large_box(self):
        _result, call = self._rate(self._order(qty=5))
        produto = call.call_args[1]["json"]["products"][0]
        self.assertEqual((produto["length"], produto["width"], produto["height"]), (40, 30, 25))
        self.assertAlmostEqual(produto["weight"], 1.85, 3)

    def test_origin_is_the_warehouse_address(self):
        expedicao = self.env["res.partner"].create({
            "name": "Expedição", "parent_id": self.env.company.partner_id.id,
            "type": "delivery", "zip": "88015-200", "country_id": self.env.ref("base.br").id,
        })
        order = self._order()
        order.warehouse_id.partner_id = expedicao
        _result, call = self._rate(order)
        self.assertEqual(call.call_args[1]["json"]["from"], {"postal_code": "88015200"})

    def test_no_allowed_service_does_not_raise(self):
        result, _call = self._rate(self._order(), payload=[service(3, ".Package", 19.9, 5),
                                                           UNAVAILABLE])
        self.assertFalse(result["success"])
        self.assertIn("não atende", result["error_message"])

    def test_api_failure_does_not_raise_during_checkout(self):
        with patch.object(requests, "request", side_effect=requests.ConnectionError("x")):
            result = self.carrier.rate_shipment(self._order())
        self.assertFalse(result["success"])
        self.assertTrue(result["error_message"])

    def test_missing_email_is_reported(self):
        self.env.company.email = False
        self.carrier.melhor_envio_contact_email = False
        result, call = self._rate(self._order())
        self.assertFalse(result["success"])
        self.assertIn("e-mail", result["error_message"])
        call.assert_not_called()

    def test_test_environment_uses_the_sandbox(self):
        self.carrier.prod_environment = False
        _result, call = self._rate(self._order())
        self.assertTrue(call.call_args[0][1].startswith(SANDBOX_URL))

    def test_only_brazil(self):
        self.assertTrue(self.carrier._is_available_for_order(self._order()))
        self.assertFalse(self.carrier._is_available_for_order(
            self._order(partner=self.foreign_buyer)))

    def test_test_button_lists_the_prices(self):
        with patch.object(requests, "request", return_value=FakeResponse(payload=QUOTE)) as call:
            action = self.carrier.action_melhor_envio_test_connection()
        self.assertEqual(call.call_args[1]["json"]["to"], {"postal_code": "01001000"})
        self.assertEqual(call.call_args[1]["json"]["products"][0]["length"], 24)
        mensagem = str(action["params"]["message"])
        self.assertIn("PAC", mensagem)
        self.assertIn("SEDEX", mensagem)

    def test_test_button_quotes_from_the_warehouse(self):
        """Sem pedido, a origem é o depósito, não o endereço da empresa."""
        expedicao = self.env["res.partner"].create({
            "name": "Expedição", "parent_id": self.env.company.partner_id.id,
            "type": "delivery", "zip": "88036-530", "country_id": self.env.ref("base.br").id,
        })
        self.env["stock.warehouse"].search(
            [("company_id", "=", self.env.company.id)], limit=1).partner_id = expedicao
        with patch.object(requests, "request", return_value=FakeResponse(payload=QUOTE)) as call:
            self.carrier.action_melhor_envio_test_connection()
        self.assertEqual(call.call_args[1]["json"]["from"], {"postal_code": "88036530"})

    def test_shipping_is_not_created_here(self):
        with self.assertRaises(UserError):
            self.carrier.send_shipping(self.env["stock.picking"])

    def test_tracking_link_uses_melhor_rastreio(self):
        picking = self.env["stock.picking"].new({"carrier_tracking_ref": "AB123456789BR"})
        self.assertEqual(self.carrier.get_tracking_link(picking),
                         "https://melhorrastreio.com.br/rastreio/AB123456789BR")


@tagged("post_install", "-at_install", "delivery_melhor_envio")
class TestMelhorEnvioToken(MelhorEnvioCarrierCase):

    def _expiring_in(self, days):
        # meio-dia UTC: o dia do vencimento não depende da hora em que o teste roda
        dia = fields.Date.context_today(self.carrier) + timedelta(days=days)
        validade = datetime.combine(dia, time(12, 0))
        exp = int((validade - datetime(1970, 1, 1)).total_seconds())
        self.carrier.melhor_envio_token = jwt(exp)

    def test_form_warns_when_close_to_expiry(self):
        self._expiring_in(60)
        self.assertTrue(self.carrier.melhor_envio_token_expiry)
        self.assertFalse(self.carrier.melhor_envio_token_expiring)
        self._expiring_in(10)
        self.assertTrue(self.carrier.melhor_envio_token_expiring)

    def test_reminder_email_on_the_warning_days_only(self):
        self.env.ref("base.user_admin").email = "admin@editora.com.br"
        emails = self.env["mail.mail"].sudo()

        self._expiring_in(20)
        antes = emails.search_count([])
        self.env["delivery.carrier"]._cron_melhor_envio_token_reminder()
        self.assertEqual(emails.search_count([]), antes)

        self._expiring_in(7)
        self.env["delivery.carrier"]._cron_melhor_envio_token_reminder()
        aviso = emails.search([], order="id desc", limit=1)
        self.assertIn("Correios econômico", aviso.subject)
        self.assertIn("admin@editora.com.br", aviso.email_to)
