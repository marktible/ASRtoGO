'use strict';
const $ = id => document.getElementById(id);
let viewedJob = null, viewedKind = 'result';
let csrf = '', activeJob = null, selectedTab = 'translate', textMode = 'text', chosenText = null, asrFile = null, comboFile = null, ticker = null, activeModels = {llm: [], asr: []}, serverState = {llm: 'offline', asr: 'offline'}, serverOwned = {llm: false, asr: false};
const visible = (id, yes) => {$(id).hidden = !yes;};

function readPreference(key) {
  try { return localStorage.getItem('asrtogo.' + key); } catch (_) { return null; }
}
function savePreference(key, value) {
  try { localStorage.setItem('asrtogo.' + key, value); } catch (_) {}
}

const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
let theme = readPreference('theme');
if (theme !== 'light' && theme !== 'dark') theme = 'system';
function applyTheme() {
  const root = document.documentElement;
  root.dataset.theme = theme === 'system' ? (systemDark.matches ? 'dark' : 'light') : theme;
  document.querySelector('meta[name="theme-color"]').content = getComputedStyle(root).getPropertyValue('--canvas').trim();
}
applyTheme();
systemDark.addEventListener('change', applyTheme);

async function api(path, method = 'GET', data) {
  const headers = {};
  if (method !== 'GET') { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = csrf; }
  const response = await fetch(path, {method, headers, credentials: 'same-origin',
     body: method !== 'GET' ? JSON.stringify(data || {}) : undefined});
  let json = {};
  try {json = await response.json();} catch (_) {}
  if (!response.ok) throw new Error(json.error || `HTTP ${response.status}`);
  return json;
}
function fail(message) { $('workspace-error').textContent = message; visible('workspace-error', !!message); }
function setTab(name) {
  selectedTab = name; fail('');
  for (const key of ['translate','asr','asr_translate']) {
    visible('pane-'+key, key === name);
    const btn = $('tab-'+(key === 'asr_translate' ? 'asr-translate' : key));
    btn.classList.toggle('active', key === name); btn.setAttribute('aria-selected', String(key === name));
  }
}
function setTextMode(mode) {
  textMode = mode; visible('text-panel', mode === 'text'); visible('file-panel', mode === 'file');
  $('text-mode').classList.toggle('active',mode === 'text');
  $('file-mode').classList.toggle('active',mode === 'file');
}
function fileSelected(inputId, labelId, file) {
  if (inputId === 'text-file') chosenText = file;
  if (inputId === 'asr-file') asrFile = file;
  if (inputId === 'combo-file') comboFile = file;
  if (file) {
    $(labelId).textContent = `${file.name} · ${(file.size/1048576).toFixed(2)} MB`;
    visible(labelId,true);
  } else visible(labelId,false);
}
function setupDrop(zone, input, label) {
  $(input).addEventListener('change', e => fileSelected(input,label,e.target.files[0]));
  $(zone).addEventListener('dragover', e => {e.preventDefault(); $(zone).classList.add('dragging');});
  $(zone).addEventListener('dragleave', () => $(zone).classList.remove('dragging'));
  $(zone).addEventListener('drop', e => {
    e.preventDefault(); $(zone).classList.remove('dragging');
    if (e.dataTransfer.files.length) fileSelected(input,label,e.dataTransfer.files[0]);
  });
}
function modelsSelect(id, records) {
  const s = $(id), previous = readPreference(id) || s.value;
  s.replaceChildren();
  if (!records.length) {
    s.append(new Option('Add a model to models/' + (id.includes('llm') ? 'translation/' : 'asr/') + ' and refresh Settings.', ''));
    return;
  }
  for (const entry of records) {
    s.append(new Option(`${entry.name} · ${Math.round(entry.size_mb)} MB`, entry.id));
  }
  if (records.some(r => r.id === previous)) s.value = previous;
  s.onchange = () => savePreference(id, s.value);
}
async function loadModels() {
  const data = await api('/api/models');
  activeModels = data;
  modelsSelect('settings-llm-model', data.llm);
  modelsSelect('settings-asr-model', data.asr);
}
function currentModel(which) {
  return which === 'llm' ? $('settings-llm-model').value : $('settings-asr-model').value;
}
async function refreshStatus() {
  try {
    const status = await api('/api/status');
    for (const k of ['llm','asr']) {
      serverState[k] = status[k];
      serverOwned[k] = !!status[k+'_owned'];
      const ready = status[k] === 'ready';
      const loading = status[k] === 'loading';
      const label = $(k+'-state-label');
      label.textContent = ready ? (serverOwned[k] ? 'Running' : 'Running externally') : loading ? 'Starting…' : 'Not running';
      const toggle = $('toggle-'+k);
      toggle.classList.toggle('is-on',ready || loading);
      toggle.textContent = ready || loading ? 'Stop' : 'Start';
      toggle.disabled = (ready || loading) && !serverOwned[k];
      toggle.title = toggle.disabled ? 'The server was started outside this web app.' : '';
    }
    if ($('dropdown').open) await refreshMaintenance();
    if (status.active_job && !activeJob) watchJob(status.active_job);
  } catch (e) { console.warn(e); }
}
function setLogin(yes, token='') {
  csrf = yes ? token : '';
  visible('login-panel',!yes); visible('workspace',yes); visible('authorized-actions',yes);
  if (yes) {loadModels().catch(e=>fail(e.message)); refreshStatus(); refreshJobs();}
  else {
    activeJob = null; clearInterval(ticker); ticker = null;
    $('result-audio').pause(); $('result-audio').removeAttribute('src'); $('result-audio').load();
    visible('result-section', false);
  }
}
function startProgress(title) {
  $('jobs-section').open = true;
  visible('result-section',false); visible('progress-section',true);
  $('progress-heading').textContent = title;
  $('progress-message').textContent = 'Preparing...';
  $('progress-pct').textContent = '…';
  $('progress-fill').style.width = '0%';
  $('progress-fill').classList.add('is-indeterminate');
  fail('');
}
function watchJob(id) {
  activeJob = id;
  if (ticker) clearInterval(ticker);
  visible('progress-section',true);
  ticker = setInterval(async () => {
    try {
      const j = await api('/api/job/'+activeJob);
      $('progress-message').textContent = j.message || j.status;
      if (j.total) {
        const pct = Math.min(100, Math.round((j.done / j.total) * 100));
        $('progress-pct').textContent = pct+'%';
        $('progress-fill').classList.remove('is-indeterminate');
        $('progress-fill').style.width = pct+'%';
      } else {
        $('progress-pct').textContent = '…';
        $('progress-fill').classList.add('is-indeterminate');
      }
      if (['done','error','cancelled'].includes(j.status)) {
        clearInterval(ticker); ticker = null;
        const finishedId = activeJob; activeJob = null;
        visible('progress-section', false);
        if (j.status !== 'done') fail(j.message + (j.has_transcript ? ' You can download the original transcript from Jobs below.' : ''));
        else {
          showResult(j).catch(e => fail(e.message));
        }
        refreshStatus(); refreshJobs();
      }
    } catch (e) {clearInterval(ticker); ticker=null; activeJob=null; fail(e.message);}
  }, 900);
}
async function translate() {
  try {
    let text, name = 'text.txt', kind = 'txt';
    if (textMode === 'file') {
      if (!chosenText) throw new Error('Select a text or subtitle file first.');
      if (chosenText.size > 10*1048576) throw new Error('The text file must be smaller than 10 MB.');
      name = chosenText.name;
      kind = (name.split('.').pop()||'').toLowerCase();
      if (!['txt','srt','vtt'].includes(kind)) throw new Error('Use .txt, .srt, or .vtt files.');
      text = await chosenText.text();
    } else text = $('source-text').value;
    if (!text.trim()) throw new Error('Enter some text to translate.');
    if (!$('settings-llm-model').value) throw new Error('No translation model available.');
    const body = {text, filename:name, kind, source:$('source-lang').value,
                  target:$('target-lang').value, llm_model:$('settings-llm-model').value};
    startProgress('Translating text');
    const job = await api('/api/translate','POST',body);
    watchJob(job.id); refreshJobs();
  } catch(e) {visible('progress-section',false); fail(e.message);}
}
async function upload(mode) {
  try {
    const combo = mode === 'asr_translate';
    const file = combo ? comboFile : asrFile;
    if (!file) throw new Error('Select an audio or video file first.');
    if (file.size === 0 || file.size > 100*1048576) throw new Error('Upload must be under 100 MB.');
    const format = $(combo ? 'combo-format' : 'asr-format').value;
    const source = combo ? $('combo-source').value : 'ja';
    const target = combo ? $('combo-target').value : 'en';
    const lang = combo ? source : $('asr-language').value;
    const asrModel = $('settings-asr-model').value;
    if (!asrModel) throw new Error('No CrispASR model available.');
    const llmModel = combo ? $('settings-llm-model').value : '';
    if (combo && !llmModel) throw new Error('No translation model available.');
    if (combo && source === target) throw new Error('Select different source and target languages.');
    startProgress(combo ? 'Transcribing and translating' : 'Transcribing audio');
    $('progress-message').textContent = 'Uploading audio to your PC...';
    const query = new URLSearchParams({mode,format,source,target,language:lang,
                 asr_model:asrModel, ...(combo ? {llm_model:llmModel, auto_unload:$('combo-auto-unload').checked} : {})});
    const response = await fetch('/api/upload?'+query, {method:'POST',credentials:'same-origin',
        headers:{'Content-Type':'application/octet-stream','X-Upload-Name':encodeURIComponent(file.name),
                 'X-CSRF-Token':csrf},body:file});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Upload failed: ${response.status}`);
    watchJob(data.id); refreshJobs();
  } catch(e) {visible('progress-section',false); fail(e.message);}
}
async function showResult(job, requestedKind) {
  // A job's result is stored on the PC: fetch the saved text instead of the
  // older, limited preview embedded in job status JSON.
  const id = job.id;
  viewedJob = job;
  const haveTranslated = job.status === 'done' || job.has_partial;
  let kind = requestedKind || (job.status === 'done' ? 'result' : job.has_partial ? 'partial' : 'transcript');
  if (kind === 'result' && job.status !== 'done') kind = job.has_partial ? 'partial' : 'transcript';
  const resultSection = $('result-section');
  visible('result-section', true);
  resultSection.open = true;
  $('result-subtitle').textContent = job.source_name || job.filename || 'Saved result';
  $('audio-filename').textContent = job.source_name || '';
  const audio = $('result-audio');
  if (job.has_audio) {
    const src = '/api/audio/' + id;
    if (!audio.getAttribute('src') || !audio.getAttribute('src').endsWith(src)) {
      audio.pause(); audio.src = src; audio.load();
    }
    visible('audio-panel', true);
  } else {
    audio.pause(); audio.removeAttribute('src'); audio.load();
    visible('audio-panel', false);
  }
  const combined = job.mode === 'asr_translate' && job.has_transcript;
  visible('result-views',combined);
  $('view-translation').disabled = !haveTranslated;
  $('view-transcript').disabled = !job.has_transcript;
  await selectResultKind(kind, false);
  $('result-section').scrollIntoView({behavior:'smooth',block:'start'});
}
async function selectResultKind(kind, scroll = false) {
  if (!viewedJob) return;
  const id = viewedJob.id;
  let response;
  try {response = await api('/api/preview/' + id + '?kind=' + encodeURIComponent(kind));}
  catch(e) {
    if (viewedJob.has_audio && !viewedJob.has_transcript && !viewedJob.has_partial && viewedJob.status !== 'done') {
      $('output-preview').textContent = 'Audio is saved, but transcription is not available yet.';
      $('result-heading').textContent = 'Audio';
      $('download-btn').hidden = true;
      return;
    }
    throw e;
  }
  $('download-btn').hidden = false;
  if (viewedJob?.id !== id) return;
  viewedKind = kind;
  $('output-preview').textContent = response.text || '(Empty result)';
  $('result-heading').textContent = kind === 'transcript' ? 'Original transcription' :
      kind === 'partial' ? 'Partial translation' : viewedJob.mode === 'asr' ? 'Transcription' : 'Translation';
  $('download-btn').onclick = () => window.location.assign('/api/download/'+id + (kind === 'result' ? '' : '?kind='+kind));
  $('view-translation').classList.toggle('active',kind !== 'transcript');
  $('view-transcript').classList.toggle('active',kind === 'transcript');
  $('preview-note').textContent = response.truncated ? 'This preview is shortened for performance. Download to get the complete file.' : '';
  visible('preview-note',!!response.truncated);
  if (scroll) $('output-preview').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function seekAudio(seconds) {
  const player = $('result-audio');
  if (!player.getAttribute('src')) return;
  const now = Number.isFinite(player.currentTime) ? player.currentTime : 0;
  const duration = Number.isFinite(player.duration) ? player.duration : Infinity;
  try {player.currentTime = Math.max(0,Math.min(duration,now + seconds));} catch (_) {}
}
function appendButton(container, label, style, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = style;
  button.textContent = label;
  button.onclick = onClick;
  container.append(button);
}
async function refreshJobs() {
  if (!csrf) return;
  try {
    const response = await api('/api/jobs');
    const list = $('jobs-list');
    $('jobs-count').textContent = response.jobs.length ? `(${response.jobs.length})` : '';
    list.replaceChildren();
    if (!response.jobs.length) {
      const blank = document.createElement('p');
      blank.className = 'jobs-empty';
      blank.textContent = 'No jobs yet. Paste or upload something to get started.';
      list.append(blank);
      return;
    }
    for (const job of response.jobs) {
      const row = document.createElement('div'); row.className = 'job-row';
      const info = document.createElement('div');
      const name = document.createElement('div'); name.className = 'job-name';
      name.textContent = job.source_name || job.filename;
      const sub = document.createElement('div'); sub.className = 'job-sub';
      const when = job.created_at ? new Date(job.created_at * 1000).toLocaleString() : '';
      let progress = job.total ? ` · ${job.done}/${job.total}` : '';
      const niceMode = {translate:'Translate',asr:'ASR',asr_translate:'ASR + Translate',archive:'Saved output'}[job.mode] || job.mode;
      const stateText = {queued:'Queued',loading:'Starting',running:'Working',uploading:'Uploading',done:'Ready',error:'Failed',cancelled:'Cancelled'}[job.status] || job.status;
      sub.textContent = `${niceMode} · ${stateText}${progress} · ${when}`;
      if (job.status === 'error') {
        sub.classList.add('is-error');
        sub.textContent += ` · ${job.message || ''}`;
      }
      info.append(name, sub);
      const actions = document.createElement('div'); actions.className = 'job-actions';
      if (job.status === 'done') {
        appendButton(actions, '↓ Download', 'job-download', () => window.location.assign('/api/download/' + job.id));
        appendButton(actions, 'View result', '', async () => {
          try { await showResult(await api('/api/job/' + job.id)); }
          catch(e) {fail(e.message);}
        });
      }
      if (job.has_audio && job.status !== 'done') appendButton(actions, '♫ Listen', '', async () => {
        try {await showResult(await api('/api/job/'+job.id), job.has_transcript ? 'transcript' : 'result');}
        catch(e) {fail(e.message);}
      });
      if (job.has_transcript) appendButton(actions, '↓ Original transcript', 'job-download',
        () => window.location.assign('/api/download/'+job.id+'?kind=transcript'));
      if (job.has_partial) appendButton(actions, '↓ Partial translation', 'job-download',
        () => window.location.assign('/api/download/'+job.id+'?kind=partial'));
      if (['queued','loading','running','uploading'].includes(job.status)) {
        appendButton(actions,'View status','',()=>{startProgress('Processing ' + (job.source_name || 'job'));watchJob(job.id);});
        appendButton(actions,'Cancel','job-cancel',async()=>{
          if (!confirm('Cancel this job?')) return;
          try {await api('/api/cancel','POST',{id:job.id});refreshJobs();}
          catch(e) {fail(e.message);}
        });
      } else {
        appendButton(actions, 'Delete', 'job-delete', async () => {
          if (!confirm('Delete this saved result and its job record from your PC?')) return;
          try {
            await api('/api/delete','POST',{id:job.id});
            if(viewedJob?.id === job.id) {viewedJob = null;$('result-audio').pause();$('result-audio').removeAttribute('src');visible('result-section',false);}
            refreshJobs();
          }
          catch(e) {fail(e.message);}
        });
      }
      row.append(info, actions); list.append(row);
    }
  } catch(e) {console.warn('Jobs list unavailable:',e);}
}
function selectPastedFile(file) {
  const ext = file.name.split('.').pop().toLowerCase();
  const text = ['txt','srt','vtt'].includes(ext);
  const media = file.type.startsWith('audio/') || file.type.startsWith('video/') ||
    ['mp3','wav','flac','ogg','opus','m4a','aac','webm','mp4','mkv','mov'].includes(ext);
  if (text) {
    setTab('translate');setTextMode('file');fileSelected('text-file','text-selected',file);
  } else if (media) {
    if (selectedTab !== 'asr_translate') setTab('asr');
    if (selectedTab === 'asr_translate') fileSelected('combo-file','combo-selected',file);
    else fileSelected('asr-file','asr-selected',file);
  } else throw new Error('Paste a TXT/SRT/VTT file or supported audio/video file.');
  fail('');
}
function acceptPastedText(text) {
  if (!text.trim()) return;
  setTab('translate');setTextMode('text');$('source-text').value=text;
  $('source-text').dispatchEvent(new Event('input'));
}
function bindClipboard() {
  document.addEventListener('paste', e => {
    if (!csrf) return;
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) {
      e.preventDefault();
      try {selectPastedFile(files[0]);} catch(err) {fail(err.message);}
    } else if (!e.target.closest('input,textarea,[contenteditable]')) {
      const text = e.clipboardData?.getData('text/plain');
      if (text) {e.preventDefault();acceptPastedText(text);}
    }
  });
  $('paste-clipboard').onclick = async () => {
    if (!navigator.clipboard) return fail('Clipboard API is unavailable on this browser. Long-press Paste for text or use the file picker for audio.');
    try {
      if (navigator.clipboard.read) {
        const items = await navigator.clipboard.read();
        for (const item of items) {
          const kind = item.types.find(t => t.startsWith('audio/') || t.startsWith('video/') || t === 'application/octet-stream');
          if (kind) {
            const blob = await item.getType(kind);
            const types = {'audio/mpeg':'mp3','audio/wav':'wav','audio/x-wav':'wav','audio/ogg':'ogg','audio/flac':'flac','audio/mp4':'m4a','video/mp4':'mp4','video/webm':'webm'};
            const ext = types[kind] || kind.split('/')[1] || 'wav';
            selectPastedFile(new File([blob], 'clipboard.' + ext, {type:kind}));return;
          }
          if (item.types.includes('text/plain')) {
            const blob = await item.getType('text/plain');
            acceptPastedText(await blob.text());return;
          }
        }
      }
      if (navigator.clipboard.readText) {acceptPastedText(await navigator.clipboard.readText());return;}
      fail('No compatible text or audio found on your clipboard.');
    } catch(e) {fail('Clipboard access blocked. Use Ctrl+V for files or the upload picker on mobile. ' + e.message);}
  };
}
function swap(a,b) {const t = $(a).value; $(a).value = $(b).value; $(b).value = t;}
function closeShutdown() {visible('shutdown-modal',false); $('confirm-password').value=''; $('confirm-text').value='';}
async function refreshMaintenance() {
  const info = await api('/api/maintenance');
  const desktop = $('toggle-desktop');
  const running = info.desktop === 'active';
  desktop.dataset.action = running ? 'stop' : 'start';
  desktop.textContent = running ? 'Stop desktop' : 'Restore desktop';
  desktop.disabled = !['active','inactive','failed'].includes(info.desktop);
  $('desktop-state').textContent = running ? 'Graphical desktop running' : 'Graphical desktop';
  $('desktop-note').textContent = info.desktop === 'unavailable' ? 'A systemd display manager is required.' :
    info.desktop === 'inactive' ? 'Desktop stopped.' : '';
  $('update-backends').disabled = info.update === 'running';
  $('update-status').textContent = {running:'Updating backends…', done:'Last update completed.',
    failed:'Update failed. See internal/data/backend-update.log on the PC.'}[info.update] || '';
}
function bindMenu() {
  const dialog = $('dropdown');
  const settingsError = message => { $('settings-error').textContent = message; };
  $('menu-button').onclick = () => {
    settingsError('');
    dialog.showModal();
    refreshStatus();
  };
  $('settings-close').onclick = () => dialog.close();
  $('toggle-desktop').onclick = async () => {
    const button = $('toggle-desktop');
    const action = button.dataset.action;
    if (action === 'stop' && !confirm('Stop the graphical desktop and close graphical apps for all users? Save your work first. To keep the studio online, launch it from SSH or a Linux text console.')) return;
    button.disabled = true;
    settingsError('');
    try { await api('/api/desktop','POST',{action}); }
    catch (e) { settingsError(e.message); }
    await refreshMaintenance().catch(e => settingsError(e.message));
  };
  $('update-backends').onclick = async () => {
    if (!confirm('Unload idle models, update llama.cpp and CrispASR, and rebuild both backends?')) return;
    $('update-backends').disabled = true;
    settingsError('');
    try { await api('/api/update-backends','POST'); }
    catch (e) { settingsError(e.message); }
    await refreshMaintenance().catch(e => settingsError(e.message));
  };
  for (const which of ['llm','asr']) {
    $('toggle-'+which).onclick = async () => {
      const action = serverState[which] === 'offline' ? 'start' : 'stop';
      if (action === 'stop' && !confirm(`Stop ${which === 'llm' ? 'translation' : 'ASR'} server?`)) return;
      $('toggle-'+which).disabled = true;
      try {
        await api('/api/'+action,'POST',{which,model:currentModel(which)});
        await refreshStatus();
      } catch (e) {settingsError(e.message);await refreshStatus();}
    };
  }
  $('stop-web').onclick = async () => {
    dialog.close();
    if (!confirm('Stop the website? You can restart it later by running bash start.sh on your PC.')) return;
    try {await api('/api/stopweb','POST'); fail('Website stopped. Restart it with bash start.sh on your PC.');}
    catch(e){fail(e.message);}
  };
  $('logout').onclick = async () => {dialog.close();try{await api('/api/logout','POST');}catch(_){ } setLogin(false);};
  $('shutdown-open').onclick = () => {dialog.close();visible('shutdown-modal',true);$('confirm-text').focus();};
  $('cancel-shutdown').onclick = closeShutdown;
  $('modal-backdrop').onclick = closeShutdown;
  $('confirm-shutdown').onclick = async () => {
    $('shutdown-error').textContent='';
    if ($('confirm-text').value!=='SHUTDOWN') { $('shutdown-error').textContent='Type SHUTDOWN exactly.'; return; }
    if (!$('confirm-password').value) { $('shutdown-error').textContent='Re-enter your web password.'; return; }
    try {const j=await api('/api/poweroff','POST',{confirmation:'SHUTDOWN',password:$('confirm-password').value});$('shutdown-error').textContent=j.message;}
    catch(e){$('shutdown-error').textContent=e.message;}
  };
}
async function initialize() {
  $('theme').value = theme;
  $('theme').onchange = () => {
    theme = $('theme').value;
    applyTheme();
    savePreference('theme', theme);
  };
  document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => setTab(b.dataset.tab));
  $('text-mode').onclick = () => setTextMode('text'); $('file-mode').onclick = () => setTextMode('file');
  setupDrop('text-drop','text-file','text-selected');
  setupDrop('audio-drop','asr-file','asr-selected');
  setupDrop('combo-drop','combo-file','combo-selected');
  const autoUnload = $('combo-auto-unload');
  autoUnload.checked = readPreference('autoUnload') === 'true';
  autoUnload.onchange = () => savePreference('autoUnload', String(autoUnload.checked));
  $('source-text').oninput = () => {$('character-count').textContent = $('source-text').value.length.toLocaleString()+' characters';};
  $('swap-lang').onclick = () => swap('source-lang','target-lang');
  $('combo-swap').onclick = () => swap('combo-source','combo-target');
  $('source-lang').onchange = () => { $('target-lang').value = $('source-lang').value==='ja' ? 'en' : 'ja';};
  $('target-lang').onchange = () => { $('source-lang').value = $('target-lang').value==='ja' ? 'en' : 'ja';};
  $('combo-source').onchange = () => { $('combo-target').value = $('combo-source').value==='ja' ? 'en' : 'ja';};
  $('combo-target').onchange = () => { $('combo-source').value = $('combo-target').value==='ja' ? 'en' : 'ja';};
  $('translate-btn').onclick = translate;
  $('asr-btn').onclick = () => upload('asr');
  $('combo-btn').onclick = () => upload('asr_translate');
  $('cancel-btn').onclick = async () => {try{await api('/api/cancel','POST',{id:activeJob});refreshJobs();}catch(e){fail(e.message);}};
  $('refresh-jobs').onclick = refreshJobs;
  $('view-translation').onclick = () => selectResultKind(viewedJob?.status === 'done' ? 'result' : 'partial').catch(e=>fail(e.message));
  $('view-transcript').onclick = () => selectResultKind('transcript').catch(e=>fail(e.message));
  $('skip-back').onclick = () => seekAudio(-10);
  $('skip-forward').onclick = () => seekAudio(10);
  bindClipboard();
  $('login-form').onsubmit = async e => {
    e.preventDefault();$('login-error').textContent='';
    try {const info=await api('/api/login','POST',{password:$('login-password').value});$('login-password').value='';setLogin(true,info.csrf);}
    catch(err){$('login-error').textContent=err.message;}
  };
  bindMenu();
  try {const info=await api('/api/session');setLogin(info.authenticated,info.csrf);}
  catch(e){$('login-error').textContent=e.message;}
  setInterval(() => {if (csrf) {refreshStatus();refreshJobs();}},7000);
}
document.addEventListener('DOMContentLoaded', initialize);
