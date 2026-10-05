# Melhor Envio para Odoo 18

Conector do [Melhor Envio](https://melhorenvio.com.br): cotação de frete no carrinho, com o preço
que a conta paga nos Correios (PAC, SEDEX, Mini Envios), na Loggi, na Jadlog e nas demais
transportadoras que o Melhor Envio vende, e compra da etiqueta ao validar a entrega. Sem contrato
com transportadora nem volume mínimo.

Branch: `18.0`.

Tudo acontece no Odoo; no painel do Melhor Envio só se põe saldo na carteira.

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

Inventário → Configuração → Métodos de Entrega → novo método, provedor **Melhor Envio**.
Nível de integração **Obter taxa e criar remessa** compra a etiqueta ao validar a entrega;
**Obter preço** só cota (a etiqueta é comprada no painel):

| Campo | O que é |
|---|---|
| Token | o do painel do Melhor Envio |
| E-mail técnico | vai no `User-Agent` de toda chamada |
| Serviços | IDs separados por vírgula; vazio = qualquer um. Correios: 1 PAC, 2 SEDEX, 17 Mini Envios. Jadlog: 3 .Package, 4 .Com |
| Escolher | o mais barato ou o mais rápido entre os serviços permitidos |
| Caixas disponíveis / Embalagem padrão | entra a menor caixa cujo peso máximo comporta o pedido; nenhuma comporta, o pedido é dividido em volumes da padrão |
| Exigir NF-e autorizada | remetente com IE só despacha com NF-e (ligado por padrão): sem nota autorizada na venda, a etiqueta não é comprada |

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

## Etiqueta

Validar a entrega (com *Obter taxa e criar remessa*):

1. **Confere** NF-e autorizada (com *Exigir NF-e*), endereço, telefone e CPF/CNPJ das duas pontas.
   O checkout do Odoo tem rua e número num campo só ("Rua X, 123"): o número sai do fim da
   linha, ou vai "S/N". Sem bairro no contato, o bairro vem do CEP pelo ViaCEP.
2. **Cota de novo** com a entrega como está (a menor caixa que comporta o peso, como no
   checkout) e escolhe o serviço do mesmo jeito.
3. **Põe no carrinho** (`/me/cart`) um envio por volume, com remetente (endereço do depósito,
   CNPJ e IE da empresa), destinatário, livros com o preço de venda, valor segurado, chave da
   NF-e (ou declaração de conteúdo, sem *Exigir NF-e*) e o nome da entrega como etiqueta.
4. **Paga** com o saldo da carteira (`/me/shipment/checkout`). Recusa (saldo insuficiente, limite
   de envios) volta como erro e a entrega não é validada.
5. **Gera** (`/me/shipment/generate`): a transportadora é avisada e, na Loggi Coleta, a coleta é
   agendada. Pega o código de rastreio e **anexa o PDF** da etiqueta (e o DANFE e o XML da NF-e,
   havendo) à entrega.

Depois do pagamento o dinheiro já saiu: falha na geração ou no PDF não desfaz a validação. Vira
aviso e atividade na entrega, e **Concluir etiqueta** termina sem pagar de novo. Se a validação
cair por outro motivo depois do pagamento, a próxima procura a etiqueta paga no Melhor Envio
(pelo CPF/CNPJ do destinatário e o nome da entrega) e a reaproveita.

**Rastreio.** O Melhor Envio não repassa os eventos da transportadora, só os marcos: etiqueta
gerada, coletado (postado), entregue, cancelado. A tarefa *Melhor Envio: atualizar rastreio*
consulta a cada 3 horas, por até 60 dias, e guarda os marcos na entrega
(`melhor_envio_tracking_events`). O código da transportadora pode chegar só depois da coleta;
quando chega, vai para o rastreio da entrega.

**Cancelar** a entrega cancela a etiqueta, se o Melhor Envio ainda deixar: com coleta já pedida
à transportadora ou pacote postado, ele recusa. Cancelada depois de gerada, o valor volta à
carteira em até 12 horas.

---

## Testes

```bash
odoo -d <db> -i delivery_melhor_envio --test-enable --test-tags /delivery_melhor_envio \
    --stop-after-init --http-port 8099
```

36 testes, com a API simulada: cabeçalhos e corpo da cotação, cache entre métodos, erros (token,
validação, conexão), escolha do serviço, caixa e valor declarado, origem pelo depósito, só Brasil,
sandbox, link do Melhor Rastreio, aviso de vencimento do token e a etiqueta: compra, pagamento,
geração e PDF, corpo do carrinho, etiqueta paga reaproveitada, pagamento recusado, falha depois
de pagar, NF-e exigida, bairro, número da rua, rastreio e cancelamento.

Referência: [documentação da API](https://docs.melhorenvio.com.br) (cálculo, carrinho, compra,
geração, impressão, status e cancelamento atualizados em 18/06/2026, conferidos em 05/10/2026).
