# Changelog

## 3.0.0 — File Studio

- O envio do **arquivo MP4 completo** substitui a inserção de links na interface.
- Upload sequencial em partes de 4 MiB, progresso e limite de 500 MiB.
- Seleção de trechos com base na transcrição; dois arquivos por destaque: Shorts 9:16 e tradicional 16:9.
- Abas separadas para **Título**, **Descrição**, **Legenda de publicação** e **Hashtags**, com copiar individual e copiar tudo.
- ZIP com vídeos, arquivo TXT de texto por corte e JSON de metadados.
- Não depende mais de Cobalt, yt-dlp, Snaptube nem de qualquer site para obtenção de vídeo.
- Compatibilidade temporária com o upload multipart da interface anterior; criação de projeto por URL desativada.
- Legendas embutidas no vídeo continuam opcionais.
- Não faz publicação automática.

## 3.1.0 — OOM reduzido e R2 opcional
- Logs do Render confirmaram reinicialização por limite de 512 MiB.
- FFmpeg extrai áudio em janelas curtas, evitando decodificar o vídeo inteiro para RAM.
- Whisper usa `beam_size=1`, `vad_filter=False`, CPU limitada a 1 thread; FFmpeg H264 com threads limitadas.
- Integração opcional com Cloudflare R2, para preservar o original, status, vídeos e ZIP; recuperação após reiniciar quando o backup já existe.
- Site tenta reconectar e retomar automaticamente uma tarefa interrompida uma vez.
- Nenhum armazenamento externo ativado, nenhuma chave armazenada no repositório e nenhuma contratação efetuada.