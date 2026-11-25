const $ = (selector) => document.querySelector(selector);
const state = { feeds: [], episodes: [], feedId: null, episode: null, segments: [], translations: {}, translationRequests: new Set(), activeLine: -1, loopLine: -1, speeds: [0.5, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2], speedIndex: 3, pollTimer: null, translationPrefetchTimer: null, toastTimer: null, followPlayback: true, touchStartY: null, touchStartTarget: null, seekDragging: false, lastPositionSaveAt: 0, lastSavedPosition: 0, lastMediaPositionAt: 0, switchingEpisode: false, episodeRequestId: 0, episodeLoading: false, positionReady: false, metadataRestoreHandler: null, metadataErrorHandler: null };
const audio = $('#audio');
try {
  const savedRate = Number(localStorage.getItem('linguacast-playback-rate'));
  const savedRateIndex = state.speeds.indexOf(savedRate);
  if (savedRateIndex >= 0) state.speedIndex = savedRateIndex;
} catch { /* Storage may be disabled; keep the default rate. */ }
const escapeHTML = (value = '') => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const fmtTime = (seconds = 0) => { const n = Math.max(0, Math.floor(Number(seconds) || 0)); return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`; };
const fmtRate = (speed) => `${Number.isInteger(speed) ? speed.toFixed(1) : speed}×`;
function setPlaybackSpeed(value) {
  const speed = Number(value);
  const index = state.speeds.indexOf(speed);
  if (index < 0) return;
  state.speedIndex = index;
  audio.defaultPlaybackRate = speed;
  audio.playbackRate = speed;
  try { localStorage.setItem('linguacast-playback-rate', String(speed)); } catch { /* Playback still works without storage. */ }
  $('#speed-button').textContent = fmtRate(speed);
  document.querySelectorAll('.speed-option').forEach((button) => button.setAttribute('aria-pressed', String(Number(button.dataset.speed) === speed)));
  $('#speed-menu').hidden = true;
  $('#speed-button').setAttribute('aria-expanded', 'false');
}
const idOf = (item) => item.id ?? item.feed_id ?? item.episode_id;
const titleOf = (item) => item.title || item.name || 'Untitled';
const showToast = (message) => { const toast = $('#toast'); toast.textContent = message; toast.classList.add('show'); clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => toast.classList.remove('show'), 2600); };

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(body.detail || body.error || body.message || `Request failed (${response.status})`); error.status = response.status; throw error; }
  return body;
}

function listFrom(data, key) { if (Array.isArray(data)) return data; return data[key] || data.items || data.results || []; }
function coverMarkup(item, extra = '') { return item.image_url || item.artwork_url || item.image ? `<img class="feed-cover ${extra}" src="${escapeHTML(item.image_url || item.artwork_url || item.image)}" alt="" loading="lazy" />` : `<span class="feed-cover cover-placeholder ${extra}" aria-hidden="true">♫</span>`; }

function episodeTranscriptStatus(episode) {
  const status = String(episode.transcript_status || '').toLowerCase();
  const labels = { ready: 'Transcript ready', queued: 'Queued', downloading: 'Downloading', processing: 'Transcribing', error: 'Failed' };
  return labels[status] ? `<span class="episode-transcript-status ${status}" role="status">${labels[status]}</span>` : '';
}

async function loadFeeds() {
  const data = await api('/api/feeds');
  state.feeds = listFrom(data, 'feeds');
  $('#feed-count').textContent = `${state.feeds.length} ${state.feeds.length === 1 ? 'podcast' : 'podcasts'}`;
  $('#empty-library').hidden = state.feeds.length > 0;
  $('#feed-list').innerHTML = state.feeds.map((feed) => `<button class="feed-card" data-feed-id="${escapeHTML(idOf(feed))}">${coverMarkup(feed)}<span class="feed-info"><span class="feed-title">${escapeHTML(titleOf(feed))}</span><span class="feed-subtitle">${escapeHTML(feed.author || feed.subtitle || 'Podcast')}</span>${feed.last_poll_error ? '<span class="feed-sync-warning">RSS update failed</span>' : ''}</span><span class="feed-arrow" aria-hidden="true">↗</span></button>`).join('');
}

async function openFeed(feedId) {
  const feed = state.feeds.find((item) => String(idOf(item)) === String(feedId));
  if (!feed) return;
  state.feedId = feedId;
  $('#episode-heading').textContent = titleOf(feed);
  $('#episode-section').hidden = false;
  $('#episode-list').innerHTML = '<div class="transcript-notice">Loading episodes…</div>';
  try {
    try { await api(`/api/feeds/${encodeURIComponent(feedId)}/refresh`, { method: 'POST', body: '{}' }); }
    catch (error) { showToast(`Could not refresh episodes. Showing saved episodes: ${error.message}`); }
    await loadFeeds().catch(() => {});
    const data = await api(`/api/feeds/${encodeURIComponent(feedId)}/episodes`);
    state.episodes = listFrom(data, 'episodes');
    if (!state.episodes.length) { $('#episode-list').innerHTML = '<div class="transcript-notice">No episodes found.</div>'; return; }
    $('#episode-list').innerHTML = state.episodes.map((episode) => {
      const description = (episode.description || episode.summary || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
      const date = episode.published_at || episode.pub_date || episode.published || '';
      return `<article class="episode-card"><div><div class="episode-heading-meta"><div class="episode-date">${escapeHTML(date ? String(date).slice(0, 10) : 'EPISODE')}</div>${episodeTranscriptStatus(episode)}</div><h3>${escapeHTML(titleOf(episode))}</h3><p>${escapeHTML(description)}</p></div><div class="episode-actions"><button class="small-play" data-episode-id="${escapeHTML(idOf(episode))}" aria-label="${escapeHTML(titleOf(episode))}  — open">▶</button></div></article>`;
    }).join('');
    $('#episode-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) { $('#episode-list').innerHTML = `<div class="transcript-notice">${escapeHTML(error.message)}</div>`; }
}

function transcriptData(data) {
  const root = data.transcript || data;
  return { status: data.status || root.status || 'ready', error: data.error || root.error || '', segments: root.segments || data.segments || [] };
}

function translationStorageKey() { return `linguacast-translations-${idOf(state.episode)}`; }
function loadEpisodeTranslations(episodeId) {
  state.translations = {};
  state.translationRequests.clear();
  try {
    const saved = JSON.parse(localStorage.getItem(`linguacast-translations-${episodeId}`) || '{}');
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
      state.translations = Object.fromEntries(Object.entries(saved)
        .filter(([, entry]) => entry && /^[a-f0-9]{64}$/.test(entry.source_fingerprint || '') && typeof entry.translation === 'string')
        .map(([index, entry]) => [index, { source_fingerprint: entry.source_fingerprint, translation: entry.translation, expanded: false }]));
    }
  } catch { /* Translation cache is optional. */ }
}
function saveEpisodeTranslations() {
  if (!state.episode) return;
  try { localStorage.setItem(translationStorageKey(), JSON.stringify(state.translations)); } catch { /* Translation still works without storage. */ }
}
function translationEntry(index) {
  const entry = state.translations[index];
  const fingerprint = state.segments[index]?.source_fingerprint;
  return entry && (entry.source_fingerprint === fingerprint || (entry.error && !entry.source_fingerprint && !fingerprint)) ? entry : null;
}
function translationRequestKey(index, episodeId = idOf(state.episode)) { return `${episodeId}:${index}`; }
function updateTranslationUI(index) {
  const line = document.querySelector(`.transcript-line[data-line="${index}"]`);
  if (!line) return;
  const button = line.querySelector('.translation-toggle');
  const panel = line.querySelector('.translation-content');
  const entry = translationEntry(index);
  const loading = state.translationRequests.has(translationRequestKey(index));
  button.disabled = loading;
  button.textContent = loading ? 'Translating…' : entry?.refreshRequired ? 'Refresh transcript' : entry?.error ? 'Retry translation' : entry?.translation ? (entry.expanded ? 'Hide Japanese' : 'Show Japanese') : 'Show Japanese';
  button.setAttribute('aria-expanded', String(Boolean(entry?.expanded || loading)));
  panel.hidden = !(entry?.expanded || loading);
  panel.setAttribute('aria-busy', String(loading));
  if (loading) { panel.textContent = 'Translating…'; panel.classList.add('translation-loading'); }
  else {
    panel.classList.remove('translation-loading');
    panel.textContent = entry?.translation || entry?.error || '';
  }
  panel.classList.toggle('translation-error', Boolean(entry?.error));
}

function renderTranslationPrefetchStatus(progress) {
  const badge = $('#translation-prefetch-status');
  if (!badge) return;
  if (!progress || progress.status === 'not_started') { badge.hidden = true; return; }
  badge.hidden = false;
  if (progress.status === 'preparing') badge.textContent = `Preparing Japanese translations · ${progress.ready} of ${progress.total}`;
  else if (progress.status === 'paused') badge.textContent = `Translations paused · ${progress.ready} of ${progress.total} ready; tap a sentence to translate`;
  else if (progress.status === 'partial') badge.textContent = `Some translations need retry · ${progress.ready} of ${progress.total} ready`;
  else badge.textContent = 'Japanese translations ready';
}

async function refreshTranslationPrefetchProgress(episodeId, requestId) {
  try {
    const progress = await api(`/api/episodes/${encodeURIComponent(episodeId)}/translation-progress`);
    if (requestId !== state.episodeRequestId || String(idOf(state.episode)) !== String(episodeId)) return;
    renderTranslationPrefetchStatus(progress);
    if (progress.status === 'ready' || progress.status === 'partial' || progress.status === 'paused' || progress.status === 'not_started') {
      clearInterval(state.translationPrefetchTimer);
      state.translationPrefetchTimer = null;
    }
  } catch { /* Progress is informational; sentence playback and translation remain available. */ }
}

function startTranslationPrefetchProgress(episodeId, requestId) {
  clearInterval(state.translationPrefetchTimer);
  state.translationPrefetchTimer = null;
  refreshTranslationPrefetchProgress(episodeId, requestId);
  state.translationPrefetchTimer = setInterval(() => refreshTranslationPrefetchProgress(episodeId, requestId), 5000);
}
async function toggleTranslation(index) {
  if (!state.episode || !state.segments[index]) return;
  const episodeId = idOf(state.episode);
  const requestKey = translationRequestKey(index, episodeId);
  if (state.translationRequests.has(requestKey)) return;
  const sourceFingerprint = state.segments[index].source_fingerprint;
  const current = translationEntry(index);
  if (current?.refreshRequired) { await refreshTranscriptForTranslation(index); return; }
  if (current?.translation) {
    current.expanded = !current.expanded;
    updateTranslationUI(index);
    saveEpisodeTranslations();
    return;
  }
  if (!/^[a-f0-9]{64}$/.test(sourceFingerprint || '')) {
    state.translations[index] = { error: 'This sentence cannot be verified yet. Refresh the transcript and try again.', expanded: true, refreshRequired: true };
    updateTranslationUI(index);
    return;
  }
  state.translationRequests.add(requestKey);
  updateTranslationUI(index);
  const requestId = state.episodeRequestId;
  try {
    const result = await api(`/api/episodes/${encodeURIComponent(episodeId)}/translations/${index}`, {
      method: 'POST',
      body: JSON.stringify({ expected_source_fingerprint: sourceFingerprint }),
    });
    if (requestId !== state.episodeRequestId || String(idOf(state.episode)) !== String(episodeId)) return;
    if (result.source_fingerprint !== sourceFingerprint) throw new Error('This sentence changed while translating. Refresh the transcript and try again.');
    const translated = String(result.translation || '');
    if (!translated) throw new Error('No translation was returned. Try again.');
    state.translations[index] = { source_fingerprint: sourceFingerprint, translation: translated, expanded: true };
    saveEpisodeTranslations();
    refreshTranslationPrefetchProgress(episodeId, requestId);
  } catch (error) {
    if (requestId !== state.episodeRequestId) return;
    const refreshRequired = error.status === 409;
    const message = refreshRequired
      ? 'This transcript sentence changed. Refresh the transcript before translating it.'
      : error.message || 'Translation failed. Try again.';
    state.translations[index] = { source_fingerprint: sourceFingerprint, error: message, expanded: true, refreshRequired };
  } finally {
    state.translationRequests.delete(requestKey);
    if (requestId === state.episodeRequestId) updateTranslationUI(index);
  }
}

async function refreshTranscriptForTranslation(index) {
  const episodeId = idOf(state.episode);
  const requestId = state.episodeRequestId;
  const button = document.querySelector(`.translation-toggle[data-translation-line="${index}"]`);
  if (button) { button.disabled = true; button.textContent = 'Refreshing…'; }
  const scrollY = window.scrollY;
  const loopFingerprint = state.loopLine >= 0 ? state.segments[state.loopLine]?.source_fingerprint : null;
  try {
    const result = await api(`/api/episodes/${encodeURIComponent(episodeId)}/transcript`);
    if (requestId !== state.episodeRequestId || String(idOf(state.episode)) !== String(episodeId)) return;
    const transcript = transcriptData(result);
    if (!transcript.segments.length) throw new Error('The refreshed transcript is not ready yet. Try again shortly.');
    state.segments = transcript.segments;
    Object.values(state.translations).forEach((entry) => { if (entry) entry.expanded = false; });
    saveEpisodeTranslations();
    renderTranscript();
    const refreshedLoop = loopFingerprint ? state.segments.findIndex((segment) => segment.source_fingerprint === loopFingerprint) : -1;
    setLoop(refreshedLoop, refreshedLoop >= 0);
    const followPlayback = state.followPlayback;
    state.followPlayback = false;
    state.activeLine = -1;
    updatePlayer();
    state.followPlayback = followPlayback;
    window.scrollTo({ top: scrollY, behavior: 'auto' });
    showToast('Transcript refreshed');
  } catch (error) {
    if (requestId !== state.episodeRequestId) return;
    showToast(`Could not refresh transcript: ${error.message}`);
    const entry = state.translations[index];
    if (entry) entry.error = error.message;
    updateTranslationUI(index);
  } finally {
    if (requestId === state.episodeRequestId) {
      const refreshedButton = document.querySelector(`.translation-toggle[data-translation-line="${index}"]`);
      if (refreshedButton) refreshedButton.disabled = false;
    }
  }
}

async function openEpisode(episodeId) {
  const requestId = ++state.episodeRequestId;
  state.episodeLoading = true;
  $('#transcribe-button').disabled = true;
  clearInterval(state.pollTimer); state.pollTimer = null;
  clearInterval(state.translationPrefetchTimer); state.translationPrefetchTimer = null;
  renderTranslationPrefetchStatus(null);
  try {
    const episode = await api(`/api/episodes/${encodeURIComponent(episodeId)}`);
    if (requestId !== state.episodeRequestId) return;
    state.episodeLoading = false;
    $('#transcribe-button').disabled = false;
    const item = episode.episode || episode;
    state.switchingEpisode = true;
    savePosition(true);
    state.positionReady = false;
    audio.pause();
    state.activeLine = -1; state.loopLine = -1; state.segments = [];
    state.followPlayback = true; $('#follow-playback').hidden = true;
    setLoop(-1);
    state.episode = item;
    loadEpisodeTranslations(episodeId);
    state.lastPositionSaveAt = 0; state.lastSavedPosition = 0;
    $('#home-view').hidden = true; $('#player-view').hidden = false;
    $('#player-title').textContent = titleOf(item);
    const feed = state.feeds.find((candidate) => String(idOf(candidate)) === String(item.feed_id));
    const mediaItem = { ...item, feed_title: item.feed_title || item.show_title || feed?.title || 'Podcast', image_url: item.image_url || feed?.image_url || '' };
    $('#player-show').textContent = mediaItem.feed_title.toUpperCase();
    updateMediaMetadata(mediaItem);
    audio.src = item.audio_path || item.audio_url || item.enclosure_url || item.url || '';
    audio.defaultPlaybackRate = state.speeds[state.speedIndex];
    audio.playbackRate = state.speeds[state.speedIndex];
    $('#speed-button').textContent = fmtRate(audio.playbackRate);
    let saved = 0;
    try { saved = Number(localStorage.getItem(`linguacast-position-${episodeId}`) || 0); } catch { /* Optional resume position. */ }
    if (state.metadataRestoreHandler) audio.removeEventListener('loadedmetadata', state.metadataRestoreHandler);
    if (state.metadataErrorHandler) audio.removeEventListener('error', state.metadataErrorHandler);
    const restoreHandler = () => {
      if (requestId !== state.episodeRequestId) return;
      if (saved > 0 && Number.isFinite(saved) && saved < audio.duration) audio.currentTime = saved;
      state.positionReady = true; state.switchingEpisode = false; state.metadataRestoreHandler = null;
      state.lastSavedPosition = saved > 0 && saved < audio.duration ? saved : 0;
      state.lastPositionSaveAt = Date.now();
      audio.removeEventListener('loadedmetadata', restoreHandler);
    };
    state.metadataRestoreHandler = restoreHandler;
    state.metadataErrorHandler = () => { if (requestId === state.episodeRequestId) { state.switchingEpisode = false; state.metadataErrorHandler = null; } };
    audio.addEventListener('loadedmetadata', restoreHandler, { once: true });
    audio.addEventListener('error', state.metadataErrorHandler, { once: true });
    audio.load();
    setPlaybackSpeed(state.speeds[state.speedIndex]);
    const result = await api(`/api/episodes/${encodeURIComponent(episodeId)}/transcript`).catch(() => ({}));
    if (requestId !== state.episodeRequestId) return;
    const transcript = transcriptData(result);
    if (transcript.segments.length) { state.segments = transcript.segments; renderTranscript(); setTranscriptStatus('ready'); startTranslationPrefetchProgress(episodeId, requestId); }
    else if (['processing', 'queued', 'downloading'].includes(transcript.status)) { state.segments = []; renderTranscript(); setTranscriptStatus('pending'); pollTranscript(episodeId, requestId); }
    else if (transcript.status === 'error') { state.segments = []; renderTranscript(); setTranscriptStatus('error', transcript.error); }
    else { state.segments = []; renderTranscript(); setTranscriptStatus('missing'); }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) {
    if (requestId === state.episodeRequestId) { state.episodeLoading = false; $('#transcribe-button').disabled = false; showToast(`Could not open episode: ${error.message}`); }
  }
}

function setTranscriptStatus(status, message = '') {
  const badge = $('#transcript-status'); const button = $('#transcribe-button'); const notice = $('#transcript-notice');
  badge.classList.toggle('pending', status === 'pending');
  if (status === 'ready') { badge.textContent = 'Transcript ready'; button.hidden = true; notice.hidden = true; }
  else if (status === 'pending') { badge.textContent = 'Transcribing'; button.hidden = true; notice.hidden = false; notice.textContent = 'Transcribing on CPU. You can return later.'; }
  else if (status === 'error') { badge.textContent = 'Transcription failed'; button.hidden = false; notice.hidden = false; notice.textContent = message || 'Transcription failed. You can try again.'; }
  else { badge.textContent = 'Not created'; button.hidden = false; notice.hidden = false; notice.textContent = 'This episode has no transcript yet. Create one to tap any word and play from that point.'; }
}

function renderTranscript() {
  const container = $('#transcript-lines');
  if (!state.segments.length) { container.innerHTML = ''; return; }
  container.innerHTML = state.segments.map((segment, index) => {
    const start = Number(segment.start) || 0; const end = Number(segment.end) || start;
    const words = Array.isArray(segment.words) ? segment.words : [];
    let wordsHTML = words.length ? words.map((word, wordIndex) => {
      const text = typeof word === 'string' ? word : (word.word || word.text || '');
      const wordStart = typeof word === 'string' ? start : (Number(word.start) || start);
      const wordEnd = typeof word === 'string' ? end : (Number(word.end) || wordStart);
      const punctuation = /^[,.!?;:%)\]}»”’]/.test(text);
      const space = wordIndex > 0 && !punctuation ? ' ' : '';
      return `${space}<span class="word" data-line="${index}" data-word="${wordIndex}" data-start="${wordStart}" data-end="${wordEnd}">${escapeHTML(text)}</span>`;
    }).join('') : escapeHTML(segment.text || '');
    if (!words.length) wordsHTML = `<span class="word" data-line="${index}" data-word="0" data-start="${start}" data-end="${end}">${wordsHTML}</span>`;
    const cached = translationEntry(index);
    const translationHTML = cached?.translation ? escapeHTML(cached.translation) : cached?.error ? escapeHTML(cached.error) : '';
    return `<article class="transcript-line" data-line="${index}" data-start="${start}" data-end="${end}"><div class="sentence-toolbar"><button type="button" class="line-time" data-play-line="${index}" aria-label="Play from ${fmtTime(start)}">${fmtTime(start)}</button><button type="button" class="translation-toggle" data-translation-line="${index}" aria-expanded="false" aria-controls="translation-${index}">Show Japanese</button><button class="line-loop" data-loop-line="${index}" aria-label="Repeat this sentence" aria-pressed="false">↻</button></div><div class="line-text">${wordsHTML}</div><div id="translation-${index}" class="translation-content" aria-live="polite" hidden>${translationHTML}</div></article>`;
  }).join('');
}

async function startTranscription() {
  if (!state.episode || state.episodeLoading) return;
  const requestId = state.episodeRequestId;
  const episodeId = idOf(state.episode);
  const button = $('#transcribe-button'); button.disabled = true; button.textContent = 'Starting…'; setTranscriptStatus('pending');
  try {
    const result = await api(`/api/episodes/${encodeURIComponent(episodeId)}/transcribe`, { method: 'POST', body: '{}' });
    if (requestId !== state.episodeRequestId) return;
    if (result.transcript?.segments?.length || result.segments?.length) { state.segments = result.transcript?.segments || result.segments; renderTranscript(); setTranscriptStatus('ready'); }
    else { pollTranscript(episodeId, requestId); }
    showToast('Transcription started');
  } catch (error) {
    if (requestId !== state.episodeRequestId) return;
    if (error.status === 409) {
      const transcript = transcriptData(await api(`/api/episodes/${encodeURIComponent(episodeId)}/transcript`).catch(() => ({})));
      if (requestId !== state.episodeRequestId) return;
      if (['queued', 'downloading', 'processing'].includes(transcript.status)) { setTranscriptStatus('pending'); pollTranscript(episodeId, requestId); showToast('Transcription is already in progress'); }
      else { setTranscriptStatus('error', error.message); showToast(`Could not start transcription: ${error.message}`); }
    } else { setTranscriptStatus('error', error.message); showToast(`Could not start transcription: ${error.message}`); }
  }
  finally { if (requestId === state.episodeRequestId) { button.disabled = false; button.innerHTML = 'Create transcript <span>✳</span>'; } }
}

function pollTranscript(episodeId, requestId = state.episodeRequestId) {
  clearInterval(state.pollTimer);
  let inFlight = false;
  let statusCheckWarningShown = false;
  const timer = setInterval(async () => {
    if (requestId !== state.episodeRequestId) { clearInterval(timer); return; }
    if (inFlight) return;
    inFlight = true;
    try {
      const result = await api(`/api/episodes/${encodeURIComponent(episodeId)}/transcript`);
      if (requestId !== state.episodeRequestId) { clearInterval(timer); return; }
      statusCheckWarningShown = false;
      const transcript = transcriptData(result);
      if (transcript.segments.length) { clearInterval(timer); if (state.pollTimer === timer) state.pollTimer = null; state.segments = transcript.segments; renderTranscript(); setTranscriptStatus('ready'); startTranslationPrefetchProgress(episodeId, requestId); showToast('Transcript ready'); }
      else if (transcript.status === 'error' || transcript.status === 'failed') { clearInterval(timer); if (state.pollTimer === timer) state.pollTimer = null; setTranscriptStatus('error', transcript.error || 'Transcription failed.'); }
      else if (!['queued', 'downloading', 'processing'].includes(transcript.status)) { clearInterval(timer); if (state.pollTimer === timer) state.pollTimer = null; setTranscriptStatus('error', 'Transcription is no longer active. You can try again.'); }
    } catch (error) {
      if (requestId !== state.episodeRequestId) { clearInterval(timer); return; }
      if (!statusCheckWarningShown) { statusCheckWarningShown = true; showToast(`Could not check transcription status: ${error.message}`); }
    } finally { inFlight = false; }
  }, 4000);
  state.pollTimer = timer;
}

function setLoop(index, enabled = true) {
  state.loopLine = enabled ? index : -1;
  document.querySelectorAll('.line-loop').forEach((button) => {
    const active = Number(button.dataset.loopLine) === state.loopLine;
    button.setAttribute('aria-pressed', String(active));
    button.setAttribute('aria-label', active ? 'Stop repeating this sentence' : 'Repeat this sentence');
  });
  $('#repeat-button').classList.toggle('active', state.loopLine >= 0);
  $('#repeat-button').setAttribute('aria-pressed', String(state.loopLine >= 0));
}

function scrollToActiveLine(index = state.activeLine) {
  if (index < 0) return;
  const line = document.querySelector(`.transcript-line[data-line="${index}"]`);
  if (!line) return;
  const playerTop = $('.mini-player').getBoundingClientRect().top;
  const rect = line.getBoundingClientRect();
  const targetTop = window.scrollY + rect.top - Math.max(12, (playerTop - rect.height) / 2);
  window.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
}

function setPlaybackFollow(enabled) {
  state.followPlayback = enabled;
  $('#follow-playback').hidden = enabled;
  if (enabled) scrollToActiveLine();
}

function pausePlaybackFollow() {
  if ($('#player-view').hidden || !state.followPlayback) return;
  setPlaybackFollow(false);
}

function activateLine(index, shouldPlay = false) {
  const line = state.segments[index]; if (!line) return;
  state.activeLine = index;
  document.querySelectorAll('.transcript-line').forEach((element, i) => element.classList.toggle('active', i === index));
  document.querySelectorAll('.line-time').forEach((button, i) => { if (i === index) button.setAttribute('aria-current', 'true'); else button.removeAttribute('aria-current'); });
  if (shouldPlay) { audio.currentTime = Number(line.start) || 0; audio.play().catch(() => {}); }
}

function savePosition(force = false) {
  if (!state.episode || !state.positionReady) return;
  const now = Date.now(); const position = Number(audio.currentTime) || 0;
  if (!force && (now - state.lastPositionSaveAt < 5000 || Math.abs(position - state.lastSavedPosition) < 5)) return;
  try { localStorage.setItem(`linguacast-position-${idOf(state.episode)}`, String(position)); }
  catch { return; }
  state.lastPositionSaveAt = now; state.lastSavedPosition = position;
}

function updateMediaPosition(force = false) {
  const mediaSession = navigator.mediaSession;
  if (!mediaSession || typeof mediaSession.setPositionState !== 'function' || !Number.isFinite(audio.duration) || audio.duration <= 0) return;
  const now = Date.now();
  if (!force && now - state.lastMediaPositionAt < 1000) return;
  try { mediaSession.setPositionState({ duration: audio.duration, playbackRate: audio.playbackRate, position: Math.max(0, Math.min(audio.currentTime, audio.duration)) }); state.lastMediaPositionAt = now; }
  catch { /* Some iOS versions reject position state before metadata is ready. */ }
}

function updateMediaMetadata(item) {
  const mediaSession = navigator.mediaSession;
  if (!mediaSession || typeof window.MediaMetadata !== 'function') return;
  const artworkUrl = item.image_url || item.artwork_url || item.image;
  const artwork = artworkUrl ? [{ src: artworkUrl, sizes: '512x512' }] : [];
  try { mediaSession.metadata = new window.MediaMetadata({ title: titleOf(item), artist: item.author || item.feed_title || item.show_title || 'Podcast', album: item.feed_title || item.show_title || '', artwork }); }
  catch { /* Media metadata is optional. */ }
}

function updatePlayButton() {
  const playing = !audio.paused && !audio.ended;
  $('#play-button').classList.toggle('playing', playing);
  $('#play-button').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  $('#play-button').setAttribute('aria-pressed', String(playing));
  if (navigator.mediaSession) { try { navigator.mediaSession.playbackState = playing ? 'playing' : 'paused'; } catch { /* Optional browser integration. */ } }
}

function activeLineAt(time) {
  let low = 0; let high = state.segments.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if ((Number(state.segments[middle].start) || 0) <= time) low = middle + 1;
    else high = middle;
  }
  const previous = low - 1;
  return previous >= 0 ? previous : -1;
}

function updatePlayer() {
  if (!state.episode) return;
  const duration = Number.isFinite(audio.duration) ? audio.duration : Number(state.episode.duration) || 0;
  $('#current-time').textContent = fmtTime(audio.currentTime); $('#duration').textContent = fmtTime(duration);
  if (!state.seekDragging) $('#seek').value = duration > 0 ? String(Math.round(audio.currentTime / duration * 1000)) : '0';
  const seekPosition = state.seekDragging && duration > 0 ? Number($('#seek').value) / 1000 * duration : audio.currentTime;
  $('#seek').setAttribute('aria-valuetext', `${fmtTime(seekPosition)} / ${fmtTime(duration)}`);
  updatePlayButton();
  savePosition(); updateMediaPosition();
  if (state.loopLine >= 0) {
    const loop = state.segments[state.loopLine];
    if (loop && audio.currentTime >= Number(loop.end)) audio.currentTime = Number(loop.start) || 0;
  }
  const index = activeLineAt(audio.currentTime);
  if (index !== state.activeLine) {
    document.querySelector('.transcript-line.active')?.classList.remove('active');
    state.activeLine = index;
    if (index >= 0) document.querySelector(`.transcript-line[data-line="${index}"]`)?.classList.add('active');
    document.querySelectorAll('.line-time').forEach((button, i) => { if (i === index) button.setAttribute('aria-current', 'true'); else button.removeAttribute('aria-current'); });
    if (index >= 0 && state.followPlayback) scrollToActiveLine(index);
  }
  document.querySelector('.word.current')?.classList.remove('current');
  if (index >= 0) {
    const words = [...document.querySelectorAll(`.word[data-line="${index}"]`)];
    let low = 0; let high = words.length;
    while (low < high) { const middle = (low + high) >> 1; if (Number(words[middle].dataset.start) <= audio.currentTime) low = middle + 1; else high = middle; }
    const current = low > 0 ? words[low - 1] : null;
    current?.classList.add('current');
  }
}

let visualUpdateFrame = null;
function startVisualUpdates() {
  if (visualUpdateFrame !== null) cancelAnimationFrame(visualUpdateFrame);
  const frame = () => {
    updatePlayer();
    if (!audio.paused && !audio.ended) visualUpdateFrame = requestAnimationFrame(frame);
    else visualUpdateFrame = null;
  };
  visualUpdateFrame = requestAnimationFrame(frame);
}

function stopVisualUpdates() {
  if (visualUpdateFrame !== null) cancelAnimationFrame(visualUpdateFrame);
  visualUpdateFrame = null;
  updatePlayer();
}

window.addEventListener('wheel', pausePlaybackFollow, { passive: true });
window.addEventListener('touchstart', (event) => { state.touchStartY = event.touches[0]?.clientY ?? null; state.touchStartTarget = event.target; }, { passive: true });
window.addEventListener('touchmove', (event) => {
  const y = event.touches[0]?.clientY;
  const overPlayerControl = state.touchStartTarget?.closest?.('.mini-player');
  if (state.touchStartY !== null && y !== undefined && !overPlayerControl && Math.abs(y - state.touchStartY) > 8) pausePlaybackFollow();
}, { passive: true });
window.addEventListener('touchend', () => { state.touchStartY = null; state.touchStartTarget = null; }, { passive: true });
window.addEventListener('keydown', (event) => {
  if (['ArrowDown', 'ArrowUp', 'PageDown', 'PageUp', 'Home', 'End', ' '].includes(event.key) && !['INPUT', 'TEXTAREA', 'BUTTON'].includes(event.target.tagName)) pausePlaybackFollow();
});

$('#add-feed-toggle').addEventListener('click', () => { $('#add-feed-panel').hidden = !$('#add-feed-panel').hidden; $('#feed-url').focus(); });
$('#cancel-add').addEventListener('click', () => { $('#add-feed-panel').hidden = true; });
$('#empty-add').addEventListener('click', () => { $('#add-feed-panel').hidden = false; $('#feed-url').focus(); });
$('#add-feed-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const input = $('#feed-url'); const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true; button.textContent = 'Loading…';
  try { await api('/api/feeds', { method: 'POST', body: JSON.stringify({ url: input.value.trim() }) }); input.value = ''; $('#add-feed-panel').hidden = true; await loadFeeds(); showToast('Podcast added to your library'); }
  catch (error) { showToast(`Could not add podcast: ${error.message}`); }
  finally { button.disabled = false; button.innerHTML = 'Add <span>↗</span>'; }
});
$('#feed-list').addEventListener('click', (event) => { const card = event.target.closest('[data-feed-id]'); if (card) openFeed(card.dataset.feedId); });
$('#episode-list').addEventListener('click', (event) => { const button = event.target.closest('[data-episode-id]'); if (button) openEpisode(button.dataset.episodeId); });
$('#back-to-shows').addEventListener('click', () => { $('#episode-section').hidden = true; window.scrollTo({ top: 0, behavior: 'smooth' }); });
$('#player-back').addEventListener('click', () => { state.episodeRequestId += 1; state.episodeLoading = false; $('#transcribe-button').disabled = false; clearInterval(state.pollTimer); state.pollTimer = null; clearInterval(state.translationPrefetchTimer); state.translationPrefetchTimer = null; audio.pause(); $('#player-view').hidden = true; $('#home-view').hidden = false; });
$('#transcribe-button').addEventListener('click', startTranscription);
$('#follow-playback').addEventListener('click', () => setPlaybackFollow(true));
$('#transcript-lines').addEventListener('click', (event) => {
  const translation = event.target.closest('[data-translation-line]');
  if (translation) { event.preventDefault(); event.stopPropagation(); toggleTranslation(Number(translation.dataset.translationLine)); return; }
  const seekLine = event.target.closest('[data-play-line]');
  if (seekLine) {
    if (state.loopLine >= 0) { setLoop(-1); showToast('Sentence repeat turned off'); }
    activateLine(Number(seekLine.dataset.playLine), true); savePosition(true); return;
  }
  const loop = event.target.closest('[data-loop-line]');
  if (loop) { const index = Number(loop.dataset.loopLine); const enabling = state.loopLine !== index; setLoop(enabling ? index : -1); if (enabling) activateLine(index, true); return; }
  const word = event.target.closest('.word'); if (word) {
    const index = Number(word.dataset.line);
    if (state.loopLine >= 0) { setLoop(-1); showToast('Sentence repeat turned off'); }
    activateLine(index, false);
    audio.currentTime = Number(word.dataset.start) || 0;
    audio.play().catch(() => {}); savePosition(true);
  }
});
$('#play-button').addEventListener('click', () => { if (audio.paused) audio.play().catch((error) => showToast(error.message)); else audio.pause(); });
$('#speed-menu').innerHTML = state.speeds.map((speed) => `<button type="button" class="speed-option" data-speed="${speed}" aria-pressed="${speed === 1}">${fmtRate(speed)}</button>`).join('');
setPlaybackSpeed(state.speeds[state.speedIndex]);
$('#speed-button').addEventListener('click', () => {
  const menu = $('#speed-menu');
  menu.hidden = !menu.hidden;
  $('#speed-button').setAttribute('aria-expanded', String(!menu.hidden));
  if (!menu.hidden) menu.querySelector('.speed-option')?.focus();
});
$('#speed-menu').addEventListener('click', (event) => { const choice = event.target.closest('[data-speed]'); if (choice) { setPlaybackSpeed(choice.dataset.speed); $('#speed-button').focus(); } });
document.addEventListener('pointerdown', (event) => { if (!event.target.closest('.speed-control') && !$('#speed-menu').hidden) { $('#speed-menu').hidden = true; $('#speed-button').setAttribute('aria-expanded', 'false'); } });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && !$('#speed-menu').hidden) { $('#speed-menu').hidden = true; $('#speed-button').setAttribute('aria-expanded', 'false'); $('#speed-button').focus(); } });
$('#rewind-button').addEventListener('click', () => { audio.currentTime = Math.max(0, audio.currentTime - 10); });
$('#forward-button').addEventListener('click', () => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + 30); });
$('#repeat-button').addEventListener('click', () => {
  const index = state.activeLine >= 0 ? state.activeLine : 0;
  if (!state.segments[index]) { showToast('Create a transcript to repeat a sentence'); return; }
  const enabling = state.loopLine !== index;
  setLoop(enabling ? index : -1);
  if (enabling) activateLine(index, true);
});

const seek = $('#seek');
const commitSeek = () => {
  if (Number.isFinite(audio.duration) && audio.duration > 0) audio.currentTime = Number(seek.value) / 1000 * audio.duration;
  state.seekDragging = false; updatePlayer(); savePosition(true); updateMediaPosition(true);
};
seek.addEventListener('pointerdown', () => { state.seekDragging = true; });
seek.addEventListener('input', () => {
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  seek.setAttribute('aria-valuetext', `${fmtTime(Number(seek.value) / 1000 * duration)} / ${fmtTime(duration)}`);
});
seek.addEventListener('change', commitSeek);
seek.addEventListener('pointercancel', () => { state.seekDragging = false; updatePlayer(); });
window.addEventListener('pointerup', () => { if (state.seekDragging) commitSeek(); }, true);

audio.addEventListener('timeupdate', updatePlayer);
audio.addEventListener('seeked', () => { updatePlayer(); savePosition(); updateMediaPosition(true); });
audio.addEventListener('play', () => { updatePlayButton(); startVisualUpdates(); });
audio.addEventListener('pause', () => { updatePlayButton(); if (!state.switchingEpisode) savePosition(true); stopVisualUpdates(); });
audio.addEventListener('loadedmetadata', () => { updatePlayer(); updateMediaPosition(true); });
audio.addEventListener('ended', () => { savePosition(true); if (state.loopLine >= 0) { audio.currentTime = Number(state.segments[state.loopLine]?.start) || 0; audio.play().catch(() => {}); } else stopVisualUpdates(); });
window.addEventListener('pagehide', () => savePosition(true));
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') savePosition(true); });

if (navigator.mediaSession) {
  const actions = {
    play: () => audio.play().catch(() => {}),
    pause: () => audio.pause(),
    seekbackward: (detail) => { audio.currentTime = Math.max(0, audio.currentTime - (detail.seekOffset || 10)); savePosition(true); },
    seekforward: (detail) => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + (detail.seekOffset || 30)); savePosition(true); },
    seekto: (detail) => { if (Number.isFinite(detail.seekTime)) { if (typeof audio.fastSeek === 'function') audio.fastSeek(detail.seekTime); else audio.currentTime = detail.seekTime; savePosition(true); } },
  };
  for (const [action, handler] of Object.entries(actions)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* Action is unsupported by this browser. */ } }
}

loadFeeds().catch((error) => { $('#empty-library').hidden = false; $('#empty-library').querySelector('p').textContent = `Could not connect to the API: ${error.message}`; });
