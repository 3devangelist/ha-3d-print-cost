# Ligar uma impressora Bambu Lab pela rede local (LAN Only)

[English](bambu-lan-only.md) | **Português**

> Verificado com a wiki oficial da Bambu Lab a 1 de novembro de 2026.
> As linhas marcadas **(não confirmado)** vêm de relatos da comunidade, não da Bambu Lab.

## Em resumo

1. Liga o **LAN Only** no ecrã da impressora.
2. Tira uma foto a essa página (sem flash).
3. No Home Assistant: **FDM Printers → Add printer → Bambu Lab (LAN) → 🖼️ Choose photo**.
4. Confirma os três valores e termina.

**O Developer Mode é opcional.** Só é preciso para mandar comandos (pausa, luz). Vê o [Passo 2](#passo-2--developer-mode-opcional).

## Porque é preciso

A FDM Printer Manager fala com as Bambu Lab **diretamente na rede de casa**.
Não usa a cloud da Bambu.

| Modo | O que faz | É preciso? |
|---|---|---|
| **LAN Only** | A impressora deixa a cloud e fala só na tua rede | **Sim** |
| **Developer Mode** | Deixa outros programas **mandar comandos** (G-code, pausa, luzes…) | **Não — opcional** |

O que funciona **só com o LAN Only** (testado numa A1 mini):

| Funciona | Precisa do Developer Mode |
|---|---|
| Estado, progresso, nome do ficheiro | Pausar, retomar e parar a partir do Home Assistant |
| Temperatura do bico | Botão da luz da câmara |
| Tomada segura: espera abaixo de 80 °C e corta a tomada | Qualquer outro comando mandado à impressora |
| Histórico e custo na FDM | |
| Câmara no cartão — A1, A1 mini, P1P, P1S (desde a 0.13.0) | |

Desde o firmware de janeiro de 2025, os comandos de outros programas precisam do **Developer Mode**.
Sem ele, a FDM mostra um aviso claro — "requires Developer Mode" — e nada se estraga.

## O que precisas da impressora

| O quê | Exemplo | Onde |
|---|---|---|
| **Endereço IP** | `192.168.1.96` | Página LAN Only, no ecrã da impressora |
| **Código de acesso** | 8 caracteres, por exemplo `1a2b3c4d` | Página LAN Only, no ecrã da impressora |
| **Número de série** | 15 ou 16 caracteres, por exemplo `0309DA561105372` | Informação do aparelho no ecrã, Bambu Studio ou Bambu Handy |

O número de série **não** é o nome da impressora, tipo `3DP-000-000`.

## Antes de começar

- Impressora e Home Assistant na **mesma rede**. Em VLANs diferentes, vê [Rede e portas](#rede-e-portas).
- Dá à impressora um **IP fixo** no router (reserva DHCP). Senão o IP pode mudar. **(não confirmado — conselho da comunidade)**
- **Atualiza o firmware primeiro.** Em LAN Only perdes as atualizações pela internet. Vê [O que perdes](#o-que-perdes-em-lan-only).

## Passo 1 — Ligar o LAN Only

| Impressora | Menu |
|---|---|
| **A1, A1 mini** (ecrã tátil) | **Settings** → página 3 → **LAN Only Mode** → ligar (fica verde) |
| **P1P, P1S** (botões) | **Settings → WLAN → LAN Only Mode** → **Yes** → mostra **ON** |
| **X1C, X1E, H2D, H2S, P2S** (ecrã tátil grande) | Lado esquerdo **Settings → Settings → LAN Only** → ligar |

Nas séries X1 e H2 também podes ligar o **LAN mode liveview** (a câmara na tua rede).

<!-- captura: A1 mini, página LAN Only ligada, com IP e código de acesso -->

## Passo 2 — Developer Mode (opcional)

**Salta este passo** se só queres o estado, as temperaturas e a tomada segura.

Liga-o só se quiseres que a FDM **mande comandos** (pausar, retomar, parar, luz).
Dá a outros programas o controlo total da impressora.

Antes de o ligares:

- A impressora mostra um **aviso de risco**.
- A Bambu Lab pode **recusar apoio técnico** a problemas causados por programas de terceiros.
- É **reversível**: desliga-se na mesma página.

| Impressora | Como |
|---|---|
| **A1, A1 mini** | Na página **LAN Only Mode**, liga o **Developer Mode**. Marca a confirmação e toca em **Enable**. |
| **P1P, P1S** | Com o LAN Only em **ON**, desce até **Developer Mode**. Lê até ao fim e escolhe **Enable**. |
| **X1, H2, P2S** | Na página **LAN Only**, liga o **Developer Mode**. Marca a confirmação e escolhe **Enable Developer Mode**. |

> ⚠️ **Lê o aviso na impressora.** Em Developer Mode **a segurança da rede é responsabilidade tua**,
> e a Bambu Lab **não dá apoio** a este modo. Mantém a impressora numa rede de confiança. Nunca a abras para a internet.

## Passo 3 — Tirar a foto

A página LAN Only mostra o **endereço IP** e o **código de acesso**. Chega uma foto.

| Faz | Não faças |
|---|---|
| Usa a **app normal da câmara** do telemóvel | Usar o flash |
| **Baixa a exposição** (−3 EV funcionou bem) | Deixar o ecrã branco e "queimado" |
| Enche a foto com o **ecrã inteiro**, de frente | Fotografar muito de lado |
| Fica quieto e deixa focar o texto | Apanhar reflexos de candeeiros no vidro |

## Passo 4 — Adicionar a impressora no Home Assistant

1. Abre **FDM Printers** na barra lateral.
2. **Add printer → Bambu Lab (LAN)**.
3. Toca em **🖼️ Choose photo** e escolhe a foto. Os campos ficam preenchidos.
   Ou escreve à mão o **IP**, o **número de série** e o **código de acesso**.
4. **Confirma os valores.** `0`/`O`, `1`/`l`/`I` e `5`/`S` parecem-se.
5. Escolhe a **tomada inteligente** que liga a impressora (opcional).
6. Termina. Aparece o cartão da impressora.

A impressora tem de estar **ligada** enquanto a adicionas. A ligação é testada antes de continuar.

### Como a foto é lida

| Tens | O que acontece | Para onde vai a foto |
|---|---|---|
| Uma **AI Task** no Home Assistant (OpenAI, Gemini, Anthropic…) | Lê os três valores | Para esse fornecedor de IA. O formulário diz qual. A cópia no Home Assistant é apagada logo a seguir. |
| Sem AI Task | O texto é lido **no teu telemóvel ou computador** (Tesseract.js) | Para lado nenhum. Só o texto é enviado. O leitor de texto vem com a integração: nada é descarregado da internet. |

## Quando o código de acesso muda

O código pode mudar quando:

- desligas e voltas a ligar o LAN Only **(não confirmado — relatos da comunidade)**;
- fazes reset à impressora ou às definições de rede;
- tocas no **atualizar** ao lado do código (A1 / A1 mini).

O que vês: a impressora fica **indisponível** e o Home Assistant mostra **"Access code rejected"**.

O que fazer:

1. Tira uma foto nova à página LAN Only.
2. O Home Assistant pede o código novo (um aviso de **reautenticação**). Também aparece na página **FDM Printers**.
3. Toca em **🖼️ Choose photo**, confirma o código e grava.

Ficas com a impressora, o histórico e os mesmos IDs das entidades.

> Se também usas a integração **Bambu Lab (ha-bambulab)**, ela guarda a sua própria cópia do código.
> Muda-o lá também: **Definições → Dispositivos e serviços → Bambu Lab → Configurar**.

## O que perdes em LAN Only

Segundo a Bambu Lab:

- **Não imprimes de fora de casa.**
- O **Bambu Handy** não funciona.
- O **histórico de impressões** não está disponível.
- **Série X1:** imprimir pelo Bambu Studio na rede local precisa de um **cartão micro SD**.

Também relatado por utilizadores **(não confirmado)**:

- Sem avisos nem atualizações de firmware pela internet.
  Para atualizar: LAN Only desligado → atualizar → LAN Only ligado (o código pode mudar).

O **Bambu Studio e o Orca Slicer** continuam a funcionar na tua rede.
A impressora aparece com um **cadeado**. Escolhe-a e escreve o **código de acesso** uma vez.

## Rede e portas

A procura automática não passa entre VLANs ou sub-redes. Escreve o **IP** à mão
e abre estas portas do Home Assistant para a impressora:

| Para quê | Porta | Tipo |
|---|---|---|
| Estado e controlo (MQTT com TLS) | **8883** | TCP |
| Ficheiros (FTPS) | 990, 50000–50100 | TCP |
| Câmara — A1, A1 mini, P1 (imagens JPEG) | 6000 | TCP |
| Câmara — X1, H2, P2S (RTSP, precisa do **LAN mode liveview**) | 322 | TCP |
| Procura automática (SSDP) | 1990, 2021 | UDP |

Estas portas vêm da lista oficial da Bambu Lab.

## Problemas comuns

| Problema | O que ver |
|---|---|
| **"Access code rejected"** | O código mudou? Vê [Quando o código de acesso muda](#quando-o-código-de-acesso-muda). São 8 caracteres, **não** o PIN de 6 dígitos da cloud. |
| **Não encontra a impressora sozinho** | A procura usa UDP 1990/2021. Redes mesh e de convidados bloqueiam-na muitas vezes. Escreve o IP à mão. |
| **Duas integrações, a procura falha** | A FDM e o ha-bambulab escutam na mesma porta. Só a procura é afetada; a ligação direta funciona. |
| **O Bambu Handy mostra a impressora desligada** | Normal em LAN Only. O Handy precisa da cloud. |
| **O Bambu Studio não vê a impressora** | Mesma rede? Escolhe a impressora com o cadeado e escreve o código de acesso. |
| **Aviso "requires Developer Mode"** | Carregaste em pausar, parar ou na luz. Liga o Developer Mode ([Passo 2](#passo-2--developer-mode-opcional)) ou ignora: o resto funciona. |
| **Ligada, mas sem estado** | Confirma o **número de série** (a impressora só responde ao seu). Desliga e liga o LAN Only e atualiza o código. |
| **Sem câmara na A1 / P1** | A câmara só aparece com a impressora ligada (o estado funciona). A porta **6000** tem de estar aberta a partir do Home Assistant. Fecha a câmara no Bambu Studio e tenta de novo: a impressora pode servir poucos ecrãs ao mesmo tempo. |
| **Sem câmara na X1 / H2 / P2S** | Ainda não é suportada na FDM (usam RTSP na porta 322). Com o ha-bambulab, liga o **LAN mode liveview** na página LAN Only. |
| **Falta a imagem da peça ou o peso (firmware novo H2 / P2S)** | Liga **Store sent files on external storage** nas opções de impressão e põe uma pen USB ou cartão SD. **(não confirmado na FDM — relatado pelo ha-bambulab)** |

Para reportar um erro, junta o ficheiro de **diagnóstico** (página do aparelho → **Download diagnostics**)
e os registos de depuração (`custom_components.fdm_printer_manager: debug`).

## Segurança

- O **código de acesso é uma palavra-passe**. Quem o tiver, na tua rede, controla a impressora.
- **Não partilhes fotos** da página LAN Only. Corta ou desfoca o código antes.
- As fotos originais do telemóvel podem ter a tua **localização GPS**.
- Não ponhas a impressora na rede de convidados e nunca abras as portas dela para a internet.

## Voltar ao modo cloud

1. Abre a página **LAN Only** na impressora.
2. Desliga o **Developer Mode** (se o ligaste) e depois o **LAN Only**.
3. O Bambu Handy volta a funcionar. O Home Assistant perde a ligação direta até voltares a ligar o LAN Only.

## Notas técnicas

- O MQTT usa **TLS na porta 8883**. Utilizador `bblp`, palavra-passe = **código de acesso**. **(utilizador não confirmado pela Bambu Lab — usado por ferramentas da comunidade)**

## Fontes

Oficiais (Bambu Lab):

- [How to enable LAN Mode](https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode)
- [How to enable Developer Mode](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode)
- [Printer network ports](https://wiki.bambulab.com/en/general/printer-network-ports)
- [How to find the serial number](https://wiki.bambulab.com/en/general/find-sn)
- [Updates and third-party integration with Bambu Connect (janeiro de 2025)](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/)

Comunidade (só para as linhas **não confirmado**):

- [Guia de configuração do ha-bambulab](https://github.com/greghesp/ha-bambulab/blob/main/docs/setup.mdx)
- [A1 mini: where do I find the access code?](https://forum.bambulab.com/t/a1-mini-where-do-i-find-the-access-code/56991)
- [Access code always needs to be re-entered](https://forum.bambulab.com/t/access-code-always-needs-to-be-re-entered/82201)
- [How to get into LAN mode and use it](https://forum.bambulab.com/t/how-to-get-into-lan-mode-and-use-it/142072)
