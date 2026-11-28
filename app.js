const $ = (selector) => document.querySelector(selector);
const SKIP_INTERVAL_MIN = 1;
const SKIP_INTERVAL_MAX = 300;
function readSkipInterval(key, fallback) {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isInteger(value) && value >= SKIP_INTERVAL_MIN && value <= SKIP_INTERVAL_MAX ? value : fallback;
  } catch { return fallback; }
}
const state = { feeds: [], episodes: [], feedId: null, episode: null, segments: [], translations: {}, translationRequests: new Set(), activeLine: -1, loopLine: -1, shadowTake: null, shadowRecording: null, shadowPending: null, shadowPlayback: null, rewindSeconds: readSkipInterval('linguacast-rewind-seconds', 10), forwardSeconds: readSkipInterval('linguacast-forward-seconds', 30), speeds: [0.5, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2], speedIndex: 3, pollTimer: null, translationPrefetchTimer: null, toastTimer: null, followPlayback: true, touchStartY: null, touchStartTarget: null, seekDragging: false, lastPositionSaveAt: 0, lastSavedPosition: 0, lastMediaPositionAt: 0, switchingEpisode: false, episodeRequestId: 0, episodeLoading: false, positionReady: false, metadataRestoreHandler: null, metadataErrorHandler: null };
const audio = $('#audio');
const shadowTakeAudio = $('#shadow-take-audio');
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
function setSkipInterval(direction, rawValue) {
  const value = Number(rawValue);
  const key = direction === 'rewind' ? 'rewindSeconds' : 'forwardSeconds';
  const storageKey = direction === 'rewind' ? 'linguacast-rewind-seconds' : 'linguacast-forward-seconds';
  if (!Number.isInteger(value) || value < SKIP_INTERVAL_MIN || value > SKIP_INTERVAL_MAX) {
    $(`#${direction}-seconds`).value = String(state[key]);
    showToast('Choose a whole number from 1 to 300 seconds');
    return false;
  }
  state[key] = value;
  try { localStorage.setItem(storageKey, String(value)); } catch { /* Keep the current setting for this session. */ }
  updateSkipControls();
  return true;
}
function updateSkipControls() {
  $('#rewind-button').textContent = `−${state.rewindSeconds}`;
  $('#rewind-button').setAttribute('aria-label', `Rewind ${state.rewindSeconds} seconds`);
  $('#rewind-button').title = `Rewind ${state.rewindSeconds} seconds`;
  $('#forward-button').textContent = `+${state.forwardSeconds}`;
  $('#forward-button').setAttribute('aria-label', `Skip forward ${state.forwardSeconds} seconds`);
  $('#forward-button').title = `Skip forward ${state.forwardSeconds} seconds`;
}
function seekBy(offset) {
  const duration = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : Infinity;
  audio.currentTime = Math.max(0, Math.min(duration, audio.currentTime + offset));
  savePosition(true);
}
const SHADOW_OFFSET_STEP_MS = 100;
const SHADOW_OFFSET_LIMIT_MS = 1500;
const SHADOW_MAX_SENTENCE_SECONDS = 60;
const SHADOW_RECORDING_TAIL_MS = 30_000;
const SHADOW_MAX_RECORDING_BYTES = 25 * 1024 * 1024;
function shadowSegment(index) {
  const segment = state.segments[index];
  if (!segment || !Number.isFinite(Number(segment.start)) || !Number.isFinite(Number(segment.end))) return null;
  const start = Number(segment.start), end = Number(segment.end);
  if (start < 0 || end <= start) return null;
  return { start, end, fingerprint: segment.source_fingerprint || `${start}:${end}:${segment.text || ''}` };
}
function shadowTakeFor(index) {
  const segment = shadowSegment(index);
  return segment && state.shadowTake && state.shadowTake.episodeId === String(idOf(state.episode))
    && state.shadowTake.fingerprint === segment.fingerprint ? state.shadowTake : null;
}
function updateShadowUI(index) {
  const line = document.querySelector(`.transcript-line[data-line="${index}"]`);
  if (!line) return;
  const panel = line.querySelector('.shadow-panel');
  const opener = line.querySelector('.shadow-open');
  const recorder = state.shadowRecording?.index === index ? state.shadowRecording : null;
  const pending = state.shadowPending?.index === index;
  const busy = Boolean(state.shadowPending || state.shadowRecording);
  const take = shadowTakeFor(index);
  const recordButton = panel.querySelector('.shadow-record');
  const stopButton = panel.querySelector('.shadow-stop');
  const status = panel.querySelector('.shadow-status');
  opener.setAttribute('aria-expanded', String(!panel.hidden));
  opener.classList.toggle('has-take', Boolean(take));
  recordButton.disabled = Boolean(state.shadowPending || (state.shadowRecording && !recorder));
  if (pending) recordButton.textContent = 'Allow microphone…';
  else recordButton.textContent = take ? 'Record again' : 'Record';
  recordButton.hidden = Boolean(recorder);
  stopButton.hidden = !recorder;
  stopButton.disabled = !recorder;
  panel.querySelectorAll('[data-shadow-mode]').forEach((button) => { button.disabled = !take || busy; });
  panel.querySelectorAll('[data-shadow-offset]').forEach((button) => { button.disabled = !take || busy; });
  const offset = take?.offsetMs || 0;
  panel.querySelector('.shadow-offset-value').textContent = `${offset < 0 ? '−' : '+'}${(Math.abs(offset) / 1000).toFixed(1)} s`;
  const playing = state.shadowPlayback?.index === index ? state.shadowPlayback.mode : '';
  panel.querySelectorAll('[data-shadow-mode]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.shadowMode === playing)));
  if (pending) status.textContent = 'Waiting for microphone permission…';
  else if (recorder) status.textContent = recorder.waitingForSpeechEnd
    ? 'Original finished. Finish speaking, then tap Stop.' : 'Recording with the original…';
  else if (take) status.textContent = 'Take ready. Kept in memory until you close this player.';
  else status.textContent = 'Record this sentence along with the original.';
  status.classList.toggle('recording', Boolean(recorder));
}
function updateAllShadowUI() {
  document.querySelectorAll('.transcript-line[data-line]').forEach((line) => updateShadowUI(Number(line.dataset.line)));
}
function stopShadowPlayback(pauseOriginal = true) {
  const playback = state.shadowPlayback;
  if (playback?.delayTimer) clearTimeout(playback.delayTimer);
  state.shadowPlayback = null;
  shadowTakeAudio.pause();
  if (playback?.delayedElement && playback.savedDelayedRate) playback.delayedElement.playbackRate = playback.savedDelayedRate;
  shadowTakeAudio.currentTime = 0;
  shadowTakeAudio.muted = false;
  audio.muted = false;
  if (playback) audio.playbackRate = state.speeds[state.speedIndex];
  if (pauseOriginal) audio.pause();
  if (playback) updateShadowUI(playback.index);
}
function stopShadowStream(recording) {
  if (recording.stopTimer) clearTimeout(recording.stopTimer);
  if (recording.monitorFrame !== null) cancelAnimationFrame(recording.monitorFrame);
  recording.stream?.getTracks().forEach((track) => track.stop());
}
function clearShadowTimers(recording) {
  if (recording.stopTimer) clearTimeout(recording.stopTimer);
  if (recording.monitorFrame !== null) cancelAnimationFrame(recording.monitorFrame);
  recording.stopTimer = null;
  recording.monitorFrame = null;
}
function cancelShadowRecording() {
  const recording = state.shadowRecording;
  if (!recording) return;
  recording.cancelled = true;
  state.shadowRecording = null;
  clearShadowTimers(recording);
  if (recording.recorder.state !== 'inactive') {
    try { recording.recorder.stop(); } catch { /* The stream may already have stopped. */ }
  }
  recording.stream?.getTracks().forEach((track) => track.stop());
  if (recording.index !== undefined) updateShadowUI(recording.index);
}
function clearShadowSession() {
  if (state.shadowPending) { state.shadowPending.cancelled = true; state.shadowPending = null; }
  stopShadowPlayback(true);
  cancelShadowRecording();
  if (state.shadowTake?.url) URL.revokeObjectURL(state.shadowTake.url);
  state.shadowTake = null;
  updateAllShadowUI();
}
function chooseShadowMimeType() {
  if (!window.MediaRecorder || typeof window.MediaRecorder.isTypeSupported !== 'function') return '';
  return ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
    .find((type) => window.MediaRecorder.isTypeSupported(type)) || '';
}
async function startShadowRecording(index) {
  const segment = shadowSegment(index);
  if (!segment || !state.episode) { showToast('This sentence cannot be recorded.'); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    showToast('Microphone recording is not supported in this browser.'); return;
  }
  if (segment.end - segment.start > SHADOW_MAX_SENTENCE_SECONDS) { showToast('This sentence is too long to record.'); return; }
  if (state.shadowPending) return;
  if (state.shadowRecording) cancelShadowRecording();
  stopShadowPlayback(true);
  const episodeId = String(idOf(state.episode));
  const requestId = state.episodeRequestId;
  const pending = { index, episodeId, requestId, fingerprint: segment.fingerprint, cancelled: false };
  state.shadowPending = pending;
  updateShadowUI(index);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    if (pending.cancelled || state.shadowPending !== pending || requestId !== state.episodeRequestId || String(idOf(state.episode)) !== episodeId
        || shadowSegment(index)?.fingerprint !== segment.fingerprint) {
      stream.getTracks().forEach((track) => track.stop()); return;
    }
    const mimeType = chooseShadowMimeType();
    const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    const playbackRate = Math.max(0.5, Number(audio.playbackRate) || 1);
    const maxRecordingMs = Math.ceil((segment.end - segment.start) / playbackRate * 1000) + SHADOW_RECORDING_TAIL_MS;
    const recording = { index, episodeId, fingerprint: segment.fingerprint, segment, stream, recorder, request: pending, playbackRate,
      chunks: [], stopTimer: null, monitorFrame: null, waitingForSpeechEnd: false, cancelled: false };
    state.shadowRecording = recording;
    state.shadowPending = null;
    recorder.addEventListener('dataavailable', (event) => { if (event.data?.size) recording.chunks.push(event.data); });
    recorder.addEventListener('error', () => {
      if (state.shadowRecording !== recording) return;
      cancelShadowRecording(); showToast('Recording failed. Please try again.');
    }, { once: true });
    recorder.addEventListener('stop', () => {
      stopShadowStream(recording);
      if (state.shadowRecording === recording) state.shadowRecording = null;
      if (!recording.cancelled && recording.chunks.length && requestId === state.episodeRequestId
          && String(idOf(state.episode)) === episodeId && shadowSegment(index)?.fingerprint === segment.fingerprint) {
        const blob = new Blob(recording.chunks, { type: recorder.mimeType || recording.chunks[0].type || 'audio/mp4' });
        if (blob.size > SHADOW_MAX_RECORDING_BYTES) {
          showToast('Recording is too large. Please record a shorter take.');
        } else {
          if (state.shadowTake?.url) URL.revokeObjectURL(state.shadowTake.url);
          state.shadowTake = { episodeId, fingerprint: segment.fingerprint, index, blob, url: URL.createObjectURL(blob), offsetMs: 0, playbackRate };
        }
      }
      if (state.shadowRecording !== recording) updateShadowUI(index);
    }, { once: true });
    recorder.start();
    audio.currentTime = segment.start;
    await audio.play();
    const monitor = () => {
      if (state.shadowRecording !== recording || recording.waitingForSpeechEnd) return;
      if (audio.currentTime >= segment.end) {
        audio.pause();
        recording.waitingForSpeechEnd = true;
        updateShadowUI(index);
        return;
      }
      recording.monitorFrame = requestAnimationFrame(monitor);
    };
    recording.monitorFrame = requestAnimationFrame(monitor);
    recording.stopTimer = setTimeout(() => {
      if (state.shadowRecording === recording) finishShadowRecording(index);
    }, maxRecordingMs);
    updateShadowUI(index);
  } catch (error) {
    stream?.getTracks().forEach((track) => track.stop());
    const ownsPending = state.shadowPending === pending;
    const ownsRecording = state.shadowRecording?.request === pending;
    if (pending.cancelled || (!ownsPending && !ownsRecording)) return;
    if (ownsPending) state.shadowPending = null;
    if (ownsRecording) cancelShadowRecording();
    stopShadowPlayback(true);
    showToast(error?.name === 'NotAllowedError' ? 'Allow microphone access to record.' : 'Could not start recording. Try again.');
    updateShadowUI(index);
  }
}
function finishShadowRecording(index) {
  const recording = state.shadowRecording;
  if (!recording || recording.index !== index || recording.recorder.state === 'inactive') return;
  clearShadowTimers(recording);
  audio.pause();
  try { recording.recorder.stop(); }
  catch { cancelShadowRecording(); showToast('Could not finish recording.'); }
  updateShadowUI(index);
}
function playShadowMode(index, mode) {
  if (state.shadowPending || state.shadowRecording) { showToast('Finish or cancel recording before playback.'); return; }
  const take = shadowTakeFor(index);
  const segment = shadowSegment(index);
  if (!segment) { showToast('This sentence cannot be played.'); return; }
  if (mode !== 'original' && !take) { showToast('Record this sentence first.'); return; }
  stopShadowPlayback(true);
  if (mode === 'original') {
    const playback = { index, mode, segment, singlePass: true };
    state.shadowPlayback = playback;
    audio.playbackRate = take?.playbackRate || audio.playbackRate;
    activateLine(index);
    audio.currentTime = segment.start;
    audio.play().catch(() => { if (state.shadowPlayback !== playback) return; stopShadowPlayback(false); showToast('Could not play the original audio.'); });
    updateShadowUI(index);
    return;
  }
  shadowTakeAudio.src = take.url;
  shadowTakeAudio.currentTime = 0;
  if (mode === 'take') {
    const playback = { index, mode, segment, singlePass: false };
    state.shadowPlayback = playback;
    audio.pause();
    shadowTakeAudio.play().catch(() => { if (state.shadowPlayback !== playback) return; stopShadowPlayback(false); showToast('Could not play your recording.'); });
    updateShadowUI(index);
    return;
  }
  const offsetMs = take.offsetMs || 0;
  const delay = Math.abs(offsetMs);
  const delayedElement = offsetMs > 0 ? shadowTakeAudio : audio;
  const playback = { index, mode, segment, singlePass: true, delayTimer: null, delayPending: delay > 0, sourceEnded: false, takeEnded: false };
  state.shadowPlayback = playback;
  audio.playbackRate = take.playbackRate || audio.playbackRate;
  audio.currentTime = segment.start;
  if (delay > 0) {
    delayedElement.muted = true;
    playback.delayedElement = delayedElement;
    playback.savedDelayedRate = delayedElement.playbackRate;
    // Keep the prewarmed, muted track alive until its offset expires, even
    // when the sentence itself is shorter than the selected offset.
    delayedElement.playbackRate = Math.min(playback.savedDelayedRate, 0.0625);
  }
  const startOriginal = audio.play();
  const startTake = shadowTakeAudio.play();
  Promise.all([startOriginal, startTake]).then(() => {
    if (state.shadowPlayback !== playback) return;
    if (delay > 0) playback.delayTimer = setTimeout(() => {
      if (state.shadowPlayback !== playback) return;
      playback.delayTimer = null;
      playback.delayPending = false;
      playback.takeEnded = false;
      if (delayedElement === shadowTakeAudio) shadowTakeAudio.currentTime = 0;
      else { audio.currentTime = segment.start; playback.sourceEnded = false; }
      delayedElement.playbackRate = playback.savedDelayedRate;
      delayedElement.muted = false;
      if (delayedElement.paused) delayedElement.play().catch(() => {
        if (state.shadowPlayback !== playback) return;
        stopShadowPlayback(true); showToast('The delayed track could not resume. Tap to try again.');
      });
    }, delay);
  }).catch(() => { if (state.shadowPlayback !== playback) return; stopShadowPlayback(true); showToast('Could not play both tracks. Tap to try again.'); });
  updateShadowUI(index);
}
function adjustShadowOffset(index, direction) {
  const take = shadowTakeFor(index);
  if (!take) return;
  stopShadowPlayback(true);
  take.offsetMs = Math.max(-SHADOW_OFFSET_LIMIT_MS, Math.min(SHADOW_OFFSET_LIMIT_MS, take.offsetMs + direction * SHADOW_OFFSET_STEP_MS));
  updateShadowUI(index);
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

function safeExpressionNotes(value, sentence) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object'
      && typeof item.phrase === 'string' && typeof item.explanation === 'string'
      && item.phrase.trim() && item.phrase.length <= 80 && item.explanation.trim() && item.explanation.length <= 180
      && (typeof sentence !== 'string' || sentence.includes(item.phrase.trim())))
    .slice(0, 2)
    .map((item) => ({ phrase: item.phrase.trim(), explanation: item.explanation.trim() }));
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
        .map(([index, entry]) => [index, {
          source_fingerprint: entry.source_fingerprint,
          translation: entry.translation,
          expressions: safeExpressionNotes(entry.expressions, state.segments[Number(index)]?.text),
          expressions_ready: entry.expressions_ready === true ? true : undefined,
          explanation_error: typeof entry.explanation_error === 'string' ? entry.explanation_error : '',
          expanded: false,
        }]));
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
  button.textContent = loading ? (entry?.translation ? 'Adding notes…' : 'Translating…')
    : entry?.refreshRequired ? 'Refresh transcript' : entry?.error ? 'Retry translation'
      : entry?.translation ? (entry.expanded ? 'Hide Japanese' : 'Show Japanese') : 'Show Japanese';
  button.setAttribute('aria-expanded', String(Boolean(entry?.expanded || loading)));
  panel.hidden = !(entry?.expanded || loading);
  panel.setAttribute('aria-busy', String(loading));
  const translationText = panel.querySelector('.translation-ja');
  const notes = panel.querySelector('.expression-notes');
  const retryButton = panel.querySelector('.expression-retry');
  translationText.textContent = loading && !entry?.translation ? 'Translating…' : entry?.translation || entry?.error || '';
  const expressions = safeExpressionNotes(entry?.expressions, state.segments[index]?.text);
  notes.replaceChildren();
  if (expressions.length) {
    notes.innerHTML = expressions.map(({ phrase, explanation }) =>
      `<div class="expression-note"><strong>${escapeHTML(phrase)}</strong><span>${escapeHTML(explanation)}</span></div>`
    ).join('');
    notes.hidden = false;
  } else if (entry?.translation && entry.expressions_ready !== true) {
    notes.textContent = loading ? 'Finding useful expressions…'
      : entry?.explanation_error || 'Expression notes are not ready.';
    notes.hidden = false;
  } else {
    notes.hidden = true;
  }
  retryButton.hidden = !(entry?.translation && entry.expressions_ready !== true && entry.explanation_error && !loading);
  panel.classList.toggle('translation-loading', loading);
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
  const current = translationEntry(index);
  if (current?.refreshRequired) { await refreshTranscriptForTranslation(index); return; }
  if (current?.translation && (current.expanded || current.expressions_ready === true || current.explanation_error)) {
    current.expanded = !current.expanded;
    updateTranslationUI(index);
    saveEpisodeTranslations();
    return;
  }
  await requestTranslation(index);
}

