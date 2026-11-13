# DarkCut AI — armazenamento durável (Cloudflare R2)

Configuração necessária **na conta do proprietário**, sem exposição de chaves.

1. Acesse https://dash.cloudflare.com/ e entre em **R2 Object Storage**.
2. Crie o bucket privado `darkcut-arquivos`, classe **Standard**.
3. Gere uma API token com **Object Read & Write** restrita ao bucket.
4. Em https://dashboard.render.com/web/srv-dbr3ps8n74is73d8q160 , abra **Environment**, adicione:

```
R2_ENDPOINT=https://SEU_ACCOUNT_ID.r2.cloudflarestorage.com
R2_BUCKET=darkcut-arquivos
R2_ACCESS_KEY_ID=SEU_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=SEU_SECRET_ACCESS_KEY
```

5. Salve as variáveis. O Render reinicia o serviço. Consulte a URL https://darkcut-ai-worker-nbs4.onrender.com/api/health e confirme `persistent_storage_enabled: true`.

**Nenhuma chave deve ser colocada no Netlify, GitHub ou nesta conversa.** O backup só começa a funcionar depois dessa configuração. Sem ela, o sistema continua com armazenamento temporário.

R2 não resolve a insuficiência de memória do Render Free (512 MiB). Para vídeos longos, use mais memória ou um computador local. A faixa gratuita R2 Standard é de 10 GB-mês, sujeita a limites, possível cobrança acima da cota e condições da Cloudflare. Não há política de expiração criada pelo app.