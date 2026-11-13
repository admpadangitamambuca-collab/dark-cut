# DarkCut AI 3.0 — File Studio

**Novo fluxo:** o usuário seleciona o **arquivo completo MP4** do celular ou computador. O site envia o vídeo em partes de 4 MiB ao motor Python (Render). O motor analisa a transcrição, seleciona trechos interessantes e entrega **para cada corte**:

- **Shorts vertical 9:16**, MP4 720 × 1280.
- **Corte tradicional horizontal 16:9**, MP4 1280 × 720.
- **Título**, **descrição**, **legenda para publicar** e **hashtags**, em abas separadas e com botões de copiar.
- ZIP com os MP4, um TXT de publicação para cada corte e um JSON dos metadados.

Não publica automaticamente em plataformas. Não é preciso informar um link do YouTube nem conectar Snaptube, Cobalt ou outro downloader. O usuário obtém o arquivo de forma autorizada por seus próprios meios e faz upload.

## Arquivos

| Caminho | Função |
|---|---|
| `site/index.html` | Formulário de arquivo, prévia, configurações, resultados e abas |
| `site/styles.css` | Design responsivo |
| `site/app.js` | Upload em partes, progresso, polling, prévias, cópia e download |
| `site/config.js` | URL pública do motor (sem segredos) |
| `worker/app/main.py` | API de upload, processamento, status, arquivos e ZIP |
| `worker/app/engine.py` | FFmpeg, transcrição, seleção, geração de metadados |
| `worker/tests/` | Testes de upload, segurança básica, exportação e legendas |
| `worker/Dockerfile` | Contêiner Python + FFmpeg |
| `render.yaml` | Modelo de serviço gratuito no Render |
| `netlify.toml` | Publica o diretório `site` |

## Publicar a interface no Netlify

Se o site estiver conectado ao repositório GitHub `admpadangitamambuca-collab/dark-cut`, use a branch `main` e confirme que a pasta de publicação é `site` (o `netlify.toml` já define isso). O upload deste código no GitHub **não comprova** que o site Netlify atual foi publicado.

Para publicação manual via Netlify Drop, use o arquivo **DarkCut_AI_v3_SITE_NETLIFY.zip** incluído na entrega; os arquivos `index.html`, `app.js`, `styles.css` e `config.js` estão na raiz desse pacote. Extraia o ZIP e arraste a pasta extraída para o Netlify Drop, caso o painel não aceite ZIP.

O `site/config.js` contém somente a URL pública do worker: `https://darkcut-ai-worker-nbs4.onrender.com`. Você pode substituí-la ou editar o campo em **Configuração** no site. **Nunca coloque uma chave de API no frontend.**

## Publicar o motor Python no Render

Serviço já existente: **`darkcut-ai-worker-nbs4`** na workspace `Administrativo's workspace`.

- Runtime: Docker; Dockerfile `worker/Dockerfile`; contexto `worker`.
- Plano: gratuito, com limitações de CPU/RAM e armazenamento temporário.
- Porta: variável `PORT` de Render; aplicativo escuta em `0.0.0.0`.
- Saúde: `GET /api/health` indica `version: "3.0.0"`, `upload_only: true`.
- CORS: `CORS_ORIGINS=https://reliable-seahorse-fe5df4.netlify.app`.
- `MAX_CONCURRENT_JOBS=1`; `WHISPER_MODEL=tiny` para prototipagem.
- `MAX_UPLOAD_BYTES` padrão: **500 MiB**. O front consulta esse valor na API de saúde.
- `JOB_TTL_HOURS` padrão: **24**, desde que a instância não reinicie/hiberne.

### Fluxo da API

1. `POST /api/uploads` com JSON: `filename`, `size`, `count`, `min_seconds`, `max_seconds`, `subtitles`. Devolve `id`, `upload_token`, `chunk_bytes`.
2. `PUT /api/uploads/{id}/chunks/{i}` com `Content-Type: application/octet-stream`, `X-Upload-Token` e uma parte de até 4 MiB. As partes devem ser enviadas sequencialmente.
3. `POST /api/uploads/{id}/finish` com o token. Inicia o trabalho.
4. `GET /api/jobs/{id}` devolve o andamento e, depois, os cortes e metadados.
5. `GET /api/jobs/{id}/files/{nome}` baixa os MP4, TXT e ZIP.
6. `POST /api/uploads/{id}/cancel` permite cancelar um upload inacabado.

