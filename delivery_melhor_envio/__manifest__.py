{
    "name": "Melhor Envio - Frete Nacional",
    "version": "18.0.1.1.0",
    "category": "Inventory/Delivery",
    "summary": "Cotação no checkout e etiqueta pelo Melhor Envio (Correios, Loggi e outras)",
    "description": """
Conector do Melhor Envio para o Odoo 18.

O Melhor Envio cota e vende frete de várias transportadoras (Correios PAC,
SEDEX e Mini Envios, Jadlog, Loggi, J&T...) sem contrato nem volume mínimo.
Este módulo cota no carrinho, com o preço que a conta paga, e compra a
etiqueta ao validar a entrega:

* **Cotação** pela API (`/api/v2/me/shipment/calculate`), com a caixa que o
  pedido ocupa, escolhendo o serviço mais barato ou o mais rápido entre os
  permitidos no método. Vários métodos dividem a mesma consulta.
* **Etiqueta**: carrinho, pagamento com o saldo da carteira, geração (na
  Loggi Coleta, a coleta é agendada) e o PDF anexado à entrega, com NF-e
  obrigatória para remetente com IE. Falha depois do pagamento não desfaz a
  validação, e uma etiqueta já paga nunca é comprada de novo.
* **Rastreio** pelos marcos do Melhor Envio, a cada 3 horas, e link do
  Melhor Rastreio. **Cancelamento** enquanto o Melhor Envio deixar.
* **Aviso** por e-mail aos administradores antes de o token vencer: sem token
  a cotação falha e o método some do checkout.
    """,
    "author": "Evolars LTDA",
    "website": "https://github.com/evolars/odoo-delivery-melhorenvio",
    "license": "AGPL-3",
    "depends": ["stock_delivery"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "data/ir_cron.xml",
        "views/delivery_melhor_envio_views.xml",
    ],
    "installable": True,
    "application": False,
}
