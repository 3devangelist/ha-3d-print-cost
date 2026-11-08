# Registar cada impressão numa folha do Google Sheets

[English](google-sheets.md) | **Português**

> Verificado com a [documentação do Google Sheets no Home Assistant](https://www.home-assistant.io/integrations/google_sheets/)
> a 1 de novembro de 2026. A Google muda os nomes dos menus de vez em quando. Se um nome não bater certo, procura o mais parecido.

## Em resumo

| Passo | Onde | Tempo | Quantas vezes |
|---|---|---|---|
| 1. Credenciais da Google Cloud | Consola da Google Cloud | cerca de 10 min | **uma vez** |
| 2. Ligar o Google Sheets | FDM Printers → **Google Sheets** → **Connect Google Sheets** | 1 min | uma vez |
| 3. Começar a registar | FDM Printers → **Google Sheets** → **Start logging** | 1 clique | uma vez |

Depois disso, cada impressão que acaba (bem, falhada ou cancelada) junta uma linha à folha.

## Porque é preciso o passo 1

A FDM Printer Manager não fala com a Google.
Usa a integração **Google Sheets** do próprio Home Assistant.
A Google só deixa um programa escrever no teu Drive com credenciais criadas **por ti**, num projeto teu.
É grátis.
É o único passo que não dá para fazer dentro do Home Assistant.

## Passo 1 — Credenciais da Google Cloud (uma vez)

### 1.1 Criar um projeto

1. Abre a [consola da Google Cloud](https://console.cloud.google.com/) e entra com a tua conta.
2. Em cima, abre a lista de projetos → **New project**.
3. Dá um nome, por exemplo `Home Assistant`, e carrega em **Create**.
4. Confirma que o projeto novo está escolhido em cima.

### 1.2 Ligar duas APIs

Abre cada link, confirma o projeto em cima e carrega em **Enable**:

| API | Link |
|---|---|
| Google Sheets API | [sheets.googleapis.com](https://console.cloud.google.com/apis/library/sheets.googleapis.com) |
| Google Drive API | [drive.googleapis.com](https://console.cloud.google.com/apis/library/drive.googleapis.com) |

### 1.3 Ecrã de consentimento (Google Auth Platform)

1. Abre [Google Auth Platform → Branding](https://console.cloud.google.com/auth/branding).
   Se disser que não está configurado, carrega em **Get started**.
2. **App name**: por exemplo `Home Assistant`. **User support email**: o teu email.
3. **Audience**: **External**.
4. **Contact information**: o teu email. Aceita a política de dados e carrega em **Create**.
5. Abre **Audience** e escolhe uma opção:

| Opção | O que acontece |
|---|---|
| **Publish app** (recomendado) | O acesso não caduca. A Google mostra uma vez o aviso "not verified". É normal numa app tua. |
| Deixar em **Testing** | Junta a tua conta Google em **Test users**. O acesso **caduca a cada 7 dias**: tens de entrar outra vez todas as semanas. |

### 1.4 Criar o cliente

1. Abre **Clients** → **Create client**.
2. **Application type**: **Web application**. Nome: o que quiseres.
3. **Authorized redirect URIs** → **Add URI**:

   ```
   https://my.home-assistant.io/redirect/oauth
   ```

   Escreve exatamente assim. Não é um exemplo.
4. Carrega em **Create**.
5. Copia já o **Client ID** e o **Client secret**. O secret não volta a aparecer.

## Passo 2 — Ligar o Google Sheets (a partir da página FDM Printers)

1. Abre **FDM Printers** na barra lateral.
2. Carrega em **Google Sheets** (em cima, ao lado de "Add printer").
3. Carrega em **Connect Google Sheets**. Abre a janela do Home Assistant para juntar o Google Sheets.
4. Da primeira vez, pede as **Application credentials**:
   - **Name**: por exemplo `Google`
   - **OAuth Client ID** e **OAuth Client Secret**: os dois valores do passo 1.4.
5. Entra com a tua conta Google. Se a Google disser que a app não está verificada,
   carrega em **Continue** (a app é tua).
6. Confirma o endereço na página "Link account to Home Assistant?" e carrega em **Link account**.
7. O Home Assistant diz **Success!**. Criou uma folha chamada **Home Assistant** no teu Google Drive.
   Podes mudar-lhe o nome no Drive.

> Já tinhas o Google Sheets ligado? Salta o passo 2.
> O botão da FDM mostra as entradas do Google Sheets que já existem.

## Passo 3 — Começar a registar (um clique)

1. Volta a **FDM Printers** → **Google Sheets**.
2. Escolhe a folha. A lista mostra cada entrada do Google Sheets pelo nome.
3. Deixa o separador **Prints**, ou escreve outro nome.
4. Carrega em **Start logging**.

O que o botão faz:

| Ação | Detalhe |
|---|---|
| Cria o separador | Só se não existir. Um separador novo leva a linha de cabeçalhos, fixa em cima. Um separador que já existe não muda. |
| Liga o registo | Em **todas** as impressoras (nas opções de cada uma: **Edit → When a print ends**). As outras opções ficam iguais. |
| Mostra o resultado | "✓ Logging to *folha* › Prints", com **Open the sheet ↗**. |

Para parar: **Google Sheets** → **Turn off for all printers**.
Para uma impressora escrever noutro separador: **Edit → When a print ends** nessa impressora (continua a funcionar).

## As colunas

| Coluna | O que tem |
|---|---|
| `created` | Quando a linha foi escrita (posta pelo Google Sheets) |
| `printer` | Nome da impressora |
| `file` | Nome do ficheiro |
| `result` | `success`, `failed`, `cancelled` ou `unknown` |
| `result_by` | Quem decidiu o resultado: `printer`, `watchdog` ou `user` |
| `started`, `finished` | Hora local, `AAAA-MM-DD HH:MM:SS` |
| `duration_min` | Minutos |
| `energy_kwh` | Energia da impressão |
| `electricity_cost`, `currency` | Custo da eletricidade |
| `notes` | Notas (por exemplo, uma correção) |
| `job_id` | Identificador da impressão |
| `filament_mm` | Filamento gasto de verdade, em mm (desde a 0.14.0; também nas impressões canceladas e falhadas). Vazio quando a impressora não o diz (Bambu Lab) |
| `filament_g` | O mesmo em gramas: `filament_mm` x o peso por mm do ficheiro, dado pelo slicer. Vazio sem esse peso |
| `filament_type` | Tipo de filamento escrito pelo slicer (por exemplo `ASA`) |
| `filament_cost` | `filament_g` x o **Filament price per kg** da impressora (Edit → Advanced). Vazio sem preço |

## Limites

| Limite | O que quer dizer |
|---|---|
| **Só a folha criada pela integração** | O Google Sheets do Home Assistant só escreve na folha que criou quando o ligaste. Não escreve numa folha feita por ti. Queres uma folha à parte? Usa **Add another spreadsheet**: junta outra entrada do Google Sheets, com uma folha nova. |
| **Uma linha por impressão** | Se marcares depois uma impressão ("Wrong? Mark as failed / Mark as OK" no cartão) e o resultado mudar, a linha dessa impressão é **alterada no sítio**: `result` e `result_by` = `user`, encontrada pelo `job_id` (uma linha ainda à espera de ser enviada é alterada antes de ir). Só quando essa linha não se encontra ou não se pode alterar é que se junta uma **linha de correção**, com o mesmo `job_id` e uma nota; aí, ao contar, fica com a última linha de cada `job_id`. |
| **Colunas pelo nome** | Cada valor vai para a coluna com o mesmo nome na linha 1. Uma coluna que falte é juntada no fim. |
| **Sem rede** | A linha fica à espera (até 200 linhas) e é enviada antes da seguinte: na próxima impressão ou quando o Home Assistant arranca. Recebes uma notificação. |

## Quem pode configurar

Só **administradores**. Configurar o registo muda as opções de todas as impressoras e cria um separador no Google. Por isso a ação `fdm_printer_manager.sheets_setup`, o botão "Google Sheets" e marcar uma impressão (`fdm_printer_manager.mark_print`, que altera linhas na folha) são recusados a outros utilizadores. Automações e scripts continuam a funcionar: correm sem utilizador.

O Home Assistant não tem permissões por ação. Para deixar outra pessoa configurar o registo, torna-a administradora (Definições → Pessoas → a pessoa → **Administrador**).

## Aproveitar um registo antigo (cabeçalhos em português)

Tinhas um separador escrito por uma automação antiga, com cabeçalhos em português?
Podes aproveitá-lo. Muda os nomes na linha 1 e escolhe o nome desse separador no passo 3.
As linhas antigas ficam.

| Cabeçalho antigo | Cabeçalho novo |
|---|---|
| `impressora` | `printer` |
| `ficheiro` | `file` |
| `inicio` | `started` |
| `fim` | `finished` |
| `duracao_min` | `duration_min` |
| `energia_kwh` | `energy_kwh` |
| `resultado` | `result` |
| `deteccao` | `result_by` |

O separador tem de estar na folha criada pela integração Google Sheets (ver Limites).

## Problemas

| O que aparece | O que fazer |
|---|---|
| "Google Sheets is not set up" | Faz o passo 2. |
| "is not working … sign in with Google again" | Definições → Dispositivos e serviços → Google Sheets → entrar outra vez. Em **Testing** isto acontece a cada 7 dias: publica a app (passo 1.3). |
| "Cannot open the spreadsheet … or add the tab" | A folha foi apagada, ou as APIs estão desligadas (passo 1.2). |
| A Google diz "Access blocked" ou "redirect_uri_mismatch" | O endereço do passo 1.4 não está exatamente `https://my.home-assistant.io/redirect/oauth`. |
| Client ID ou secret errado | Definições → Dispositivos e serviços → ⋮ → **Application credentials**: apaga-as e repete o passo 2. |