O endpoint antigo multipart `/api/upload-jobs` permanece **apenas por compatibilidade temporária** com sites antigos. O antigo processamento de links (`POST /api/jobs`) retorna 410, porque o novo app é **arquivo-first**.

## Textos gerados para cada corte

O sistema transcreve usando Faster-Whisper e pontua janelas da fala para identificar possíveis momentos de interesse. Ele **não garante** que sejam os melhores trechos em todo tipo de vídeo. Em vídeos sem fala inteligível, a análise pode falhar.

- **Sem** `OPENAI_API_KEY`, os títulos, descrições, legendas e hashtags são **sugestões extrativas**, produzidas com base na transcrição, identificadas claramente no site. Não são textos de um LLM externo.
- **Com** `OPENAI_API_KEY` configurada **somente no Render**, o sistema solicita uma proposta para cada corte via API compatível. Isso pode gerar custos de uso e não está ativado por padrão. Se a API externa falhar, retorna ao texto extrativo.

Legendas automáticas **dentro do vídeo** são opcionais e diferentes da **legenda de publicação** exibida em aba própria.

## Executar testes locais

Requisitos: Python 3.11+, FFmpeg (com `ffprobe` e suporte a `libx264`/legendas), internet para baixar o modelo Whisper na primeira transcrição.

```bash
cd worker
python -m venv .venv
# Ativar ambiente virtual
pip install -r requirements.txt
pip install pytest httpx
pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Em outro terminal, da raiz do projeto:

```bash
python -m http.server 8888 --directory site
```

Abra http://localhost:8888 e, em **Configuração**, selecione http://localhost:8080 para testar localmente.

## Limitações e segurança importantes

- O **Render gratuito** pode entrar em suspensão após inatividade; vídeos e cortes ficam em armazenamento **efêmero** e desaparecem se o serviço reiniciar, hibernar ou for republicado. Baixe os resultados logo após o processamento.
- Limite por arquivo: 500 MiB (configurável). Limite de tamanho não garante que o servidor gratuito tenha memória, CPU e disco para vídeos longos. O arquivo completo e os dois cortes gerados compartilham espaço temporário durante o processamento.
- O upload em blocos reduz o tamanho das requisições, mas **não** transforma o Render gratuito em infraestrutura para arquivos ilimitados. Uploads não são retomados após reinício; o navegador pode precisar reenvialos.
- A API atualmente não exige login para criar trabalhos. O token de upload protege etapas de um trabalho, **mas não equivale à autenticação do usuário**. Não anuncie o serviço como plataforma pública de uso irrestrito sem autenticação, cotas, rate limiting, armazenamento durável e uma fila de trabalho.
- Só envie conteúdo que você tenha direito de utilizar. Nenhuma plataforma externa recebe publicações automaticamente.

## Versão

**3.0.0** — Novo aplicativo de upload de arquivo completo, sem dependências de download de YouTube; 4 abas por corte; envio em partes; ZIP com vídeo e kit de publicação.


## Correção da transcrição no Render (3.0.1)

A imagem do worker instala `libgomp1` (dependência nativa/OpenMP de bibliotecas de inferência),
limita `huggingface-hub` à família 0.x para compatibilidade com `faster-whisper==1.1.1`
e valida a instrução `from faster_whisper import WhisperModel` **durante o build Docker**.
A rota `/api/health` informa `transcription_ready`; um valor `true` confirma que o módulo importa
(sem garantir que o modelo esteja baixado ou que o Render gratuito tenha RAM suficiente para transcrever vídeos longos).
Falhas de importação e carregamento do modelo são registradas no Render com traceback,
e o usuário recebe uma mensagem distinguindo biblioteca ausente e falha de inicialização do modelo.