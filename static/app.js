const $ = (selector) => document.querySelector(selector);
const form = $('#analyze-form');
const urlInput = $('#url');
const analyzeButton = $('#analyze-button');
const downloadButton = $('#download-button');
let analysis = null;
let refreshing = false;
let refreshTimer;
let failures = 0;
const pendingActions = new Set();
const cards = new Map();
const labels = { queued: 'Sırada', processing: 'Hazırlanıyor', paused: 'Duraklatıldı', complete: 'Hazır', error: 'Tamamlanamadı', cancelled: 'İptal edildi' };

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(options.method === 'POST' && path === '/api/analyze' ? 100000 : 20000) });
  } catch (error) {
    throw new Error(error.name === 'TimeoutError' ? 'İşlem zamanında yanıt vermedi. Listeyi yenileyip kontrol edebilirsin.' : 'Sunucuya ulaşılamıyor. Bağlantını ve uygulamanın çalıştığını kontrol et.');
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'İşlem tamamlanamadı. Bilgileri kontrol edip yeniden dene.');
    error.status = response.status;
    throw error;
  }
  return data;
}

function message(selector, text) {
  $(selector).textContent = text;
  $(selector).hidden = !text;
}

function formatBytes(bytes) {
  return bytes ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : '';
}

function busy(value) {
  analyzeButton.disabled = value;
  urlInput.disabled = value;
  $('#example-button').disabled = value;
  form.setAttribute('aria-busy', String(value));
}

function clearResult() {
  analysis = null;
  $('#result').hidden = true;
  $('#added-notice').hidden = true;
  message('#error', '');
}

urlInput.addEventListener('input', clearResult);
$('#example-button').addEventListener('click', () => {
  urlInput.value = 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4';
  form.requestSubmit();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  clearResult();
  busy(true);
  analyzeButton.textContent = 'Kaynak aranıyor…';
  try {
    analysis = await api('/api/analyze', { method: 'POST', body: JSON.stringify({ url: urlInput.value.trim() }) });
    $('#video-title').textContent = analysis.title;
    const meta = [analysis.source];
    if (analysis.route === 'proton') meta.push('Proton VPN ile bulundu');
    if (analysis.duration) {
      const seconds = Math.round(analysis.duration);
      meta.push(`${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`);
    }
    if (analysis.size) meta.push(`Yaklaşık ${formatBytes(analysis.size)}`);
    $('#video-meta').textContent = meta.join(' · ');
    $('#quality').replaceChildren(new Option('En iyi kalite', ''));
    for (const height of analysis.qualities) $('#quality').add(new Option(`${height}p`, String(height)));
    $('#result').hidden = false;
    downloadButton.disabled = false;
  } catch (error) {
    message('#error', error.message);
  } finally {
    busy(false);
    analyzeButton.textContent = 'Videoyu bul ↗';
  }
});

downloadButton.addEventListener('click', async () => {
  if (!analysis) return;
  message('#error', '');
  busy(true);
  downloadButton.disabled = true;
  $('#quality').disabled = true;
  try {
    const height = $('#quality').value;
    await api('/api/downloads', { method: 'POST', body: JSON.stringify({ analysis_id: analysis.id, height: height ? Number(height) : null }) });
    $('#added-notice').hidden = false;
    await refreshJobs();
  } catch (error) {
    message('#error', error.message);
  } finally {
    busy(false);
    downloadButton.disabled = false;
    $('#quality').disabled = false;
  }
});

function actionButton(card, action, title, emphasized = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = title;
  button.className = emphasized ? 'primary queue-action' : 'text-button queue-action';
  button.dataset.action = action;
  button.setAttribute('aria-label', `${title}: ${card.querySelector('h3').textContent}`);
  card.querySelector('.job-actions').append(button);
}

