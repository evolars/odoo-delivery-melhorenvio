from odoo import fields, models


class MelhorEnvioPaid(models.Model):
    """Etiquetas pagas, gravadas fora da transação da validação.

    O pagamento acontece no meio da validação da entrega. Se a validação for
    desfeita depois disso, o Odoo apaga tudo o que ela gravou, inclusive o ID da
    etiqueta paga, e a próxima tentativa compraria outra. Este registro é gravado
    numa transação própria, logo depois do pagamento, e sobrevive.
    """

    _name = "melhor.envio.paid"
    _description = "Etiqueta paga no Melhor Envio"
    _order = "id desc"

    # Inteiro, e não many2one: a chave estrangeira travaria na linha da entrega,
    # que a validação mantém bloqueada.
    picking_ref = fields.Integer(string="Entrega (ID)", required=True, index=True)
    order_ids = fields.Char(string="Etiquetas", required=True)
