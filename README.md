# DarkCut AI 2.2 — Creative Studio

Atualização visual e funcional do gerador de cortes. Interface em português brasileiro, pronta para hospedar no **Netlify**, mais um serviço de processamento separado em Python/FFmpeg.

**O que faz**

1. Recebe um link de vídeo do YouTube, live **arquivada** ou arquivo direto de vídeo público via HTTPS.
2. Obtém o conteúdo via Cobalt (para YouTube) ou link direto de mídia, transcreve a fala com Faster-Whisper e identifica trechos interessantes usando texto e pontuação contextual.
3. Exporta cada trecho em **vertical 720 × 1280** e **horizontal 1280 × 720**, ambos MP4; legendas automáticas opcionais.
4. Cria **um título, hashtags e um resumo por corte**, fundamentados na transcrição do trecho. Com `OPENAI_API_KEY` configurada, esse material é criado por modelo de linguagem; sem chave, oferece sugestões **extrativas**, claramente identificadas na interface como tal.
5. Mostra prévia dos dois formatos, abas independentes Título / Hashtags / Resumo, botões para copiar e download de cada MP4 ou ZIP total (incluindo JSON de metadados).
6. **Não envia conteúdo ao YouTube** nem publica automaticamente.

## Estrutura

```
site/index.html       Interface principal
site/styles.css       Identidade visual profissional e responsiva
site/app.js           Envio de jobs, progresso, resultados, tabs e copiar
site/config.js        Endereço público do worker (NUNCA chave de IA)
netlify.toml          Publica a pasta site no Netlify
worker/app/main.py    API FastAPI + jobs + downloads
worker/app/engine.py  Transcrição, ranking, IA, legendas e renders
worker/app/cobalt.py  Download de vídeos YouTube via Cobalt autorizado
worker/requirements.txt
worker/Dockerfile
worker/tests/         Testes unitários
```

## Por que são dois serviços?

O Netlify hospeda a parte estática e não é adequado para processar vídeos longos, transcrever e exportar vários MP4 em uma única função curta. O **worker precisa rodar em um servidor/container com CPU e FFmpeg**, por exemplo uma instância Cloud Run com CPU sempre alocada.

O ZIP contém todo o código, porém **publicar somente o ZIP no Netlify não cria automaticamente o worker**. Para processar vídeos, configure ambos os serviços.

## Testar em computador local

Requer Python 3.11+, FFmpeg e FFprobe no `PATH`, além de internet para instalar pacotes, baixar modelos e acessar vídeos.

```sh
cd worker
python -m venv .venv
# Ative o ambiente virtual conforme o sistema operacional
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Em outro terminal, na raiz do projeto:

```sh
python -m http.server 8888 --directory site
```

Abra `http://localhost:8888`, vá em **Configuração** e salve `http://localhost:8080`. O primeiro uso da transcrição baixa o modelo Whisper configurado (padrão `base`).

## Implantar o worker no Cloud Run (exemplo)

1. Tenha uma conta e um projeto Google Cloud com faturamento, Cloud Run, Artifact Registry e Cloud Build habilitados. **Cloud Run não é garantidamente gratuito**.
2. A partir da pasta `worker`, construa e implante a imagem Docker. Exemplo ilustrativo com Google Cloud CLI:

```sh
# Execute na pasta worker e substitua o projeto e a região.
gcloud run deploy darkcut-worker \
  --source . \
  --project SEU_PROJETO \
  --region southamerica-east1 \
  --allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --timeout 3600 \
  --set-env-vars CORS_ORIGINS=https://SEU-SITE.netlify.app,WHISPER_MODEL=base
```

3. Adicione `OPENAI_API_KEY` como **segredo** do Cloud Run (Secret Manager), sem colocá-la em `site/config.js`, GitHub ou arquivos públicos. Configure o modelo via `OPENAI_MODEL` se desejado. Dependendo da conta/versão do Cloud Run, pode ser necessário ajustar a sintaxe de `--no-cpu-throttling`.
4. Copie o endereço público HTTPS retornado pelo Cloud Run.
5. Publique a pasta `site` no Netlify (ou a raiz deste projeto, que já contém `netlify.toml`). No Netlify, configure `site/config.js` com `workerUrl: "https://SEU-WORKER..."` **ou** abra a tela Configuração para salvar a URL no seu navegador.
6. Certifique-se de que `CORS_ORIGINS` contenha **exatamente a origem HTTPS do seu site Netlify**. Teste `/api/health` no worker.

