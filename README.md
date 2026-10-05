# Melhor Envio para Odoo 18

Conector do [Melhor Envio](https://melhorenvio.com.br): cotação de frete no carrinho, com o preço
que a conta paga nos Correios (PAC, SEDEX, Mini Envios), na Jadlog e nas demais transportadoras
que o Melhor Envio vende. Sem contrato com transportadora nem volume mínimo.

Branch: `18.0`.

**Ainda não compra etiqueta.** A etiqueta é comprada no painel do Melhor Envio; o código de
rastreio vai na entrega e o link de rastreio aponta para o Melhor Rastreio.

---

## Instalação

Depende de `stock_delivery` e da biblioteca `requests`. Pelo Doodba (é assim que entra na
imagem `odoo-template`):

```yaml
# custom/src/repos.yaml
./odoo-delivery-melhorenvio:
  defaults:
    depth: $DEPTH_DEFAULT
  remotes:
    evolars: https://github.com/evolars/odoo-delivery-melhorenvio.git
  target: evolars $ODOO_VERSION
  merges:
    - evolars $ODOO_VERSION
```

```yaml
# custom/src/addons.yaml
odoo-delivery-melhorenvio:
  - delivery_melhor_envio
```

## Token

Conta no Melhor Envio (CNPJ ou CPF, gratuita). No painel: **Integrações → Permissões de acesso →
Gerar novo token**, marcar todas as permissões e copiar o token (ele só aparece uma vez).

O token é um JWT e traz a validade: o formulário do método mostra *Token válido até* e avisa
quando faltam 15 dias. Uma tarefa diária manda e-mail aos administradores 15, 7, 3 e 1 dia antes
e no dia do vencimento. **Sem token válido a cotação falha e o método some do checkout.**

O Melhor Envio exige, em toda chamada, um `User-Agent` com o nome da aplicação e um e-mail de
contato técnico: o campo *E-mail técnico* do método (padrão: o e-mail da empresa).

O ambiente segue o campo *Ambiente* do método: em produção, `melhorenvio.com.br`; em teste, o
[sandbox](https://sandbox.melhorenvio.com.br), que tem cadastro e token próprios.

---

## Configuração

Inventário → Configuração → Métodos de Entrega → novo método, provedor **Melhor Envio**,
Nível de integração **Obter preço** (*Get Rate*):

| Campo | O que é |
|---|---|
| Token | o do painel do Melhor Envio |
| E-mail técnico | vai no `User-Agent` de toda chamada |
| Serviços | IDs separados por vírgula; vazio = qualquer um. Correios: 1 PAC, 2 SEDEX, 17 Mini Envios. Jadlog: 3 .Package, 4 .Com |
| Escolher | o mais barato ou o mais rápido entre os serviços permitidos |
| Caixas disponíveis / Embalagem padrão | entra a menor caixa cujo peso máximo comporta o pedido; nenhuma comporta, o pedido é dividido em volumes da padrão |

**Testar cotação** cota um volume da menor caixa, 300 g, do endereço de saída até São Paulo e
mostra o preço e o prazo de cada serviço: confere token, e-mail e CEP de origem de uma vez.

### Dois métodos, uma chamada

O checkout mostra um preço por método. Para oferecer "econômico" e "expresso", crie dois métodos
com serviços diferentes (ex.: `1,17` e `2`). A resposta da API traz todos os serviços, e o
cliente guarda a cotação por 10 minutos: o segundo método sai da mesma chamada.

---

## Como cota

* **Origem:** o endereço do depósito do pedido (ou o da empresa). É o CEP de onde o pacote sai.
* **Destino:** o endereço de entrega. Fora do Brasil o método não aparece.
* **Volumes:** a caixa escolhida, fechada, vai como um "produto" da API (medidas inteiras em cm,
  arredondadas para cima; peso dos livros mais o da caixa). O valor dos produtos (o que o
  cliente paga, não o custo) vai em `insurance_value`, dividido entre os volumes.
* **Preço e prazo:** `custom_price` e `custom_delivery_time`, que já trazem os ajustes da conta.
  Serviço que não atende o trecho vem com `error` e fica de fora.

Falha na cotação (token vencido, CEP inválido, Melhor Envio fora do ar) não estoura no checkout:
o método aparece indisponível e o comprador segue com os outros.

---

## Testes

```bash
odoo -d <db> -i delivery_melhor_envio --test-enable --test-tags /delivery_melhor_envio \
    --stop-after-init --http-port 8099
```

25 testes, com a API simulada: cabeçalhos e corpo da cotação, cache entre métodos, erros (token,
validação, conexão), escolha do serviço, caixa e valor declarado, origem pelo depósito, só Brasil,
sandbox, link do Melhor Rastreio e o aviso de vencimento do token.

Referência: [documentação da API](https://docs.melhorenvio.com.br) (cálculo de fretes atualizado
em 18/06/2026, conferido em 05/10/2026).