function renderJob(job) {
  let card = cards.get(job.id);
  if (!card) {
    card = document.createElement('article');
    card.className = 'job-card';
    card.dataset.id = job.id;
    // All dynamic source text is assigned with textContent, never parsed as HTML.
    card.innerHTML = '<div class="job-top"><span class="job-type" aria-hidden="true">MP4</span><h3></h3><span class="job-status"></span></div><p class="job-meta"></p><p class="job-message"></p><progress max="100" aria-label="Dosya hazırlama ilerlemesi"></progress><div class="job-bottom"><span class="job-expiry"></span><div class="job-actions"></div></div>';
    cards.set(job.id, card);
  }
  card.dataset.status = job.status;
  card.querySelector('h3').textContent = job.title;
  card.querySelector('.job-status').textContent = job.status === 'queued' ? `${labels.queued} · ${job.queue_position}` : labels[job.status];
  card.querySelector('.job-meta').textContent = [job.height ? `${job.height}p'ye kadar` : 'En iyi kalite', formatBytes(job.size), job.route === 'proton' ? 'Proton VPN' : ''].filter(Boolean).join(' · ');
  card.querySelector('.job-message').textContent = job.message;
  const progress = card.querySelector('progress');
  progress.hidden = job.status !== 'processing';
  if (job.percent == null) progress.removeAttribute('value');
  else progress.value = job.percent;
  card.querySelector('.job-expiry').textContent = job.expires_at ? `Temizlenme: ${new Date(job.expires_at * 1000).toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' })}` : 'İşlem sürerken dosyalar saklanır.';

  const signature = `${job.status}:${job.retryable}`;
  if (card.dataset.actions !== signature) {
    const focusedAction = card.contains(document.activeElement) ? document.activeElement.dataset.action : null;
    card.querySelector('.job-actions').replaceChildren();
    if (job.status === 'complete') {
      const link = document.createElement('a');
      link.href = `/api/downloads/${job.id}/file`;
      link.download = `${job.title}.mp4`;
      link.className = 'primary queue-action';
      link.textContent = 'MP4 indir ↓';
      link.setAttribute('aria-label', `MP4 indir: ${job.title}`);
      card.querySelector('.job-actions').append(link);
    }
    if (['queued', 'processing'].includes(job.status)) {
      actionButton(card, 'pause', 'Duraklat');
      actionButton(card, 'cancel', 'İptal et');
    } else {
      if (job.retryable && ['paused', 'error'].includes(job.status)) actionButton(card, 'resume', job.status === 'paused' ? 'Devam et' : 'Yeniden dene', true);
      actionButton(card, 'purge', 'Sil');
    }
    card.dataset.actions = signature;
    if (focusedAction) card.querySelector(`[data-action="${focusedAction}"]`)?.focus({ preventScroll: true });
  }
  card.querySelectorAll('button').forEach((button) => { button.disabled = pendingActions.has(job.id); });
  return card;
}

function renderJobs(jobs) {
  const list = $('#jobs-list');
  const keys = new Set(jobs.map((job) => job.id));
  for (const [key, card] of cards) {
    if (!keys.has(key)) { card.remove(); cards.delete(key); }
  }
  jobs.forEach((job, index) => {
    const card = renderJob(job);
    if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
  });
  $('#job-count').textContent = jobs.length;
  $('#jobs-empty').hidden = jobs.length > 0;
}

function scheduleRefresh(delay) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refreshJobs, delay);
}

async function refreshJobs() {
  if (refreshing) { scheduleRefresh(500); return; }
  refreshing = true;
  $('#refresh-jobs').disabled = true;
  let delay = 10000;
  try {
    const data = await api('/api/downloads');
    failures = 0;
    renderJobs(data.jobs);
    message('#queue-notice', '');
    if (data.jobs.some((job) => ['queued', 'processing'].includes(job.status))) delay = 1500;
  } catch (error) {
    failures++;
    delay = Math.min(30000, 2000 * 2 ** Math.min(failures, 4));
    message('#queue-notice', `${error.message} İşlem listesi otomatik yeniden kontrol edilecek.`);
  } finally {
    refreshing = false;
    $('#refresh-jobs').disabled = false;
    scheduleRefresh(delay);
  }
}

$('#jobs-list').addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const card = button.closest('.job-card');
  const key = card.dataset.id;
  if (pendingActions.has(key)) return;
  pendingActions.add(key);
  card.querySelectorAll('button').forEach((control) => { control.disabled = true; });
  message('#queue-error', '');
  try {
    const action = button.dataset.action;
    await api(`/api/downloads/${key}${action === 'cancel' ? '' : `/${action}`}`, { method: ['cancel', 'purge'].includes(action) ? 'DELETE' : 'POST' });
  } catch (error) {
    message('#queue-error', error.message);
  } finally {
    pendingActions.delete(key);
    card.querySelectorAll('button').forEach((control) => { control.disabled = false; });
    scheduleRefresh(0);
  }
});

$('#refresh-jobs').addEventListener('click', () => scheduleRefresh(0));
window.addEventListener('online', () => scheduleRefresh(0));
document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleRefresh(0); });
refreshJobs();

async function refreshNetworkStatus() {
  try {
    const status = await api('/api/network');
    $('#network-note').dataset.ready = String(status.configured);
    $('#network-label').textContent = status.configured ? 'Proton VPN · Gerektiğinde otomatik' : 'Doğrudan bağlantı';
  } catch { /* Downloads remain usable when this optional status is unavailable. */ }
}
refreshNetworkStatus();
window.addEventListener('focus', refreshNetworkStatus);
