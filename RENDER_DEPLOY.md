# Deploy do motor DarkCut AI no Render

O DarkCut AI tem um **frontend estático** na pasta `site/` e uma **API de processamento** em `worker/`. Este guia trata apenas do Render; nenhuma alteração no Netlify é necessária para esta etapa.

## Opção 1: Blueprint com o arquivo render.yaml

1. Acesse https://dashboard.render.com/ e entre com o GitHub que tem acesso a `admpadangitamambuca-collab/dark-cut`.
2. Selecione **New → Blueprint** e escolha esse repositório, branch `main`.
3. Confirme as configurações previstas em `render.yaml` antes de implantar: serviço web Docker, pasta de contexto `worker/`, healthcheck `/api/health`.
4. A configuração inicial pede **plano gratuito**, sem solicitar contratação de plano pago. **Não significa que o motor conseguirá processar vídeos nesse plano:** 512 MB RAM/0,1 CPU podem ser insuficientes para transcrição e FFmpeg.
5. Quando aparecer o endereço público `https://...onrender.com`, abra `https://...onrender.com/api/health`: a resposta esperada inclui `"ok": true`.
6. Defina `CORS_ORIGINS` em **Environment** com o domínio HTTPS real do site que irá usar a API. A configuração padrão permite apenas localhost.
7. `OPENAI_API_KEY` é **opcional** para gerar títulos por modelo; adicione somente em **Environment**, nunca no GitHub ou no navegador. Sem a chave, o motor usa títulos/hashtags/resumos extraídos da transcrição.

## Opção 2: Web Service manual

- Fonte: `admpadangitamambuca-collab/dark-cut`, branch `main`
- Runtime: **Docker**
- Dockerfile: `worker/Dockerfile`
- Docker context: `worker`
- Health check: `/api/health`
- Serviço deve aceitar tráfego público HTTPS, com permissões adequadas de acesso.

## Limitações antes de uso real

- O serviço usa disco temporário e guarda jobs em memória. Reinicializações, hibernação ou novas versões podem interromper trabalhos e perder downloads.
- A API de jobs não possui autenticação/rate limiting. CORS não impede uso direto por terceiros. Não exponha para uso público sem controles contra abuso e sobrecarga.
- O plano gratuito tem limitações de CPU/RAM e hiberna por inatividade; vídeos longos não são recomendados. Use uma instância mais potente, fila e armazenamento persistente para produção.
- Lives precisam estar arquivadas e acessíveis para download. O algoritmo não garante os momentos de maior viralização.
- Publique/processse somente conteúdos cujo uso seja autorizado.

## Verificação mínima do serviço

```sh
curl -i https://SEU-SERVICO.onrender.com/api/health
```

A ligação do endereço ao site existente poderá ser feita depois, pela tela de configuração do DarkCut AI, sem vincular o Netlify ao GitHub.
