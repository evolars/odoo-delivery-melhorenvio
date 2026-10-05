import logging
import math
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount, format_date

from .melhor_envio_client import (
    MelhorEnvioClient,
    MelhorEnvioError,
    extract_options,
    token_expiry,
)

_logger = logging.getLogger(__name__)

CHOICES = [
    ("cheapest", "Mais barato"),
    ("fastest", "Mais rápido"),
]

TRACKING_URL = "https://melhorrastreio.com.br/rastreio/%s"

# CEP de destino da cotação de teste: Praça da Sé, São Paulo.
TEST_DESTINATION = "01001000"

# Avisos por e-mail antes de o token vencer. Sem token válido a cotação falha e o
# método some do checkout, sem ninguém perceber.
TOKEN_WARNING_DAYS = 15
TOKEN_REMINDER_DAYS = (15, 7, 3, 1, 0)


def ceil_int(value):
    """Arredonda para cima, sem que o ruído do float vire uma unidade a mais."""
    return int(math.ceil(round(value or 0.0, 6)))


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(
        selection_add=[("melhor_envio", "Melhor Envio")],
        ondelete={"melhor_envio": "set default"},
    )
    melhor_envio_token = fields.Char(string="Token", groups="base.group_system")
    melhor_envio_token_expiry = fields.Datetime(
        string="Token válido até", compute="_compute_melhor_envio_token_expiry",
        compute_sudo=True,
    )
    melhor_envio_token_expiring = fields.Boolean(compute="_compute_melhor_envio_token_expiry",
                                                 compute_sudo=True)
    melhor_envio_contact_email = fields.Char(
        string="E-mail técnico",
        default=lambda self: self.env.company.email,
        help="Vai no User-Agent de toda chamada: o Melhor Envio exige um e-mail de "
             "contato técnico da integração.",
    )
    melhor_envio_services = fields.Char(
        string="Serviços",
        help="IDs dos serviços do Melhor Envio que este método pode usar, separados por "
             "vírgula. Correios: 1 PAC, 2 SEDEX, 17 Mini Envios. Jadlog: 3 .Package, "
             "4 .Com. Vazio: qualquer serviço que o Melhor Envio cotar.",
    )
    melhor_envio_choice = fields.Selection(CHOICES, string="Escolher", default="cheapest")
    melhor_envio_default_package_type_id = fields.Many2one(
        "stock.package.type", string="Embalagem padrão",
        help="Caixa usada quando nenhuma das caixas disponíveis comporta o pedido: "
             "o pedido é dividido em volumes dela, pelo peso máximo.",
    )
    melhor_envio_package_type_ids = fields.Many2many(
        "stock.package.type", "delivery_carrier_melhor_envio_package_type_rel",
        string="Caixas disponíveis",
        help="Na cotação, entra a menor caixa cujo peso máximo comporta o pedido.",
    )

    @api.depends("melhor_envio_token")
    def _compute_melhor_envio_token_expiry(self):
        limite = fields.Datetime.now() + timedelta(days=TOKEN_WARNING_DAYS)
        for carrier in self:
            validade = token_expiry(carrier.melhor_envio_token)
            carrier.melhor_envio_token_expiry = validade
            carrier.melhor_envio_token_expiring = bool(validade and validade <= limite)

    @api.onchange("delivery_type")
    def _onchange_delivery_type_melhor_envio(self):
        # Ainda não há criação de envio: a etiqueta é comprada no painel.
        if self.delivery_type == "melhor_envio":
            self.integration_level = "rate"

    # ------------------------------------------------------------------ #
    # Infraestrutura                                                      #
    # ------------------------------------------------------------------ #

    def _melhor_envio_get_client(self):
        """Cliente com o token deste método.

        O ambiente segue o campo `prod_environment`: fora de produção as
        chamadas vão para o sandbox, que tem cadastro e token próprios.
        """
        self.ensure_one()
        carrier = self.sudo()
        email = carrier.melhor_envio_contact_email or self.env.company.email
        if not email:
            raise MelhorEnvioError(self.env._(
                "Informe o e-mail técnico do método %s: o Melhor Envio exige um contato "
                "em toda chamada.", self.name,
            ))
        return MelhorEnvioClient(
            token=carrier.melhor_envio_token,
            user_agent="%s - Odoo (%s)" % (self.env.company.name, email),
            sandbox=self.prod_environment is not True,
            env=self.env,
        )

    def _melhor_envio_origin_partner(self, order=None, picking=None):
        """De onde o pacote sai: o endereço do depósito, ou o da empresa. Sem
        pedido (o botão de teste), o depósito principal da empresa."""
        if picking and picking.picking_type_id.warehouse_id.partner_id:
            return picking.picking_type_id.warehouse_id.partner_id
        if order and order.warehouse_id.partner_id:
            return order.warehouse_id.partner_id
        company = (order or picking or self).company_id or self.env.company
        deposito = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", company.id)], limit=1,
        )
        return deposito.partner_id or company.partner_id

    def _match(self, partner, order):
        """O Melhor Envio só entrega dentro do Brasil."""
        if (self.delivery_type == "melhor_envio" and partner.country_id
                and partner.country_id.code != "BR"):
            return False
        return super()._match(partner, order)

    # ------------------------------------------------------------------ #
    # Unidades                                                            #
    # ------------------------------------------------------------------ #

    def _melhor_envio_length_cm(self, value):
        """Medida da embalagem, na unidade do Odoo (mm por padrão), em cm."""
        uom = self.env["product.template"]._get_length_uom_id_from_ir_config_parameter()
        return uom._compute_quantity(value or 0.0, self.env.ref("uom.product_uom_cm"),
                                     round=False)

    def _melhor_envio_weight_kg(self, value):
        uom = self.env["product.template"]._get_weight_uom_id_from_ir_config_parameter()
        return uom._compute_quantity(value or 0.0, self.env.ref("uom.product_uom_kgm"),
                                     round=False)

    # ------------------------------------------------------------------ #
    # Cotação                                                             #
    # ------------------------------------------------------------------ #

    def _melhor_envio_order_package_type(self, order):
        """A menor caixa que comporta o pedido; sem nenhuma, a embalagem padrão."""
        peso = order._get_estimated_weight()
        caixas = self.melhor_envio_package_type_ids.filtered(
            lambda caixa: caixa.max_weight and caixa.max_weight >= peso + caixa.base_weight
        )
        if caixas:
            return min(caixas, key=lambda caixa: (
                caixa.packaging_length * caixa.width * caixa.height, caixa.max_weight,
            ))
        return self.melhor_envio_default_package_type_id

    def _melhor_envio_products(self, pacotes, valor):
        """Cada volume do Odoo vira um "produto" da API, com as medidas da caixa
        fechada. Medidas inteiras e para cima: para baixo cotaria menos do que o
        pacote custa. O valor dos produtos é dividido entre os volumes e vai no
        seguro."""
        parcela = round((valor or 0.0) / len(pacotes), 2) if pacotes else 0.0
        produtos = []
        for indice, pacote in enumerate(pacotes, 1):
            dimensao = pacote.dimension or {}
            produtos.append({
                "id": "volume-%d" % indice,
                "width": max(ceil_int(self._melhor_envio_length_cm(dimensao.get("width"))), 1),
                "height": max(ceil_int(self._melhor_envio_length_cm(dimensao.get("height"))), 1),
                "length": max(ceil_int(
                    self._melhor_envio_length_cm(dimensao.get("length"))), 1),
                "weight": max(round(self._melhor_envio_weight_kg(pacote.weight), 3), 0.001),
                "insurance_value": parcela,
                "quantity": 1,
            })
        return produtos

    def _melhor_envio_allowed_services(self):
        texto = (self.melhor_envio_services or "").replace(";", ",").replace(" ", ",")
        return {int(parte) for parte in texto.split(",") if parte.strip().isdigit()}

    def _melhor_envio_pick_option(self, opcoes):
        """Entre os serviços permitidos, o mais barato ou o mais rápido."""
        permitidos = self._melhor_envio_allowed_services()
        if permitidos:
            opcoes = [opcao for opcao in opcoes if opcao["id"] in permitidos]
        if not opcoes:
            return None
        if self.melhor_envio_choice == "fastest":
            return min(opcoes, key=lambda opcao: (
                opcao["days"] if opcao["days"] is not None else 999, opcao["price"],
            ))
        return min(opcoes, key=lambda opcao: opcao["price"])

    def _melhor_envio_quote_order(self, order):
        """Pergunta ao Melhor Envio e devolve a opção escolhida, ou None quando
        nenhum serviço permitido atende o trecho."""
        _ = self.env._
        caixa = self._melhor_envio_order_package_type(order)
        if not caixa:
            raise MelhorEnvioError(_("Configure a embalagem padrão do método %s.", self.name))
        pacotes = self._get_packages_from_order(order, caixa)
        linhas = order.order_line.filtered(
            lambda line: not line.is_delivery and not line.display_type
        )
        # valor declarado: o que o cliente paga pelos produtos, não o custo
        valor = sum(line.price_reduce_taxinc * line.product_uom_qty for line in linhas)
        response = self._melhor_envio_get_client().quote(
            self._melhor_envio_origin_partner(order=order).zip,
            order.partner_shipping_id.zip,
            self._melhor_envio_products(pacotes, valor),
        )
        return self._melhor_envio_pick_option(extract_options(response))

    def melhor_envio_rate_shipment(self, order):
        self.ensure_one()
        _ = self.env._
        try:
            opcao = self._melhor_envio_quote_order(order)
        except UserError as error:
            # Cotação não pode estourar no checkout: o comprador precisa seguir
            # com os outros métodos.
            return {"success": False, "price": 0.0,
                    "error_message": str(error), "warning_message": False}

        if not opcao:
            return {
                "success": False, "price": 0.0,
                "error_message": _("Este método de entrega não atende o CEP informado."),
                "warning_message": False,
            }
        aviso = False
        if opcao["days"]:
            aviso = _(
                "%(company)s %(service)s: entrega em até %(days)s dias úteis após a postagem.",
                company=opcao["company"], service=opcao["name"], days=opcao["days"],
            )
        return {
            "success": True,
            "price": opcao["price"],
            "error_message": False,
            "warning_message": aviso,
        }

    def action_melhor_envio_test_connection(self):
        """Cota um volume da menor caixa daqui até São Paulo: confere token,
        e-mail e CEP de origem, e mostra o preço de cada serviço."""
        self.ensure_one()
        _ = self.env._
        caixa = (self.melhor_envio_package_type_ids.sorted(
            lambda c: c.packaging_length * c.width * c.height
        )[:1] or self.melhor_envio_default_package_type_id)
        largura, altura, comprimento = (
            max(ceil_int(self._melhor_envio_length_cm(medida)), 1)
            for medida in (caixa.width, caixa.height, caixa.packaging_length)
        ) if caixa else (17, 5, 24)
        produto = {
            "id": "teste", "width": largura, "height": altura, "length": comprimento,
            "weight": 0.3, "insurance_value": 50.0, "quantity": 1,
        }
        client = self._melhor_envio_get_client()
        opcoes = extract_options(client.quote(
            self._melhor_envio_origin_partner().zip, TEST_DESTINATION, [produto],
        ))
        if not opcoes:
            raise MelhorEnvioError(_("O token funcionou, mas o Melhor Envio não cotou nenhum serviço."))
        moeda = self.env.company.currency_id
        # texto puro: a notificação escapa HTML
        precos = " · ".join(
            "%s %s %s%s" % (
                opcao["company"], opcao["name"], format_amount(self.env, opcao["price"], moeda),
                _(" (%s dias)", opcao["days"]) if opcao["days"] is not None else "",
            )
            for opcao in sorted(opcoes, key=lambda opcao: opcao["price"])
        )
        validade = self.melhor_envio_token_expiry
        if validade:
            precos += ". " + _("Token válido até %s.", format_date(self.env, validade))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Melhor Envio (%s): 1 livro de 300 g até São Paulo",
                           _("produção") if not client.sandbox else _("sandbox")),
                "message": precos,
                "sticky": True,
            },
        }

    # ------------------------------------------------------------------ #
    # Envio e rastreio                                                    #
    # ------------------------------------------------------------------ #

    def melhor_envio_send_shipping(self, pickings):
        raise UserError(self.env._(
            "O método %s só cota o frete. Compre a etiqueta no painel do Melhor Envio e "
            "informe o código de rastreio na entrega. Para validar entregas sem esta "
            "mensagem, deixe o Nível de integração do método em \"Obter preço\".",
            self.name,
        ))

    def melhor_envio_get_tracking_link(self, picking):
        """O Melhor Rastreio, do próprio Melhor Envio, rastreia Correios, Jadlog,
        Loggi, J&T e as demais transportadoras que ele vende."""
        codigo = (picking.carrier_tracking_ref or "").split(",")[0].strip()
        return TRACKING_URL % codigo if codigo else False

    # ------------------------------------------------------------------ #
    # Aviso de vencimento do token                                        #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_melhor_envio_token_reminder(self):
        """E-mail aos administradores quando o token está para vencer."""
        hoje = fields.Date.context_today(self)
        destinatarios = self.env.ref("base.group_system").sudo().users.filtered(
            lambda user: user.active and user.email and not user.share
        )
        if not destinatarios:
            return
        for carrier in self.sudo().search([("delivery_type", "=", "melhor_envio")]):
            validade = carrier.melhor_envio_token_expiry
            if not validade:
                continue
            dias = (validade.date() - hoje).days
            if dias not in TOKEN_REMINDER_DAYS:
                continue
            self.env["mail.mail"].sudo().create({
                "subject": self.env._("Token do Melhor Envio vence em %(dias)s dias (%(metodo)s)",
                                      dias=dias, metodo=carrier.name),
                "email_to": ",".join(destinatarios.mapped("email_formatted")),
                "body_html": Markup(
                    "<p>%s</p><p>%s</p>"
                ) % (
                    self.env._(
                        "O token do método de entrega %(metodo)s vence em %(data)s. "
                        "Sem ele, o frete não é cotado e o método some do checkout.",
                        metodo=carrier.name, data=format_date(self.env, validade),
                    ),
                    self.env._(
                        "Gere outro no painel do Melhor Envio (Integrações → Permissões de "
                        "acesso) e cole em Inventário → Configuração → Métodos de entrega."
                    ),
                ),
                "auto_delete": True,
            })
