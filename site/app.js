(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const els = {
    form: $('create-form'), file: $('video-file'), drop: $('upload-zone'),
    fileDetails: $('upload-file-details'), filePreview: $('file-preview'),
    originalPreview: $('original-preview'), previewName: $('file-preview-name'),
    previewData: $('file-preview-data'), shortsCount: $('shorts-count'), shortsLength: $('shorts-length'),
    normalCount: $('normal-count'), normalLength: $('normal-length'),
    subtitles: $('include-subtitles'), error: $('form-error'), button: $('generate-btn'),
    buttonLabel: $('generate-label'), progress: $('progress-panel'), stage: $('job-stage'),
    percent: $('progress-number'), fill: $('progress-fill'), empty: $('results-empty'),
    list: $('clip-list'), resultCount: $('results-count'), resultDesc: $('results-desc'),
    downloadAll: $('download-all'), workerUrl: $('worker-url'),
    saveWorker: $('save-worker'), workerFeedback: $('worker-feedback'),
    review: $('review'), reviewList: $('review-list'), reviewSummary: $('review-summary'),
    reviewPlayer: $('review-player'), reviewPlayerCaption: $('review-player-caption'),
    reviewRender: $('review-render'), reviewError: $('review-error'),
    reviewSelectionCount: $('review-selection-count'),
  };
  const configured = (window.DARKCUT_CONFIG?.workerUrl || '').trim().replace(/\/$/, '');
  let worker = configured || (localStorage.getItem('darkcut_worker') || '').trim().replace(/\/$/, '');
  let currentFile = null;
  let localPreviewUrl = '';
  let currentJob = null;
  let maxUploadBytes = 500 * 1048576;
  let timer = null;
  let reviewBounds = null;
  let reviewShownFor = null;
  const resumedJobs = new Set();
  const connectionFailures = new Map();
  const view = {};
  const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clock = (seconds) => {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  };
  const sizeText = (n) => `${(n / 1048576).toLocaleString('pt-BR', {maximumFractionDigits: 1})} MB`;
  const setError = (text) => { els.error.hidden = !text; els.error.textContent = text || ''; };
  const fileUrl = (path) => worker && /^\/api\/jobs\/[a-f0-9]{32}\/files\/[\w.-]+$/.test(path || '') ? worker + path : null;
  const isWorkerUrl = (value) => {
    try {
      const u = new URL(value);
      return u.protocol === 'https:' || (u.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(u.hostname));
    } catch (_) { return false; }
  };
  function busy(isBusy) {
    els.button.disabled = isBusy;
    els.file.disabled = isBusy;
    els.buttonLabel.textContent = isBusy ? 'Preparando os seus cortes…' : 'Analisar vídeo e revisar cortes';
  }
  function showProgress(value, message) {
    const pct = Math.max(0, Math.min(100, Number(value) || 0));
    els.progress.hidden = false;
    els.percent.textContent = `${pct}%`;
    els.fill.style.width = `${pct}%`;
    els.stage.textContent = message || 'Processando…';
  }
  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(worker + path, {
        ...options,
        signal: options.signal || AbortSignal.timeout(30000),
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      });
    } catch (_) {
      throw new Error('Não foi possível comunicar com o motor. Confira a conexão e se o servidor Render está ativo.');
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('O servidor retornou uma resposta inválida.'); }
    if (!response.ok) {
      throw new Error(typeof data.detail === 'string' ? data.detail : `Erro do servidor (HTTP ${response.status}).`);
    }
    return data;
  }
  function showSelectedFile(file) {
    currentFile = file || null;
    if (localPreviewUrl) URL.revokeObjectURL(localPreviewUrl);
    localPreviewUrl = '';
    els.originalPreview.removeAttribute('src');
    els.originalPreview.load();
    els.filePreview.hidden = true;
    els.previewData.textContent = '';
    if (!file) {
      els.fileDetails.textContent = `Limite: ${sizeText(maxUploadBytes)}. Use conteúdo que você tem autorização para editar.`;
      return;
    }
    els.fileDetails.textContent = `${file.name} · ${sizeText(file.size)} · limite de ${sizeText(maxUploadBytes)}`;
    if (!file.name.toLowerCase().endsWith('.mp4')) {
      setError('O arquivo precisa estar no formato MP4.');
      return;
    }
    if (file.size > maxUploadBytes) {
      setError(`Arquivo acima do limite atual (${sizeText(maxUploadBytes)}). Comprima o vídeo ou use um arquivo menor.`);
      return;
    }
    setError('');
    els.previewName.textContent = file.name;
    els.previewData.textContent = `${sizeText(file.size)} · carregando duração…`;
    localPreviewUrl = URL.createObjectURL(file);
    els.originalPreview.src = localPreviewUrl;
    els.filePreview.hidden = false;
  }
  els.file.addEventListener('change', () => showSelectedFile(els.file.files?.[0]));
  for (const name of ['dragenter', 'dragover']) {
    els.drop.addEventListener(name, (event) => { event.preventDefault(); els.drop.classList.add('drag-over'); });
  }
  for (const name of ['dragleave', 'drop']) {
    els.drop.addEventListener(name, (event) => { event.preventDefault(); els.drop.classList.remove('drag-over'); });
  }
  els.drop.addEventListener('drop', (event) => {
    if (els.file.disabled) return;
    showSelectedFile(event.dataTransfer?.files?.[0]);
  });
  els.originalPreview.addEventListener('loadedmetadata', () => {
    if (currentFile) {
      const duration = Number.isFinite(els.originalPreview.duration) ? ` · ${clock(els.originalPreview.duration)} de vídeo` : '';
      els.previewData.textContent = sizeText(currentFile.size) + duration;
    }
  });

  async function sendVideoInChunks(file, settings) {
    const upload = await api('/api/uploads', {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, size: file.size, ...settings }),
    });
    const token = upload.upload_token;
    const chunkBytes = upload.chunk_bytes || 4 * 1048576;
    try {
      let uploaded = 0;
      for (let i = 0; uploaded < file.size; i++) {
        const part = file.slice(uploaded, Math.min(uploaded + chunkBytes, file.size));
        let response;
        try {
          response = await fetch(`${worker}/api/uploads/${upload.id}/chunks/${i}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/octet-stream', 'X-Upload-Token': token },
            body: part,
            signal: AbortSignal.timeout(120000),
          });
        } catch (_) {
          throw new Error('Conexão interrompida durante o upload. Confira a internet e reenvie o arquivo.');
        }
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(typeof detail.detail === 'string' ? detail.detail : `Erro ao enviar parte ${i + 1} (HTTP ${response.status}).`);
        }
        const info = await response.json();
        uploaded = info.received;
        showProgress(2 + Math.round(uploaded / file.size * 20), `Enviando o vídeo — ${Math.round(uploaded / file.size * 100)}%`);
      }
      showProgress(23, 'Upload concluído. Entregando o vídeo ao motor de cortes…');
      return await api(`/api/uploads/${upload.id}/finish`, {
        method: 'POST', headers: { 'X-Upload-Token': token },
      });
    } catch (error) {
      try {
        await api(`/api/uploads/${upload.id}/cancel`, {
          method: 'POST', headers: { 'X-Upload-Token': token }, signal: AbortSignal.timeout(5000),
        });
      } catch (_) { /* server will clean abandoned uploads */ }
      throw error;
    }
  }

  async function start(event) {
    event.preventDefault();
    if (!isWorkerUrl(worker)) {
      setError('Configure uma URL válida do motor no menu Configuração.');
      $('setup').scrollIntoView({ behavior: 'smooth' });
      return;
    }
    if (!currentFile) { setError('Selecione o arquivo MP4 completo para começar.'); return; }
    if (!currentFile.name.toLowerCase().endsWith('.mp4')) { setError('Envie um vídeo no formato MP4.'); return; }
    if (currentFile.size < 12 || currentFile.size > maxUploadBytes) {
      setError(`Escolha um vídeo de até ${sizeText(maxUploadBytes)}.`); return;
    }
    if (timer) clearTimeout(timer);
    setError('');
    els.review.hidden = true;
    els.reviewList.replaceChildren();
    reviewShownFor = null;
    reviewBounds = null;
    busy(true);
    els.list.replaceChildren();
    els.empty.hidden = true;
    els.downloadAll.hidden = true;
    els.resultCount.textContent = '';
    showProgress(2, 'Criando o projeto e preparando o envio…');
    const shortParts = els.shortsLength.value.split(',').map(Number);
    const normalParts = els.normalLength.value.split(',').map(Number);
    const shortsCount = Number(els.shortsCount.value);
    const normalCount = Number(els.normalCount.value);
    if (!shortsCount && !normalCount) { setError('Escolha ao menos um Short ou corte tradicional.'); busy(false); return; }
    try {
      const result = await sendVideoInChunks(currentFile, {
        count: shortsCount || 1, min_seconds: shortParts[0], max_seconds: shortParts[1],
        review_before_render: true, shorts_count: shortsCount, traditional_count: normalCount,
        shorts_min_seconds: shortParts[0], shorts_max_seconds: shortParts[1],
        traditional_min_seconds: normalParts[0], traditional_max_seconds: normalParts[1],
        subtitles: els.subtitles.checked,
      });
      currentJob = result.id;
      await poll(result.id);
    } catch (error) {
      showProgress(0, 'Falha ao iniciar o projeto.');
      setError(error.message);
      busy(false);
    }
  }

  async function poll(jobId) {
    if (currentJob !== jobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      connectionFailures.delete(jobId);
      if (currentJob !== jobId) return;
      showProgress(job.progress, job.message);
      if (job.status === 'review') {
        busy(false);
        showProgress(50, 'Análise concluída. Confira os cortes sugeridos abaixo.');
        displayReview(job);
        return;
      }
      if (job.clips?.length) renderResults(job);
      if (job.status === 'completed') {
        els.review.hidden = true;
        busy(false);
        showProgress(100, 'Tudo pronto! Baixe os formatos e copie os textos de publicação.');
        els.resultDesc.textContent = 'Para cada corte: Shorts, vídeo horizontal, título, descrição, legenda e hashtags.';
        const bundle = fileUrl(job.bundle_url);
        els.downloadAll.hidden = !bundle;
        els.downloadAll.onclick = () => bundle && window.open(bundle, '_blank', 'noopener,noreferrer');
        $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      if (job.status === 'interrupted') {
        if (job.can_resume && !resumedJobs.has(jobId)) {
          resumedJobs.add(jobId);
          showProgress(job.progress || 24, 'Recuperando o processamento após reinicialização do servidor…');
          await api(`/api/jobs/${encodeURIComponent(jobId)}/resume`, { method: 'POST' });
          timer = setTimeout(() => poll(jobId), 4000);
          return;
        }
        busy(false);
        setError(job.message || 'O trabalho foi interrompido pelo servidor.');
        return;
      }
      if (job.status === 'failed') {
        busy(false);
        if (job.phase === 'render' && job.suggestions?.length && reviewShownFor === jobId) {
          els.review.hidden = false;
          els.reviewRender.disabled = false;
          updateReviewSelection();
          els.reviewError.hidden = false;
          els.reviewError.textContent = `A renderização falhou: ${job.message || 'erro desconhecido'}. Você pode ajustar a seleção e tentar novamente.`;
          els.stage.textContent = 'Revise a seleção e tente renderizar novamente.';
          return;
        }
        setError(job.message || 'O processamento do vídeo falhou.');
        els.stage.textContent = 'Não foi possível finalizar este vídeo.';
        return;
      }
      timer = setTimeout(() => poll(jobId), 3000);
    } catch (error) {
      const n = (connectionFailures.get(jobId) || 0) + 1;
      connectionFailures.set(jobId, n);
      if (n <= 12 && currentJob === jobId) {
        els.stage.textContent = `Servidor indisponível, tentando reconectar (${n}/12)…`;
        timer = setTimeout(() => poll(jobId), 5000);
        return;
      }
      busy(false);
      setError(error.message + ' O servidor pode ter reiniciado por falta de memória. ' +
        'Sem armazenamento externo configurado, os trabalhos em andamento não podem ser recuperados.');
      els.stage.textContent = 'Conexão com o motor interrompida';
    }
  }

  function updateReviewSelection() {
    const selected = [...els.reviewList.querySelectorAll('.review-select:checked')].length;
    const all = els.reviewList.querySelectorAll('.review-select').length;
    els.reviewSelectionCount.textContent = `${selected} de ${all} sugestões selecionadas`;
    els.reviewRender.disabled = selected < 1;
  }

  function displayReview(job) {
    if (reviewShownFor === job.id) return;
    reviewShownFor = job.id;
    els.review.hidden = false;
    els.reviewList.replaceChildren();
    els.reviewError.hidden = true;
    els.reviewPlayer.removeAttribute('src');
    if (localPreviewUrl) els.reviewPlayer.src = localPreviewUrl;
    els.reviewSummary.textContent = `${job.suggestions.length} sugestões avaliadas em dois perfis. Ajuste o início, o fim e o título; desmarque o que não deseja exportar.`;
    if (job.omitted_profiles?.length) {
      els.reviewSummary.textContent += ` Sem trechos adequados para: ${job.omitted_profiles.join(', ')}.`;
    }
    const labels = {hook:'Gancho', clarity:'Clareza', impact:'Impacto', context:'Contexto', closure:'Conclusão'};
    for (const proposal of job.suggestions) {
      const article = document.createElement('article');
      article.className = 'review-card';
      article.dataset.id = proposal.id;
      const isShort = proposal.profile === 'shorts';
      const score = proposal.scores || {};
      const title = proposal.title || 'Destaque do vídeo';
      const metrics = Object.entries(labels).map(([key,label]) => `<div class="review-metric"><span>${label}</span><span>${Number(score[key] || 0)}/100</span><div class="review-track"><div style="width:${Math.min(100, Math.max(0, Number(score[key]) || 0))}%"></div></div></div>`).join('');
      article.innerHTML = `
        <div class="review-card-head"><label class="review-check"><input class="review-select" type="checkbox" checked><span>${isShort ? '▯ SHORTS 9:16' : '▭ TRADICIONAL 16:9'} · ${escapeHtml(proposal.id)}</span></label><span class="review-score">${Number(score.overall || 0)}<small>/100</small></span></div>
        <div class="review-metrics">${metrics}</div>
        <label class="field-label">TÍTULO SUGERIDO<input class="review-title-input" type="text" maxlength="100" value="${escapeHtml(title)}" aria-label="Título do corte ${escapeHtml(proposal.id)}"></label>
        <div class="review-time-fields"><label class="field-label">INÍCIO (SEG.)<input class="review-start-input" type="number" step="0.1" min="0" max="${Number(job.source_duration)}" value="${Number(proposal.start)}"></label><label class="field-label">FIM (SEG.)<input class="review-end-input" type="number" step="0.1" min="0" max="${Number(job.source_duration)}" value="${Number(proposal.end)}"></label></div>
        <p class="review-transcript">${escapeHtml((proposal.transcript || '').slice(0,360))}</p>
        <button type="button" class="review-preview-btn">▶ Assistir trecho (${clock(proposal.start)}–${clock(proposal.end)})</button>`;
      article.querySelector('.review-select').addEventListener('change', updateReviewSelection);
      article.querySelector('.review-preview-btn').addEventListener('click', () => {
        if (!localPreviewUrl) {
          els.reviewPlayerCaption.textContent = 'Para assistir, mantenha o arquivo MP4 selecionado no aparelho.';
          return;
        }
        const start = Number(article.querySelector('.review-start-input').value);
        const end = Number(article.querySelector('.review-end-input').value);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return;
        reviewBounds = {start, end};
        els.reviewPlayer.currentTime = start;
        els.reviewPlayerCaption.textContent = `${isShort ? 'Short' : 'Tradicional'} ${proposal.id} · ${clock(start)} até ${clock(end)}`;
        els.reviewPlayer.play().catch(() => {});
        els.reviewPlayer.scrollIntoView({behavior:'smooth', block:'nearest'});
      });
      els.reviewList.append(article);
    }
    updateReviewSelection();
    els.review.scrollIntoView({behavior:'smooth', block:'start'});
  }

  els.reviewPlayer.addEventListener('timeupdate', () => {
    if (reviewBounds && els.reviewPlayer.currentTime >= reviewBounds.end) {
      els.reviewPlayer.pause();
      reviewBounds = null;
    }
  });

  async function renderApproved() {
    if (!currentJob) return;
    const choices = [];
    for (const card of els.reviewList.querySelectorAll('.review-card')) {
      if (!card.querySelector('.review-select').checked) continue;
      const start = Number(card.querySelector('.review-start-input').value);
      const end = Number(card.querySelector('.review-end-input').value);
      const title = card.querySelector('.review-title-input').value.trim();
      const minLength = card.dataset.id.startsWith('S') ? 8 : 30;
      const maxLength = card.dataset.id.startsWith('S') ? 180 : 600;
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start ||
          end - start < minLength || end - start > maxLength || !title) {
        els.reviewError.hidden = false;
        els.reviewError.textContent = `Revise os horários e o título de ${card.dataset.id}. Duração permitida: ${minLength}–${maxLength} segundos.`;
        return;
      }
      choices.push({id:card.dataset.id, start, end, title});
    }
    if (!choices.length) return;
    els.reviewError.hidden = true;
    els.reviewRender.disabled = true;
    els.reviewRender.textContent = 'Iniciando renderização…';
    try {
      await api(`/api/jobs/${encodeURIComponent(currentJob)}/render`, {
        method:'POST', body:JSON.stringify({clips:choices}),
      });
      els.review.hidden = true;
      showProgress(51, 'Renderizando somente os trechos aprovados…');
      await poll(currentJob);
    } catch (error) {
      els.review.hidden = false;
      els.reviewError.hidden = false;
      els.reviewError.textContent = error.message;
      els.reviewRender.disabled = false;
    } finally {
      els.reviewRender.textContent = '✦ Renderizar cortes aprovados ↗';
    }
  }
  els.reviewRender.addEventListener('click', renderApproved);

  async function copyText(value) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
    const input = document.createElement('textarea');
    input.value = value;
    input.style.position = 'fixed'; input.style.opacity = '0';
    document.body.append(input); input.select();
    const ok = document.execCommand('copy'); input.remove();
    if (!ok) throw new Error('Não foi possível copiar');
  }

  function renderResults(job) {
    els.empty.hidden = true;
    els.resultCount.textContent = `(${job.clips.length})`;
    const existing = new Map([...els.list.querySelectorAll('[data-clip-id]')].map(node => [Number(node.dataset.clipId), node]));
    for (const clip of job.clips) {
      if (existing.has(clip.id)) continue;
      const vertical = fileUrl(clip.files?.vertical);
      const horizontal = fileUrl(clip.files?.horizontal);
      view[clip.id] = { format: vertical ? 'vertical' : 'horizontal', tab: 'title' };
      const description = clip.description || clip.summary || '';
      const caption = clip.caption || description;
      const tags = (Array.isArray(clip.hashtags) ? clip.hashtags : []).join(' ');
      const values = { title: clip.title || '', description, caption, hashtags: tags };
      const labels = { title: 'título', description: 'descrição', caption: 'legenda', hashtags: 'hashtags' };
      const allText = `TÍTULO\n${values.title}\n\nDESCRIÇÃO\n${description}\n\nLEGENDA PARA PUBLICAR\n${caption}\n\nHASHTAGS\n${tags}`;
      const article = document.createElement('article');
      article.className = 'clip-card'; article.dataset.clipId = clip.id;
      article.innerHTML = `
        <div class="clip-upper">
          <div class="clip-preview"><div class="video-slot">${vertical || horizontal ? `<video controls playsinline preload="metadata" src="${escapeHtml(vertical || horizontal)}"></video>` : 'Vídeo indisponível'}</div>
            <div class="format-picker">${vertical ? `<button type="button" data-format="vertical" class="selected">▯ Shorts 9:16</button>` : ''}${horizontal ? `<button type="button" data-format="horizontal" class="${vertical ? '' : 'selected'}">▭ Normal 16:9</button>` : ''}</div>
          </div>
          <div class="clip-content">
            <div class="clip-meta-row"><span class="clip-index">✦ ${clip.profile === 'shorts' ? 'SHORT' : clip.profile === 'traditional' ? 'CORTE NORMAL' : 'CORTE'} ${String(clip.id).padStart(2, '0')}</span><span class="clip-time">${clock(clip.start)} – ${clock(clip.end)} · ${Math.round(clip.duration)}s</span></div>
            ${clip.scores ? `<div class="origin-label">Avaliação editorial: ${Number(clip.scores.overall || 0)}/100 · Indicativa, não prevê viralização</div>` : ''}
            <h3>${escapeHtml(values.title)}</h3>
            <div class="origin-label">${clip.metadata_source === 'ia' ? 'Textos gerados por IA' : 'Sugestões baseadas na transcrição'} · Revise antes de publicar</div>
            <div class="clip-tabs" role="tablist" aria-label="Textos para o corte ${clip.id}">
              <button type="button" class="active" role="tab" aria-selected="true" data-tab="title">Título</button>
              <button type="button" role="tab" aria-selected="false" data-tab="description">Descrição</button>
              <button type="button" role="tab" aria-selected="false" data-tab="caption">Legenda p/ publicar</button>
              <button type="button" role="tab" aria-selected="false" data-tab="hashtags"># Hashtags</button>
            </div>
            <div class="tab-body" role="tabpanel">${escapeHtml(values.title)}</div>
            <div class="publication-tools"><button type="button" class="copy-btn">▢ Copiar título</button><button type="button" class="copy-all">▢ Copiar tudo</button></div>
          </div>
        </div>
        <div class="clip-downloads">
          ${vertical ? `<a class="download-chip primary" href="${escapeHtml(vertical)}" target="_blank" rel="noopener noreferrer">⇩ Shorts 9:16</a>` : ''}
          ${horizontal ? `<a class="download-chip" href="${escapeHtml(horizontal)}" target="_blank" rel="noopener noreferrer">⇩ Corte normal 16:9</a>` : ''}
          <small>MP4 · Publicação manual</small>
        </div>`;
      const slot = article.querySelector('.video-slot');
      article.querySelectorAll('[data-format]').forEach(btn => btn.addEventListener('click', () => {
        const format = btn.dataset.format;
        const path = fileUrl(clip.files?.[format]);
        if (!path) return;
        view[clip.id].format = format;
        article.querySelectorAll('[data-format]').forEach(b => b.classList.toggle('selected', b === btn));
        slot.innerHTML = `<video controls playsinline preload="metadata" src="${escapeHtml(path)}"></video>`;
      }));
      const body = article.querySelector('.tab-body');
      const copyBtn = article.querySelector('.copy-btn');
      article.querySelectorAll('[data-tab]').forEach(btn => btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        view[clip.id].tab = tab;
        article.querySelectorAll('[data-tab]').forEach(b => {
          b.classList.toggle('active', b === btn);
          b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        });
        body.textContent = values[tab] || '';
        body.classList.toggle('hashtag-content', tab === 'hashtags');
        copyBtn.textContent = `▢ Copiar ${labels[tab]}`;
      }));
      copyBtn.addEventListener('click', async () => {
        const tab = view[clip.id].tab;
        try { await copyText(values[tab] || ''); copyBtn.textContent = '✓ Copiado!'; }
        catch (_) { copyBtn.textContent = 'Falha ao copiar'; }
      });
      article.querySelector('.copy-all').addEventListener('click', async (event) => {
        try { await copyText(allText); event.currentTarget.textContent = '✓ Tudo copiado!'; }
        catch (_) { event.currentTarget.textContent = 'Falha ao copiar'; }
      });
      els.list.appendChild(article);
    }
  }

  function updateNav() {
    const target = location.hash.slice(1) || 'studio';
    document.querySelectorAll('[data-nav]').forEach(el => el.classList.toggle('active', el.dataset.nav === target));
  }
  async function checkWorker() {
    const status = await api('/api/health');
    if (!status.upload_only || !status.review_supported) throw new Error('Atualize o motor Render para a versão 3.2 antes de usar a revisão dos cortes.');
    maxUploadBytes = Number(status.max_upload_bytes) || maxUploadBytes;
    if (currentFile) showSelectedFile(currentFile);
    else showSelectedFile(null);
    els.workerFeedback.textContent = `Motor conectado (v${status.version}). Revisão Precision Cuts habilitada. Textos: ${status.ai_metadata_enabled ? 'IA conectada' : 'sugestões baseadas na transcrição (sem API de IA)'}.`;
  }
  els.saveWorker.addEventListener('click', async () => {
    const value = els.workerUrl.value.trim().replace(/\/$/, '');
    if (!isWorkerUrl(value)) { els.workerFeedback.textContent = 'Informe uma URL HTTPS pública válida.'; return; }
    worker = value;
    localStorage.setItem('darkcut_worker', worker);
    els.workerFeedback.textContent = 'Endereço salvo. Verificando o motor…';
    try { await checkWorker(); }
    catch (error) { els.workerFeedback.textContent = `Conexão não confirmada: ${error.message}`; }
  });
  els.workerUrl.value = worker;
  if (isWorkerUrl(worker)) checkWorker().catch(() => {
    els.workerFeedback.textContent = 'O motor pode estar iniciando. Use Salvar conexão para verificar novamente.';
  });
  els.form.addEventListener('submit', start);
  window.addEventListener('hashchange', updateNav);
  updateNav();
})();