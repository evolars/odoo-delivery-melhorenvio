import logging
from datetime import timedelta

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Ciclo de vida da etiqueta no Melhor Envio.
STATUSES = [
    ("pending", "No carrinho"),
    ("released", "Paga"),
    ("generated", "Gerada, aguardando a coleta"),
    ("received", "Recebida no ponto"),
    ("posted", "A caminho"),
    ("delivered", "Entregue"),
    ("undelivered", "Não entregue"),
    ("paused", "Entrega interrompida"),
    ("suspended", "Suspensa"),
    ("canceled", "Cancelada"),
    ("expired", "Expirada"),
]

# Do que a API devolve para a linha do tempo da entrega. O Melhor Envio não
# repassa os eventos da transportadora, só os marcos.
MILESTONES = [
    ("generated_at", "Etiqueta gerada; coleta agendada"),
    ("posted_at", "Coletado pela transportadora"),
    ("delivered_at", "Entregue"),
    ("canceled_at", "Envio cancelado"),
]

FINAL_STATUSES = ("delivered", "canceled", "expired")

# Envios mais antigos que isto deixam de ser consultados pela tarefa agendada.
TRACKING_WINDOW_DAYS = 60


class StockPicking(models.Model):
    _inherit = "stock.picking"

    melhor_envio_order_ids = fields.Char(
        string="Etiquetas Melhor Envio", copy=False,
        help="IDs das etiquetas compradas no Melhor Envio, separados por vírgula.",
    )
    melhor_envio_protocol = fields.Char(string="Protocolo Melhor Envio", copy=False)
    melhor_envio_status = fields.Selection(STATUSES, string="Status Melhor Envio", copy=False)
    melhor_envio_tracking_events = fields.Json(
        string="Eventos do rastreio Melhor Envio", copy=False,
        help="Do mais recente para o mais antigo: date, time, description, location.",
    )
    melhor_envio_delivered = fields.Boolean(string="Entregue (Melhor Envio)", copy=False)
    melhor_envio_checked_at = fields.Datetime(string="Rastreio consultado em", copy=False)

    def _melhor_envio_ids(self):
        self.ensure_one()
        return [i.strip() for i in (self.melhor_envio_order_ids or "").split(",") if i.strip()]

    def action_melhor_envio_finish(self):
        """Termina uma etiqueta paga que não saiu (geração, rastreio ou PDF)."""
        for picking in self:
            if picking.delivery_type != "melhor_envio" or not picking.melhor_envio_order_ids:
                raise UserError(self.env._("Esta entrega não tem etiqueta do Melhor Envio paga."))
            carrier = picking.carrier_id
            resultado = carrier._melhor_envio_finish(carrier._melhor_envio_get_client(), picking)
            if resultado["tracking_number"] and not picking.carrier_tracking_ref:
                picking.carrier_tracking_ref = resultado["tracking_number"]
        return True

    def action_melhor_envio_refresh_tracking(self):
        for picking in self:
            if picking.delivery_type != "melhor_envio" or not picking.melhor_envio_order_ids:
                raise UserError(self.env._("Esta entrega não tem etiqueta do Melhor Envio."))
            picking._melhor_envio_refresh_tracking()
        return True

    def _melhor_envio_refresh_tracking(self):
        """Status, código de rastreio e marcos da etiqueta. O código pode chegar
        só depois da coleta (até 1 dia útil, conforme a transportadora); quando
        chega, vai para o rastreio da entrega."""
        for picking in self:
            ids = picking._melhor_envio_ids()
            if not ids:
                continue
            situacao = picking.carrier_id._melhor_envio_get_client().tracking(ids)
            dados = [situacao.get(i) or {} for i in ids]
            eventos = []
            for rotulo_campo, rotulo in MILESTONES:
                quando = max((d.get(rotulo_campo) or "" for d in dados), default="")
                if quando:
                    eventos.append({
                        "date": quando[:10], "time": quando[11:16],
                        "description": rotulo, "location": "",
                    })
            eventos.sort(key=lambda e: (e["date"], e["time"]), reverse=True)
            status = (dados[0].get("status") or "").replace("cancelled", "canceled")
            valores = {
                "melhor_envio_tracking_events": eventos,
                "melhor_envio_checked_at": fields.Datetime.now(),
                "melhor_envio_delivered": all(d.get("status") == "delivered" for d in dados),
            }
            if status in dict(STATUSES):
                valores["melhor_envio_status"] = status
            codigos = [d.get("tracking") or d.get("melhorenvio_tracking") for d in dados]
            if all(codigos) and not picking.carrier_tracking_ref:
                valores["carrier_tracking_ref"] = ",".join(codigos)
            picking.write(valores)

    def _cron_melhor_envio_refresh_tracking(self):
        """Atualiza as etiquetas a caminho. Erro numa entrega não para as outras."""
        limite = fields.Datetime.now() - timedelta(days=TRACKING_WINDOW_DAYS)
        pickings = self.search([
            ("delivery_type", "=", "melhor_envio"),
            ("state", "=", "done"),
            ("melhor_envio_order_ids", "!=", False),
            ("melhor_envio_delivered", "=", False),
            ("melhor_envio_status", "not in", list(FINAL_STATUSES)),
            ("date_done", ">=", limite),
        ])
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    picking._melhor_envio_refresh_tracking()
            except UserError as error:
                _logger.warning("Melhor Envio: rastreio de %s não atualizado: %s",
                                picking.name, error)