### Variáveis úteis no worker

| Nome | Padrão | Uso |
|---|---|---|
| `OPENAI_API_KEY` | vazio | Ativa metadados gerados por IA (servidor somente) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Modelo do endpoint de chat |
| `OPENAI_CHAT_URL` | API da OpenAI | Endpoint compatível opcional |
| `WHISPER_MODEL` | `base` | Modelo de reconhecimento de fala no CPU |
| `CORS_ORIGINS` | localhost:8888/8000 | Origens exatas permitidas, separadas por vírgula |
| `MAX_CONCURRENT_JOBS` | `1` | Processamentos simultâneos |
| `MAX_SOURCE_BYTES` | `1073741824` | Limite de tamanho da origem, em bytes |
| `MAX_SOURCE_SECONDS` | `10800` | Duração máxima aceita da origem |
| `JOB_TTL_HOURS` | `24` | Tempo até limpeza de diretórios antigos |
| `DATA_DIR` | `/tmp/darkcut-jobs` | Pasta de jobs e vídeos |

## Limitações técnicas, testes e melhorias de produção

- **Não foi possível executar um vídeo do YouTube real neste ambiente**, nem instalar/rodar Faster-Whisper nesta sessão. A integração YouTube/Whisper/IA deverá ser validada com internet e segredos adequados no worker após deploy. Uma instância Cobalt própria ou autorizada é necessária para links do YouTube; vídeos com DRM, login ou transmissões ainda em andamento podem não funcionar.
- O algoritmo atual seleciona candidatos por **pistas textuais e transcrição**, não por um modelo treinado para prever viralização. Não há garantia de que os trechos serão os melhores em todos os casos. O enquadramento vertical é centralizado; ainda não usa rastreamento automático de rostos.
- Sem chave configurada, o sistema **não finge** usar uma IA de linguagem para títulos, resumos e hashtags: a interface exibe *Sugestão baseada na transcrição*.
- Os jobs rodam em thread dentro do mesmo processo; os arquivos são guardados no disco local efêmero do contêiner. O modo `min-instances=1` e CPU sempre alocada é uma **configuração de protótipo**. Para serviço público/alta escala, implemente fila durável, armazenamento de objetos, autenticação, controle de custos, reintentos, antivírus, limpeza periódica e proteção contra abuso. Reinicializações da instância podem perder jobs.
- **Segurança:** o worker é público no exemplo, aceita somente origens de navegador autorizadas para CORS, mas CORS sozinho **não autentica** clientes. Um endpoint público com mídia e inferência pode gerar custos. Antes de abrir para terceiros, adicione autenticação, rate limiting, uma fila e validação reforçada de hosts/redirecionamentos contra SSRF.
- Use vídeos próprios ou para os quais possua os direitos e permissões necessários.

## Verificações locais realizadas

- Oito testes automatizados do motor e da API: metadados extrativos, temporização SRT, escrita de legendas, recorte não sobreposto, bloqueio de link inseguro e link do YouTube.
- Código Python compilado; JavaScript passou `node --check`.
- Exportação real pelo FFmpeg testada com vídeo sintético e legenda, confirmando **720×1280** e **1280×720**.

**Versão:** 2.2 (reconstrução da especificação v2.1 + novas abas e design; não é patch binário do ZIP v2.1 porque o arquivo anterior não estava acessível nesta sessão).

## Motor de download Cobalt (sem yt-dlp)

O único mecanismo de obtenção de vídeos do YouTube é o **Cobalt**, configurável via `COBALT_API_URL` e `COBALT_API_KEY` no servidor. A instância deve ser própria ou autorizada; serviços Cobalt públicos não autorizam por padrão o uso automatizado por outros produtos. Consulte [COBALT_SETUP.md](COBALT_SETUP.md). O Cobalt não garante eliminar bloqueios do YouTube e a versão gratuita do Render possui recursos limitados.