async function requestTranslation(index) {
  if (!state.episode || !state.segments[index]) return;
  const episodeId = idOf(state.episode);
  const requestKey = translationRequestKey(index, episodeId);
  if (state.translationRequests.has(requestKey)) return;
  const sourceFingerprint = state.segments[index].source_fingerprint;
  const current = translationEntry(index);
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
    state.translations[index] = {
      source_fingerprint: sourceFingerprint,
      translation: translated,
      expressions: safeExpressionNotes(result.expressions, state.segments[index]?.text),
      expressions_ready: result.expressions_ready === true,
      explanation_error: result.explanation_error || '',
      expanded: true,
    };
    saveEpisodeTranslations();
    refreshTranslationPrefetchProgress(episodeId, requestId);
  } catch (error) {
    if (requestId !== state.episodeRequestId) return;
    const refreshRequired = error.status === 409;
    const message = refreshRequired
      ? 'This transcript sentence changed. Refresh the transcript before translating it.'
      : error.message || 'Translation failed. Try again.';
    state.translations[index] = current?.translation
      ? { ...current, explanation_error: message, refreshRequired, expanded: true }
      : { source_fingerprint: sourceFingerprint, error: message, expanded: true, refreshRequired };
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
  if (state.episode && String(idOf(state.episode)) !== String(episodeId)) clearShadowSession();
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
    return `<article class="transcript-line" data-line="${index}" data-start="${start}" data-end="${end}"><div class="sentence-toolbar"><button type="button" class="line-time" data-play-line="${index}" aria-label="Play from ${fmtTime(start)}">${fmtTime(start)}</button><button type="button" class="translation-toggle" data-translation-line="${index}" aria-expanded="false" aria-controls="translation-${index}">Show Japanese</button><button class="line-loop" data-loop-line="${index}" aria-label="Repeat this sentence" aria-pressed="false">↻</button><button class="shadow-open" type="button" data-shadow-open="${index}" aria-label="Practice this sentence" aria-expanded="false" aria-controls="shadow-panel-${index}" title="Record this sentence">🎙</button></div><div class="line-text">${wordsHTML}</div><div id="translation-${index}" class="translation-content" aria-live="polite" hidden><div class="translation-ja">${translationHTML}</div><div class="expression-notes" hidden></div><button type="button" class="expression-retry" data-translation-retry="${index}" hidden>Retry expression notes</button></div><section class="shadow-panel" id="shadow-panel-${index}" aria-label="Shadow this sentence" hidden><div class="shadow-actions"><button type="button" class="shadow-record" data-shadow-record="${index}">Record</button><button type="button" class="shadow-stop" data-shadow-stop="${index}" hidden>Stop recording</button><button type="button" data-shadow-mode="original" data-shadow-index="${index}">Original</button><button type="button" data-shadow-mode="take" data-shadow-index="${index}" disabled>My recording</button><button type="button" data-shadow-mode="both" data-shadow-index="${index}" disabled>Play together</button></div><div class="shadow-offset"><span>Recording start</span><button type="button" data-shadow-offset="-1" data-shadow-index="${index}" aria-label="Start recording 0.1 seconds earlier" disabled>−0.1</button><span class="shadow-offset-value">+0.0 s</span><button type="button" data-shadow-offset="1" data-shadow-index="${index}" aria-label="Start recording 0.1 seconds later" disabled>+0.1</button></div><p class="shadow-status" role="status" aria-live="polite">Record this sentence along with the original.</p><p class="shadow-hint">Use headphones for best results. This take stays in memory until you close the player.</p></section></article>`;
  }).join('');
  state.segments.forEach((_, index) => updateTranslationUI(index));
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
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
    button.setAttribute('aria-label', active ? 'Stop repeating this sentence' : 'Repeat this sentence');
    button.title = active ? 'Stop repeating this sentence' : 'Repeat this sentence';
    button.closest('.transcript-line')?.classList.toggle('looping', active);
  });
  $('#repeat-button').classList.toggle('active', state.loopLine >= 0);
  $('#repeat-button').setAttribute('aria-pressed', String(state.loopLine >= 0));
  $('#repeat-button').setAttribute('aria-label', state.loopLine >= 0 ? 'Stop sentence repeat' : 'Repeat selected sentence');
  $('#repeat-button').title = state.loopLine >= 0 ? 'Stop sentence repeat' : 'Repeat selected sentence';
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
  if (state.shadowRecording && !state.shadowRecording.waitingForSpeechEnd
      && audio.currentTime >= state.shadowRecording.segment.end) {
    audio.pause();
    state.shadowRecording.waitingForSpeechEnd = true;
    updateShadowUI(state.shadowRecording.index);
  }
  if (state.shadowPlayback?.singlePass && !state.shadowPlayback.sourceEnded
      && audio.currentTime >= state.shadowPlayback.segment.end) {
    audio.pause();
    state.shadowPlayback.sourceEnded = true;
    if (state.shadowPlayback.mode === 'original') stopShadowPlayback(false);
    else if (state.shadowPlayback.takeEnded) stopShadowPlayback(false);
  }
  if (!state.shadowRecording && !state.shadowPlayback && state.loopLine >= 0) {
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
$('#player-back').addEventListener('click', () => { clearShadowSession(); state.episodeRequestId += 1; state.episodeLoading = false; $('#transcribe-button').disabled = false; clearInterval(state.pollTimer); state.pollTimer = null; clearInterval(state.translationPrefetchTimer); state.translationPrefetchTimer = null; audio.pause(); $('#player-view').hidden = true; $('#home-view').hidden = false; });
$('#transcribe-button').addEventListener('click', startTranscription);
$('#follow-playback').addEventListener('click', () => setPlaybackFollow(true));
$('#transcript-lines').addEventListener('click', (event) => {
  const shadowOpen = event.target.closest('[data-shadow-open]');
  if (shadowOpen) {
    const index = Number(shadowOpen.dataset.shadowOpen);
    const panel = document.querySelector(`#shadow-panel-${index}`);
    if (!panel.hidden && (state.shadowRecording?.index === index || state.shadowPending?.index === index)) {
      if (state.shadowPending?.index === index) { state.shadowPending.cancelled = true; state.shadowPending = null; }
      if (state.shadowRecording?.index === index) cancelShadowRecording();
    }
    panel.hidden = !panel.hidden;
    updateShadowUI(index);
    return;
  }
  const shadowRecord = event.target.closest('[data-shadow-record]');
  if (shadowRecord) { startShadowRecording(Number(shadowRecord.dataset.shadowRecord)); return; }
  const shadowStop = event.target.closest('[data-shadow-stop]');
  if (shadowStop) { finishShadowRecording(Number(shadowStop.dataset.shadowStop)); return; }
  const shadowMode = event.target.closest('[data-shadow-mode]');
  if (shadowMode) { playShadowMode(Number(shadowMode.dataset.shadowIndex), shadowMode.dataset.shadowMode); return; }
  const shadowOffset = event.target.closest('[data-shadow-offset]');
  if (shadowOffset) { adjustShadowOffset(Number(shadowOffset.dataset.shadowIndex), Number(shadowOffset.dataset.shadowOffset)); return; }
  const retry = event.target.closest('[data-translation-retry]');
  if (retry) { event.preventDefault(); event.stopPropagation(); requestTranslation(Number(retry.dataset.translationRetry)); return; }
  const translation = event.target.closest('[data-translation-line]');
  if (translation) { event.preventDefault(); event.stopPropagation(); toggleTranslation(Number(translation.dataset.translationLine)); return; }
  const seekLine = event.target.closest('[data-play-line]');
  if (seekLine) {
    if (state.shadowRecording) cancelShadowRecording();
    if (state.shadowPlayback) stopShadowPlayback(true);
    if (state.loopLine >= 0) { setLoop(-1); showToast('Sentence repeat turned off'); }
    activateLine(Number(seekLine.dataset.playLine), true); savePosition(true); return;
  }
  const loop = event.target.closest('[data-loop-line]');
  if (loop) { if (state.shadowRecording) cancelShadowRecording(); if (state.shadowPlayback) stopShadowPlayback(true); const index = Number(loop.dataset.loopLine); const enabling = state.loopLine !== index; setLoop(enabling ? index : -1); if (enabling) activateLine(index, true); return; }
  const word = event.target.closest('.word'); if (word) {
    if (state.shadowRecording) cancelShadowRecording();
    if (state.shadowPlayback) stopShadowPlayback(true);
    const index = Number(word.dataset.line);
    if (state.loopLine >= 0) { setLoop(-1); showToast('Sentence repeat turned off'); }
    activateLine(index, false);
    audio.currentTime = Number(word.dataset.start) || 0;
    audio.play().catch(() => {}); savePosition(true);
  }
});
$('#play-button').addEventListener('click', () => { if (state.shadowPlayback) { stopShadowPlayback(true); return; } if (audio.paused) audio.play().catch((error) => showToast(error.message)); else audio.pause(); });
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
$('#rewind-button').addEventListener('click', () => seekBy(-state.rewindSeconds));
$('#forward-button').addEventListener('click', () => seekBy(state.forwardSeconds));
updateSkipControls();
$('#rewind-seconds').value = String(state.rewindSeconds);
$('#forward-seconds').value = String(state.forwardSeconds);
$('#skip-settings-button').addEventListener('click', () => {
  const menu = $('#skip-settings-menu');
  menu.hidden = !menu.hidden;
  $('#skip-settings-button').setAttribute('aria-expanded', String(!menu.hidden));
  if (!menu.hidden) $('#rewind-seconds').focus();
});
$('#rewind-seconds').addEventListener('change', (event) => setSkipInterval('rewind', event.currentTarget.value));
$('#forward-seconds').addEventListener('change', (event) => setSkipInterval('forward', event.currentTarget.value));
document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('.player-settings-row') && !$('#skip-settings-menu').hidden) {
    $('#skip-settings-menu').hidden = true;
    $('#skip-settings-button').setAttribute('aria-expanded', 'false');
  }
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('#skip-settings-menu').hidden) {
    $('#skip-settings-menu').hidden = true;
    $('#skip-settings-button').setAttribute('aria-expanded', 'false');
    $('#skip-settings-button').focus();
  }
});
$('#repeat-button').addEventListener('click', () => {
  if (state.shadowRecording) cancelShadowRecording();
  if (state.shadowPlayback) stopShadowPlayback(true);
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
audio.addEventListener('ended', () => { savePosition(true); if (state.shadowRecording) { audio.pause(); state.shadowRecording.waitingForSpeechEnd = true; updateShadowUI(state.shadowRecording.index); } else if (state.shadowPlayback) { state.shadowPlayback.sourceEnded = true; if (state.shadowPlayback.mode === 'original' || state.shadowPlayback.takeEnded) stopShadowPlayback(false); } else if (state.loopLine >= 0) { audio.currentTime = Number(state.segments[state.loopLine]?.start) || 0; audio.play().catch(() => {}); } else stopVisualUpdates(); });
shadowTakeAudio.addEventListener('ended', () => {
  const playback = state.shadowPlayback;
  if (!playback) return;
  if (playback.mode === 'both') {
    playback.takeEnded = true;
    if (playback.delayedElement === shadowTakeAudio && playback.delayPending) return;
    if (playback.sourceEnded) stopShadowPlayback(false);
    return;
  }
  stopShadowPlayback(false);
});
window.addEventListener('pagehide', () => { savePosition(true); clearShadowSession(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') { savePosition(true); clearShadowSession(); } });

if (navigator.mediaSession) {
  const actions = {
    play: () => audio.play().catch(() => {}),
    pause: () => audio.pause(),
    seekbackward: (detail = {}) => seekBy(-(Number.isFinite(detail.seekOffset) ? Math.abs(detail.seekOffset) : state.rewindSeconds)),
    seekforward: (detail = {}) => seekBy(Number.isFinite(detail.seekOffset) ? Math.abs(detail.seekOffset) : state.forwardSeconds),
    seekto: (detail) => { if (Number.isFinite(detail.seekTime)) { if (typeof audio.fastSeek === 'function') audio.fastSeek(detail.seekTime); else audio.currentTime = detail.seekTime; savePosition(true); } },
  };
  for (const [action, handler] of Object.entries(actions)) { try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* Action is unsupported by this browser. */ } }
}

loadFeeds().catch((error) => { $('#empty-library').hidden = false; $('#empty-library').querySelector('p').textContent = `Could not connect to the API: ${error.message}`; });
