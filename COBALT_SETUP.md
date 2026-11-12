# DarkCut AI — Cobalt como único motor de YouTube

A versão atual não usa `yt-dlp`. O frontend Netlify continua igual, e a API Python `worker/` continua cuidando do corte, transcrição, títulos e arquivos MP4. **Vídeos do YouTube são obtidos exclusivamente através de uma instância Cobalt própria ou com permissão explícita.** URLs HTTPS diretas de arquivo de vídeo continuam aceitas pelo transferidor seguro do servidor.

## Arquitetura

1. Netlify envia um trabalho ao `darkcut-ai-worker-nbs4` no Render.
2. Worker consulta `COBALT_API_URL` com `POST /` e, quando configurado, `Authorization: Api-Key ...`.
3. Cobalt devolve um link de mídia (resposta `tunnel` ou `redirect`); worker baixa com tamanho máximo e checagem de destino público HTTPS.
4. Worker transcreve com Faster-Whisper, pontua trechos e exporta MP4 vertical e horizontal.

A API também pode responder `error`, `picker` ou `local-processing`: para esses casos, o worker exibe uma mensagem de erro específica; não promete um download bem-sucedido.

## Variáveis do worker no Render

- `COBALT_API_URL`: URL raiz HTTPS de sua instância Cobalt, por exemplo `https://seu-cobalt.onrender.com/`. **Obrigatória para YouTube.** Não existe instância pública definida por padrão.
- `COBALT_API_KEY`: chave privada fornecida pelo operador da instância (opcional se a instância autorizada não exige autenticação).
- `CORS_ORIGINS`: domínio do frontend, já configurado no Render.
- `MAX_SOURCE_BYTES`: limite para cada mídia. Padrão 1 GiB.
- `WHISPER_MODEL`: modelo de transcrição, `tiny` na instância gratuita.

As chaves ficam apenas em **Render → Environment**. Nunca configure no site, GitHub ou exemplos públicos. O endpoint `GET /api/health` informa `download_engine: cobalt` e `cobalt_configured` sem expor segredos.

## Hospedar uma instância Cobalt

O diretório `cobalt/` contém uma imagem Docker derivada da imagem oficial `ghcr.io/imputnet/cobalt:11`. O entrypoint gera um arquivo privado de chave e **requer autenticação**.

Para criar como um Web Service **Docker** adicional no Render:

- Repositório: `admpadangitamambuca-collab/dark-cut`, branch `main`
- Dockerfile: `cobalt/Dockerfile`
- Docker context: `.`
- Nome: `darkcut-cobalt-api`
- Plano: gratuito para teste, sem contratar plano pago sem autorização
- Environment:
  - `API_URL=https://<endereco-real-do-servico>.onrender.com/`
  - `COBALT_PRIVATE_API_KEY=<UUIDv4 privado gerado pelo administrador>`

A instância configura `API_PORT=$PORT`, `API_AUTH_REQUIRED=1` e `API_KEY_URL=file:///tmp/darkcut-cobalt-keys.json` automaticamente.

Configure as variáveis do **worker** para apontar a essa instância: `COBALT_API_URL` e `COBALT_API_KEY` (mesma chave que a instância Cobalt). Depois, verifique os dois serviços. **Não habilite APIs públicas de terceiros sem autorização.**

## Limitações e segurança

- A instância gratuita pode hibernar e não é indicada para vídeo longo, download/conversão simultâneos ou uso público contínuo.
- O YouTube pode exigir autenticação, PO Token e outras verificações mesmo quando o Cobalt é usado. Não há garantia de que um vídeo bloqueado no Render seja recuperado pelo Cobalt no mesmo IP ou datacenter.
- O `worker/` ainda permite jobs publicamente sem autenticação e apenas limita concorrência. CORS não constitui autenticação. Antes de divulgar amplamente, implemente autenticação/rate limit, fila durável, persistência e limites de execução.
- Esta versão só implementa respostas `tunnel` e `redirect` do Cobalt. `picker` e `local-processing` são recusadas de modo controlado.
- Utilize apenas conteúdo que você tem direito de usar e respeite os termos das plataformas.

Fontes oficiais:
- https://github.com/imputnet/cobalt/blob/main/docs/api.md
- https://github.com/imputnet/cobalt/blob/main/docs/run-an-instance.md
- https://github.com/imputnet/cobalt/blob/main/docs/protect-an-instance.md