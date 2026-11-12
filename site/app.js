(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const els = {
    form: $('create-form'), url: $('video-url'), count: $('clip-count'), length: $('clip-length'),
    subtitles: $('include-subtitles'), error: $('form-error'), button: $('generate-btn'),
    buttonLabel: $('generate-label'), progress: $('progress-panel'), stage: $('job-stage'),
    percent: $('progress-number'), fill: $('progress-fill'), empty: $('results-empty'),
    list: $('clip-list'), resultCount: $('results-count'), resultDesc: $('results-desc'),
    downloadAll: $('download-all'), workerUrl: $('worker-url'), saveWorker: $('save-worker'),
    workerFeedback: $('worker-feedback')
  };
  const initialUrl = (window.DARKCUT_CONFIG?.workerUrl || '').trim().replace(/\/$/, '');
  let worker = initialUrl || (localStorage.getItem('darkcut_worker') || '').trim().replace(/\/$/, '');
  let currentJob = null;
  let timer = null;
  let view = {};

  // Escape all text interpolated into HTML; video links always come from a server-generated path.
  const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clock = (secs) => {
    const s = Math.max(0, Math.floor(Number(secs) || 0));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  };
  const setMessage = (s) => { els.error.hidden = !s; els.error.textContent = s || ''; };
  const urlFor = (path) => {
    if (!worker || !/^\/api\/jobs\/[a-f0-9]{32}\/files\/[\w.-]+$/.test(path || '')) return null;
    return worker + path;
  };
  function markBusy(busy) {
    els.button.disabled = busy;
    els.buttonLabel.textContent = busy ? 'Processando seu vídeo…' : 'Gerar cortes com IA';
  }
  function showProgress(pct, message) {
    els.progress.hidden = false;
    els.percent.textContent = `${pct || 0}%`;
    els.fill.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
    els.stage.textContent = message || 'Processando…';
  }
  async function api(route, options = {}) {
    const timeout = AbortSignal.timeout(25000);
    let response;
    try {
      response = await fetch(worker + route, { ...options, signal: timeout, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    } catch (_) {
      throw new Error('Não foi possível conectar ao worker. Confira o endereço e se ele está ativo.');
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('O worker retornou uma resposta inválida.'); }
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || 'Erro de processamento');
      throw new Error(detail);
    }
    return data;
  }
  async function start(event) {
    event.preventDefault();
    if (!worker || !isWorkerUrl(worker)) { setMessage('Configure primeiro uma URL HTTPS do worker (ou localhost para testes).'); $('setup').scrollIntoView({ behavior:'smooth' }); return; }
    setMessage('');
    if (timer) clearTimeout(timer);
    const parts = els.length.value.split(',').map(Number);
    const payload = { url: els.url.value.trim(), count: Number(els.count.value), min_seconds:parts[0], max_seconds:parts[1], subtitles:els.subtitles.checked };
    markBusy(true);
    showProgress(2, 'Enviando projeto ao servidor…');
    els.list.replaceChildren();
    els.empty.hidden = true;
    els.downloadAll.hidden = true;
    els.resultCount.textContent = '';
    try {
      const job = await api('/api/jobs', { method:'POST', body:JSON.stringify(payload) });
      currentJob = job.id;
      await poll(job.id);
    } catch (e) {
      showProgress(0, 'Não foi possível iniciar o projeto.');
      setMessage(e.message);
      markBusy(false);
    }
  }
  async function poll(jobId) {
    if (currentJob !== jobId) return;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (currentJob !== jobId) return;
      showProgress(job.progress, job.message);
      if (job.clips?.length) renderResults(job);
      if (job.status === 'completed') {
        markBusy(false);
        showProgress(100, 'Tudo pronto. Escolha seus formatos e copie os textos.');
        els.resultDesc.textContent = 'Confira cada corte, copie os metadados e baixe os dois formatos.';
        const bundle = urlFor(job.bundle_url);
        els.downloadAll.hidden = !bundle;
        els.downloadAll.onclick = () => bundle && window.open(bundle, '_blank', 'noopener,noreferrer');
        $('results').scrollIntoView({ behavior:'smooth', block:'start' });
        return;
      }
      if (job.status === 'failed') {
        markBusy(false);
        setMessage(job.message || 'O processamento falhou.');
        els.stage.textContent = 'Não foi possível finalizar este projeto.';
        return;
      }
      timer = setTimeout(() => poll(jobId), 2700);
    } catch (e) {
      markBusy(false);
      setMessage(e.message + ' A tarefa pode continuar no servidor; verifique novamente antes de reiniciar.');
      els.stage.textContent = 'Conexão interrompida';
    }
  }
  function renderResults(job) {
    els.empty.hidden = true;
    els.resultCount.textContent = `(${job.clips.length})`;
    const byId = new Map([...els.list.querySelectorAll('[data-clip-id]')].map((el) => [Number(el.dataset.clipId), el]));
    for (const clip of job.clips) {
      if (byId.has(clip.id)) continue;
      view[clip.id] = { format:'vertical', tab:'title' };
      const item = document.createElement('article');
      item.className = 'clip-card'; item.dataset.clipId = clip.id;
      const aiLabel = clip.metadata_source === 'ia' ? 'Gerado por IA' : 'Sugestão baseada na transcrição';
      const vertical = urlFor(clip.files?.vertical);
      const horizontal = urlFor(clip.files?.horizontal);
      item.innerHTML = `
        <div class="clip-upper">
          <div class="clip-preview"><div class="video-slot">${vertical ? `<video controls playsinline preload="metadata" src="${escapeHtml(vertical)}"></video>` : `<div class="preview-placeholder">Vídeo indisponível</div>`}</div>
            <div class="format-picker"><button class="selected" type="button" data-format="vertical">▯ Vertical 9:16</button><button type="button" data-format="horizontal">▭ Horizontal 16:9</button></div>
          </div>
          <div class="clip-content">
            <div class="clip-meta-row"><span class="clip-index">✦ CORTE ${String(clip.id).padStart(2,'0')}</span><span class="clip-time">${clock(clip.start)} – ${clock(clip.end)} · ${Math.round(clip.duration)}s</span></div>
            <h3>${escapeHtml(clip.title)}</h3><div class="origin-label">${escapeHtml(aiLabel)} · Preparado para compartilhar</div>
            <div class="clip-tabs" role="tablist" aria-label="Informações do corte ${clip.id}">
              <button role="tab" aria-selected="true" type="button" class="active" data-tab="title">Título</button>
              <button role="tab" aria-selected="false" type="button" data-tab="hashtags"># Hashtags</button>
              <button role="tab" aria-selected="false" type="button" data-tab="summary">Resumo</button>
            </div>
            <div class="tab-body" role="tabpanel">${escapeHtml(clip.title)}</div>
            <div class="clipboard-area"><button type="button" class="copy-btn">▢ Copiar título</button></div>
          </div>
        </div>
        <div class="clip-downloads">${vertical ? `<a class="download-chip primary" href="${escapeHtml(vertical)}" target="_blank" rel="noopener noreferrer">⇩ Baixar vertical</a>` : ''}${horizontal ? `<a class="download-chip" href="${escapeHtml(horizontal)}" target="_blank" rel="noopener noreferrer">⇩ Baixar horizontal</a>` : ''}<small>MP4 • Sem publicação automática</small></div>`;
      const video = item.querySelector('.video-slot');
      item.querySelectorAll('[data-format]').forEach((btn) => btn.addEventListener('click', () => {
        const format = btn.dataset.format;
        const src = urlFor(clip.files?.[format]);
        if (!src) return;
        view[clip.id].format = format;
        item.querySelectorAll('[data-format]').forEach(b => b.classList.toggle('selected', b === btn));
        video.innerHTML = `<video controls playsinline preload="metadata" src="${escapeHtml(src)}"></video>`;
      }));
      const body = item.querySelector('.tab-body');
      const copy = item.querySelector('.copy-btn');
      const tags = (Array.isArray(clip.hashtags) ? clip.hashtags : []).join(' ');
      const values = {title:clip.title,hashtags:tags,summary:clip.summary};
      const labels = {title:'título',hashtags:'hashtags',summary:'resumo'};
      item.querySelectorAll('[data-tab]').forEach((btn) => btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        view[clip.id].tab = tab;
        item.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b === btn); b.setAttribute('aria-selected', b === btn ? 'true' : 'false'); });
        body.textContent = values[tab] || '';
        body.classList.toggle('hashtag-content', tab === 'hashtags');
        copy.textContent = `▢ Copiar ${labels[tab]}`;
      }));
      copy.addEventListener('click', async () => {
        const tab = view[clip.id].tab;
        try {
          if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(values[tab] || '');
          else { const x = document.createElement('textarea'); x.value = values[tab] || ''; x.style.position = 'fixed'; x.style.opacity = 0; document.body.append(x); x.select(); if (!document.execCommand('copy')) throw new Error('copy'); x.remove(); }
          copy.textContent = '✓ Copiado!';
          setTimeout(() => { if (view[clip.id].tab === tab) copy.textContent = `▢ Copiar ${labels[tab]}`; }, 1600);
        } catch (_) { copy.textContent = 'Não foi possível copiar'; }
      });
      els.list.appendChild(item);
    }
  }
  function isWorkerUrl(value) {
    try { const u = new URL(value); return u.protocol === 'https:' || (u.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(u.hostname)); } catch (_) { return false; }
  }
  function updateNav() {
    const target = location.hash.slice(1) || 'studio';
    document.querySelectorAll('[data-nav]').forEach(el => el.classList.toggle('active', el.dataset.nav === target));
  }
  $('save-worker').addEventListener('click', async () => {
    const value = els.workerUrl.value.trim().replace(/\/$/, '');
    if (!isWorkerUrl(value)) { els.workerFeedback.textContent = 'Informe uma URL HTTPS pública ou http://localhost para testes.'; return; }
    worker = value;
    localStorage.setItem('darkcut_worker', worker);
    els.workerFeedback.textContent = 'Endereço salvo neste navegador. Testando conexão…';
    try {
      const health = await api('/api/health');
      els.workerFeedback.textContent = `Worker conectado (v${health.version}). Títulos e resumos por IA: ${health.ai_metadata_enabled ? 'ativados' : 'não configurados; disponíveis sugestões extrativas'}.`;
    } catch (e) { els.workerFeedback.textContent = `Endereço salvo, mas a conexão falhou: ${e.message}`; }
  });
  els.workerUrl.value = worker;
  els.form.addEventListener('submit', start);
  window.addEventListener('hashchange', updateNav);
  updateNav();
})();
