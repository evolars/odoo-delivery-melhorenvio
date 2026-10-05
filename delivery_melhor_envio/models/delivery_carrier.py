import base64
import logging
import math
import re
from datetime import timedelta

from markupsafe import Markup

import odoo
from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount, format_date

from .melhor_envio_client import (
    MelhorEnvioClient,
    MelhorEnvioError,
    digits,
    district_for_postal_code,
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


# Limites de tamanho dos campos de texto do carrinho.
MAX_NAME = 255
MAX_PRODUCT_NAME = 255

# "Rua X, 123", "Rua X 123", "Rua X, nº 123": o número no fim da linha.
STREET_NUMBER = re.compile(r"^(?P<rua>.*?)[,\s]+(?:n[º°o.]*\s*)?(?P<numero>\d+[a-zA-Z]?)\s*$")


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
    melhor_envio_require_nfe = fields.Boolean(
        string="Exigir NF-e autorizada", default=True,
        help="Remetente com Inscrição Estadual é contribuinte do ICMS e despacha com NF-e: "
             "a declaração de conteúdo é só para pessoa física e quem não é contribuinte. "
             "Ligado, a etiqueta não é comprada sem NF-e autorizada na venda.",
    )

    @api.depends("melhor_envio_token")
    def _compute_melhor_envio_token_expiry(self):
        limite = fields.Datetime.now() + timedelta(days=TOKEN_WARNING_DAYS)
        for carrier in self:
            validade = token_expiry(carrier.melhor_envio_token)
            carrier.melhor_envio_token_expiry = validade
            carrier.melhor_envio_token_expiring = bool(validade and validade <= limite)

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
        return self._melhor_envio_package_type_for(order._get_estimated_weight())

    def _melhor_envio_package_type_for(self, peso):
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
    # Compra da etiqueta                                                  #
    # ------------------------------------------------------------------ #

    def _melhor_envio_requires_nfe(self, company):
        """Remetente com IE precisa de NF-e. Sem a localização fiscal (OCA) não há
        como emitir nem conferir a nota pelo Odoo."""
        if not self.melhor_envio_require_nfe or "document_key" not in self.env["account.move"]._fields:
            return False
        ie = digits(getattr(company.partner_id, "l10n_br_ie_code", False))
        return bool(ie)

    def _melhor_envio_nfe(self, picking):
        """A chave da NF-e autorizada da venda, ou False."""
        faturas = picking.sale_id.invoice_ids.filtered(lambda move: move.state == "posted")
        if not faturas or "document_key" not in faturas._fields:
            return False
        for fatura in faturas.sorted("id", reverse=True):
            chave = digits(fatura.document_key)
            if len(chave) != 44:
                continue
            if "state_edoc" in fatura._fields and fatura.state_edoc != "autorizada":
                continue
            return {"key": chave, "move": fatura}
        return False

    def _melhor_envio_street(self, partner):
        """Logradouro e número separados, como a API pede. O checkout tem um
        campo só ("Rua X, 123"); a localização separa quando reconhece."""
        rua = (getattr(partner, "street_name", False) or "").strip()
        numero = (getattr(partner, "street_number", False) or "").strip()
        if not (rua and numero):
            encontrado = STREET_NUMBER.match((partner.street or "").strip())
            if encontrado:
                rua, numero = encontrado.group("rua").strip(" ,"), encontrado.group("numero")
            else:
                rua, numero = (partner.street or "").strip(), ""
        return rua, numero or "S/N"

    def _melhor_envio_party(self, partner, name=None, document_partner=None):
        """Remetente ou destinatário no formato do carrinho."""
        _ = self.env._
        comercial = (document_partner or partner).commercial_partner_id
        rua, numero = self._melhor_envio_street(partner)
        bairro = (getattr(partner, "district", False) or "").strip() \
            or district_for_postal_code(partner.zip)
        if not bairro:
            raise MelhorEnvioError(_(
                "O endereço de %s está sem bairro, e o CEP não informa: preencha o bairro "
                "no contato.", partner.display_name,
            ))
        if not rua or not partner.zip:
            raise MelhorEnvioError(_("O endereço de %s está incompleto.", partner.display_name))
        cidade = partner.city or getattr(partner, "city_id", False) and partner.city_id.name
        telefone = digits(partner.phone or partner.mobile or comercial.phone or comercial.mobile)
        if not telefone:
            raise MelhorEnvioError(_(
                "%s está sem telefone: a transportadora exige um para a entrega.",
                partner.display_name,
            ))
        dados = {
            "name": (name or partner.name or comercial.name or "")[:MAX_NAME],
            "phone": telefone,
            "email": partner.email or comercial.email or "",
            "address": rua,
            "complement": (partner.street2 or "").strip(),
            "number": numero,
            "district": bairro,
            "city": cidade or "",
            "state_abbr": partner.state_id.code or "",
            "country_id": "BR",
            "postal_code": digits(partner.zip),
        }
        documento = digits(comercial.vat)
        if len(documento) == 14:
            dados["company_document"] = documento
            ie = digits(getattr(comercial, "l10n_br_ie_code", False))
            if ie:
                dados["state_register"] = ie
        elif len(documento) == 11:
            dados["document"] = documento
        else:
            raise MelhorEnvioError(_(
                "%s está sem CPF/CNPJ válido: a transportadora exige o documento.",
                comercial.display_name,
            ))
        return dados

    def _melhor_envio_picking_products(self, picking):
        """Os livros da entrega, com o preço de venda: vão na declaração da
        etiqueta, e a soma é o valor segurado."""
        produtos = []
        for move in picking.move_ids.filtered(lambda m: m.state != "cancel" and m.quantity):
            linha = move.sale_line_id
            preco = linha.price_reduce_taxinc if linha else move.product_id.lst_price
            produtos.append({
                "name": (move.product_id.display_name or "")[:MAX_PRODUCT_NAME],
                "quantity": int(math.ceil(move.quantity)),
                "unitary_value": round(preco or 0.0, 2),
            })
        return produtos

    def _melhor_envio_picking_packages(self, picking):
        """Volumes da entrega. Sem "colocar em pacote", tudo vai na menor caixa
        que comporta o peso, como na cotação, com o peso da caixa somado."""
        caixa = self._melhor_envio_package_type_for(picking._get_estimated_weight())
        if not caixa:
            raise MelhorEnvioError(self.env._(
                "Configure a embalagem padrão do método %s.", self.name))
        pacotes = self._get_packages_from_picking(picking, caixa)
        for pacote in pacotes:
            if pacote.name == "Bulk Content":
                pacote.weight += caixa.base_weight
        return pacotes

    def _melhor_envio_volume(self, pacote):
        dimensao = pacote.dimension or {}
        return {
            "width": max(ceil_int(self._melhor_envio_length_cm(dimensao.get("width"))), 1),
            "height": max(ceil_int(self._melhor_envio_length_cm(dimensao.get("height"))), 1),
            "length": max(ceil_int(self._melhor_envio_length_cm(dimensao.get("length"))), 1),
            "weight": max(round(self._melhor_envio_weight_kg(pacote.weight), 3), 0.001),
        }

    def _melhor_envio_cart_payload(self, picking, pacote, servico, nfe, produtos, valor):
        company = picking.company_id
        remetente = self._melhor_envio_party(
            self._melhor_envio_origin_partner(picking=picking),
            name=company.name, document_partner=company.partner_id,
        )
        destinatario = self._melhor_envio_party(picking.partner_id)
        opcoes = {
            "insurance_value": round(valor, 2),
            "receipt": False,
            "own_hand": False,
            "reverse": False,
            "non_commercial": not nfe,
            "plataform": "Odoo",
            "tags": [{"tag": picking.name, "url": None}],
        }
        if nfe:
            opcoes["invoice"] = {"key": nfe["key"]}
        return {
            "service": servico["id"],
            "from": remetente,
            "to": destinatario,
            "products": produtos,
            "volumes": [self._melhor_envio_volume(pacote)],
            "options": opcoes,
        }

    def _melhor_envio_rate_picking(self, client, picking, pacotes, valor):
        """Cota de novo com a entrega como ela é, e escolhe o serviço do mesmo
        jeito que no checkout."""
        parcela = round(valor / len(pacotes), 2) if pacotes else 0.0
        produtos = []
        for indice, pacote in enumerate(pacotes, 1):
            produtos.append(dict(self._melhor_envio_volume(pacote), id="volume-%d" % indice,
                                 insurance_value=parcela, quantity=1))
        opcao = self._melhor_envio_pick_option(extract_options(client.quote(
            self._melhor_envio_origin_partner(picking=picking).zip,
            picking.partner_id.zip, produtos,
        )))
        if not opcao:
            raise MelhorEnvioError(self.env._(
                "Nenhum serviço do método %s atende o CEP %s hoje.",
                self.name, picking.partner_id.zip,
            ))
        return opcao

    def _melhor_envio_save_paid(self, picking, ids):
        """Grava as etiquetas pagas numa transação própria: se a validação for
        desfeita depois do pagamento, o registro fica, e a próxima tentativa
        reaproveita a etiqueta em vez de pagar outra."""
        valores = {"picking_ref": picking.id, "order_ids": ",".join(ids)}
        if odoo.modules.module.current_test:
            self.env["melhor.envio.paid"].sudo().create(valores)
            return
        with self.env.registry.cursor() as cr:
            api.Environment(cr, SUPERUSER_ID, {})["melhor.envio.paid"].create(valores)

    def _melhor_envio_saved_paid(self, picking):
        pago = self.env["melhor.envio.paid"].sudo().search(
            [("picking_ref", "=", picking.id)], limit=1)
        return pago.order_ids or False

    def _melhor_envio_check_date(self, picking):
        """A coleta é agendada quando a etiqueta é gerada: não se compra antes do
        dia previsto da entrega (na pré-venda, o "Envio a partir de")."""
        if not picking.scheduled_date:
            return
        previsto = fields.Datetime.context_timestamp(self, picking.scheduled_date).date()
        if previsto > fields.Date.context_today(self):
            raise MelhorEnvioError(self.env._(
                "A entrega %(entrega)s está prevista para %(data)s. A coleta é agendada no "
                "momento em que a etiqueta é gerada: valide a entrega nesse dia (antes das "
                "11h, para a coleta sair no mesmo dia).",
                entrega=picking.name, data=format_date(self.env, previsto),
            ))

    def melhor_envio_send_shipping(self, pickings):
        """Compra, paga e gera a etiqueta de cada entrega, e anexa o PDF.

        Antes do dia previsto da entrega, não compra: a coleta seria agendada
        antes da hora. Até o pagamento, qualquer recusa volta como erro e a
        validação não acontece. Depois dele o dinheiro já saiu: as etiquetas são
        gravadas fora da validação, falhas viram aviso na entrega, nunca erro, e
        *Concluir etiqueta* termina o que faltou.
        """
        self.ensure_one()
        _ = self.env._
        client = self._melhor_envio_get_client()
        resultado = []
        for picking in pickings:
            if not picking.melhor_envio_order_ids:
                picking.melhor_envio_order_ids = self._melhor_envio_saved_paid(picking)
            if picking.melhor_envio_order_ids:
                resultado.append(self._melhor_envio_finish(client, picking))
                continue

            self._melhor_envio_check_date(picking)
            nfe = self._melhor_envio_nfe(picking)
            if not nfe and self._melhor_envio_requires_nfe(picking.company_id):
                raise MelhorEnvioError(_(
                    "A venda %s não tem NF-e autorizada. A editora tem Inscrição Estadual e "
                    "só despacha com nota: emita e autorize a NF-e e valide a entrega de novo.",
                    picking.sale_id.name or picking.name,
                ))
            produtos = self._melhor_envio_picking_products(picking)
            valor = sum(p["unitary_value"] * p["quantity"] for p in produtos)
            pacotes = self._melhor_envio_picking_packages(picking)
            servico = self._melhor_envio_rate_picking(client, picking, pacotes, valor)
            parcela = valor / len(pacotes)
            ids = []
            for pacote in pacotes:
                pedido = client.add_to_cart(self._melhor_envio_cart_payload(
                    picking, pacote, servico, nfe, produtos, parcela))
                ids.append(pedido["id"])
            try:
                client.checkout(ids)
            except MelhorEnvioError as error:
                raise MelhorEnvioError(_(
                    "O Melhor Envio não aceitou o pagamento da etiqueta: %s Confira o saldo "
                    "da carteira no painel do Melhor Envio.", str(error),
                )) from error
            self._melhor_envio_save_paid(picking, ids)
            picking.melhor_envio_order_ids = ",".join(ids)
            resultado.append(self._melhor_envio_finish(client, picking))
        return resultado

    def _melhor_envio_finish(self, client, picking):
        """Gera a etiqueta paga, pega o rastreio e anexa o PDF. Não estoura:
        a etiqueta já foi paga."""
        _ = self.env._
        ids = picking._melhor_envio_ids()
        preco, rastreios, protocolos = 0.0, [], []
        try:
            situacao = client.tracking(ids)
            gerar = [i for i in ids if (situacao.get(i) or {}).get("status") == "released"]
            if gerar:
                for chave, retorno in (client.generate(gerar) or {}).items():
                    if isinstance(retorno, dict) and retorno.get("status") is False:
                        raise MelhorEnvioError(retorno.get("message") or chave)
            for i in ids:
                pedido = client.order(i)
                preco += float(pedido.get("price") or 0.0)
                codigo = pedido.get("tracking") or pedido.get("self_tracking")
                if codigo:
                    rastreios.append(codigo)
                if pedido.get("protocol"):
                    protocolos.append(pedido["protocol"])
            picking.melhor_envio_protocol = ",".join(protocolos)
            self._melhor_envio_attach_labels(client, picking, ids)
            picking.melhor_envio_status = "generated"
        except UserError as error:
            erro = str(error)
            _logger.warning("Melhor Envio: etiqueta paga sem concluir em %s: %s", picking.name, erro)
            picking.message_post(body=_(
                "A etiqueta do Melhor Envio foi paga, mas não terminou de sair: %s Use "
                "\"Concluir etiqueta\" na entrega para tentar de novo, sem pagar outra vez.",
                erro,
            ))
            picking.activity_schedule(
                "mail.mail_activity_data_warning", fields.Date.context_today(self),
                note=_("Etiqueta do Melhor Envio paga e não concluída: %s", erro),
                user_id=picking.user_id.id or self.env.user.id,
            )
        return {"exact_price": preco, "tracking_number": ",".join(rastreios) or False}

    def _melhor_envio_attach_labels(self, client, picking, ids):
        """Etiqueta em PDF e, havendo, DANFE e XML da NF-e, que vão no pacote."""
        anexos = self.env["ir.attachment"]
        for i in ids:
            if anexos.search_count([("res_model", "=", "stock.picking"),
                                    ("res_id", "=", picking.id),
                                    ("description", "=", "melhor_envio:%s" % i)]):
                continue
            anexos |= anexos.create({
                "name": "%s-MelhorEnvio-%s.pdf" % (self._get_delivery_label_prefix(), i[:8]),
                "type": "binary",
                "datas": base64.b64encode(client.label_pdf(i)),
                "mimetype": "application/pdf",
                "description": "melhor_envio:%s" % i,
                "res_model": "stock.picking",
                "res_id": picking.id,
            })
        nfe = self._melhor_envio_nfe(picking)
        if nfe and anexos:
            fatura = nfe["move"]
            for campo in ("file_report_id", "authorization_file_id"):
                arquivo = getattr(fatura, campo, False) if campo in fatura._fields else False
                if arquivo:
                    anexos |= arquivo.sudo().copy({"res_model": "stock.picking",
                                                   "res_id": picking.id})
        if anexos:
            picking.message_post(
                body=self.env._("Etiqueta do Melhor Envio (%s): imprima e cole no pacote, com o "
                                "DANFE.", self.name),
                attachment_ids=anexos.ids,
            )

    # ------------------------------------------------------------------ #
    # Rastreio e cancelamento                                             #
    # ------------------------------------------------------------------ #

    def melhor_envio_get_tracking_link(self, picking):
        """O Melhor Rastreio, do próprio Melhor Envio, rastreia Correios, Jadlog,
        Loggi, J&T e as demais transportadoras que ele vende."""
        codigo = (picking.carrier_tracking_ref or "").split(",")[0].strip()
        return TRACKING_URL % codigo if codigo else False

    def melhor_envio_cancel_shipment(self, pickings):
        """Cancela a etiqueta no Melhor Envio, se ele ainda deixar.

        Gerada a etiqueta de um serviço com coleta, a transportadora já foi
        avisada e o cancelamento pode ser recusado. Cancelada depois de gerada,
        o estorno cai na carteira em até 12 horas.
        """
        self.ensure_one()
        _ = self.env._
        client = self._melhor_envio_get_client()
        for picking in pickings:
            ids = picking._melhor_envio_ids()
            if not ids:
                continue
            permitido = client.cancellable(ids)
            negados = [i for i in ids if not (permitido.get(i) or {}).get("cancellable")]
            if negados:
                raise MelhorEnvioError(_(
                    "O Melhor Envio não deixa mais cancelar esta etiqueta: a coleta já foi "
                    "pedida à transportadora ou o pacote já saiu. Fale com o suporte do "
                    "Melhor Envio."
                ))
            for i in ids:
                client.cancel(i, _("Entrega %s cancelada no Odoo", picking.name))
            etiquetas = self.env["ir.attachment"].search([
                ("res_model", "=", "stock.picking"), ("res_id", "=", picking.id),
                ("description", "in", ["melhor_envio:%s" % i for i in ids]),
            ])
            for etiqueta in etiquetas:
                etiqueta.name = "CANCELADA-%s" % etiqueta.name
            picking.write({"melhor_envio_order_ids": False, "melhor_envio_status": "canceled"})
            self.env["melhor.envio.paid"].sudo().search(
                [("picking_ref", "=", picking.id)]).unlink()
            picking.message_post(body=_(
                "Etiqueta do Melhor Envio cancelada; o valor volta à carteira em até 12 horas. "
                "Para despachar de novo, use Enviar para a transportadora."
            ))
        return True

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
