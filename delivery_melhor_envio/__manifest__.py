{
    "name": "Melhor Envio - Frete Nacional",
    "version": "18.0.1.0.0",
    "category": "Inventory/Delivery",
    "summary": "Cotação de frete no checkout pelo Melhor Envio (Correios, Jadlog e outras)",
    "description": """
Conector do Melhor Envio para o Odoo 18.

O Melhor Envio cota e vende frete de várias transportadoras (Correios PAC,
SEDEX e Mini Envios, Jadlog, Loggi, J&T...) sem contrato nem volume mínimo.
Este módulo cota no carrinho, com o preço que a conta paga:

* **Cotação** pela API (`/api/v2/me/shipment/calculate`), com a caixa que o
  pedido ocupa, escolhendo o serviço mais barato ou o mais rápido entre os
  permitidos no método. Dois métodos (econômico e expresso) dividem a mesma
  consulta: a resposta traz todos os serviços.
* **Rastreio** pelo Melhor Rastreio, a partir do código informado na entrega.
* **Aviso** por e-mail aos administradores antes de o token vencer: sem token
  a cotação falha e o método some do checkout.

Ainda **não compra a etiqueta**: ela é comprada no painel do Melhor Envio e o
código de rastreio é informado na entrega.
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
