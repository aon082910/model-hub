import * as THREE from '/assets/vendor/three.module.min.js';
import { STLLoader } from '/assets/vendor/STLLoader.js';
import { OBJLoader } from '/assets/vendor/OBJLoader.js';
import { ThreeMFLoader } from '/assets/vendor/3MFLoader.js';
import { FBXLoader } from '/assets/vendor/FBXLoader.js';
import { OrbitControls } from '/assets/vendor/OrbitControls.js';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const DEFAULT_VIEWER_MODEL_COLOR = '#c9ced6';
let viewerModelColor = DEFAULT_VIEWER_MODEL_COLOR;
let viewerSessionColor = DEFAULT_VIEWER_MODEL_COLOR;

function normalizeViewerModelColor(value) {
  return typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value)
    ? value
    : DEFAULT_VIEWER_MODEL_COLOR;
}

function createViewerMaterial() {
  const material = new THREE.MeshStandardMaterial({ color: viewerSessionColor });
  material.userData.modelHubPreviewMaterial = true;
  return material;
}

function applyViewerSessionColor() {
  if (!scene) return;
  scene.traverse((object) => {
    if (!object.isMesh || !object.material) return;
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) {
      if (material?.userData?.modelHubPreviewMaterial && material.color) {
        material.color.set(viewerSessionColor);
        material.needsUpdate = true;
      }
    }
  });
}

function ensureViewerColorControl() {
  let control = $('#viewer-color-control');
  if (!control) {
    control = document.createElement('div');
    control.id = 'viewer-color-control';
    control.className = 'row';
    control.style.cssText = 'margin-top:8px;align-items:center;';
    control.innerHTML = `
      <label style="display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px" title="Temporary for this preview; the next preview uses the saved default color.">
        Preview color
        <input id="viewer-live-color" type="color" value="${DEFAULT_VIEWER_MODEL_COLOR}" aria-label="Temporary preview color">
      </label>`;
    $('#viewer-canvas-wrap').insertAdjacentElement('afterend', control);
  }

  const picker = $('#viewer-live-color');
  picker.value = viewerSessionColor;
  picker.oninput = () => {
    viewerSessionColor = normalizeViewerModelColor(picker.value);
    applyViewerSessionColor();
  };
}

// ---------- Tabs and pages ----------
// Hash routing, so every page has an address that survives a reload and works with
// the back button: #/library, #/projects, #/supplies ... and #/model/12, #/project/3
const TAB_LOADERS = {
  library: () => loadModels(), search: () => openSearchPage(''), wishlist: () => loadWishlist(), duplicates: () => loadDuplicates(), following: () => loadFollowing(), stats: () => loadStats(), activity: () => loadActivity(),
  collections: () => { loadRules(); return loadCollections(); }, projects: () => loadProjects(),
  supplies: () => loadSupplies(), matches: () => loadMatches(), filament: () => loadFilament(),
  queue: () => loadQueue(), orders: () => loadOrders(), creators: () => loadCreators(''), calendar: () => loadCalendar(), settings: () => loadSettings(),
};

function showSection(sectionId, activeTab) {
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === activeTab));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === sectionId));
}

let routeToken = 0;   // a page that finishes loading after you moved on must not draw itself
function route() {
  const hash = location.hash || '#/library';
  const token = ++routeToken;
  disposeViewer();
  window.scrollTo(0, 0);
  const search = hash.match(/^#\/search(?:\/(.*))?$/);
  if (search) {
    showSection('tab-search', 'search');
    return openSearchPage(search[1] ? decodeURIComponent(search[1]) : '');
  }
  const labels = hash.match(/^#\/labels\/(filament|supplies)$/);
  if (labels) {
    showSection('tab-labels', labels[1]);
    return loadLabels(labels[1]);
  }
  const scanned = hash.match(/^#\/(spool|supply)\/(\d+)$/);
  if (scanned) {
    const spool = scanned[1] === 'spool';
    showSection(spool ? 'tab-filament' : 'tab-supplies', spool ? 'filament' : 'supplies');
    return (spool ? loadFilament() : loadSupplies()).then(() => highlightRow(spool ? '#filament-table' : '#supplies-table', scanned[2]));
  }
  const listing = hash.match(/^#\/listing\/([a-z0-9]+)\/([A-Za-z0-9_-]+)$/);
  if (listing) {
    showSection('page-listing', 'search');
    return openListingPage(listing[1], listing[2], token);
  }
  const creator = hash.match(/^#\/creators(?:\/(.*))?$/);
  if (creator) {
    showSection('tab-creators', 'creators');
    return loadCreators(creator[1] ? decodeURIComponent(creator[1]) : '');
  }
  const detail = hash.match(/^#\/(model|project)\/(\d+)$/);
  if (detail && detail[1] === 'model') {
    showSection('page-model', 'library');
    return openModelPage(parseInt(detail[2]), token);
  }
  if (detail) {
    showSection('page-project', 'projects');
    return openProjectPage(parseInt(detail[2]), token);
  }
  const tab = hash.replace(/^#\//, '');
  const name = TAB_LOADERS[tab] ? tab : 'library';
  document.title = 'Model Hub';
  showSection(`tab-${name}`, name);
  return TAB_LOADERS[name]();
}
window.addEventListener('hashchange', route);
$$('#tabs button').forEach(btn => btn.addEventListener('click', () => {
  const target = `#/${btn.dataset.tab}`;
  if (location.hash === target) route(); else location.hash = target;
}));

// ---------- Library ----------
async function loadModels() {
  fillFitPrinters();
  const params = libraryParams();
  const res = await fetch(`/api/library/models?${params}`);
  if (!res.ok) {
    $('#f-status').textContent = await sourceErrorText(res);
    renderGrid([]);
    return;
  }
  $('#f-status').textContent = '';
  libraryTotal = parseInt(res.headers.get('X-Total-Count') || '0') || 0;
  const [models] = await Promise.all([res.json(), loadFavorites()]);
  renderGrid(models);
}

// ---------- Sliced files kept with a model ----------
function formatMinutes(m) {
  if (m == null) return '';
  const h = Math.floor(m / 60), min = Math.round(m % 60);
  return h ? `${h} h ${min} min` : `${min} min`;
}

async function renderSlicedFiles(model) {
  const panel = $('#model-sliced-panel');
  const [files, printerInfo] = await Promise.all([
    fetch(`/api/print-files?model_id=${model.id}`).then(r => (r.ok ? r.json() : [])),
    currentUser.role === 'admin' ? fetch('/api/printers').then(r => (r.ok ? r.json() : { printers: [] })) : Promise.resolve({ printers: [] }),
  ]);
  if (!currentModel || currentModel.id !== model.id) return;
  const printers = printerInfo.printers || [];
  panel.innerHTML = `
    <h3>Sliced files</h3>
    <p class="muted">G-code or a sliced .3mf you made for this model, kept here with what the slicer said about it. Not part of a backup (they can be big).</p>
    ${files.length ? files.map(f => `
      <div class="sliced-row" data-id="${f.id}">
        <b>${esc(f.filename)}</b> <span class="muted">${formatBytes(f.size_bytes)}</span>
        <div class="muted">${[f.slicer, f.est_minutes != null ? formatMinutes(f.est_minutes) : '', f.est_grams != null ? `${f.est_grams} g` : '', f.filament_type, f.layer_height ? `${f.layer_height} mm layers` : ''].filter(Boolean).map(esc).join(' &middot; ') || 'no details found in the file'}</div>
        ${f.notes ? `<div>${esc(f.notes)}</div>` : ''}
        ${f.file_exists ? '' : '<div class="warn-text">The file is missing from this server (it is not in backups). Upload it again.</div>'}
        <div class="row">
          <a class="button-link" href="/api/print-files/${f.id}/download">Download</a>
          ${f.sendable && f.file_exists && printers.length ? `<select class="sliced-printer">${printers.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
            <label class="inline-check"><input type="checkbox" class="sliced-start"> start</label><button class="sliced-send">Send to printer</button>` : ''}
          <button class="sliced-delete">Delete</button>
        </div>
      </div>`).join('') : '<p class="muted">None yet.</p>'}
    <div class="row">
      <input id="sliced-file" type="file" accept=".gcode,.gco,.g,.bgcode,.3mf">
      <input id="sliced-notes" placeholder="Note (optional)">
      <button id="sliced-upload" class="primary">Keep this file</button><span id="sliced-status" class="muted"></span>
    </div>`;
  $('#sliced-upload').onclick = async () => {
    const file = $('#sliced-file').files[0];
    if (!file) { $('#sliced-status').textContent = 'Choose a file first.'; return; }
    $('#sliced-status').textContent = 'Uploading...';
    const form = new FormData();
    form.append('model_id', String(model.id));
    form.append('file', file);
    if ($('#sliced-notes').value) form.append('notes', $('#sliced-notes').value);
    const res = await fetch('/api/print-files', { method: 'POST', body: form });
    if (res.ok) renderSlicedFiles(model); else $('#sliced-status').textContent = await sourceErrorText(res);
  };
  $$('#model-sliced-panel .sliced-delete').forEach(b => b.onclick = async () => {
    if (!confirm('Delete this sliced file?')) return;
    await fetch(`/api/print-files/${b.closest('.sliced-row').dataset.id}`, { method: 'DELETE' });
    renderSlicedFiles(model);
  });
  $$('#model-sliced-panel .sliced-send').forEach(b => b.onclick = async () => {
    const row = b.closest('.sliced-row');
    const start = row.querySelector('.sliced-start').checked;
    if (start && !confirm('Start printing as soon as the file arrives? Check that the bed is clear and the printer is ready.')) return;
    const form = new FormData();
    form.append('print_file_id', row.dataset.id);
    form.append('start', start ? 'true' : 'false');
    b.disabled = true;
    const res = await fetch(`/api/printers/${row.querySelector('.sliced-printer').value}/send`, { method: 'POST', body: form });
    b.disabled = false;
    $('#sliced-status').textContent = res.ok ? ((await res.json()).started ? 'Sent, and the printer started it.' : 'Sent. It is waiting on the printer.') : await sourceErrorText(res);
  });
}

// ---------- Recent changes ----------
const activityState = { offset: 0, total: 0 };

function activityRow(e) {
  return `
    <div class="activity-row ${e.undone ? 'undone' : ''}" data-id="${e.id}">
      <span class="muted">${esc(String(e.at).slice(0, 16).replace('T', ' '))}</span>
      <b>${esc(e.actor)}</b>
      <span class="grow">${esc(e.summary)}</span>
      ${e.undone ? `<span class="muted">undone${e.undone_by ? ' by ' + esc(e.undone_by) : ''}</span>` : ''}
      ${e.undoable ? '<button class="activity-undo">Undo</button>' : ''}
    </div>`;
}

async function loadActivity(append) {
  if (!append) { activityState.offset = 0; $('#activity-list').innerHTML = ''; }
  const params = new URLSearchParams({ limit: 50, offset: activityState.offset });
  if ($('#activity-actor').value) params.set('actor', $('#activity-actor').value);
  if ($('#activity-action').value) params.set('action', $('#activity-action').value);
  const res = await fetch(`/api/activity?${params}`);
  if (!res.ok) return;
  const data = await res.json();
  activityState.total = data.total;
  if (!append) {
    const keep = $('#activity-actor').value;
    $('#activity-actor').innerHTML = '<option value="">everyone</option>' + data.actors.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
    $('#activity-actor').value = keep;
  }
  $('#activity-list').insertAdjacentHTML('beforeend', data.items.length || append ? data.items.map(activityRow).join('') : '<p class="muted">Nothing recorded yet.</p>');
  activityState.offset += data.items.length;
  $('#activity-more').classList.toggle('hidden', activityState.offset >= data.total);
}

async function undoActivity(id) {
  const res = await fetch(`/api/activity/${id}/undo`, { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  showNotice(res.ok ? `Undone: ${data.restored} put back.` : (data.detail || 'Could not undo that.'));
  return res.ok;
}

$('#activity-actor').addEventListener('change', () => loadActivity());
$('#activity-action').addEventListener('change', () => loadActivity());
$('#activity-more-btn').addEventListener('click', () => loadActivity(true));
$('#activity-list').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('activity-undo')) return;
  e.target.disabled = true;
  if (await undoActivity(e.target.closest('.activity-row').dataset.id)) loadActivity();
  else e.target.disabled = false;
});
$('#bulk-status').addEventListener('click', async (e) => {
  if (e.target.id !== 'bulk-undo') return;
  if (await undoActivity(e.target.dataset.id)) { $('#bulk-status').textContent = 'Undone.'; loadModels(); }
});

// ---------- Library filters, saved searches, bulk edit ----------
let libraryTotal = 0;
const bulk = { select: false, picked: new Set() };

function libraryParams() {
  const params = new URLSearchParams();
  const put = (key, value) => { if (value) params.set(key, value); };
  put('q', $('#search-box').value);
  if ($('#dup-only').checked) params.set('duplicates_only', 'true');
  put('printed', $('#printed-filter').value);
  put('designer', $('#f-designer').value.trim());
  put('license', $('#f-license').value.trim());
  put('collection_id', $('#f-collection').value);
  put('project_id', $('#f-project').value);
  put('linked', $('#f-linked').value);
  put('sort', $('#f-sort').value);
  if ($('#f-fits').checked) params.set('fits_bed', 'true');
  if ($('#f-notes').checked) params.set('has_notes', 'true');
  if ($('#f-latest').checked) params.set('latest_only', 'true');
  if ($('#f-failed').checked) params.set('failed_before', 'true');
  if ($('#f-favorite').checked) params.set('favorite', 'true');
  put('fits_printer', $('#f-fits-printer').value);
  return params;
}

function setLibraryFilters(values) {
  $('#search-box').value = values.q || '';
  $('#dup-only').checked = values.duplicates_only === 'true' || values.duplicates_only === true;
  $('#printed-filter').value = values.printed === undefined ? '' : String(values.printed);
  $('#f-designer').value = values.designer || '';
  $('#f-license').value = values.license || '';
  $('#f-collection').value = values.collection_id ? String(values.collection_id) : '';
  $('#f-project').value = values.project_id ? String(values.project_id) : '';
  $('#f-linked').value = values.linked === undefined ? '' : String(values.linked);
  $('#f-sort').value = values.sort || '';
  $('#f-fits').checked = values.fits_bed === true || values.fits_bed === 'true';
  $('#f-notes').checked = values.has_notes === true || values.has_notes === 'true';
  $('#f-latest').checked = values.latest_only === true || values.latest_only === 'true';
  $('#f-failed').checked = values.failed_before === true || values.failed_before === 'true';
  $('#f-favorite').checked = values.favorite === true || values.favorite === 'true';
  $('#f-fits-printer').value = values.fits_printer ? String(values.fits_printer) : '';
}

// a filter changed: back to the first page, then reload (the paging script resets on a search-box input)
function filtersChanged() { $('#search-box').dispatchEvent(new Event('input', { bubbles: true })); }

async function loadFilterChoices() {
  const [collections, projects, saved] = await Promise.all([
    fetch('/api/collections').then(r => (r.ok ? r.json() : [])),
    fetch('/api/projects').then(r => (r.ok ? r.json() : [])),
    fetch('/api/saved-searches').then(r => (r.ok ? r.json() : [])),
  ]);
  const keep = [$('#f-collection').value, $('#f-project').value];
  $('#f-collection').innerHTML = '<option value="">any collection</option>' + collections.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  $('#f-project').innerHTML = '<option value="">any project</option>' + projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  $('#f-collection').value = keep[0];
  $('#f-project').value = keep[1];
  savedSearches = saved;
  $('#f-saved').innerHTML = '<option value="">saved searches...</option>' + saved.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('');
}
let savedSearches = [];

['#f-designer', '#f-license'].forEach(sel => $(sel).addEventListener('input', debounce(filtersChanged, 400)));
['#f-collection', '#f-project', '#f-linked', '#f-sort', '#f-fits', '#f-notes', '#f-latest', '#f-failed', '#f-favorite', '#f-fits-printer'].forEach(sel => $(sel).addEventListener('change', filtersChanged));
$('#library-filters').addEventListener('toggle', () => { if ($('#library-filters').open) loadFilterChoices(); });
$('#f-clear').addEventListener('click', () => { setLibraryFilters({}); filtersChanged(); });
$('#f-saved').addEventListener('change', () => {
  const chosen = savedSearches.find(x => String(x.id) === $('#f-saved').value);
  if (!chosen) return;
  setLibraryFilters(chosen.params);
  filtersChanged();
});
$('#f-save').addEventListener('click', async () => {
  const params = Object.fromEntries(libraryParams());
  for (const key of ['fits_bed', 'has_notes', 'duplicates_only', 'latest_only', 'failed_before']) if (params[key] === 'true') params[key] = true;
  const name = prompt('Name this search:');
  if (!name) return;
  const res = await jsonRequest('POST', '/api/saved-searches', { name, params });
  $('#f-status').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
  if (res.ok) loadFilterChoices();
});
$('#f-delete-saved').addEventListener('click', async () => {
  const id = $('#f-saved').value;
  if (!id) return;
  await fetch(`/api/saved-searches/${id}`, { method: 'DELETE' });
  loadFilterChoices();
});

// bulk selection
function updateBulkBar() {
  $('#bulk-bar').classList.toggle('hidden', !bulk.select);
  $('#bulk-count').textContent = `${bulk.picked.size} selected`;
  $('#select-mode-btn').textContent = bulk.select ? 'Done selecting' : 'Select';
  $$('#grid .card').forEach(card => card.classList.toggle('selected', bulk.picked.has(parseInt(card.dataset.id))));
}

async function updateBulkInputs() {
  const action = $('#bulk-action').value;
  const needsChoice = ['add_collection', 'remove_collection', 'add_project', 'remove_project'].includes(action);
  const needsText = ['add_tag', 'remove_tag', 'set_designer', 'set_license'].includes(action);
  $('#bulk-text').classList.toggle('hidden', !needsText);
  $('#bulk-choice').classList.toggle('hidden', !needsChoice);
  $('#bulk-text').placeholder = action.endsWith('tag') ? 'tag' : action === 'set_designer' ? 'designer (empty clears it)' : 'license (empty clears it)';
  if (needsChoice) {
    const url = action.endsWith('collection') ? '/api/collections' : '/api/projects';
    const rows = await fetch(url).then(r => (r.ok ? r.json() : []));
    $('#bulk-choice').innerHTML = rows.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  }
}

$('#select-mode-btn').addEventListener('click', () => { bulk.select = !bulk.select; if (!bulk.select) bulk.picked.clear(); updateBulkBar(); updateBulkInputs(); });
$('#bulk-action').addEventListener('change', updateBulkInputs);
$('#bulk-none').addEventListener('click', () => { bulk.picked.clear(); updateBulkBar(); });
$('#bulk-page').addEventListener('click', () => { $$('#grid .card').forEach(c => bulk.picked.add(parseInt(c.dataset.id))); updateBulkBar(); });
$('#bulk-all').addEventListener('click', async () => {
  const res = await fetch(`/api/library/models?${libraryParams()}&limit=5000`);
  if (!res.ok) { $('#bulk-status').textContent = await sourceErrorText(res); return; }
  (await res.json()).forEach(m => bulk.picked.add(m.id));
  updateBulkBar();
  $('#bulk-status').textContent = libraryTotal > 5000 ? 'That is more than 5000: only the first 5000 were selected.' : '';
});
$('#grid').addEventListener('click', (e) => {
  if (!bulk.select) return;
  const card = e.target.closest('.card');
  if (!card) return;
  e.preventDefault();
  const id = parseInt(card.dataset.id);
  if (bulk.picked.has(id)) bulk.picked.delete(id); else bulk.picked.add(id);
  updateBulkBar();
});
$('#bulk-apply').addEventListener('click', async () => {
  const action = $('#bulk-action').value;
  if (!bulk.picked.size) { $('#bulk-status').textContent = 'Select some models first.'; return; }
  const body = { action, ids: [...bulk.picked] };
  if (['add_tag', 'remove_tag', 'set_designer', 'set_license'].includes(action)) body.value = $('#bulk-text').value;
  if (['add_collection', 'remove_collection', 'add_project', 'remove_project'].includes(action)) body.value = parseInt($('#bulk-choice').value);
  if (!confirm(`Apply "${$('#bulk-action').selectedOptions[0].textContent}" to ${bulk.picked.size} model${bulk.picked.size === 1 ? '' : 's'}?`)) return;
  const res = await jsonRequest('POST', '/api/bulk', body);
  const data = await res.json().catch(() => ({}));
  $('#bulk-status').innerHTML = res.ok ? `Changed ${data.changed}, already so: ${data.unchanged}.${data.activity_id ? ` <button id="bulk-undo" data-id="${data.activity_id}">Undo</button>` : ''}` : esc(data.detail || 'Could not apply that.');
  if (res.ok) loadModels();
});

// ---------- Starred models (each login has its own) ----------
let favoriteIds = new Set();
async function loadFavorites() {
  const res = await fetch('/api/favorites');
  favoriteIds = new Set(res.ok ? (await res.json()).ids : []);
}
function paintStar() {
  const button = $('#model-star');
  const on = !!currentModel && favoriteIds.has(currentModel.id);
  button.innerHTML = on ? '&#9733;' : '&#9734;';
  button.classList.toggle('on', on);
  button.setAttribute('aria-pressed', on ? 'true' : 'false');
  button.title = on ? 'Starred: click to remove the star' : 'Star this model';
}
$('#model-star').addEventListener('click', async () => {
  if (!currentModel) return;
  const on = !favoriteIds.has(currentModel.id);
  const res = await fetch(`/api/favorites/${currentModel.id}`, { method: on ? 'PUT' : 'DELETE' });
  if (!res.ok) return;
  if (on) favoriteIds.add(currentModel.id); else favoriteIds.delete(currentModel.id);
  paintStar();
});

// one library card (used by the Library grid and the Search page)
function libraryCard(m) {
  const card = document.createElement('a');
  card.className = 'card' + (m.is_duplicate_of ? ' duplicate' : '');
  card.href = `#/model/${m.id}`;
  card.dataset.id = m.id;
  // models the renderer couldn't thumbnail (STEP, FBX...) borrow the first picture from their site listing
  const siteImages = parseJsonList(m.source_images);
  const thumb = m.thumbnail_path ? `/api/library/thumbnails/${m.thumbnail_path}`
    : (siteImages.length ? `/api/library/models/${m.id}/source/images/${siteImages[0]}` : '');
  card.innerHTML = `
    ${thumb ? `<img src="${thumb}" loading="lazy" alt="">` : `<div style="height:120px;display:flex;align-items:center;justify-content:center;color:#666">${esc(m.extension)}</div>`}
    <div class="meta">
      <div class="fname" title="${esc(m.filename)}">${favoriteIds.has(m.id) ? '<span class="fav-badge" title="Starred">&#9733;</span>' : ''}${esc(m.filename)}</div>
      <div class="tags">${esc((m.tags || []).map(t => t.name).join(', '))}</div>
      ${m.print_count ? `<div class="printed-badge" title="Last printed ${esc(String(m.last_printed_at || '').slice(0, 10))}">&#10003; printed${m.print_count > 1 ? ` x${m.print_count}` : ''}</div>` : ''}
    </div>`;
  return card;
}

function renderGrid(models) {
  const grid = $('#grid');
  grid.innerHTML = '';
  for (const m of models) grid.appendChild(libraryCard(m));
  updateBulkBar();
}

$('#search-box').addEventListener('input', debounce(loadModels, 300));
$('#dup-only').addEventListener('change', loadModels);
$('#printed-filter').addEventListener('change', filtersChanged);
$('#scan-btn').addEventListener('click', async () => {
  $('#scan-btn').textContent = 'Scanning...';
  const res = await fetch('/api/library/scan', { method: 'POST' });
  const result = await res.json();
  $('#scan-btn').textContent = 'Rescan Library';
  alert(`Scan complete: ${result.found} found, ${result.added} added, ${result.updated} updated, ${result.duplicates} duplicates.`);
  loadModels();
});

$('#semantic-btn').addEventListener('click', async () => {
  const q = $('#search-box').value;
  if (!q) return alert('Type a search query first, then click Semantic.');
  const res = await fetch(`/api/library/search/semantic?q=${encodeURIComponent(q)}`);
  if (!res.ok) return alert('Semantic search unavailable -- configure AI in Settings first.');
  renderGrid(await res.json());
});

$('#tag-all-btn').addEventListener('click', async () => {
  const res = await fetch('/api/ai/tag-all?only_untagged=true', { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return alert(err.detail || 'Could not start tagging job.');
  }
  pollTagProgress();
});

let pollHandle = null;
function pollTagProgress() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = setInterval(async () => {
    const res = await fetch('/api/ai/tag-all/status');
    const s = await res.json();
    $('#tag-progress').textContent = s.running
      ? `Tagging ${s.done}/${s.total}${s.estimated_cost_usd ? ` (~$${s.estimated_cost_usd.toFixed(3)})` : ''}`
      : '';
    if (!s.running) {
      clearInterval(pollHandle);
      loadModels();
    }
  }, 1500);
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ---------- 3D Viewer ----------
let renderer, scene, camera, controls, animId;
let viewerGeneration = 0;

// The browser parses the whole file to preview it, at many times the file's
// size -- a multi-plate .3mf holds several times its zipped size in XML.
// Past these, ask before loading rather than risk hanging the tab.
const LARGE_PREVIEW_FACES = 2_000_000;
const LARGE_PREVIEW_BYTES = 150 * 2 ** 20;
const LARGE_PREVIEW_3MF_BYTES = 25 * 2 ** 20;

function previewIsTooLarge(model) {
  if (model.face_count) return model.face_count > LARGE_PREVIEW_FACES;
  const limit = model.extension === '.3mf' ? LARGE_PREVIEW_3MF_BYTES : LARGE_PREVIEW_BYTES;
  return (model.size_bytes || 0) > limit;
}

let currentModel = null;   // the full model record the page is showing

function formatBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function clearModelPage() {
  currentModel = null;
  $('#model-title').textContent = 'Loading...';
  ['#model-facts', '#model-tags-panel', '#model-details-panel', '#viewer-source', '#model-links-panel', '#viewer-info']
    .forEach(sel => { $(sel).innerHTML = ''; });
}

async function openModelPage(id, token) {
  clearModelPage();
  const res = await fetch(`/api/library/models/${id}/full`);
  if (token !== routeToken) return;
  if (!res.ok) { $('#model-title').textContent = 'Model not found'; return; }
  const model = await res.json();
  if (token !== routeToken) return;
  currentModel = model;
  viewerSessionColor = viewerModelColor;
  document.title = `${model.filename} - Model Hub`;
  renderModelPage(model);
  startModelViewer(model);
}

// re-draw the panels after a change; the 3D view is left alone
async function refreshModelPage() {
  if (!currentModel) return;
  const res = await fetch(`/api/library/models/${currentModel.id}/full`);
  if (!res.ok || !currentModel) return;
  currentModel = await res.json();
  renderModelPage(currentModel);
}

function renderModelPage(model) {
  $('#model-title').textContent = model.filename;
  renderModelFacts(model);
  renderModelTags(model);
  renderModelDetails(model);
  renderViewerSource(model);
  renderModelLinks(model);
  renderModelPrints(model);
  renderModelPrinter(model);
  renderPrintSettings(model);
  renderSlicerPanel(model);
  renderCostPanel(model);
  renderHealthPanel(model);
  loadFavorites().then(paintStar);
  renderVersions(model);
  renderSlicedFiles(model);
  renderSharePanel('#model-share-panel', 'model', model.id);
}

async function fillFitPrinters() {
  const res = await fetch('/api/fit/printers');
  if (!res.ok) return;
  const list = (await res.json()).filter(p => p.bed);
  const keep = $('#f-fits-printer').value;
  $('#f-fits-printer').innerHTML = '<option value="">any</option>' + list.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  $('#f-fits-printer').value = keep;
  $('#f-fits-printer').parentElement.classList.toggle('hidden', !list.length);
}

// ---------- What a print costs, and what to ask for it ----------
const money2 = v => (v == null ? '-' : `$${Number(v).toFixed(2)}`);
async function renderCostPanel(model) {
  const panel = $('#model-cost-panel');
  if (!panel) return;
  const [res, spools] = await Promise.all([fetch(`/api/costs/quote?model_id=${model.id}`), fetch('/api/filament').then(r => (r.ok ? r.json() : []))]);
  if (!currentModel || currentModel.id !== model.id) return;
  const first = res.ok ? await res.json() : null;
  panel.innerHTML = `<h3>Cost to print</h3>
    <div class="row"><label>Grams <input id="cost-grams" type="number" min="0" step="0.1" value="${first ? first.grams : ''}"></label>
      <label>Minutes <input id="cost-minutes" type="number" min="0" step="1" value="${first ? first.minutes : ''}"></label>
      <label>How many <input id="cost-qty" type="number" min="1" value="1" style="width:5em"></label>
      <label>Spool <select id="cost-spool"><option value="">(none)</option>${spools.map(f => `<option value="${f.id}" ${first && f.id === first.filament_id ? 'selected' : ''}>${esc([f.material, f.brand, f.color].filter(Boolean).join(' '))}</option>`).join('')}</select></label></div>
    <div id="cost-result" class="muted">${first ? '' : 'This model has no kept sliced file or print to take grams and minutes from: type them in.'}</div>`;
  const run = async () => {
    const g = $('#cost-grams').value, m = $('#cost-minutes').value;
    if (g === '' || m === '') return;
    const params = new URLSearchParams({ grams: g, minutes: m, quantity: $('#cost-qty').value || 1 });
    if ($('#cost-spool').value) params.set('filament_id', $('#cost-spool').value);
    const r = await fetch(`/api/costs/quote?${params}`);
    if (!r.ok) { $('#cost-result').textContent = await sourceErrorText(r); return; }
    const q = await r.json();
    $('#cost-result').innerHTML = `<table class="facts"><tbody>
      <tr><th>Filament</th><td>${money2(q.unit.filament)}</td></tr><tr><th>Electricity</th><td>${money2(q.unit.electricity)}</td></tr><tr><th>Machine time</th><td>${money2(q.unit.machine)}</td></tr>
      <tr><th>Failed prints (${q.failure_pct}%)</th><td>${money2(q.unit.failures)}</td></tr><tr><th>Cost of one</th><td><b>${money2(q.unit.cost)}</b></td></tr>
      <tr><th>Ask for (+${q.margin_pct}%)</th><td><b>${money2(q.unit.price)}</b></td></tr>
      ${q.quantity > 1 ? `<tr><th>For ${q.quantity}: cost / price</th><td>${money2(q.total.cost)} / <b>${money2(q.total.price)}</b></td></tr>` : ''}</tbody></table>
      <div class="muted">Failure allowance from ${esc(q.failure_from)}; filament price from ${esc(q.filament_from)}.${q.missing.length ? ` Not counted: ${q.missing.map(esc).join(', ')} (Settings, Costs).` : ''}</div>`;
  };
  panel.querySelectorAll('input, select').forEach(el => el.addEventListener('change', run));
  run();
}

// ---------- Is the mesh sound, and a repaired copy ----------
function renderHealthPanel(model) {
  const panel = $('#model-health-panel');
  if (!panel) return;
  const meshy = ['.stl', '.3mf', '.obj', '.fbx'].includes(model.extension);
  const more = model.designer ? `<p><a href="#/creators/${encodeURIComponent(model.designer)}">More by ${esc(model.designer)}</a></p>` : '';
  panel.innerHTML = `<h3>Mesh check</h3>` + (meshy ? `<p class="muted">Looks for holes, flipped or duplicate faces and other things that make a slicer print a model wrongly. A repair makes a new copy next to this one (never replacing it) and groups the two as versions.</p>
    <div class="row"><button id="health-check">Check it</button><button id="health-repair" class="hidden">Make a repaired copy</button><span id="health-msg" class="muted"></span></div>
    <div id="health-result"></div>` : '<p class="muted">Only STL, 3MF, OBJ and FBX meshes can be checked.</p>') + more;
  if (!meshy) return;
  const msg = $('#health-msg');
  $('#health-check').onclick = async () => {
    msg.textContent = 'Checking...';
    const res = await fetch(`/api/library/models/${model.id}/health`);
    if (!res.ok) { msg.textContent = await sourceErrorText(res); return; }
    const h = await res.json();
    msg.textContent = '';
    $('#health-result').innerHTML = (h.ok === true ? '<p><b>Looks sound.</b></p>' : h.ok === false ? '<p><b>Problems found:</b></p>' : '')
      + (h.issues.length ? `<ul class="issue-list">${h.issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>` : '')
      + `<p class="muted">${Number(h.faces).toLocaleString()} faces.</p>`;
    $('#health-repair').classList.toggle('hidden', !h.fixable);
  };
  $('#health-repair').onclick = async () => {
    msg.textContent = 'Repairing...';
    const res = await fetch(`/api/library/models/${model.id}/repair`, { method: 'POST' });
    if (!res.ok) { msg.textContent = await sourceErrorText(res); return; }
    const d = await res.json();
    location.hash = `#/model/${d.model.id}`;
  };
}

// ---------- Open a model in a slicer on this computer ----------
const SLICER_BUTTONS = [['prusaslicer', 'PrusaSlicer'], ['orcaslicer', 'OrcaSlicer'], ['bambustudio', 'Bambu Studio']];
async function renderSlicerPanel(model) {
  const panel = $('#model-slicer-panel');
  if (!panel) return;
  const fitInfo = await fetch(`/api/fit/model/${model.id}`).then(r => (r.ok ? r.json() : { fits: [], too_big: [] }));
  if (!currentModel || currentModel.id !== model.id) return;
  if (!['.stl', '.3mf', '.obj', '.step', '.stp'].includes(model.extension)) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  panel.innerHTML = `<h3>Open in a slicer</h3>
    <div class="row">${SLICER_BUTTONS.map(([id, label]) => `<button class="slicer-open" data-slicer="${id}">${label}</button>`).join('')}</div>
    ${fitInfo.fits.length || fitInfo.too_big.length ? `<p class="muted">${fitInfo.fits.length ? 'Fits: ' + fitInfo.fits.map(p => esc(p.name)).join(', ') + '. ' : ''}${fitInfo.too_big.length ? `<span class="warn-text">Too big for: ${fitInfo.too_big.map(p => esc(p.name)).join(', ')}.</span>` : ''}</p>` : ''}
    <p class="muted slicer-note">The slicer on this computer fetches the file from a link that works for 15 minutes, so Model Hub has to be reachable from here at the address in your browser. If nothing opens, the slicer is not installed or too old to open links.</p>`;
  panel.querySelectorAll('.slicer-open').forEach(btn => btn.onclick = async () => {
    const res = await fetch(`/api/slicer-link/${model.id}`);
    if (!res.ok) { panel.querySelector('.slicer-note').textContent = await sourceErrorText(res); return; }
    const data = await res.json();
    window.location.href = data.schemes[btn.dataset.slicer] + encodeURIComponent(location.origin + data.path);
  });
}

// ---------- What worked: print settings per model ----------
const PRINT_SETTING_FIELDS = [
  ['material', 'Material'], ['layer_height', 'Layer height (mm)'], ['infill', 'Infill'], ['supports', 'Supports'],
  ['nozzle_temp', 'Nozzle temperature'], ['bed_temp', 'Bed temperature'], ['speed', 'Speed'], ['profile', 'Slicer profile'],
];

function renderPrintSettings(model) {
  let saved = {};
  try { saved = JSON.parse(model.print_settings || '{}') || {}; } catch (e) { saved = {}; }
  $('#model-settings-panel').innerHTML = `
    <h3>What worked</h3>
    <div class="stack">
      ${PRINT_SETTING_FIELDS.map(([key, label]) => `<label>${esc(label)} <input class="ps-field" data-key="${key}" value="${esc(saved[key] || '')}"></label>`).join('')}
      <label>Notes <textarea class="ps-field" data-key="notes" rows="3" placeholder="Orientation, tricks, what to avoid...">${esc(saved.notes || '')}</textarea></label>
      <div class="row"><button id="ps-save">Save</button><button id="ps-again" title="Queue it again with the filament and grams of its last print">Print again</button><span id="ps-status" class="muted"></span></div>
    </div>`;
  fetch(`/api/library/models/${model.id}/suggested-settings`).then(r => (r.ok ? r.json() : null)).then(data => {
    if (!data || !$('#ps-status')) return;
    const sg = data.suggested, have = data.current || {};
    const fill = ['material', 'layer_height', 'profile'].filter(k => sg[k] && !have[k]);
    if (!fill.length && !sg.avoid.length) return;
    const box = document.createElement('div');
    box.className = 'hint-box';
    box.innerHTML = `<b>From how its prints went</b> (${sg.based_on} good print${sg.based_on === 1 ? '' : 's'}):
      ${['material', 'layer_height', 'profile'].filter(k => sg[k]).map(k => `${esc(k.replace('_', ' '))} ${esc(sg[k])}`).join(', ') || 'nothing certain yet'}.
      ${sg.avoid.length ? `<div class="warn-text">Better not in: ${sg.avoid.map(esc).join(', ')}.</div>` : ''}
      ${fill.length ? '<button id="ps-use">Fill in the empty boxes</button>' : ''}`;
    $('#model-settings-panel h3').after(box);
    const use = $('#ps-use');
    if (use) use.onclick = () => { fill.forEach(k => { const f = $(`#model-settings-panel .ps-field[data-key="${k}"]`); if (f) f.value = sg[k]; }); $('#ps-status').textContent = 'Filled in; press Save to keep them.'; };
  });
  $('#ps-save').onclick = async () => {
    const body = {};
    $$('#model-settings-panel .ps-field').forEach(f => { body[f.dataset.key] = f.value; });
    const res = await jsonRequest('PUT', `/api/library/models/${model.id}/print-settings`, body);
    $('#ps-status').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    if (res.ok) setTimeout(() => { const el = $('#ps-status'); if (el) el.textContent = ''; }, 2000);
  };
  $('#ps-again').onclick = async () => {
    const res = await fetch(`/api/queue/again/${model.id}`, { method: 'POST' });
    $('#ps-status').textContent = res.ok ? 'Added to the print queue.' : await sourceErrorText(res);
  };
}

// ---------- Versions of a model ----------
async function renderVersions(model) {
  const panel = $('#model-versions-panel');
  const fam = model.family;
  let suggestions = [];
  const res = await fetch(`/api/families/suggest/${model.id}`);
  if (res.ok) suggestions = (await res.json()).suggestions;
  if (!currentModel || currentModel.id !== model.id) return;
  panel.innerHTML = `
    <h3>Versions</h3>
    ${fam ? `
      <div class="muted">${fam.name ? esc(fam.name) : 'This model has other versions'}</div>
      <ul class="link-list">${fam.members.map(m => `
        <li data-id="${m.id}">
          ${m.id === model.id ? `<b>${esc(m.filename)}</b>` : `<a href="#/model/${m.id}">${esc(m.filename)}</a>`}
          <input class="version-label" value="${esc(m.version_label || '')}" placeholder="label (v2...)" maxlength="40" aria-label="Version label">
          ${m.id === model.id ? '<button class="version-leave">Leave group</button>' : ''}
        </li>`).join('')}</ul>` : '<p class="muted">Not grouped with other versions.</p>'}
    ${suggestions.length ? `<div class="muted">Looks like another version:</div>
      <ul class="link-list">${suggestions.map(s => `<li><a href="#/model/${s.id}">${esc(s.filename)}</a> <span class="muted">${s.same_shape ? 'same shape' : 'similar name'}</span>
        <button class="version-join" data-id="${s.id}">Group with this model</button></li>`).join('')}</ul>` : ''}
    <span id="versions-status" class="muted"></span>`;
  $$('#model-versions-panel .version-join').forEach(btn => btn.onclick = async () => {
    const r = await jsonRequest('POST', '/api/families', { model_ids: [model.id, parseInt(btn.dataset.id)] });
    if (r.ok) refreshModelPage(); else $('#versions-status').textContent = await sourceErrorText(r);
  });
  $$('#model-versions-panel .version-label').forEach(input => input.onchange = async () => {
    const r = await jsonRequest('PUT', `/api/families/members/${input.closest('li').dataset.id}`, { label: input.value });
    $('#versions-status').textContent = r.ok ? 'Saved.' : await sourceErrorText(r);
  });
  const leave = $('#model-versions-panel .version-leave');
  if (leave) leave.onclick = async () => { await fetch(`/api/families/members/${model.id}`, { method: 'DELETE' }); refreshModelPage(); };
}

// ---------- Share links (a model or a project) ----------
async function renderSharePanel(selector, kind, targetId) {
  const panel = $(selector);
  if (!panel) return;
  if (currentUser.role === 'viewer') { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const res = await fetch(`/api/shares?kind=${kind}&target_id=${targetId}`);
  const links = res.ok ? await res.json() : [];
  panel.innerHTML = `
    <h3>Share</h3>
    <p class="muted">A secret link that shows this ${kind} read-only to anyone who has it, without a login. Notes, print history and (unless you tick it) costs are never shown.
      Anyone who can reach this server's address can open it, so for people outside your network you need to expose Model Hub yourself (a reverse proxy or VPN).</p>
    ${links.length ? `<ul class="link-list">${links.map(l => `
      <li data-id="${l.id}"><input class="share-url" readonly value="${esc(location.origin + l.path)}" aria-label="Share link">
        <button class="share-copy">Copy</button>
        <span class="muted">${l.allow_downloads ? 'downloads on' : 'no downloads'}${l.show_costs ? ' · costs shown' : ''}${l.expires_at ? ` · ${l.expired ? 'expired' : 'expires ' + esc(String(l.expires_at).slice(0, 10))}` : ''}</span>
        <button class="share-revoke danger">Stop sharing</button></li>`).join('')}</ul>` : ''}
    <div class="row">
      <label class="inline-check"><input type="checkbox" class="share-downloads"> Allow file downloads</label>
      ${kind === 'project' ? '<label class="inline-check"><input type="checkbox" class="share-costs"> Show part costs</label>' : ''}
      <label>Expires in <select class="share-expiry"><option value="">never</option><option value="1">1 day</option><option value="7">a week</option><option value="30">30 days</option></select></label>
      <button class="share-create primary">Create a link</button><span class="share-status muted"></span>
    </div>`;
  panel.querySelector('.share-create').onclick = async () => {
    const body = { kind, target_id: targetId, allow_downloads: panel.querySelector('.share-downloads').checked };
    if (kind === 'project') body.show_costs = panel.querySelector('.share-costs').checked;
    const days = panel.querySelector('.share-expiry').value;
    if (days) body.expires_days = parseInt(days);
    const r = await jsonRequest('POST', '/api/shares', body);
    if (r.ok) renderSharePanel(selector, kind, targetId); else panel.querySelector('.share-status').textContent = await sourceErrorText(r);
  };
  panel.onclick = async (e) => {
    const row = e.target.closest('li');
    if (!row) return;
    if (e.target.classList.contains('share-copy')) {
      const input = row.querySelector('.share-url');
      input.select();
      const ok = await copyText(input.value);
      e.target.textContent = ok ? 'Copied' : 'Select + copy';
    } else if (e.target.classList.contains('share-revoke')) {
      if (!confirm('Stop sharing? The link stops working at once.')) return;
      await fetch(`/api/shares/${row.dataset.id}`, { method: 'DELETE' });
      renderSharePanel(selector, kind, targetId);
    }
  };
}

// ---------- QR labels for spools and supplies ----------
let labelItems = [];

async function loadLabels(kind) {
  $('#labels-back').href = kind === 'filament' ? '#/filament' : '#/supplies';
  $('#labels-title').textContent = kind === 'filament' ? 'Spool labels' : 'Supply labels';
  const res = await fetch(kind === 'filament' ? '/api/filament' : '/api/inventory');
  labelItems = (res.ok ? await res.json() : []).map(i => kind === 'filament'
    ? { id: i.id, route: `spool/${i.id}`, title: [i.material, i.brand, i.color].filter(Boolean).join(' '), line: `${i.remaining_g} g left` }
    : { id: i.id, route: `supply/${i.id}`, title: i.name, line: [i.category, i.location].filter(Boolean).join(' · ') });
  $('#labels-list').innerHTML = labelItems.length ? labelItems.map(i => `
    <label class="inline-check"><input type="checkbox" class="label-pick" value="${i.id}" checked> ${esc(i.title)}</label>`).join('') : '<p class="muted">Nothing to label yet.</p>';
  drawLabelSheet();
}

function drawLabelSheet() {
  const picked = new Set([...$$('.label-pick:checked')].map(c => parseInt(c.value)));
  $('#labels-sheet').innerHTML = labelItems.filter(i => picked.has(i.id)).map(i => `
    <div class="label-card">
      <img src="/api/qr?text=${encodeURIComponent(location.origin + '/#/' + i.route)}" alt="QR code">
      <div><b>${esc(i.title)}</b><div class="muted">${esc(i.line)}</div></div>
    </div>`).join('');
}

$('#labels-list').addEventListener('change', drawLabelSheet);
$('#labels-all').addEventListener('click', () => { $$('.label-pick').forEach(c => { c.checked = true; }); drawLabelSheet(); });
$('#labels-none').addEventListener('click', () => { $$('.label-pick').forEach(c => { c.checked = false; }); drawLabelSheet(); });
$('#labels-print').addEventListener('click', () => window.print());

// a scanned label opens its row: scroll to it and flash it
function highlightRow(tableSelector, id) {
  const row = document.querySelector(`${tableSelector} tr[data-id="${id}"]`);
  if (!row) return;
  row.scrollIntoView({ block: 'center' });
  row.classList.add('flash');
  setTimeout(() => row.classList.remove('flash'), 2500);
}

// ---------- Print history on a model's page ----------
function ratingText(n) { return n ? '\u2605'.repeat(n) + '\u2606'.repeat(5 - n) : ''; }

// "failed 3 of 5 attempts, usually: Warped or curled" from a hint of /api/prints/hints
function hintText(h) {
  if (!h) return '';
  const usual = h.top_label ? `, usually: ${h.top_label}` : '';
  return `Failed ${h.failures} of ${h.attempts} attempt${h.attempts === 1 ? '' : 's'}${usual}`;
}
async function loadHints(modelId) {
  const res = await fetch(`/api/prints/hints${modelId ? `?model_id=${modelId}` : ''}`);
  return res.ok ? (await res.json()).hints : {};
}

// why a print failed (the server's list), fetched once
let failureReasons = null;
async function loadFailureReasons() {
  if (failureReasons) return failureReasons;
  const res = await fetch('/api/prints/reasons');
  failureReasons = res.ok ? await res.json() : [];
  return failureReasons;
}
function outcomeOptions(reasons, selected) {
  return `<option value="">Worked</option>` + reasons.map(r => `<option value="${esc(r.key)}" ${r.key === selected ? 'selected' : ''}>Failed: ${esc(r.label)}</option>`).join('');
}

async function renderModelPrints(model) {
  const panel = $('#model-prints-panel');
  const [logs, spools, reasons, hints] = await Promise.all([
    fetch(`/api/prints?model_id=${model.id}`).then(r => (r.ok ? r.json() : { total: 0, items: [] })),
    fetch('/api/filament').then(r => (r.ok ? r.json() : [])),
    loadFailureReasons(),
    loadHints(model.id),
  ]);
  const hint = hints[model.id];
  if (!currentModel || currentModel.id !== model.id) return;
  const spoolText = f => [f.material, f.brand, f.color].filter(Boolean).join(' ');
  const spoolById = new Map(spools.map(f => [f.id, f]));
  const today = new Date().toISOString().slice(0, 10);
  panel.innerHTML = `
    ${hint ? `<div class="hint-box"><b>&#9888; ${esc(hintText(hint))}</b> (${hint.rate}%)
      ${hint.materials.length ? `<div class="muted">On ${hint.materials.map(m => `${esc(m.material)} (${m.failures})`).join(', ')}.</div>` : ''}
      ${hint.tip ? `<div>${esc(hint.tip)}</div>` : ''}</div>` : ''}
    <h3>Print history${logs.total ? ` (${logs.total})` : ''}</h3>
    ${logs.items.length ? logs.items.map(l => `
      <div class="print-row${l.outcome === 'failed' ? ' print-failed' : ''}" data-id="${l.id}">
        <div class="print-main">
          <b>${esc(String(l.printed_at).slice(0, 10))}</b>
          ${l.outcome === 'failed' ? `<span class="status-badge status-failed">Failed${l.failure_label ? ': ' + esc(l.failure_label) : ''}</span>` : ''}
          ${l.rating ? `<span class="stars" title="${l.rating} of 5">${ratingText(l.rating)}</span>` : ''}
          ${l.grams != null ? `<span class="muted">${l.grams} g${l.filament_id && spoolById.get(l.filament_id) ? ' of ' + esc(spoolText(spoolById.get(l.filament_id))) : ''}</span>` : ''}
          ${l.minutes != null ? `<span class="muted">${Math.round(l.minutes)} min</span>` : ''}
          ${l.energy_kwh != null ? `<span class="muted" title="Measured by the printer's smart plug">${l.energy_kwh} kWh</span>` : ''}
          ${l.source === 'queue' ? '<span class="muted">from the print queue</span>' : ''}
          ${l.notes ? `<div>${esc(l.notes)}</div>` : ''}
        </div>
        ${l.timelapse_url ? `<div><a href="${esc(safeUrl(l.timelapse_url))}" target="_blank" rel="noopener noreferrer">&#9654; Time-lapse</a>
          ${location.protocol === 'https:' && !/^https:/i.test(l.timelapse_url) ? '' : `<button class="timelapse-play" data-url="${esc(safeUrl(l.timelapse_url))}">Play here</button>`}</div>` : ''}
        ${l.has_photo ? `<a href="/api/prints/${l.id}/photo" target="_blank" rel="noopener"><img class="print-photo" src="/api/prints/${l.id}/photo?v=${Date.now()}" loading="lazy" alt="Photo of the print"></a>` : ''}
        <div class="print-actions">
          <select class="print-outcome" aria-label="How it went">${outcomeOptions(reasons, l.outcome === 'failed' ? (l.failure_reason || 'other') : '')}</select>
          <label class="button-link print-photo-btn">${l.has_photo ? 'Replace photo' : 'Take photo'}<input type="file" accept="image/*" capture="environment" class="print-photo-input" hidden></label>
          <label class="button-link print-photo-btn">Choose photo<input type="file" accept="image/*" class="print-photo-input" hidden></label>
          <button class="print-timelapse">Time-lapse</button>
          <button class="print-delete">Delete</button>
        </div>
        <div class="timelapse-box hidden" data-printer="${l.printer_id || ''}">
          <div class="row"><input class="timelapse-url" value="${esc(l.timelapse_url || '')}" placeholder="https://... link to the video" aria-label="Time-lapse link">
            <button class="timelapse-save">Save</button>
            ${currentUser.role === 'admin' && l.printer_id ? '<button class="timelapse-find">Find on the printer</button>' : ''}</div>
          <div class="timelapse-found muted"></div>
        </div>
      </div>`).join('') : '<p class="muted">Not printed yet.</p>'}
    <h3>Log a print</h3>
    <div class="stack">
      <div class="row">
        <label>Date <input id="print-date" type="date" value="${today}"></label>
        <label>How it went <select id="print-outcome">${outcomeOptions(reasons, '')}</select></label>
        <label>Rating <select id="print-rating"><option value="">-</option>${[5, 4, 3, 2, 1].map(n => `<option value="${n}">${ratingText(n)}</option>`).join('')}</select></label>
      </div>
      <div class="row">
        <select id="print-spool"><option value="">(no filament)</option>${spools.map(f =>
          `<option value="${f.id}">${esc(spoolText(f))} (${f.remaining_g} g left)</option>`).join('')}</select>
        <input id="print-grams" type="number" min="0" step="0.1" placeholder="grams used">
        <input id="print-minutes" type="number" min="0" step="1" placeholder="minutes">
      </div>
      <label class="inline-check"><input type="checkbox" id="print-deduct" checked> Take the grams from the spool</label>
      <textarea id="print-notes" rows="2" placeholder="How it went, settings, changes..."></textarea>
      <div class="row"><button id="print-add" class="primary">Log this print</button><span id="print-status" class="muted"></span></div>
    </div>`;

  $('#print-add').onclick = async () => {
    const body = { model_id: model.id, printed_at: $('#print-date').value, notes: $('#print-notes').value,
      deduct: $('#print-deduct').checked };
    if ($('#print-outcome').value) { body.outcome = 'failed'; body.failure_reason = $('#print-outcome').value; }
    if ($('#print-rating').value) body.rating = parseInt($('#print-rating').value);
    if ($('#print-spool').value) body.filament_id = parseInt($('#print-spool').value);
    if ($('#print-grams').value) body.grams = parseFloat($('#print-grams').value);
    if ($('#print-minutes').value) body.minutes = parseFloat($('#print-minutes').value);
    const res = await jsonRequest('POST', '/api/prints', body);
    if (res.ok) refreshModelPage(); else $('#print-status').textContent = await sourceErrorText(res);
  };
  $$('#model-prints-panel .print-timelapse').forEach(btn => btn.onclick = () => btn.closest('.print-row').querySelector('.timelapse-box').classList.toggle('hidden'));
  $$('#model-prints-panel .timelapse-play').forEach(btn => btn.onclick = () => {
    const holder = btn.parentElement;
    const open = holder.querySelector('video');
    if (open) { open.remove(); return; }
    const video = document.createElement('video');
    video.controls = true; video.preload = 'metadata'; video.className = 'timelapse-video'; video.src = btn.dataset.url;
    video.onerror = () => { video.remove(); holder.insertAdjacentHTML('beforeend', '<div class="warn-text">The browser could not play that video here; use the link to open it.</div>'); };
    holder.appendChild(video);
  });
  $$('#model-prints-panel .timelapse-save').forEach(btn => btn.onclick = async () => {
    const row = btn.closest('.print-row');
    const res = await jsonRequest('PATCH', `/api/prints/${row.dataset.id}`, { timelapse_url: row.querySelector('.timelapse-url').value.trim() });
    if (res.ok) refreshModelPage(); else row.querySelector('.timelapse-found').textContent = await sourceErrorText(res);
  });
  $$('#model-prints-panel .timelapse-find').forEach(btn => btn.onclick = async () => {
    const row = btn.closest('.print-row');
    const out = row.querySelector('.timelapse-found');
    out.textContent = 'Asking the printer...';
    const res = await fetch(`/api/prints/${row.dataset.id}/timelapse-candidates`);
    if (!res.ok) { out.textContent = await sourceErrorText(res); return; }
    const data = await res.json();
    out.innerHTML = data.files.length ? `Videos on ${esc(data.printer)}, closest to this print first:` + data.files.map(f =>
      `<div class="row"><a href="${esc(safeUrl(f.url))}" target="_blank" rel="noopener noreferrer">${esc(f.name)}</a>${f.size ? ` <span class="muted">${formatBytes(f.size)}</span>` : ''}
       <button class="timelapse-use" data-url="${esc(f.url)}">Use this one</button></div>`).join('') : `${esc(data.printer)} has no time-lapse videos (Klipper needs the moonraker-timelapse plugin).`;
    out.querySelectorAll('.timelapse-use').forEach(use => use.onclick = async () => {
      await jsonRequest('PATCH', `/api/prints/${row.dataset.id}`, { timelapse_url: use.dataset.url });
      refreshModelPage();
    });
  });
  $$('#model-prints-panel .print-outcome').forEach(sel => sel.onchange = async () => {
    const res = await jsonRequest('PATCH', `/api/prints/${sel.closest('.print-row').dataset.id}`,
      sel.value ? { outcome: 'failed', failure_reason: sel.value } : { outcome: 'done' });
    if (res.ok) refreshModelPage(); else showNotice(await sourceErrorText(res));
  });
  $$('#model-prints-panel .print-delete').forEach(btn => btn.onclick = async () => {
    if (!confirm('Delete this print entry? Filament it took from a spool is put back.')) return;
    await fetch(`/api/prints/${btn.closest('.print-row').dataset.id}`, { method: 'DELETE' });
    refreshModelPage();
  });
  $$('#model-prints-panel .print-photo-input').forEach(input => input.onchange = async () => {
    if (!input.files.length) return;
    const form = new FormData();
    form.append('file', input.files[0]);
    const res = await fetch(`/api/prints/${input.closest('.print-row').dataset.id}/photo`, { method: 'POST', body: form });
    if (res.ok) refreshModelPage(); else alert(await sourceErrorText(res));
  });
}

function renderModelFacts(m) {
  const dims = m.bbox_x != null && m.bbox_y != null && m.bbox_z != null
    ? `${m.bbox_x.toFixed(1)} × ${m.bbox_y.toFixed(1)} × ${m.bbox_z.toFixed(1)} mm` : '';
  const rows = [
    ['Type', m.extension], ['Size', formatBytes(m.size_bytes)], ['Dimensions', dims],
    ['Volume', m.volume_mm3 != null ? `${(m.volume_mm3 / 1000).toFixed(2)} cm³` : ''],
    ['Vertices / faces', `${m.vertex_count ?? '?'} / ${m.face_count ?? '?'}`],
    ['Watertight', m.is_watertight == null ? 'unknown' : (m.is_watertight ? 'yes' : 'no')],
    ['File', m.path], ['Added', m.created_at ? String(m.created_at).slice(0, 10) : ''],
  ].filter(([, value]) => value);
  $('#model-facts').innerHTML = `
    <h3>Details</h3>
    <table class="facts"><tbody>${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join('')}</tbody></table>
    ${m.file_exists === false ? '<p class="warn-text">The file is missing from the library folder.</p>' : ''}
    ${m.ai_description ? `<p class="model-desc">${esc(m.ai_description)}</p>` : ''}
    <div class="row"><a class="button-link" href="/api/library/models/${m.id}/file" download>Download file</a></div>`;
}

function renderModelTags(m) {
  const tags = m.tags || [];
  $('#model-tags-panel').innerHTML = `
    <h3>Tags</h3>
    <div class="source-tags">${tags.length ? tags.map(t =>
      `<span class="chip">${esc(t.name)}${t.ai_generated ? ' ✨' : ''} <button class="chip-x" data-tag="${esc(t.name)}" title="Remove this tag">&times;</button></span>`).join('')
      : '<span class="muted">No tags yet</span>'}</div>
    <div class="row">
      <input id="model-new-tag" placeholder="Add a tag">
      <button id="model-add-tag">Add</button>
      <button id="model-ai-tag">Tag with AI</button>
    </div>
    <span id="model-tag-status" class="muted"></span>`;
  const add = async () => {
    const name = $('#model-new-tag').value.trim();
    if (!name) return;
    await jsonRequest('POST', `/api/tags/models/${m.id}`, { name });
    refreshModelPage();
  };
  $('#model-add-tag').onclick = add;
  $('#model-new-tag').onkeydown = (e) => { if (e.key === 'Enter') add(); };
  $$('#model-tags-panel .chip-x').forEach(btn => btn.onclick = async () => {
    await fetch(`/api/tags/models/${m.id}/${encodeURIComponent(btn.dataset.tag)}`, { method: 'DELETE' });
    refreshModelPage();
  });
  $('#model-ai-tag').onclick = async () => {
    $('#model-tag-status').textContent = 'Asking the AI...';
    const res = await fetch(`/api/ai/tag/${m.id}`, { method: 'POST' });
    const result = await res.json().catch(() => ({}));
    if (result.status === 'ok') refreshModelPage();
    else $('#model-tag-status').textContent = result.reason || result.detail || 'The AI did not tag this model.';
  };
}

function renderModelDetails(m) {
  $('#model-details-panel').innerHTML = `
    <h3>Info</h3>
    <div class="stack">
      <label>Designer <input id="model-designer" value="${esc(m.designer || '')}"></label>
      <label>License <input id="model-license" value="${esc(m.license || '')}"></label>
      <label>Source link <input id="model-source-url" value="${esc(m.source_url || '')}" placeholder="Where this model came from"></label>
      <label>Notes <textarea id="model-notes" rows="4" placeholder="Print settings, mods, reminders...">${esc(m.notes || '')}</textarea></label>
      <div class="row"><button id="model-save-meta">Save</button><span id="model-meta-status" class="muted"></span></div>
    </div>`;
  $('#model-save-meta').onclick = async () => {
    const res = await jsonRequest('PATCH', `/api/library/models/${m.id}`, {
      designer: $('#model-designer').value, license: $('#model-license').value,
      source_url: $('#model-source-url').value, notes: $('#model-notes').value,
    });
    $('#model-meta-status').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    if (res.ok) setTimeout(() => { const el = $('#model-meta-status'); if (el) el.textContent = ''; }, 2000);
  };
}

async function renderModelLinks(model) {
  const panel = $('#model-links-panel');
  const [projects, collections, spools] = await Promise.all([
    fetch('/api/projects').then(r => (r.ok ? r.json() : [])),
    fetch('/api/collections').then(r => (r.ok ? r.json() : [])),
    fetch('/api/filament').then(r => (r.ok ? r.json() : [])),
  ]);
  if (!currentModel || currentModel.id !== model.id) return;      // moved on while this loaded
  const inProject = new Set(model.projects.map(p => p.id));
  const inCollection = new Set(model.collections.map(c => c.id));
  const spoolText = f => esc([f.material, f.brand, f.color].filter(Boolean).join(' '));
  panel.innerHTML = `
    <h3>Projects</h3>
    ${model.projects.length ? `<ul class="link-list">${model.projects.map(p =>
      `<li><a href="#/project/${p.id}">${esc(p.name)}</a><span class="status-badge status-${esc(p.status)}">${esc(p.status)}</span></li>`).join('')}</ul>`
      : '<p class="muted">Not in any project yet.</p>'}
    <div class="row">
      <select id="viewer-project"><option value="">(choose a project)</option>${projects.filter(p => !inProject.has(p.id)).map(p =>
        `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
      <button id="viewer-add-project-btn">Add to project</button>
      <span id="viewer-project-result" class="muted"></span>
    </div>

    <h3>Collections</h3>
    ${model.collections.length ? `<div class="source-tags">${model.collections.map(c =>
      `<span class="chip">${esc(c.name)} <button class="chip-x collection-x" data-collection="${c.id}" title="Remove from this collection">&times;</button></span>`).join('')}</div>`
      : '<p class="muted">Not in any collection.</p>'}
    <div class="row">
      <select id="model-collection"><option value="">(choose a collection)</option>${collections.filter(c => !inCollection.has(c.id)).map(c =>
        `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      <button id="model-add-collection">Add to collection</button>
    </div>

    <h3>Print</h3>
    <div class="row">
      <select id="viewer-est-material">
        <option value="PLA">PLA</option><option value="PETG">PETG</option><option value="ABS">ABS</option>
        <option value="TPU">TPU</option><option value="Nylon">Nylon</option>
      </select>
      <input id="viewer-est-infill" placeholder="Infill % (default 15)">
      <button id="viewer-estimate-btn">Estimate print</button>
    </div>
    <div id="viewer-estimate-result" class="muted"></div>
    ${model.queue.length ? `<ul class="link-list">${model.queue.map(q =>
      `<li>Print queue #${q.position + 1}<span class="status-badge status-${esc(q.status)}">${esc(q.status)}</span>${
        q.estimated_grams != null ? `<span class="muted">${q.estimated_grams} g</span>` : ''}</li>`).join('')}</ul>` : ''}
    <div class="row">
      <select id="viewer-queue-filament"><option value="">(no filament selected)</option>${spools.map(f =>
        `<option value="${f.id}">${spoolText(f)} (${f.remaining_g}g left)</option>`).join('')}</select>
      <button id="viewer-add-queue-btn">Add to print queue</button>
    </div>

    ${model.duplicates.length ? `<h3>Same file elsewhere</h3><ul class="link-list">${model.duplicates.map(d =>
      `<li><a href="#/model/${d.id}">${esc(d.filename)}</a><span class="muted">${esc(d.path)}</span></li>`).join('')}</ul>` : ''}`;

  $('#viewer-add-project-btn').onclick = async () => {
    const projectId = $('#viewer-project').value;
    if (!projectId) { $('#viewer-project-result').textContent = 'Choose a project first.'; return; }
    const res = await fetch(`/api/projects/${projectId}/models/${model.id}`, { method: 'POST' });
    if (res.ok) refreshModelPage(); else $('#viewer-project-result').textContent = 'Could not add to the project.';
  };
  $('#model-add-collection').onclick = async () => {
    const collectionId = $('#model-collection').value;
    if (!collectionId) return;
    await fetch(`/api/collections/${collectionId}/models/${model.id}`, { method: 'POST' });
    refreshModelPage();
  };
  $$('#model-links-panel .collection-x').forEach(btn => btn.onclick = async () => {
    await fetch(`/api/collections/${btn.dataset.collection}/models/${model.id}`, { method: 'DELETE' });
    refreshModelPage();
  });
  $('#viewer-estimate-btn').onclick = () => runEstimate(model.id);
  $('#viewer-add-queue-btn').onclick = () => addToQueue(model.id);
}

// ---- the 3D view ----
function startModelViewer(model) {
  initViewer();
  ensureViewerColorControl();
  ensureViewerTools(model);
  const fileUrl = `/api/library/models/${model.id}/file`;
  const loaders = {
    '.stl': loadSTL, '.obj': loadOBJ, '.3mf': load3MF, '.fbx': loadFBX, '.step': loadSTEP, '.stp': loadSTEP,
  };
  const loadFn = loaders[model.extension];
  const info = $('#viewer-info');
  if (loadFn && previewIsTooLarge(model)) {
    const faces = model.face_count ? `${(model.face_count / 1e6).toFixed(1)}M faces` : `${Math.round(model.size_bytes / 2 ** 20)} MB`;
    info.insertAdjacentHTML('beforeend',
      `<div id="large-preview-note" class="warn-text">This model is very large (${faces}) and may freeze or crash this browser tab to preview live. `
      + `<button id="large-preview-load-btn">Load anyway</button></div>`);
    $('#large-preview-load-btn').onclick = () => {
      $('#large-preview-note').remove();
      loadFn(fileUrl);
    };
  } else if (loadFn) {
    loadFn(fileUrl);
  } else {
    info.insertAdjacentHTML('beforeend',
      `<div class="warn-text">Live viewer not available for ${esc(model.extension)} yet -- download the original file instead.</div>`);
  }
}

// Releases the WebGL context and GPU memory. Browsers allow only a handful of
// live contexts, so leaving a model page without this would eventually blank the viewer.
function disposeViewer() {
  viewerGeneration += 1;                 // late-arriving loader callbacks must not touch the next view
  if (animId) { cancelAnimationFrame(animId); animId = null; }
  if (viewerResizeHandler) { window.removeEventListener('resize', viewerResizeHandler); viewerResizeHandler = null; }
  if (scene) {
    scene.traverse((object) => {
      if (object.geometry) object.geometry.dispose();
      const materials = Array.isArray(object.material) ? object.material : (object.material ? [object.material] : []);
      for (const material of materials) {
        for (const value of Object.values(material)) if (value && value.isTexture) value.dispose();
        material.dispose();
      }
    });
  }
  if (controls) controls.dispose();
  if (renderer) {
    const spent = renderer.domElement;
    renderer.dispose();
    renderer.forceContextLoss();
    // a canvas whose context was force-lost can never get a working one again, so the next model's
    // view gets a fresh canvas in its place (reusing it left every later 3D view blank)
    const fresh = document.createElement('canvas');
    fresh.id = spent.id;
    fresh.className = spent.className;
    spent.replaceWith(fresh);
  }
  renderer = scene = camera = controls = null;
  resetViewerTools();
}

// ---------- 3D view tools: measure, section, compare ----------
const viewerTools = { measure: false, points: [], marks: [], line: null, plane: null, section: false, compare: null };
const COMPARE_COLOR = 0xff8a3d;

function resetViewerTools() {
  Object.assign(viewerTools, { measure: false, points: [], marks: [], line: null, plane: null, section: false, compare: null });
}

function viewerMeshes(skipCompare) {
  const out = [];
  if (!scene) return out;
  scene.traverse(o => { if (o.isMesh && !(skipCompare && viewerTools.compare && viewerTools.compare.getObjectById(o.id))) out.push(o); });
  return out;
}

function viewerBox() {
  const box = new THREE.Box3();
  viewerMeshes(true).forEach(m => box.expandByObject(m));
  return box;
}

function clearMeasure() {
  viewerTools.marks.forEach(m => { scene.remove(m); m.geometry.dispose(); m.material.dispose(); });
  if (viewerTools.line) { scene.remove(viewerTools.line); viewerTools.line.geometry.dispose(); viewerTools.line.material.dispose(); }
  viewerTools.marks = [];
  viewerTools.points = [];
  viewerTools.line = null;
}

function applySection(axis, position) {
  const normals = { x: [-1, 0, 0], y: [0, -1, 0], z: [0, 0, -1] };
  const plane = new THREE.Plane(new THREE.Vector3(...normals[axis]), position);
  viewerTools.plane = plane;
  renderer.localClippingEnabled = true;
  viewerMeshes(false).forEach(mesh => {
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    mats.forEach(m => { m.clippingPlanes = viewerTools.section ? [plane] : []; m.side = viewerTools.section ? THREE.DoubleSide : THREE.FrontSide; m.needsUpdate = true; });
  });
}

async function loadComparisonObject(model) {
  const loaders = { '.stl': () => new STLLoader(), '.obj': () => new OBJLoader(), '.3mf': () => new ThreeMFLoader(), '.fbx': () => new FBXLoader() };
  const make = loaders[model.extension];
  if (!make) throw new Error(`${model.extension} files cannot be compared here`);
  const loaded = await make().loadAsync(`/api/library/models/${model.id}/file`);
  const object = loaded.isBufferGeometry ? new THREE.Mesh(loaded) : loaded;
  const material = new THREE.MeshStandardMaterial({ color: COMPARE_COLOR, transparent: true, opacity: 0.55, depthWrite: false });
  object.traverse(child => { if (child.isMesh) child.material = material; });
  const box = new THREE.Box3().setFromObject(object);
  object.position.sub(box.getCenter(new THREE.Vector3()));      // lined up by their centers
  return { object, size: box.getSize(new THREE.Vector3()) };
}

async function ensureViewerTools(model) {
  let bar = $('#viewer-tools');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'viewer-tools';
    bar.className = 'viewer-tools';
    $('#viewer-canvas-wrap').insertAdjacentElement('afterend', bar);
  }
  const others = [];
  if (model.family) model.family.members.filter(m => m.id !== model.id).forEach(m => others.push({ id: m.id, label: `${m.filename}${m.version_label ? ' (' + m.version_label + ')' : ''}` }));
  const found = await fetch(`/api/families/suggest/${model.id}`).then(r => (r.ok ? r.json() : { suggestions: [] }));
  found.suggestions.forEach(s => { if (!others.some(o => o.id === s.id)) others.push({ id: s.id, label: s.filename }); });
  if (!currentModel || currentModel.id !== model.id) return;
  bar.innerHTML = `
    <button id="tool-measure" type="button" title="Click two points on the model">Measure</button>
    <button id="tool-section" type="button" title="Cut the model with a plane to look inside">Section</button>
    <select id="section-axis" class="hidden" aria-label="Section axis"><option value="z">Z (height)</option><option value="x">X</option><option value="y">Y</option></select>
    <input id="section-pos" class="hidden" type="range" min="0" max="100" value="100" aria-label="Section position">
    ${others.length ? `<select id="tool-compare" aria-label="Compare with another version"><option value="">Compare with...</option>${others.map(o => `<option value="${o.id}">${esc(o.label)}</option>`).join('')}</select>` : ''}
    <span id="tool-readout" class="muted"></span>`;
  const readout = $('#tool-readout');

  $('#tool-measure').onclick = () => {
    viewerTools.measure = !viewerTools.measure;
    $('#tool-measure').classList.toggle('active', viewerTools.measure);
    if (!viewerTools.measure) clearMeasure();
    readout.textContent = viewerTools.measure ? 'Click one point, then another.' : '';
  };

  const canvas = renderer.domElement;
  let down = null;
  canvas.addEventListener('pointerdown', (e) => { down = [e.clientX, e.clientY]; });
  canvas.addEventListener('pointerup', (e) => {
    if (!viewerTools.measure || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObjects(viewerMeshes(true), false).find(h => !viewerTools.plane || !viewerTools.section || viewerTools.plane.distanceToPoint(h.point) >= 0);
    if (!hit) return;
    if (viewerTools.points.length >= 2) clearMeasure();
    viewerTools.points.push(hit.point.clone());
    const size = viewerBox().getSize(new THREE.Vector3());
    const dot = new THREE.Mesh(new THREE.SphereGeometry(Math.max(size.x, size.y, size.z, 1) / 90, 12, 8), new THREE.MeshBasicMaterial({ color: 0xff3b30, depthTest: false }));
    dot.position.copy(hit.point);
    dot.renderOrder = 10;
    scene.add(dot);
    viewerTools.marks.push(dot);
    if (viewerTools.points.length === 2) {
      const [a, b] = viewerTools.points;
      const geometry = new THREE.BufferGeometry().setFromPoints([a, b]);
      viewerTools.line = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: 0xff3b30, depthTest: false }));
      viewerTools.line.renderOrder = 10;
      scene.add(viewerTools.line);
      const d = b.clone().sub(a);
      readout.textContent = `${a.distanceTo(b).toFixed(2)} mm  (x ${Math.abs(d.x).toFixed(2)}, y ${Math.abs(d.y).toFixed(2)}, z ${Math.abs(d.z).toFixed(2)})`;
    } else {
      readout.textContent = 'Now click the second point.';
    }
  });

  const updateSection = () => {
    const axis = $('#section-axis').value;
    const box = viewerBox();
    const lo = box.min[axis], hi = box.max[axis];
    const position = lo + (hi - lo) * (parseInt($('#section-pos').value) / 100);
    applySection(axis, position);
    readout.textContent = viewerTools.section ? `Cut at ${axis.toUpperCase()} = ${position.toFixed(1)} (range ${lo.toFixed(1)} to ${hi.toFixed(1)})` : '';
  };
  $('#tool-section').onclick = () => {
    viewerTools.section = !viewerTools.section;
    $('#tool-section').classList.toggle('active', viewerTools.section);
    $('#section-axis').classList.toggle('hidden', !viewerTools.section);
    $('#section-pos').classList.toggle('hidden', !viewerTools.section);
    updateSection();
  };
  $('#section-axis').onchange = updateSection;
  $('#section-pos').oninput = updateSection;

  const compare = $('#tool-compare');
  if (compare) compare.onchange = async () => {
    if (viewerTools.compare) { scene.remove(viewerTools.compare); viewerTools.compare = null; }
    if (!compare.value) { readout.textContent = ''; return; }
    readout.textContent = 'Loading the other version...';
    try {
      const other = await fetch(`/api/library/models/${compare.value}`).then(r => r.json());
      const { object, size } = await loadComparisonObject(other);
      if (!scene || !currentModel || currentModel.id !== model.id) return;
      viewerTools.compare = object;
      scene.add(object);
      const mine = viewerBox().getSize(new THREE.Vector3());
      const diff = size.clone().sub(mine);
      readout.textContent = `Orange = ${other.filename}, lined up by their centers. Size difference (x, y, z): ${diff.x.toFixed(1)}, ${diff.y.toFixed(1)}, ${diff.z.toFixed(1)} mm.`;
    } catch (err) {
      readout.textContent = String(err.message || err);
    }
  };
}

let viewerResizeHandler = null;

function initViewer() {
  const canvas = $('#viewer-canvas');
  const wrap = $('#viewer-canvas-wrap');
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setSize(wrap.clientWidth, wrap.clientHeight);
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d0f12);
  camera = new THREE.PerspectiveCamera(45, wrap.clientWidth / wrap.clientHeight, 0.1, 10000);
  camera.position.set(50, 50, 100);
  controls = new OrbitControls(camera, renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2));
  const dirLight = new THREE.DirectionalLight(0xffffff, 1.5);
  dirLight.position.set(1, 1, 1);
  scene.add(dirLight);

  function animate() {
    animId = requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  // the modal's canvas height changes with viewport width/orientation on
  // mobile (see the #viewer-canvas-wrap mobile media query), so the
  // renderer/camera need to re-fit rather than stretching the last frame
  if (viewerResizeHandler) window.removeEventListener('resize', viewerResizeHandler);
  viewerResizeHandler = () => {
    if (!renderer || !wrap.clientWidth || !wrap.clientHeight) return;
    renderer.setSize(wrap.clientWidth, wrap.clientHeight);
    camera.aspect = wrap.clientWidth / wrap.clientHeight;
    camera.updateProjectionMatrix();
  };
  window.addEventListener('resize', viewerResizeHandler);
}

// A loader finishing after you moved to another model (or page) must not add its
// geometry to the new scene, so each remembers which view it was started for.
function addToScene(object, generation) {
  if (generation !== viewerGeneration || !scene) return;
  scene.add(object);
  frameCameraOn(object);
}

function loadSTL(url) {
  const generation = viewerGeneration;
  const loader = new STLLoader();
  loader.load(url, (geometry) => {
    geometry.center();
    addToScene(new THREE.Mesh(geometry, createViewerMaterial()), generation);
  }, undefined, (err) => showViewerError(err, generation));
}

function loadOBJ(url) {
  const generation = viewerGeneration;
  const loader = new OBJLoader();
  loader.load(url, (object) => {
    object.traverse((child) => {
      if (child.isMesh) child.material = createViewerMaterial();
    });
    addToScene(object, generation);
  }, undefined, (err) => showViewerError(err, generation));
}

function load3MF(url) {
  const generation = viewerGeneration;
  const loader = new ThreeMFLoader();
  loader.load(url, (object) => addToScene(object, generation), undefined, (err) => showViewerError(err, generation));
}

function loadFBX(url) {
  const generation = viewerGeneration;
  const loader = new FBXLoader();
  loader.load(url, (object) => {
    object.traverse((child) => {
      if (child.isMesh) child.material = createViewerMaterial();
    });
    addToScene(object, generation);
  }, undefined, (err) => showViewerError(err, generation));
}

// occt-import-js is a ~7MB WASM CAD kernel -- loaded lazily on first STEP
// view, not at page load, and cached (module init is expensive, running it
// twice per session would be wasteful).
let occtModulePromise = null;
function getOcctModule() {
  if (!occtModulePromise) {
    occtModulePromise = window.occtimportjs({ locateFile: (path) => `/assets/vendor/${path}` });
  }
  return occtModulePromise;
}

async function loadSTEP(url) {
  const generation = viewerGeneration;
  $('#viewer-info').insertAdjacentHTML('beforeend',
    '<div id="step-loading-note">Loading STEP geometry (this can take a few seconds)…</div>');
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Could not fetch file (${res.status})`);
    const fileBuffer = new Uint8Array(await res.arrayBuffer());
    const occt = await getOcctModule();
    if (generation !== viewerGeneration) return;
    const result = occt.ReadStepFile(fileBuffer, null);
    if (!result.success || !result.meshes.length) {
      throw new Error('STEP file parsed but contained no visible geometry (assembly-only or metadata-only file?)');
    }
    const group = new THREE.Group();
    const defaultMaterial = createViewerMaterial();
    for (const resultMesh of result.meshes) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(resultMesh.attributes.position.array, 3));
      if (resultMesh.attributes.normal) {
        geometry.setAttribute('normal', new THREE.Float32BufferAttribute(resultMesh.attributes.normal.array, 3));
      }
      geometry.setIndex(resultMesh.index.array);
      if (!resultMesh.attributes.normal) geometry.computeVertexNormals();
      const material = resultMesh.color
        ? new THREE.MeshStandardMaterial({ color: new THREE.Color(resultMesh.color[0], resultMesh.color[1], resultMesh.color[2]) })
        : defaultMaterial;
      group.add(new THREE.Mesh(geometry, material));
    }
    addToScene(group, generation);
  } catch (err) {
    showViewerError(err, generation);
  } finally {
    document.getElementById('step-loading-note')?.remove();
  }
}

function showViewerError(err, generation) {
  console.error(err);
  if (generation !== undefined && generation !== viewerGeneration) return;
  $('#viewer-info').insertAdjacentHTML('beforeend',
    `<div class="error-text">Failed to load model preview: ${esc(err.message || err)}</div>`);
}

// centers geometry at the origin and points the camera/orbit target at it,
// regardless of whether the loader returned a raw Mesh (STL) or a Group (OBJ/3MF)
function frameCameraOn(object3d) {
  const box = new THREE.Box3().setFromObject(object3d);
  const center = box.getCenter(new THREE.Vector3());
  object3d.position.sub(center);
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z, 1) / 2;
  camera.position.set(1, 0.8, 1.2).normalize().multiplyScalar(radius * 1.9);
  camera.near = radius / 100;
  camera.far = radius * 100;
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
}

// ---------- Match a model to its Printables / MakerWorld listing ----------
const PROVIDER_LABELS = {
  printables: 'Printables', makerworld: 'MakerWorld', sketchfab: 'Sketchfab', thingiverse: 'Thingiverse',
  myminifactory: 'MyMiniFactory', cults3d: 'Cults3D', commons: 'Wikimedia Commons', nasa3d: 'NASA 3D Resources', smithsonian: 'Smithsonian 3D', archive: 'Internet Archive (Thingiverse)',
};

function parseJsonList(value) {
  try { const list = JSON.parse(value || '[]'); return Array.isArray(list) ? list : []; }
  catch (e) { return []; }
}

function safeHttpUrl(url) {
  return /^https?:\/\//i.test(url || '') ? url : '';
}

function renderViewerSource(model) {
  const box = $('#viewer-source');
  if (!model.source_provider) {
    box.innerHTML = `
      <div class="source-card">
        <div class="row">
          <button id="source-find">Find this model online</button>
          <input id="source-url" placeholder="...or paste a model link">
          <button id="source-link-url">Link</button>
        </div>
        <div class="source-options">
          <label><input type="checkbox" id="source-opt-images" checked> Save pictures</label>
          <label><input type="checkbox" id="source-opt-fill" checked> Fill designer / license</label>
          <label><input type="checkbox" id="source-opt-tags"> Add the site's tags</label>
        </div>
        <div id="source-status" class="muted"></div>
        <div id="source-results"></div>
      </div>`;
    $('#source-find').onclick = () => findSourceMatches(model);
    $('#source-link-url').onclick = () => {
      const url = $('#source-url').value.trim();
      if (url) linkSource(model, { url });
    };
    return;
  }

  const images = parseJsonList(model.source_images);
  const tags = parseJsonList(model.source_tags);
  const stamp = model.source_synced_at ? encodeURIComponent(model.source_synced_at) : '';
  const description = model.source_description || '';
  box.innerHTML = `
    <div class="source-card">
      <div class="source-head">
        <span class="badge">${esc(PROVIDER_LABELS[model.source_provider] || model.source_provider)}</span>
        ${model.source_linked_by === 'auto' ? '<span class="badge warn-badge" title="Linked automatically by the matching job">auto-linked</span>' : ''}
        ${safeHttpUrl(model.source_url)
          ? `<a href="${esc(model.source_url)}" target="_blank" rel="noopener noreferrer">${esc(model.source_title || model.source_url)}</a>`
          : esc(model.source_title || '')}
      </div>
      <div class="muted">${[model.designer && `by ${esc(model.designer)}`, model.license && esc(model.license)].filter(Boolean).join(' · ')}</div>
      ${images.length ? `<div class="source-gallery">${images.map(n =>
        `<a href="/api/library/models/${model.id}/source/images/${n}?v=${stamp}" target="_blank" rel="noopener"><img src="/api/library/models/${model.id}/source/images/${n}?v=${stamp}" loading="lazy" alt=""></a>`).join('')}</div>` : ''}
      ${tags.length ? `<div class="source-tags">${tags.map(t => `<span class="chip">${esc(t)}</span>`).join('')}</div>` : ''}
      ${description ? `<details><summary>Description</summary><div class="source-desc">${esc(description)}</div></details>` : ''}
      <div class="row">
        ${model.source_linked_by === 'auto' ? '<button id="source-confirm">Looks right</button>' : ''}
        <button id="source-refresh">Re-fetch from site</button>
        <button id="source-add-tags">Add site tags</button>
        <button id="source-unlink">Unlink</button>
        <span id="source-status" class="muted"></span>
      </div>
    </div>`;
  if ($('#source-confirm')) {
    $('#source-confirm').onclick = async () => {
      await fetch(`/api/source-match/models/${model.id}/confirm`, { method: 'POST' });
      model.source_linked_by = 'manual';
      renderModelPage(model);
    };
  }
  $('#source-refresh').onclick = () => linkSource(model, { provider: model.source_provider, source_id: model.source_id });
  $('#source-add-tags').onclick = () => linkSource(model,
    { provider: model.source_provider, source_id: model.source_id, images: false, fill_details: false, add_tags: true });
  $('#source-unlink').onclick = async () => {
    if (!confirm('Unlink this model from its listing and delete the saved pictures?')) return;
    const res = await fetch(`/api/library/models/${model.id}/source`, { method: 'DELETE' });
    if (res.ok) {
      Object.assign(model, await res.json());
      renderModelPage(model);
    }
  };
}

async function sourceErrorText(res) {
  const err = await res.json().catch(() => ({}));
  return err.detail || `Request failed (${res.status})`;
}

async function findSourceMatches(model) {
  const status = $('#source-status');
  const results = $('#source-results');
  status.textContent = 'Searching...';
  results.innerHTML = '';
  const res = await fetch(`/api/library/models/${model.id}/source/suggest`);
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  const data = await res.json();
  const problems = Object.entries(data.errors || {}).map(([p, msg]) => `${PROVIDER_LABELS[p] || p}: ${msg}`);
  status.textContent = (data.results.length
    ? `Matches for "${data.query}" (best first)` : `No matches for "${data.query}".`)
    + (problems.length ? ` ${problems.join('; ')}` : '');
  results.innerHTML = data.results.map((r, i) => `
    <div class="source-result" data-index="${i}">
      ${r.thumbnail ? `<img src="${esc(r.thumbnail)}" loading="lazy" referrerpolicy="no-referrer" alt="">` : '<div class="source-noimg"></div>'}
      <div class="source-result-info">
        <div><b>${esc(r.title)}</b></div>
        <div class="muted">${esc(PROVIDER_LABELS[r.provider] || r.provider)} · ${esc(r.designer)}${r.license ? ' · ' + esc(r.license) : ''} · ${Math.round(r.score * 100)}% match</div>
        <a href="${esc(safeHttpUrl(r.url))}" target="_blank" rel="noopener noreferrer">view listing</a>
      </div>
      <button class="source-pick">Link</button>
    </div>`).join('');
  results.querySelectorAll('.source-pick').forEach((btn) => {
    btn.onclick = () => {
      const r = data.results[parseInt(btn.closest('.source-result').dataset.index)];
      linkSource(model, { provider: r.provider, source_id: r.source_id });
    };
  });
}

async function linkSource(model, listing) {
  const status = $('#source-status');
  status.textContent = 'Fetching from the site...';
  const body = { ...listing };
  if ($('#source-opt-images')) {
    body.images = $('#source-opt-images').checked;
    body.fill_details = $('#source-opt-fill').checked;
    body.add_tags = $('#source-opt-tags').checked;
  }
  const res = await jsonRequest('POST', `/api/library/models/${model.id}/source`, body);
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  Object.assign(model, await res.json());
  renderModelPage(model);
  $('#source-status').textContent = 'Saved.';
}

// ---------- Print estimate + add-to-queue (model page) ----------
let lastEstimate = null;

async function runEstimate(modelId) {
  const material = $('#viewer-est-material').value;
  const infillPct = parseFloat($('#viewer-est-infill').value);
  const infill = isNaN(infillPct) ? 0.15 : infillPct / 100;
  $('#viewer-estimate-result').textContent = 'Estimating…';
  const res = await fetch(`/api/library/models/${modelId}/estimate?material=${material}&infill=${infill}`);
  const est = res.ok ? await res.json() : {};
  lastEstimate = est;
  if (est.estimated_grams == null && est.estimated_minutes == null) {
    $('#viewer-estimate-result').textContent = est.note || 'Estimate unavailable.';
    return;
  }
  const grams = est.estimated_grams != null ? `${est.estimated_grams} g` : '? g';
  const mins = est.estimated_minutes != null ? `${Math.round(est.estimated_minutes)} min` : '? min';
  let text = `${grams} · ${mins} (${est.source})`;
  const learnedRes = await fetch(`/api/estimates/${modelId}${est.estimated_minutes != null ? `?base=${est.estimated_minutes}` : ''}`);
  const learned = learnedRes.ok ? await learnedRes.json() : {};
  if (learned.basis === 'history' || learned.basis === 'adjusted') {
    est.estimated_minutes = learned.minutes;                   // the queue gets the time your own prints suggest
    text += learned.basis === 'history'
      ? ` · your own ${learned.samples === 1 ? 'print' : learned.samples + ' prints'} took about ${formatMinutes(learned.minutes)}`
      : ` · prints here usually take ${learned.factor}x the estimate, so about ${formatMinutes(learned.minutes)}`;
  }
  $('#viewer-estimate-result').textContent = text;
}

async function addToQueue(modelId) {
  const filamentId = $('#viewer-queue-filament').value || null;
  await fetch('/api/queue', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model_id: modelId,
      filament_id: filamentId ? parseInt(filamentId) : null,
      estimated_grams: lastEstimate ? lastEstimate.estimated_grams : null,
      estimated_minutes: lastEstimate ? lastEstimate.estimated_minutes : null,
    }),
  });
  await refreshModelPage();
}

// ---------- Projects ----------
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url || '') ? url : '';
}

function jsonRequest(method, url, body) {
  return fetch(url, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function renderPartRow(projectId, part) {
  const url = safeUrl(part.purchase_url);
  const cost = part.unit_cost != null ? `$${part.unit_cost.toFixed(2)}` : '';
  return `
    <tr class="${part.quantity_needed ? '' : 'part-complete'}" data-project="${projectId}" data-part="${part.id}" data-name="${esc(part.name.toLowerCase())}">
      <td>${esc(part.name)}${part.notes ? `<br><small>${esc(part.notes)}</small>` : ''}</td>
      <td>${esc(part.category)}</td>
      <td>${part.quantity}</td>
      <td><input type="number" min="0" class="part-owned" value="${part.quantity_owned}" aria-label="Quantity owned"></td>
      <td>${part.quantity_needed ? `<b>${part.quantity_needed}</b>` : 'have all'}</td>
      <td>${cost}</td>
      <td>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">buy</a>` : ''}</td>
      <td><button class="part-del">delete</button></td>
    </tr>`;
}

let projectSpools = [];
let projectSupplies = [];

function spoolOptions(selectedId) {
  return projectSpools.map(f => `<option value="${f.id}" ${f.id === selectedId ? 'selected' : ''}>${
    esc([f.material, f.brand, f.color].filter(Boolean).join(' '))} (${f.remaining_g}g left)</option>`).join('');
}

function renderProjectModel(m, locked) {
  const lines = m.filament.map(l => `
    <div class="filament-line" data-line="${l.id}">
      <select class="line-spool" ${locked ? 'disabled' : ''}>${spoolOptions(l.filament_id)}${
        l.remaining_g == null ? `<option value="${l.filament_id}" selected>(deleted spool)</option>` : ''}</select>
      <input class="line-grams" type="number" min="0" step="0.1" value="${l.grams}" aria-label="Grams" ${locked ? 'disabled' : ''}> g
      ${locked ? '' : '<button class="line-del" title="Remove this filament">&times;</button>'}
    </div>`).join('');
  const picture = (m.source_images || [])[0];
  const thumb = m.thumbnail_path ? `/api/library/thumbnails/${esc(m.thumbnail_path)}`
    : (picture ? `/api/library/models/${m.id}/source/images/${picture}` : '');
  const by = [m.designer && `by ${esc(m.designer)}`, m.license && esc(m.license)].filter(Boolean).join(' · ');
  return `
    <div class="project-model" data-model="${m.id}" data-source-id="${esc(m.source_id || '')}">
      <div class="project-model-top">
        ${thumb ? `<a href="#/model/${m.id}"><img class="pm-thumb" src="${thumb}" alt="" loading="lazy"></a>` : ''}
        <div class="project-model-head">
          <a href="#/model/${m.id}"><b>${esc(m.filename)}</b></a>
          ${m.filament_grams ? `<span class="muted">${m.filament_grams}g</span>` : ''}
          ${by || safeUrl(m.source_url) ? `<div class="muted pm-by">${by}${safeUrl(m.source_url) ? ` <a href="${esc(m.source_url)}" target="_blank" rel="noopener noreferrer">listing</a>` : ''}</div>` : ''}
          <div class="pm-actions">
            ${m.source_provider === 'makerworld' ? '<button class="model-import-parts" title="Add the parts list from the MakerWorld listing this model is linked to">Import parts</button>' : ''}
            ${locked ? '' : `<button class="model-estimate" title="Estimate filament use for this model">Estimate</button>
            <button class="model-unlink" title="Remove from project">&times;</button>`}
          </div>
          <span class="model-estimate-note muted"></span>
        </div>
      </div>
      <div class="filament-suggest"></div>
      ${lines}
      ${locked ? '' : `
      <div class="filament-line filament-add">
        <select class="new-line-spool">${projectSpools.length ? spoolOptions(null) : '<option value="">(add filament in the Filament tab first)</option>'}</select>
        <input class="new-line-grams" type="number" min="0" step="0.1" placeholder="grams" aria-label="Grams">
        <button class="line-add" ${projectSpools.length ? '' : 'disabled'}>Add filament</button>
      </div>`}
    </div>`;
}

function projectSummaryText(p) {
  const cost = p.cost_needed ? ` · $${p.cost_needed.toFixed(2)} to buy` : '';
  return p.parts_total
    ? (p.parts_missing ? `${p.parts_missing} of ${p.parts_total} parts needed${cost}` : 'all parts on hand')
    : 'no parts listed';
}

// one project, full page (#/project/ID)
function renderProject(p) {
  const locked = p.filament_deducted;
  const models = p.models.map(m => renderProjectModel(m, locked)).join('');
  const totals = p.filament_totals.map(t => `
    <span class="${t.short ? 'filament-short' : ''}">${esc(t.filament_label)}: ${t.grams}g${t.remaining_g != null ? ` (spool has ${t.remaining_g}g)` : ''}${t.short ? ' &mdash; not enough!' : ''}</span>`).join(' · ');
  const filamentSummary = p.filament_totals.length
    ? `<div class="project-summary">Filament: <b>${p.filament_grams}g</b> &mdash; ${totals}
        ${locked ? '<br><small>Already subtracted from inventory. Move the project back to planning/building to undo.</small>' : ''}</div>`
    : '';
  const c = p.cost || { total: 0 };
  const money = v => `$${Number(v).toFixed(2)}`;
  const costPanel = c.total > 0 ? `
    <div class="project-summary project-cost">Cost: <b>${money(c.total)}</b> &mdash;
      filament ${money(c.filament)}${c.filament_unpriced_g ? ` <small>(${c.filament_unpriced_g} g on spools without a price)</small>` : ''}
      &middot; parts ${money(c.parts)}${c.parts_unpriced ? ` <small>(${c.parts_unpriced} without a price)</small>` : ''}
      ${c.electricity != null ? `&middot; electricity ${money(c.electricity)} <small>(about ${c.print_hours} h at ${c.printer_watts} W, rough)</small>` : ''}
    </div>` : '';
  return `
    <div class="project-card page-card" data-project="${p.id}" data-locked="${locked ? 1 : 0}">
      <div class="project-head">
        <input class="project-field project-title" data-field="name" value="${esc(p.name)}" aria-label="Project name">
        <select class="project-status" aria-label="Status">
          ${['planning', 'building', 'printed', 'done'].map(s => `<option value="${s}" ${s === p.status ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        <a class="button-link" href="/api/projects/${p.id}/export.pdf" download>Export PDF</a>
        <button class="project-del">Delete project</button>
      </div>
      <span class="project-save-status muted"></span>
      <div class="stack">
        <label>Description <textarea class="project-field" data-field="description" rows="2" placeholder="What is this project?">${esc(p.description || '')}</textarea></label>
        <label>Notes <textarea class="project-field" data-field="notes" rows="4" placeholder="Print settings, assembly notes, links...">${esc(p.notes || '')}</textarea></label>
      </div>
      <div class="project-summary">${projectSummaryText(p)}</div>
      ${filamentSummary}
      ${costPanel}

      <h3>3D models</h3>
      ${models ? `<div class="project-models">${models}</div>`
        : '<p class="muted">No models yet. Open a model from the Library and use <b>Add to project</b>.</p>'}

      <h3>Parts</h3>
      ${p.parts.length ? `
      <div class="table-scroll">
        <table class="parts-table"><thead><tr>
          <th>Part</th><th>Type</th><th>Need</th><th>Own</th><th>To get</th><th>Unit cost</th><th></th><th></th>
        </tr></thead><tbody>${p.parts.map(part => renderPartRow(p.id, part)).join('')}</tbody></table>
      </div>` : '<p class="muted">No parts yet.</p>'}
      <div class="row part-add">
        <input class="new-part-name" list="supply-names" placeholder="Part name (e.g. ESP32, M3x8 screws, solder)">
        <select class="new-part-category">
          <option value="electronics">electronics</option>
          <option value="parts">parts</option>
          <option value="supplies">supplies</option>
        </select>
        <input class="new-part-qty" type="number" min="1" value="1" aria-label="Quantity needed" title="Quantity needed">
        <input class="new-part-owned" type="number" min="0" value="0" aria-label="Quantity owned" title="Quantity already owned">
        <input class="new-part-cost" type="number" min="0" step="0.01" placeholder="Unit cost" aria-label="Unit cost">
        <input class="new-part-url" placeholder="Purchase link (optional)">
        <button class="part-add-btn">Add Part</button>
      </div>
      <div class="parts-import-bar">
        <button class="parts-import-toggle">Import parts from a MakerWorld listing</button>
      </div>
      <div class="parts-import hidden">
        <div class="row">
          <input class="parts-import-url" placeholder="Paste a MakerWorld model link">
          <button class="parts-import-fetch">Get parts list</button>
        </div>
        <div class="parts-import-status muted"></div>
        <div class="parts-import-results"></div>
      </div>
    </div>`;
}

let currentProjectId = null;

async function openProjectPage(id, token) {
  const body = $('#project-page-body');
  body.innerHTML = '<p class="muted">Loading...</p>';
  const [res, spools, supplies] = await Promise.all([
    fetch(`/api/projects/${id}`),
    fetch('/api/filament').then(r => r.json()),
    fetch('/api/inventory').then(r => (r.ok ? r.json() : [])),
  ]);
  if (token !== routeToken) return;
  if (!res.ok) { body.innerHTML = '<p class="muted">Project not found.</p>'; return; }
  projectSpools = spools;
  projectSupplies = supplies;
  // typing a part name suggests (and, on pick, pre-fills type and cost from) the supplies you own
  $('#supply-names').innerHTML = [...new Set(supplies.map(i => i.name))].map(n => `<option value="${esc(n)}"></option>`).join('');
  const project = await res.json();
  if (token !== routeToken) return;
  currentProjectId = id;
  document.title = `${project.name} - Model Hub`;
  body.innerHTML = renderProject(project);
  loadFilamentSuggestions(body);
  renderSharePanel('#project-share-panel', 'project', id);
}

// after a change on the project page, redraw it; elsewhere refresh the list
function refreshProjectViews() {
  if (/^#\/project\/\d+$/.test(location.hash)) return openProjectPage(currentProjectId, routeToken);
  return loadProjects();
}

// What the model's listing recommends (MakerWorld), matched to the spools you own
async function loadFilamentSuggestions(root) {
  const card = root.querySelector('.project-card');
  if (!card || card.dataset.locked === '1') return;
  const projectId = card.dataset.project;
  for (const box of card.querySelectorAll('.project-model')) {
    if (!box.dataset.sourceId) continue;
    const res = await fetch(`/api/projects/${projectId}/models/${box.dataset.model}/filament-suggestions`);
    if (!res.ok || !document.contains(box)) continue;
    const rows = await res.json();
    if (!rows.length) continue;
    box.querySelector('.filament-suggest').innerHTML = '<div class="muted">The listing suggests:</div>' + rows.map(r => `
      <div class="suggest-row" data-material="${esc(r.material)}" data-spool="${r.spool_id ?? ''}">
        <span>${esc(r.label)}</span>
        ${r.spool_id
          ? (r.already_added ? '<span class="muted">already added</span>'
            : `<button class="suggest-add">Use ${esc(r.spool_label)}</button>`)
          : '<span class="muted">no matching spool in your Filament list</span>'}
      </div>`).join('');
  }
}

// the Projects tab: an overview, each card opens the project's page
function renderProjectSummary(p) {
  return `
    <a class="project-summary-card" href="#/project/${p.id}">
      <div class="psc-top"><b>${esc(p.name)}</b><span class="status-badge status-${esc(p.status)}">${esc(p.status)}</span></div>
      ${p.description ? `<div class="muted psc-desc">${esc(p.description)}</div>` : ''}
      <div class="project-summary">${p.models.length} model${p.models.length === 1 ? '' : 's'} &middot; ${projectSummaryText(p)}</div>
    </a>`;
}

async function loadProjects() {
  const onlyNeeding = $('#projects-needing-only').checked;
  const res = await fetch(`/api/projects?only_needing_parts=${onlyNeeding}`);
  const projects = res.ok ? await res.json() : [];
  $('#projects-list').innerHTML = projects.length
    ? `<div class="project-summary-grid">${projects.map(renderProjectSummary).join('')}</div>`
    : `<p class="muted">${onlyNeeding ? 'No projects need parts right now.' : 'No projects yet. Add one above.'}</p>`;
  if (!$('#shopping-list').classList.contains('hidden')) loadShoppingList();
}

let shoppingCombine = false;

// navigator.clipboard only exists on https / localhost, and this app is usually
// opened over plain http on a LAN address, so fall back to a hidden textarea
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through to the textarea method */ }
  const area = document.createElement('textarea');
  area.value = text;
  area.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  area.remove();
  return ok;
}

async function loadShoppingList() {
  const combine = shoppingCombine ? 'true' : 'false';
  const data = await (await fetch(`/api/projects/shopping-list?combine=${combine}`)).json();
  const box = $('#shopping-list');
  box.classList.remove('hidden');
  const rows = data.items.map(i => `
    <tr>
      <td>${esc(i.name)}</td><td>${esc(i.category)}</td><td>${i.quantity_needed}</td>
      <td>${i.in_stock ? `<span class="in-stock" title="Same-named item in Supplies">${i.in_stock} in stock</span>` : ''}</td>
      <td>${esc(i.project_name)}</td>
      <td>${i.cost_needed != null ? `$${i.cost_needed.toFixed(2)}` : ''}</td>
      <td>${safeUrl(i.purchase_url) ? `<a href="${esc(safeUrl(i.purchase_url))}" target="_blank" rel="noopener noreferrer">buy</a>` : ''}</td>
    </tr>`).join('');
  box.innerHTML = `
    <h3>Shopping List <small>(parts still needed for projects that aren't done)</small></h3>
    <div class="toolbar">
      <label><input type="checkbox" id="shopping-combine" ${shoppingCombine ? 'checked' : ''}> Combine identical parts across projects</label>
      ${data.items.length ? `
      <a class="button-link" href="/api/projects/shopping-list/export?format=csv&combine=${combine}" download>Download CSV</a>
      <a class="button-link" href="/api/projects/shopping-list/export?format=txt&combine=${combine}" download>Download text</a>
      <button id="shopping-copy">Copy as text</button>
      <span id="shopping-copy-status" class="muted"></span>` : ''}
    </div>
    ${data.items.length ? `
    <div class="table-scroll"><table class="parts-table"><thead><tr>
      <th>Part</th><th>Type</th><th>Qty</th><th>Have</th><th>Project</th><th>Cost</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="project-summary">Estimated total: $${data.total_cost.toFixed(2)}</div>`
      : '<p class="muted">Nothing to buy -- every project has the parts it needs.</p>'}
    ${(data.low_filament || []).length ? `
    <h3>Filament running low</h3>
    <ul class="link-list">${data.low_filament.map(f => `<li>${esc(f.label)} <span class="muted">${f.remaining_g} g left${f.cost != null ? ` &middot; last price $${Number(f.cost).toFixed(2)}` : ''}</span>
      ${safeUrl(f.purchase_url) ? `<a href="${esc(safeUrl(f.purchase_url))}" target="_blank" rel="noopener noreferrer">buy</a>` : ''}</li>`).join('')}</ul>` : ''}`;
  $('#shopping-combine').onchange = (e) => { shoppingCombine = e.target.checked; loadShoppingList(); };
  const copyBtn = $('#shopping-copy');
  if (copyBtn) {
    copyBtn.onclick = async () => {
      const res = await fetch(`/api/projects/shopping-list/export?format=txt&combine=${combine}`);
      const ok = res.ok && await copyText(await res.text());
      $('#shopping-copy-status').textContent = ok ? 'Copied.' : 'Could not copy -- use Download text instead.';
    };
  }
}

// ---------- Supplies on hand ----------
let suppliesCache = [];

async function loadSupplies() {
  const params = new URLSearchParams();
  const q = $('#supply-search').value.trim();
  if (q) params.set('q', q);
  if ($('#supply-filter-category').value) params.set('category', $('#supply-filter-category').value);
  if ($('#supply-low-only').checked) params.set('low_stock', 'true');
  const res = await fetch(`/api/inventory?${params}`);
  suppliesCache = res.ok ? await res.json() : [];
  $('#supplies-table tbody').innerHTML = suppliesCache.map(i => `
    <tr data-id="${i.id}" class="${i.low_stock ? 'low-stock' : ''}">
      <td><input class="supply-edit" data-field="name" value="${esc(i.name)}" aria-label="Name">
        ${safeUrl(i.purchase_url) ? `<a href="${esc(safeUrl(i.purchase_url))}" target="_blank" rel="noopener noreferrer">buy</a>` : ''}
        ${i.low_stock ? '<span class="low-flag">low</span>' : ''}</td>
      <td><select class="supply-edit" data-field="category" aria-label="Type">${
        ['electronics', 'parts', 'supplies'].map(c => `<option value="${c}" ${c === i.category ? 'selected' : ''}>${c}</option>`).join('')}</select></td>
      <td><input class="supply-edit supply-num" data-field="quantity" type="number" min="0" value="${i.quantity}" aria-label="Quantity"></td>
      <td><input class="supply-edit supply-num" data-field="min_quantity" type="number" min="0" value="${i.min_quantity}" aria-label="Low stock at"></td>
      <td><input class="supply-edit" data-field="location" value="${esc(i.location || '')}" aria-label="Location"></td>
      <td><input class="supply-edit supply-num" data-field="unit_cost" type="number" min="0" step="0.01" value="${i.unit_cost ?? ''}" aria-label="Unit cost"></td>
      <td><button class="supply-del">delete</button></td>
    </tr>`).join('');
  $('#supplies-empty').classList.toggle('hidden', suppliesCache.length > 0);
  if (!$('#sup-labels-link')) $('#supply-export').insertAdjacentHTML('afterend', ' <a id="sup-labels-link" class="button-link" href="#/labels/supplies">Print QR labels</a>');
}

$('#add-supply-btn').addEventListener('click', async () => {
  const name = $('#supply-name').value.trim();
  if (!name) return;
  const res = await jsonRequest('POST', '/api/inventory', {
    name,
    category: $('#supply-category').value,
    quantity: $('#supply-qty').value || 0,
    min_quantity: $('#supply-min').value || 0,
    location: $('#supply-location').value,
    unit_cost: $('#supply-cost').value,
  });
  if (!res.ok) return alert((await res.json().catch(() => ({}))).detail || 'Could not add item.');
  $('#supply-name').value = '';
  $('#supply-location').value = '';
  $('#supply-cost').value = '';
  loadSupplies();
});
$('#supply-search').addEventListener('input', debounce(loadSupplies, 300));
$('#supply-filter-category').addEventListener('change', loadSupplies);
$('#supply-low-only').addEventListener('change', loadSupplies);

$('#supplies-table').addEventListener('change', async (e) => {
  const input = e.target.closest('.supply-edit');
  if (!input) return;
  const id = input.closest('tr').dataset.id;
  const res = await jsonRequest('PATCH', `/api/inventory/${id}`, { [input.dataset.field]: input.value });
  if (!res.ok) alert((await res.json().catch(() => ({}))).detail || 'Could not save that change.');
  loadSupplies();
});
$('#supplies-table').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('supply-del')) return;
  if (!confirm('Delete this item from your supplies?')) return;
  await fetch(`/api/inventory/${e.target.closest('tr').dataset.id}`, { method: 'DELETE' });
  loadSupplies();
});

$('#add-project-btn').addEventListener('click', async () => {
  const name = $('#new-project-name').value.trim();
  if (!name) return;
  const res = await jsonRequest('POST', '/api/projects', { name, description: $('#new-project-description').value });
  $('#new-project-name').value = '';
  $('#new-project-description').value = '';
  if (res.ok) location.hash = `#/project/${(await res.json()).id}`;   // straight to the new project's page
  else loadProjects();
});
$('#projects-needing-only').addEventListener('change', loadProjects);
$('#shopping-list-btn').addEventListener('click', () => {
  const box = $('#shopping-list');
  if (box.classList.contains('hidden')) loadShoppingList();
  else box.classList.add('hidden');
});

$('#project-page-body').addEventListener('click', async (e) => {
  const card = e.target.closest('.project-card');
  if (!card) return;
  const projectId = card.dataset.project;
  const target = e.target;

  if (target.classList.contains('project-del')) {
    if (!confirm('Delete this project and its parts list?')) return;
    await fetch(`/api/projects/${projectId}`, { method: 'DELETE' });
    location.hash = '#/projects';
    return;
  } else if (target.classList.contains('suggest-add')) {
    const row = target.closest('.suggest-row');
    const box = target.closest('.project-model');
    target.disabled = true;
    target.textContent = 'Adding...';
    const est = await fetch(`/api/library/models/${box.dataset.model}/estimate?material=${encodeURIComponent(row.dataset.material)}`)
      .then(r => (r.ok ? r.json() : {})).catch(() => ({}));
    const res = await jsonRequest('POST', `/api/projects/${projectId}/models/${box.dataset.model}/filament`,
      { filament_id: row.dataset.spool, grams: est.estimated_grams ?? 0 });
    if (!res.ok) { target.disabled = false; return alert((await res.json().catch(() => ({}))).detail || 'Could not add filament.'); }
  } else if (target.classList.contains('part-del')) {
    await fetch(`/api/projects/${projectId}/parts/${target.closest('tr').dataset.part}`, { method: 'DELETE' });
  } else if (target.classList.contains('model-unlink')) {
    const res = await fetch(`/api/projects/${projectId}/models/${target.closest('.project-model').dataset.model}`, { method: 'DELETE' });
    if (!res.ok) return alert((await res.json().catch(() => ({}))).detail || 'Could not remove model.');
  } else if (target.classList.contains('line-add')) {
    const box = target.closest('.project-model');
    const filamentId = box.querySelector('.new-line-spool').value;
    if (!filamentId) return;
    const res = await jsonRequest('POST', `/api/projects/${projectId}/models/${box.dataset.model}/filament`,
      { filament_id: filamentId, grams: box.querySelector('.new-line-grams').value || 0 });
    if (!res.ok) return alert((await res.json().catch(() => ({}))).detail || 'Could not add filament.');
  } else if (target.classList.contains('line-del')) {
    const res = await fetch(`/api/projects/${projectId}/filament/${target.closest('.filament-line').dataset.line}`, { method: 'DELETE' });
    if (!res.ok) return alert((await res.json().catch(() => ({}))).detail || 'Could not remove filament.');
  } else if (target.classList.contains('model-estimate')) {
    await estimateProjectModel(projectId, target.closest('.project-model'));
    return;
  } else if (target.classList.contains('parts-import-toggle')) {
    card.querySelector('.parts-import').classList.toggle('hidden');
    return;
  } else if (target.classList.contains('parts-import-fetch')) {
    const url = card.querySelector('.parts-import-url').value.trim();
    if (url) await fetchListingParts(card, { url });
    return;
  } else if (target.classList.contains('model-import-parts')) {
    card.querySelector('.parts-import').classList.remove('hidden');
    await fetchListingParts(card, { provider: 'makerworld', source_id: target.closest('.project-model').dataset.sourceId });
    return;
  } else if (target.classList.contains('parts-import-add')) {
    await addImportedParts(card, projectId);
    return;
  } else if (target.classList.contains('part-add-btn')) {
    const name = card.querySelector('.new-part-name').value.trim();
    if (!name) return;
    const res = await jsonRequest('POST', `/api/projects/${projectId}/parts`, {
      name,
      category: card.querySelector('.new-part-category').value,
      quantity: card.querySelector('.new-part-qty').value,
      quantity_owned: card.querySelector('.new-part-owned').value,
      unit_cost: card.querySelector('.new-part-cost').value,
      purchase_url: card.querySelector('.new-part-url').value,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      return alert(err.detail || 'Could not add part.');
    }
  } else {
    return;
  }
  refreshProjectViews();
});

// ---------- Import a listing's parts list into a project ----------
let importedParts = new WeakMap();   // project card -> the rows last fetched for it

async function fetchListingParts(card, params) {
  const status = card.querySelector('.parts-import-status');
  const results = card.querySelector('.parts-import-results');
  status.textContent = 'Getting the parts list...';
  results.innerHTML = '';
  const res = await fetch(`/api/sources/parts?${new URLSearchParams(params)}`);
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  const data = await res.json();
  importedParts.set(card, data.parts);
  const have = new Set([...card.querySelectorAll('.parts-table tbody tr')].map(r => r.dataset.name));
  status.innerHTML = `${esc(data.listing.title)} ${data.parts.length ? `&mdash; ${data.parts.length} part(s). Tick the ones to add; names, types and quantities can be edited first.` : ''}`
    + (data.note ? ` <span>${esc(data.note)}</span>` : '');
  if (!data.parts.length) return;
  results.innerHTML = `
    <div class="table-scroll"><table class="parts-table import-table"><thead><tr>
      <th></th><th>Part</th><th>Type</th><th>Qty</th><th>Unit cost</th><th></th>
    </tr></thead><tbody>${data.parts.map((p, i) => {
      const dup = have.has(p.name.toLowerCase());
      return `
      <tr data-index="${i}">
        <td><input type="checkbox" class="import-pick" ${dup ? '' : 'checked'} aria-label="Add this part"></td>
        <td><input class="import-name" value="${esc(p.name)}">
          <small>${esc(p.notes || '')}${dup ? ' <b>(already in this project)</b>' : ''}</small></td>
        <td><select class="import-category">${['electronics', 'parts', 'supplies'].map(c =>
          `<option value="${c}" ${c === p.category ? 'selected' : ''}>${c}</option>`).join('')}</select></td>
        <td><input class="import-qty" type="number" min="1" value="${p.quantity}"></td>
        <td><input class="import-cost" type="number" min="0" step="0.01" value="${p.unit_cost ?? ''}"></td>
        <td>${safeUrl(p.purchase_url) ? `<a href="${esc(p.purchase_url)}" target="_blank" rel="noopener noreferrer">store</a>` : ''}</td>
      </tr>`;
    }).join('')}</tbody></table></div>
    <div class="row"><button class="parts-import-add">Add ticked parts to this project</button></div>`;
}

async function addImportedParts(card, projectId) {
  const source = importedParts.get(card) || [];
  const rows = [...card.querySelectorAll('.import-table tbody tr')].filter(r => r.querySelector('.import-pick').checked);
  if (!rows.length) { card.querySelector('.parts-import-status').textContent = 'Tick at least one part.'; return; }
  const parts = rows.map(r => ({
    name: r.querySelector('.import-name').value,
    category: r.querySelector('.import-category').value,
    quantity: r.querySelector('.import-qty').value,
    unit_cost: r.querySelector('.import-cost').value,
    purchase_url: source[parseInt(r.dataset.index)].purchase_url,
    notes: source[parseInt(r.dataset.index)].notes,
  }));
  const res = await jsonRequest('POST', `/api/projects/${projectId}/parts/bulk`, { parts });
  if (!res.ok) { card.querySelector('.parts-import-status').textContent = await sourceErrorText(res); return; }
  refreshProjectViews();
}

// Fills in filament grams from the print estimate, using the material of the
// model's first assigned spool (PLA if none yet). With several filament lines
// the total is split evenly -- edit the numbers to match the real color split.
async function estimateProjectModel(projectId, box) {
  const note = box.querySelector('.model-estimate-note');
  const lineRows = [...box.querySelectorAll('.filament-line[data-line]')];
  const firstSpool = lineRows.length
    ? projectSpools.find(f => f.id === parseInt(lineRows[0].querySelector('.line-spool').value))
    : projectSpools.find(f => f.id === parseInt(box.querySelector('.new-line-spool').value));
  const material = encodeURIComponent(firstSpool?.material || 'PLA');
  note.textContent = 'Estimating…';
  const res = await fetch(`/api/library/models/${box.dataset.model}/estimate?material=${material}`);
  const est = res.ok ? await res.json() : {};
  if (est.estimated_grams == null) {
    note.textContent = est.note || 'Estimate unavailable for this model.';
    return;
  }
  if (lineRows.length) {
    const each = Math.round((est.estimated_grams / lineRows.length) * 10) / 10;
    for (const row of lineRows) {
      await jsonRequest('PATCH', `/api/projects/${projectId}/filament/${row.dataset.line}`, { grams: each });
    }
    refreshProjectViews();
  } else {
    box.querySelector('.new-line-grams').value = est.estimated_grams;
    note.textContent = `~${est.estimated_grams}g (${est.source}) -- pick a spool and click Add filament.`;
  }
}

$('#project-page-body').addEventListener('change', async (e) => {
  const card = e.target.closest('.project-card');
  if (!card) return;
  const projectId = card.dataset.project;
  if (e.target.classList.contains('project-field')) {
    // name / description / notes save on their own, without redrawing the page under your cursor
    const field = e.target.dataset.field;
    const status = card.querySelector('.project-save-status');
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}`, { [field]: e.target.value });
    if (!res.ok) { status.textContent = await sourceErrorText(res); refreshProjectViews(); return; }
    status.textContent = 'Saved.';
    setTimeout(() => { status.textContent = ''; }, 2000);
    if (field === 'name') document.title = `${e.target.value.trim()} - Model Hub`;
    return;
  }
  if (e.target.classList.contains('new-part-name')) {
    const owned = projectSupplies.find(s => s.name.toLowerCase() === e.target.value.trim().toLowerCase());
    if (owned) {
      card.querySelector('.new-part-category').value = owned.category;
      const cost = card.querySelector('.new-part-cost');
      if (!cost.value && owned.unit_cost != null) cost.value = owned.unit_cost;
      if (!card.querySelector('.new-part-url').value && owned.purchase_url) {
        card.querySelector('.new-part-url').value = owned.purchase_url;
      }
    }
  } else if (e.target.classList.contains('project-status')) {
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}`, { status: e.target.value });
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || 'Could not change status.');
    refreshProjectViews();
  } else if (e.target.classList.contains('line-grams') || e.target.classList.contains('line-spool')) {
    const row = e.target.closest('.filament-line');
    const body = e.target.classList.contains('line-grams')
      ? { grams: e.target.value || 0 } : { filament_id: e.target.value };
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}/filament/${row.dataset.line}`, body);
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || 'Could not update filament.');
    refreshProjectViews();
  } else if (e.target.classList.contains('part-owned')) {
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}/parts/${e.target.closest('tr').dataset.part}`,
      { quantity_owned: e.target.value });
    if (!res.ok) alert('Quantity owned must be a whole number, 0 or more.');
    refreshProjectViews();
  }
});

// ---------- Match the whole library (review queue) ----------
const MATCH_PAGE = 20;
let matchOffset = 0;
let matchTotal = 0;
let matchItems = [];
let matchPoll = null;

function matchLinkOptions() {
  return {
    images: $('#match-opt-images').checked,
    fill_details: $('#match-opt-fill').checked,
    add_tags: $('#match-opt-tags').checked,
  };
}

// ---------- Listing updates (Matches tab) ----------
let updatesPoll = null;

async function loadUpdates() {
  const res = await fetch('/api/source-updates/changed');
  if (!res.ok) return;
  const listings = (await res.json()).listings;
  $('#match-updates-count').textContent = listings.length ? `(${listings.length} changed)` : '';
  $('#updates-list').innerHTML = listings.length ? listings.map(l => `
    <div class="auto-row" data-provider="${esc(l.provider)}" data-source="${esc(l.source_id)}">
      <span class="badge">${esc(l.label)}</span>
      <b>${esc(l.title || l.source_id)}</b>
      <span class="status-badge status-building">${esc(l.changes.join(', '))} changed</span>
      <span class="muted">${l.models.map(m => `<a href="#/model/${m.id}">${esc(m.filename)}</a>`).join(', ')}</span>
      ${safeHttpUrl(l.url) ? `<a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">listing</a>` : ''}
      <button class="update-refresh">Refresh from the listing</button>
      <button class="update-dismiss">Dismiss</button>
    </div>`).join('') : '<p class="muted">No changes found.</p>';
}

async function refreshUpdatesStatus() {
  const res = await fetch('/api/source-updates/status');
  if (!res.ok) return;
  const s = await res.json();
  $('#updates-status').textContent = s.running
    ? `Checking ${s.checked} of ${s.total} (${s.changed} changed${s.errors ? `, ${s.errors} problems` : ''})...`
    : (s.message ? `${s.message}${s.checked ? ` ${s.checked} checked, ${s.changed} changed.` : ''}` : '');
  if (s.running && !updatesPoll) updatesPoll = setInterval(async () => { await refreshUpdatesStatus(); loadUpdates(); }, 2000);
  if (!s.running && updatesPoll) { clearInterval(updatesPoll); updatesPoll = null; loadUpdates(); }
}

$('#match-updates-box').addEventListener('toggle', () => { if ($('#match-updates-box').open) { loadUpdates(); refreshUpdatesStatus(); } });
$('#updates-check').addEventListener('click', async () => {
  const res = await jsonRequest('POST', '/api/source-updates/start', {});
  if (!res.ok) $('#updates-status').textContent = await sourceErrorText(res);
  refreshUpdatesStatus();
});
$('#updates-stop').addEventListener('click', async () => { await fetch('/api/source-updates/stop', { method: 'POST' }); refreshUpdatesStatus(); });
$('#updates-list').addEventListener('click', async (e) => {
  const row = e.target.closest('.auto-row');
  const refresh = e.target.classList.contains('update-refresh');
  if (!row || !(refresh || e.target.classList.contains('update-dismiss'))) return;
  e.target.disabled = true;
  const res = await jsonRequest('POST', `/api/source-updates/${refresh ? 'refresh' : 'dismiss'}`,
    { provider: row.dataset.provider, source_id: row.dataset.source });
  if (!res.ok) { $('#updates-status').textContent = await sourceErrorText(res); e.target.disabled = false; return; }
  loadUpdates();
});

async function loadMatches() {
  await refreshMatchStatus();
  await loadMatchQueue();
  await loadAutoLinked();
  loadUpdates();
}

async function loadAutoLinked() {
  const res = await fetch('/api/source-match/auto-linked');
  const items = res.ok ? await res.json() : [];
  const box = $('#match-auto-list');
  box.classList.toggle('hidden', !items.length);
  box.innerHTML = items.length ? `
    <h3>Linked automatically &mdash; please double-check</h3>
    ${items.map(i => `
      <div class="auto-row" data-model="${i.id}">
        <a href="#/model/${i.id}"><b>${esc(i.filename)}</b></a>
        <span class="muted">&rarr; ${esc(PROVIDER_LABELS[i.source_provider] || i.source_provider)}: ${esc(i.source_title || '')}${i.designer ? ` by ${esc(i.designer)}` : ''}</span>
        ${safeHttpUrl(i.source_url) ? `<a href="${esc(i.source_url)}" target="_blank" rel="noopener noreferrer">listing</a>` : ''}
        <button class="auto-ok">Looks right</button>
        <button class="auto-unlink">Unlink</button>
      </div>`).join('')}` : '';
}

$('#match-auto-list').addEventListener('click', async (e) => {
  const row = e.target.closest('.auto-row');
  if (!row) return;
  const id = row.dataset.model;
  if (e.target.classList.contains('auto-ok')) {
    await fetch(`/api/source-match/models/${id}/confirm`, { method: 'POST' });
  } else if (e.target.classList.contains('auto-unlink')) {
    if (!confirm('Unlink this model from the listing and delete its saved pictures?')) return;
    await fetch(`/api/library/models/${id}/source`, { method: 'DELETE' });
  } else {
    return;
  }
  loadAutoLinked();
  refreshMatchStatus();
});

async function refreshMatchStatus() {
  const res = await fetch('/api/source-match/status');
  if (!res.ok) return;
  const { job, summary } = await res.json();
  $('#match-summary').innerHTML = [
    ['Models', summary.total], ['Linked', summary.linked], ['Waiting for review', summary.waiting_review],
    ['No match found', summary.no_match], ['Skipped', summary.skipped], ['Not checked yet', summary.unchecked],
  ].map(([label, n]) => `<div class="match-stat"><b>${n}</b><span>${label}</span></div>`).join('');
  $('#match-start').disabled = job.running;
  $('#match-stop').classList.toggle('hidden', !job.running);
  $('#match-progress').textContent = job.running
    ? `Searching... ${job.checked} of ${job.total} checked, ${job.with_candidates} with matches${job.auto_linked ? `, ${job.auto_linked} linked automatically` : ''}${job.errors ? `, ${job.errors} errors` : ''}`
    : (job.message ? `${job.message}${job.checked ? ` (${job.checked} checked, ${job.with_candidates} with matches${job.auto_linked ? `, ${job.auto_linked} linked automatically` : ''})` : ''}` : '');
  if (job.running && !matchPoll) {
    matchPoll = setInterval(async () => {
      await refreshMatchStatus();
      await loadMatchQueue();
      await loadAutoLinked();
    }, 2500);
  } else if (!job.running && matchPoll) {
    clearInterval(matchPoll);
    matchPoll = null;
  }
}

async function loadMatchQueue() {
  const minScore = $('#match-min').value;
  const res = await fetch(`/api/source-match/queue?offset=${matchOffset}&limit=${MATCH_PAGE}&min_score=${minScore}`);
  if (!res.ok) return;
  const data = await res.json();
  matchTotal = data.total;
  if (!data.items.length && matchOffset > 0) { matchOffset = Math.max(0, matchOffset - MATCH_PAGE); return loadMatchQueue(); }
  matchItems = data.items;
  const ticked = new Set([...$$('.match-pick:checked')].map(c => c.closest('.match-row').dataset.model));
  $('#match-queue').innerHTML = data.items.length ? data.items.map(renderMatchRow).join('')
    : '<p class="muted">Nothing to review. Click "Find matches" to look up the models that are not linked yet.</p>';
  $$('.match-pick').forEach(c => { if (ticked.has(c.closest('.match-row').dataset.model)) c.checked = true; });
  const pages = Math.max(1, Math.ceil(matchTotal / MATCH_PAGE));
  $('#match-pager').classList.toggle('hidden', matchTotal <= MATCH_PAGE);
  $('#match-page-info').textContent = `Page ${Math.floor(matchOffset / MATCH_PAGE) + 1} of ${pages} (${matchTotal} to review)`;
  $('#match-prev').disabled = matchOffset === 0;
  $('#match-next').disabled = matchOffset + MATCH_PAGE >= matchTotal;
}

function renderMatchRow(item) {
  const m = item.model;
  const thumb = m.thumbnail_path
    ? `<img src="/api/library/thumbnails/${esc(m.thumbnail_path)}" loading="lazy" alt="">`
    : `<div class="source-noimg">${esc(m.extension)}</div>`;
  return `
    <div class="match-row" data-model="${m.id}">
      <div class="match-model">
        <input type="checkbox" class="match-pick" aria-label="Link the best match for ${esc(m.filename)}">
        ${thumb}
        <a class="match-name" href="#/model/${m.id}" title="${esc(m.filename)}">${esc(m.filename)}</a>
        <button class="match-skip">None of these</button>
      </div>
      <div class="match-candidates">${item.candidates.map((c, i) => `
        <div class="match-candidate ${i === 0 ? 'best' : ''}" data-index="${i}">
          ${c.thumbnail ? `<img src="${esc(c.thumbnail)}" loading="lazy" referrerpolicy="no-referrer" alt="">` : '<div class="source-noimg"></div>'}
          <div class="match-candidate-info">
            <b>${esc(c.title)}</b>
            <div class="muted">${esc(PROVIDER_LABELS[c.provider] || c.provider)} &middot; ${esc(c.designer || '')}${c.license ? ' &middot; ' + esc(c.license) : ''} &middot; ${Math.round(c.score * 100)}%</div>
            ${safeHttpUrl(c.url) ? `<a href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">view listing</a>` : ''}
          </div>
          <button class="match-link">Link</button>
        </div>`).join('')}
      </div>
    </div>`;
}

async function linkMatch(modelId, candidate) {
  const res = await jsonRequest('POST', `/api/library/models/${modelId}/source`,
    { provider: candidate.provider, source_id: candidate.source_id, ...matchLinkOptions() });
  return res.ok ? null : await sourceErrorText(res);
}

$('#match-start').addEventListener('click', async () => {
  const auto = $('#match-auto').value;
  const res = await jsonRequest('POST', '/api/source-match/start',
    { recheck_none: $('#match-recheck').checked, auto_link_min: auto ? parseFloat(auto) : null });
  if (!res.ok) $('#match-progress').textContent = await sourceErrorText(res);
  matchOffset = 0;
  await refreshMatchStatus();
});
$('#match-stop').addEventListener('click', async () => {
  await fetch('/api/source-match/stop', { method: 'POST' });
  $('#match-progress').textContent = 'Stopping...';
});
$('#match-min').addEventListener('change', () => { matchOffset = 0; loadMatchQueue(); });
$('#match-prev').addEventListener('click', () => { matchOffset = Math.max(0, matchOffset - MATCH_PAGE); loadMatchQueue(); });
$('#match-next').addEventListener('click', () => { matchOffset += MATCH_PAGE; loadMatchQueue(); });
$('#match-select-high').addEventListener('click', () => {
  matchItems.forEach(item => {
    const box = document.querySelector(`.match-row[data-model="${item.model.id}"] .match-pick`);
    if (box) box.checked = item.best_score >= 0.9;
  });
});
$('#match-select-none').addEventListener('click', () => $$('.match-pick').forEach(c => { c.checked = false; }));

$('#match-queue').addEventListener('click', async (e) => {
  const row = e.target.closest('.match-row');
  if (!row) return;
  const modelId = parseInt(row.dataset.model);
  const item = matchItems.find(i => i.model.id === modelId);
  if (e.target.classList.contains('match-skip')) {
    await fetch(`/api/source-match/models/${modelId}/skip`, { method: 'POST' });
  } else if (e.target.classList.contains('match-link')) {
    e.target.disabled = true;
    e.target.textContent = 'Linking...';
    const problem = await linkMatch(modelId, item.candidates[parseInt(e.target.closest('.match-candidate').dataset.index)]);
    if (problem) { e.target.disabled = false; e.target.textContent = 'Link'; $('#match-link-status').textContent = problem; return; }
  } else {
    return;
  }
  await refreshMatchStatus();
  await loadMatchQueue();
});

$('#match-link-selected').addEventListener('click', async () => {
  const picks = [...$$('.match-pick:checked')].map(c => parseInt(c.closest('.match-row').dataset.model));
  if (!picks.length) { $('#match-link-status').textContent = 'Tick at least one row first.'; return; }
  if (!confirm(`Link the best match for ${picks.length} model(s)? Each one downloads a few pictures, so this can take a little while.`)) return;
  const button = $('#match-link-selected');
  button.disabled = true;
  let done = 0;
  const problems = [];
  for (const modelId of picks) {
    $('#match-link-status').textContent = `Linking ${done + 1} of ${picks.length}...`;
    const item = matchItems.find(i => i.model.id === modelId);
    const problem = item && item.candidates.length ? await linkMatch(modelId, item.candidates[0]) : 'no candidate';
    if (problem) problems.push(`${item ? item.model.filename : modelId}: ${problem}`);
    done += 1;
  }
  button.disabled = false;
  $('#match-link-status').textContent = `Linked ${picks.length - problems.length} of ${picks.length}.`
    + (problems.length ? ` Problems: ${problems.slice(0, 3).join('; ')}` : '');
  await refreshMatchStatus();
  await loadMatchQueue();
});

// ---------- Sites: what each can do, and their keys (Settings) ----------
let providerInfo = [];

async function loadProviderInfo() {
  const res = await fetch('/api/sources/providers');
  providerInfo = res.ok ? await res.json() : [];
  return providerInfo;
}

function downloadSummary(p) {
  return p.can_download ? 'files can be downloaded into your library from here'
    : 'search and preview here; add files with the browser extension';
}

async function renderSiteSettings(settings) {
  await loadProviderInfo();
  const free = providerInfo.filter(p => !p.needs_credentials);
  const keyed = providerInfo.filter(p => p.needs_credentials);
  $('#sites-settings').innerHTML = `
    <div class="site-setting"><h4>No account needed</h4>
      ${free.map(p => `<div>${esc(p.label)} <span class="muted">&mdash; ${downloadSummary(p)}</span></div>`).join('')}</div>
    ${keyed.map(p => `
      <div class="site-setting" data-provider="${p.id}">
        <h4>${esc(p.label)} ${p.enabled ? '<span class="status-badge status-done">connected</span>' : ''}</h4>
        <div class="muted">${esc(p.help || '')} (${downloadSummary(p)}.)</div>
        <div class="row">${p.fields.map(f => `
          <label>${esc(f.label)}
            <input class="site-field" data-field="${f.name}" type="${f.secret ? 'password' : 'text'}" autocomplete="off"
              value="${f.secret ? (f.set ? '********' : '') : esc((settings || {})[`${p.id}_${f.name}`] || '')}" placeholder="${f.set ? '' : '(not set)'}">
          </label>`).join('')}</div>
        <div><button class="site-save">Save</button> <button class="site-test">Test</button> <span class="site-status muted"></span></div>
      </div>`).join('')}`;
}

$('#sites-settings').addEventListener('click', async (e) => {
  const box = e.target.closest('.site-setting[data-provider]');
  if (!box) return;
  const provider = box.dataset.provider;
  const status = box.querySelector('.site-status');
  if (e.target.classList.contains('site-save')) {
    const body = {};
    box.querySelectorAll('.site-field').forEach(i => { body[`${provider}_${i.dataset.field}`] = i.value.trim(); });
    const res = await jsonRequest('PUT', '/api/settings', body);
    status.textContent = res.ok ? 'Saved.' : 'Could not save.';
    if (res.ok) setTimeout(async () => renderSiteSettings(await (await fetch('/api/settings')).json()), 800);
  } else if (e.target.classList.contains('site-test')) {
    status.textContent = 'Testing...';
    const res = await fetch(`/api/sources/test/${provider}`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    status.textContent = res.ok ? data.message : (data.detail || 'Could not test.');
    status.className = `site-status ${res.ok && data.ok ? 'in-stock' : 'error-text'}`;
  }
});

// ---------- Downloads: the queue shared by the Search and listing pages ----------
let downloadsPoll = null;
let lastDownloads = { items: [] };
const ACTIVE_STATES = ['queued', 'downloading', 'importing'];

function downloadRow(item) {
  const pct = item.bytes_total ? Math.min(100, Math.round(item.bytes_done / item.bytes_total * 100)) : 0;
  const models = (item.model_ids || []).map(id => `<a href="#/model/${id}">model ${id}</a>`).join(', ');
  return `
    <div class="downloads-row" data-id="${item.id}">
      <span class="badge">${esc(PROVIDER_LABELS[item.provider] || item.provider)}</span>
      <a class="grow" href="#/listing/${item.provider}/${encodeURIComponent(item.source_id)}"><b>${esc(item.title || item.source_id)}</b></a>
      <span class="status-badge status-${item.status === 'done' ? 'done' : item.status === 'error' ? 'failed' : 'building'}">${esc(item.status)}</span>
      <span class="muted">${esc(item.message || '')}</span>
      ${models ? `<span>${models}</span>` : ''}
      ${ACTIVE_STATES.includes(item.status) ? '<button class="download-cancel">Cancel</button>' : ''}
      ${item.status === 'downloading' && item.bytes_total ? `<div class="progress"><span style="width:${pct}%"></span></div>` : ''}
    </div>`;
}

function renderDownloads(data) {
  lastDownloads = data;
  const panel = $('#downloads-panel');
  const items = [...data.items].reverse();
  panel.classList.toggle('hidden', items.length === 0);
  panel.innerHTML = items.length ? `
    <h3>Downloads</h3>
    ${items.map(downloadRow).join('')}
    ${items.some(i => !ACTIVE_STATES.includes(i.status)) ? '<div class="row"><button id="downloads-clear">Clear finished</button></div>' : ''}` : '';
  const box = $('#listing-download');
  if (box && currentListing) {
    const mine = data.items.filter(i => i.provider === currentListing.details.provider && i.source_id === currentListing.details.source_id).pop();
    box.innerHTML = mine ? downloadRow(mine) : '';
  }
}

async function refreshDownloads() {
  const res = await fetch('/api/discover/downloads');
  if (!res.ok) return;
  const data = await res.json();
  const wasActive = lastDownloads.items.some(i => ACTIVE_STATES.includes(i.status));
  renderDownloads(data);
  const active = data.items.some(i => ACTIVE_STATES.includes(i.status));
  if (active && !downloadsPoll) downloadsPoll = setInterval(refreshDownloads, 1500);
  if (!active && downloadsPoll) { clearInterval(downloadsPoll); downloadsPoll = null; }
  if (wasActive && !active) onDownloadsSettled(data);
}

// what finished: mark results as "in library" and refresh an open listing
function onDownloadsSettled(data) {
  for (const item of data.items) {
    if (!item.model_ids || !item.model_ids.length) continue;
    const hit = searchState.online.find(r => r.provider === item.provider && r.source_id === item.source_id);
    if (hit) hit.in_library = item.model_ids[0];
  }
  if (!$('#tab-search').classList.contains('hidden') && $('#tab-search').classList.contains('active')) renderSearchResults();
  if (currentListing && $('#page-listing').classList.contains('active')) {
    openListingPage(currentListing.details.provider, currentListing.details.source_id, routeToken);
  }
}

$('#downloads-panel').addEventListener('click', async (e) => {
  if (e.target.id === 'downloads-clear') {
    await fetch('/api/discover/downloads/clear', { method: 'POST' });
    refreshDownloads();
  } else if (e.target.classList.contains('download-cancel')) {
    await fetch(`/api/discover/downloads/${e.target.closest('.downloads-row').dataset.id}`, { method: 'DELETE' });
    refreshDownloads();
  }
});

async function queueDownloads(items, options) {
  const res = await jsonRequest('POST', '/api/discover/downloads', { items, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return { error: data.detail || 'Could not start the download.' };
  renderDownloads(data);
  if (data.added.length && !downloadsPoll) downloadsPoll = setInterval(refreshDownloads, 1500);
  return data;
}

// ---------- Wishlist ----------
// Saves a listing, or removes it when wishlistId is given. Returns the new wishlist id (null when removed), or false on failure.
async function toggleWishlist(wishlistId, listing) {
  if (wishlistId) {
    const res = await fetch(`/api/wishlist/${wishlistId}`, { method: 'DELETE' });
    return res.ok ? null : false;
  }
  const res = await jsonRequest('POST', '/api/wishlist', {
    provider: listing.provider, source_id: listing.source_id, title: listing.title,
    thumbnail: listing.thumbnail, url: listing.url, designer: listing.designer, license: listing.license,
  });
  return res.ok ? (await res.json()).id : false;
}

let wishlistPoll = null;

function wishlistRow(i) {
  const dl = i.download;
  const busy = dl && ACTIVE_STATES.includes(dl.status);
  const open = `#/listing/${i.provider}/${encodeURIComponent(i.source_id)}`;
  return `
    <div class="wishlist-row" data-id="${i.id}">
      ${i.thumbnail && safeHttpUrl(i.thumbnail)
        ? `<a href="${open}"><img class="result-thumb" src="${esc(i.thumbnail)}" loading="lazy" referrerpolicy="no-referrer" alt=""></a>`
        : '<div class="result-thumb"></div>'}
      <div class="result-info grow">
        <a href="${open}"><b>${esc(i.title)}</b></a>
        <div class="muted">${esc(i.label)}${i.designer ? ' · ' + esc(i.designer) : ''}${i.license ? ' · ' + esc(i.license) : ''}</div>
        <div class="row">
          <select class="wishlist-priority" aria-label="Priority">${[['2', 'High'], ['1', 'Normal'], ['0', 'Low']].map(([v, t]) =>
            `<option value="${v}" ${String(i.priority) === v ? 'selected' : ''}>${t} priority</option>`).join('')}</select>
          <select class="wishlist-state" aria-label="Status">${[['wanted', 'Still want it'], ['got', 'Got it elsewhere'], ['skip', 'Not any more']].map(([v, t]) =>
            `<option value="${v}" ${i.status === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
        </div>
        <input class="wishlist-note" value="${esc(i.note || '')}" placeholder="Add a note..." maxlength="2000" aria-label="Note for ${esc(i.title)}">
        <div class="result-actions">
          ${i.in_library ? `<a class="button-link" href="#/model/${i.in_library}">In your library</a>`
            : busy ? `<span class="status-badge status-building">${esc(dl.status)}</span> <span class="muted">${esc(dl.message || '')}</span>`
            : dl && dl.status === 'error' ? `<span class="error-text">${esc(dl.message || 'Failed')}</span> <button class="wishlist-add">Try again</button>`
            : i.can_download ? '<button class="wishlist-add">Add to library</button>'
            : `<span class="muted" title="${esc(i.download_note || '')}">view only</span>`}
          <a class="button-link" href="${open}">Preview</a>
          <button class="wishlist-remove">Remove</button>
        </div>
      </div>
    </div>`;
}

async function loadWishlist() {
  const res = await fetch('/api/wishlist');
  if (!res.ok) return;
  const items = await res.json();
  $('#wishlist-list').innerHTML = items.length
    ? items.map(wishlistRow).join('')
    : '<p class="muted">Nothing saved yet. Use <b>Save</b> on a search result or listing page.</p>';
  const waiting = items.some(i => i.download && ACTIVE_STATES.includes(i.download.status));
  if (waiting && !wishlistPoll) wishlistPoll = setInterval(() => { if (location.hash === '#/wishlist') loadWishlist(); else stopWishlistPoll(); }, 2000);
  if (!waiting) stopWishlistPoll();
}

function stopWishlistPoll() { if (wishlistPoll) { clearInterval(wishlistPoll); wishlistPoll = null; } }

async function wishlistDownload(body) {
  const status = $('#wishlist-status');
  status.textContent = 'Queueing...';
  const res = await jsonRequest('POST', '/api/wishlist/download',
    { ...body, images: $('#wishlist-opt-images').checked, add_tags: $('#wishlist-opt-tags').checked });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { status.textContent = data.detail || 'Could not start the downloads.'; return; }
  const bits = [`Added ${data.added.length} to the download queue.`];
  if (data.already_in_library) bits.push(`${data.already_in_library} already in your library.`);
  if (data.rejected.length) {
    const sites = [...new Set(data.rejected.map(x => PROVIDER_LABELS[x.provider] || x.provider))];
    bits.push(`${data.rejected.length} can't be added from here (${sites.join(', ')}): ${data.rejected[0].reason}`);
  }
  status.textContent = bits.join(' ');
  await loadWishlist();
  if (data.added.length) refreshDownloads();
}

$('#wishlist-download-all').addEventListener('click', () => wishlistDownload({ all: true }));
$('#wishlist-remove-added').addEventListener('click', async () => {
  const res = await fetch('/api/wishlist/remove-added', { method: 'POST' });
  const data = res.ok ? await res.json() : null;
  $('#wishlist-status').textContent = data ? `Removed ${data.removed} that are already in your library.` : 'Could not clean up the wishlist.';
  loadWishlist();
});
$('#wishlist-list').addEventListener('click', async (e) => {
  const row = e.target.closest('.wishlist-row');
  if (!row) return;
  const id = parseInt(row.dataset.id);
  if (e.target.classList.contains('wishlist-remove')) {
    await fetch(`/api/wishlist/${id}`, { method: 'DELETE' });
    loadWishlist();
  } else if (e.target.classList.contains('wishlist-add')) {
    e.target.disabled = true;
    wishlistDownload({ ids: [id] });
  }
});
$('#wishlist-list').addEventListener('change', async (e) => {
  const kinds = { 'wishlist-note': ['note', v => v], 'wishlist-priority': ['priority', v => parseInt(v)], 'wishlist-state': ['status', v => v] };
  const kind = [...e.target.classList].find(c => kinds[c]);
  if (!kind) return;
  const id = parseInt(e.target.closest('.wishlist-row').dataset.id);
  const [field, convert] = kinds[kind];
  const res = await jsonRequest('PATCH', `/api/wishlist/${id}`, { [field]: convert(e.target.value) });
  $('#wishlist-status').textContent = res.ok ? 'Saved.' : 'Could not save that.';
  if (res.ok && field !== 'note') loadWishlist();
});

async function wishlistImport(url, body) {
  const status = $('#wishlist-status');
  status.textContent = 'Importing...';
  const res = await jsonRequest('POST', url, body);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { status.textContent = data.detail || 'Could not import.'; return; }
  const bits = [`Added ${data.added}.`];
  if (data.already) bits.push(`${data.already} already on the list.`);
  if (data.unrecognized) bits.push(`${data.unrecognized} link${data.unrecognized === 1 ? '' : 's'} not recognised.`);
  if (data.failed && data.failed.length) bits.push(`${data.failed.length} could not be looked up (${data.failed[0].reason}).`);
  if (data.left_for_next_time) bits.push(`${data.left_for_next_time} left: press the button again to continue.`);
  status.textContent = bits.join(' ');
  loadWishlist();
}
$('#wishlist-import-links').addEventListener('click', () => wishlistImport('/api/wishlist/import-links', { text: $('#wishlist-links').value }));
$('#wishlist-import-likes').addEventListener('click', () => wishlistImport('/api/wishlist/import-thingiverse', { kind: 'likes' }));
$('#wishlist-import-collection').addEventListener('click', () =>
  wishlistImport('/api/wishlist/import-thingiverse', { kind: 'collection', ref: $('#wishlist-collection-url').value.trim() }));

// ---------- Search: the library and every site at once ----------
let lastSearchHash = '#/search';
const searchState = {
  q: '', providers: new Set(), page: 1, online: [], library: [], errors: {}, hasMore: false, loaded: false,
  selected: new Map(),
};

function resultKey(r) { return `${r.provider}:${r.source_id}`; }

function renderSiteChips() {
  $('#discover-sites').innerHTML = providerInfo.map(p => `
    <label class="site-chip ${p.enabled ? '' : 'disabled'}" title="${p.enabled ? '' : 'Add its key in Settings to search it'}">
      <input type="checkbox" value="${p.id}" ${searchState.providers.has(p.id) ? 'checked' : ''} ${p.enabled ? '' : 'disabled'}>
      ${esc(p.label)}${p.enabled ? '' : ' <small>(needs a key)</small>'}${p.can_download ? ' <span class="dl-badge" title="Files can be added to your library from here">&#8681;</span>' : ''}
    </label>`).join('') + '<a href="#/settings" class="muted">Set up more sites</a>';
  $$('#discover-sites input').forEach(box => box.onchange = () => {
    if (box.checked) searchState.providers.add(box.value); else searchState.providers.delete(box.value);
  });
}

async function openSearchPage(q) {
  lastSearchHash = location.hash || '#/search';
  if (!providerInfo.length) await loadProviderInfo();
  if (!searchState.providers.size) providerInfo.filter(p => p.enabled).forEach(p => searchState.providers.add(p.id));
  for (const id of [...searchState.providers]) {
    if (!providerInfo.some(p => p.id === id && p.enabled)) searchState.providers.delete(id);
  }
  renderSiteChips();
  $('#discover-q').value = q || searchState.q;
  refreshDownloads();
  if (q && (q !== searchState.q || !searchState.loaded)) return runSearch(q, 1);
  if (searchState.loaded) renderSearchResults();
}

async function runSearch(q, page) {
  const status = $('#discover-status');
  if (page === 1) {
    Object.assign(searchState, { q, page: 1, online: [], library: [], errors: {}, hasMore: false, loaded: false });
    searchState.selected.clear();
  }
  status.textContent = 'Searching...';
  const params = new URLSearchParams({
    q, page, limit: 12, providers: [...searchState.providers].join(','),
    library: $('#discover-library').checked && page === 1,
  });
  const res = await fetch(`/api/discover/search?${params}`);
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  const data = await res.json();
  if (q !== searchState.q) return;                               // a newer search replaced this one
  searchState.page = page;
  if (page === 1) searchState.library = data.library;
  searchState.online.push(...data.online);
  searchState.errors = { ...searchState.errors, ...data.errors };
  searchState.hasMore = data.has_more;
  searchState.loaded = true;
  renderSearchResults();
}

function renderSearchResults() {
  const state = searchState;
  const problems = Object.entries(state.errors).map(([p, msg]) => `${PROVIDER_LABELS[p] || p}: ${msg}`);
  $('#discover-status').textContent = state.loaded
    ? `${state.online.length} result${state.online.length === 1 ? '' : 's'} online for "${state.q}"${problems.length ? ` — ${problems.join('; ')}` : ''}`
    : '';

  const lib = $('#discover-library-results');
  lib.innerHTML = state.library.length ? `<h3>In your library (${state.library.length})</h3><div class="grid" id="discover-library-grid"></div>` : '';
  if (state.library.length) {
    const grid = $('#discover-library-grid');
    state.library.forEach(m => grid.appendChild(libraryCard(m)));
  }

  $('#discover-online').innerHTML = state.online.map(r => {
    const key = resultKey(r);
    const canPick = r.can_download && !r.in_library;
    const siteLabel = PROVIDER_LABELS[r.provider] || r.provider;
    return `
      <div class="result-card ${state.selected.has(key) ? 'picked' : ''}" data-key="${esc(key)}">
        ${canPick ? `<label class="result-pick"><input type="checkbox" class="result-check" ${state.selected.has(key) ? 'checked' : ''} aria-label="Select ${esc(r.title)}"></label>` : ''}
        <a href="#/listing/${r.provider}/${encodeURIComponent(r.source_id)}">${r.thumbnail
          ? `<img class="result-thumb" src="${esc(r.thumbnail)}" loading="lazy" referrerpolicy="no-referrer" alt="">` : '<div class="result-thumb"></div>'}</a>
        <div class="result-info">
          <a href="#/listing/${r.provider}/${encodeURIComponent(r.source_id)}"><b>${esc(r.title)}</b></a>
          <div class="muted">${esc(siteLabel)}${r.designer ? ' · ' + esc(r.designer) : ''}${r.license ? ' · ' + esc(r.license) : ''} · ${Math.round(r.score * 100)}%</div>
          <div class="result-actions">
            ${r.in_library ? `<a class="button-link" href="#/model/${r.in_library}">In your library</a>`
              : (r.can_download ? '<button class="result-add">Add to library</button>'
                : '<span class="muted" title="Open the listing for how to add this one">view only</span>')}
            <a class="button-link" href="#/listing/${r.provider}/${encodeURIComponent(r.source_id)}">Preview</a>
            <button class="result-save ${r.wishlist_id ? 'saved' : ''}" title="${r.wishlist_id ? 'On your wishlist (click to remove)' : 'Save to your wishlist'}">${r.wishlist_id ? '&#9733; Saved' : '&#9734; Save'}</button>
          </div>
        </div>
      </div>`;
  }).join('');

  const downloadable = state.online.some(r => r.can_download && !r.in_library);
  $('#discover-bulk').classList.toggle('hidden', !downloadable);
  $('#discover-more').classList.toggle('hidden', !(state.loaded && state.hasMore));
}

$('#discover-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = $('#discover-q').value.trim();
  if (!q) return;
  const target = `#/search/${encodeURIComponent(q)}`;
  if (location.hash === target) runSearch(q, 1); else location.hash = target;
});
$('#discover-more-btn').addEventListener('click', () => runSearch(searchState.q, searchState.page + 1));

$('#discover-online').addEventListener('click', async (e) => {
  const card = e.target.closest('.result-card');
  if (!card) return;
  const r = searchState.online.find(x => resultKey(x) === card.dataset.key);
  if (!r) return;
  if (e.target.classList.contains('result-check')) {
    if (e.target.checked) searchState.selected.set(card.dataset.key, r); else searchState.selected.delete(card.dataset.key);
    card.classList.toggle('picked', e.target.checked);
  } else if (e.target.classList.contains('result-save')) {
    e.target.disabled = true;
    const id = await toggleWishlist(r.wishlist_id, r);
    if (id === false) { $('#discover-status').textContent = 'Could not update the wishlist.'; e.target.disabled = false; return; }
    r.wishlist_id = id;
    renderSearchResults();
  } else if (e.target.classList.contains('result-add')) {
    e.target.disabled = true;
    e.target.textContent = 'Queued...';
    const data = await queueDownloads([{ provider: r.provider, source_id: r.source_id, title: r.title, thumbnail: r.thumbnail }],
      { images: $('#discover-opt-images').checked, add_tags: $('#discover-opt-tags').checked });
    if (data.error || (data.rejected && data.rejected.length)) {
      e.target.disabled = false;
      e.target.textContent = 'Add to library';
      $('#discover-status').textContent = data.error || data.rejected[0].reason;
    }
  }
});

$('#discover-select-all').addEventListener('click', () => {
  searchState.online.filter(r => r.can_download && !r.in_library).forEach(r => searchState.selected.set(resultKey(r), r));
  renderSearchResults();
});
$('#discover-select-none').addEventListener('click', () => { searchState.selected.clear(); renderSearchResults(); });
$('#discover-add-selected').addEventListener('click', async () => {
  const picks = [...searchState.selected.values()];
  const status = $('#discover-bulk-status');
  if (!picks.length) { status.textContent = 'Tick at least one result first.'; return; }
  status.textContent = `Queueing ${picks.length}...`;
  const data = await queueDownloads(
    picks.map(r => ({ provider: r.provider, source_id: r.source_id, title: r.title, thumbnail: r.thumbnail })),
    { images: $('#discover-opt-images').checked, add_tags: $('#discover-opt-tags').checked });
  if (data.error) { status.textContent = data.error; return; }
  status.textContent = `Added ${data.added.length} to the download queue.`
    + (data.rejected.length ? ` Skipped ${data.rejected.length}: ${data.rejected[0].reason}` : '');
  searchState.selected.clear();
  renderSearchResults();
});

// ---------- A listing's own page (#/listing/<site>/<id>) ----------
let currentListing = null;

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

async function openListingPage(provider, id, token) {
  const body = $('#listing-body');
  $('#listing-back').href = lastSearchHash.startsWith('#/search') ? lastSearchHash : '#/search';
  if (!currentListing || currentListing.details.provider !== provider || currentListing.details.source_id !== id) {
    body.innerHTML = '<p class="muted">Loading...</p>';
  }
  const res = await fetch(`/api/discover/listing?provider=${encodeURIComponent(provider)}&source_id=${encodeURIComponent(id)}`);
  if (token !== routeToken) return;
  if (!res.ok) { currentListing = null; body.innerHTML = `<p class="error-text">${esc(await sourceErrorText(res))}</p>`; return; }
  const data = await res.json();
  if (token !== routeToken) return;
  currentListing = data;
  document.title = `${data.details.title} - Model Hub`;
  renderListing(data);
  refreshDownloads();
}

function renderListing(data) {
  const d = data.details;
  const site = PROVIDER_LABELS[d.provider] || d.provider;
  const inLibrary = data.in_library;
  const downloading = lastDownloads.items.some(i => i.provider === d.provider && i.source_id === d.source_id && ACTIVE_STATES.includes(i.status));
  const parts = d.parts || [];
  const filaments = d.filaments || [];
  $('#listing-body').innerHTML = `
    <h2 class="page-title">${esc(d.title)}</h2>
    <div class="listing-meta">
      <span class="badge">${esc(site)}</span>
      ${d.designer ? `<span>by <b>${esc(d.designer)}</b></span>` : ''}
      ${d.license ? `<span>${esc(d.license)}</span>` : ''}
      ${d.category ? `<span>${esc(d.category)}</span>` : ''}
      ${d.likes != null ? `<span>${plural(d.likes, 'like')}</span>` : ''}
      ${d.downloads != null ? `<span>${d.downloads.toLocaleString()} downloads/views</span>` : ''}
    </div>

    <div class="listing-actions">
      ${data.can_download && !inLibrary.length && !downloading && data.files && data.files.some(f => f.selectable)
        ? '<button id="listing-add" class="primary">Add to library</button>' : ''}
      <button id="listing-save" class="${data.wishlist_id ? 'saved' : ''}">${data.wishlist_id ? '&#9733; On your wishlist' : '&#9734; Save to wishlist'}</button>
      ${data.can_follow ? `<button id="listing-follow" class="${data.following_id ? 'saved' : ''}">${data.following_id ? '&#10003; Following ' : 'Follow '}${esc(d.designer || 'this designer')}</button>` : ''}
      ${safeHttpUrl(d.url) ? `<a class="button-link" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Open on ${esc(site)}</a>` : ''}
      <label class="muted"><input type="checkbox" id="listing-opt-tags"> Also add the site's tags</label>
    </div>
    ${inLibrary.length ? `<div class="notice ok">Already in your library: ${inLibrary.map(m =>
      `<a href="#/model/${m.id}">${esc(m.filename)}</a>`).join(', ')}</div>` : ''}
    ${!data.can_download ? `<div class="notice">${esc(data.download_note || 'Files cannot be added from here.')}</div>` : ''}
    ${data.can_download && data.files_note ? `<div class="notice">${esc(data.files_note)}</div>` : ''}
    <div id="listing-download"></div>

    ${(d.images || []).length ? `<div class="listing-gallery">${d.images.map(u => safeHttpUrl(u)
      ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer"><img src="${esc(u)}" loading="lazy" referrerpolicy="no-referrer" alt=""></a>` : '').join('')}</div>` : ''}

    <div class="page-grid">
      <div class="page-main">
        <div class="panel"><h3>Description</h3>
          ${d.description ? `<div class="listing-desc">${esc(d.description)}</div>` : '<p class="muted">No description.</p>'}</div>
        ${parts.length ? `<div class="panel"><h3>Parts the listing says you need</h3>
          <table class="parts-table"><tbody>${parts.map(p => `<tr><td>${esc(p.name)}</td><td>${esc(p.category)}</td><td>x${p.quantity}</td>
            <td>${p.unit_cost != null ? `$${Number(p.unit_cost).toFixed(2)}` : ''}</td></tr>`).join('')}</tbody></table>
          <p class="muted">Add these to a project from its page with <b>Import parts</b>.</p></div>` : ''}
      </div>
      <aside class="page-side">
        ${(d.tags || []).length ? `<div class="panel"><h3>Tags</h3><div class="source-tags">${d.tags.map(t => `<span class="chip">${esc(t)}</span>`).join('')}</div></div>` : ''}
        ${data.files && data.files.length ? `<div class="panel"><h3>Files</h3>
          <p class="muted">${inLibrary.length ? 'Files on the site.' : 'Tick the files to add to your library.'}</p>
          ${!inLibrary.length && data.files.filter(f => f.selectable).length > 2
            ? '<div class="row"><button id="files-all" type="button">Tick all</button><button id="files-none" type="button">Untick all</button></div>' : ''}
          <ul class="link-list file-list">${data.files.map(f => `<li class="${f.selectable ? '' : 'file-off'}">
            <label><input type="checkbox" class="file-pick" value="${esc(String(f.id))}" ${f.selected ? 'checked' : ''}
              ${f.selectable && !inLibrary.length ? '' : 'disabled'}> ${esc(f.name)}</label>
            <span class="muted">${f.size ? formatBytes(f.size) : ''}${f.selectable ? '' : ' · not a model file'}</span></li>`).join('')}</ul></div>` : ''}
        ${filaments.length ? `<div class="panel"><h3>Suggested filament</h3><ul class="link-list">${filaments.map(f =>
          `<li>${esc(f.label)}</li>`).join('')}</ul></div>` : ''}
      </aside>
    </div>`;
  renderDownloads(lastDownloads);
  $$('#files-all, #files-none').forEach(b => b.onclick = () => {
    $$('.file-pick:not(:disabled)').forEach(c => { c.checked = b.id === 'files-all'; });
  });
  const save = $('#listing-save');
  save.onclick = async () => {
    save.disabled = true;
    const id = await toggleWishlist(data.wishlist_id, {
      provider: d.provider, source_id: d.source_id, title: d.title, thumbnail: (d.images || [])[0],
      url: d.url, designer: d.designer, license: d.license,
    });
    if (id === false) { save.disabled = false; return; }
    data.wishlist_id = id;
    renderListing(data);
  };
  const follow = $('#listing-follow');
  if (follow) {
    follow.onclick = async () => {
      follow.disabled = true;
      const res = data.following_id
        ? await fetch(`/api/designers/${data.following_id}`, { method: 'DELETE' })
        : await jsonRequest('POST', '/api/designers', { provider: d.provider, handle: d.designer_handle, name: d.designer });
      if (!res.ok) { follow.disabled = false; follow.textContent = await sourceErrorText(res); return; }
      data.following_id = data.following_id ? null : (await res.json()).id;
      renderListing(data);
      refreshFollowBadge();
    };
  }
  const add = $('#listing-add');
  if (add) {
    add.onclick = async () => {
      const fileIds = [...$$('.file-pick:checked')].map(c => c.value);
      if (!fileIds.length) {
        $('#listing-download').innerHTML = '<div class="notice error-text">Tick at least one file to add.</div>';
        return;
      }
      add.disabled = true;
      add.textContent = 'Queued...';
      const res = await queueDownloads(
        [{ provider: d.provider, source_id: d.source_id, title: d.title, thumbnail: (d.images || [])[0], file_ids: fileIds }],
        { images: true, add_tags: $('#listing-opt-tags').checked });
      if (res.error || (res.rejected && res.rejected.length)) {
        add.disabled = false;
        add.textContent = 'Add to library';
        $('#listing-download').innerHTML = `<div class="notice error-text">${esc(res.error || res.rejected[0].reason)}</div>`;
      } else {
        refreshDownloads();
      }
    };
  }
}

// ---------- Linked models: review and unlink in bulk (Matches tab) ----------
const LINKED_PAGE = 20;
const linkedState = { offset: 0, total: 0, items: [], picked: new Set() };

function linkedParams() {
  return { provider: $('#linked-provider').value, linked_by: $('#linked-by').value, q: $('#linked-q').value.trim() };
}

async function loadLinked() {
  if (!providerInfo.length) await loadProviderInfo();
  const select = $('#linked-provider');
  if (select.options.length <= 1) {
    providerInfo.forEach(p => select.insertAdjacentHTML('beforeend', `<option value="${p.id}">${esc(p.label)}</option>`));
  }
  const params = new URLSearchParams({ ...linkedParams(), offset: linkedState.offset, limit: LINKED_PAGE });
  const res = await fetch(`/api/source-match/linked?${params}`);
  if (!res.ok) return;
  const data = await res.json();
  if (!data.items.length && linkedState.offset > 0) { linkedState.offset = Math.max(0, linkedState.offset - LINKED_PAGE); return loadLinked(); }
  Object.assign(linkedState, { total: data.total, items: data.items });
  $('#linked-list').innerHTML = data.items.length ? data.items.map(i => `
    <div class="linked-row" data-id="${i.id}">
      <input type="checkbox" class="linked-pick" ${linkedState.picked.has(i.id) ? 'checked' : ''} aria-label="Select ${esc(i.filename)}">
      <a href="#/model/${i.id}"><b>${esc(i.filename)}</b></a>
      <span class="muted">&rarr; ${esc(PROVIDER_LABELS[i.source_provider] || i.source_provider)}: ${esc(i.source_title || '')}</span>
      <span class="status-badge">${esc(i.source_linked_by)}</span>
    </div>`).join('') : '<p class="muted">No linked models match.</p>';
  const pages = Math.max(1, Math.ceil(data.total / LINKED_PAGE));
  $('#linked-pager').classList.toggle('hidden', data.total <= LINKED_PAGE);
  $('#linked-page-info').textContent = `Page ${Math.floor(linkedState.offset / LINKED_PAGE) + 1} of ${pages} (${data.total} linked)`;
  $('#linked-prev').disabled = linkedState.offset === 0;
  $('#linked-next').disabled = linkedState.offset + LINKED_PAGE >= data.total;
}

$('#match-linked-box').addEventListener('toggle', () => { if ($('#match-linked-box').open) loadLinked(); });
['#linked-provider', '#linked-by'].forEach(sel => $(sel).addEventListener('change', () => { linkedState.offset = 0; linkedState.picked.clear(); loadLinked(); }));
$('#linked-q').addEventListener('input', debounce(() => { linkedState.offset = 0; linkedState.picked.clear(); loadLinked(); }, 300));
$('#linked-prev').addEventListener('click', () => { linkedState.offset = Math.max(0, linkedState.offset - LINKED_PAGE); loadLinked(); });
$('#linked-next').addEventListener('click', () => { linkedState.offset += LINKED_PAGE; loadLinked(); });
$('#linked-list').addEventListener('change', (e) => {
  if (!e.target.classList.contains('linked-pick')) return;
  const id = parseInt(e.target.closest('.linked-row').dataset.id);
  if (e.target.checked) linkedState.picked.add(id); else linkedState.picked.delete(id);
});
$('#linked-select-page').addEventListener('click', () => {
  linkedState.items.forEach(i => linkedState.picked.add(i.id));
  $$('.linked-pick').forEach(c => { c.checked = true; });
});
$('#linked-select-none').addEventListener('click', () => {
  linkedState.picked.clear();
  $$('.linked-pick').forEach(c => { c.checked = false; });
});

async function unlinkAndReload(body, count) {
  if (!confirm(`Unlink ${count} model${count === 1 ? '' : 's'} from their online listings? Their saved pictures are deleted; the models and files stay.`)) return;
  const res = await jsonRequest('POST', '/api/source-match/unlink', body);
  const data = res.ok ? await res.json() : null;
  $('#linked-status').textContent = data ? `Unlinked ${data.unlinked}.` : await sourceErrorText(res);
  linkedState.picked.clear();
  await loadLinked();
  refreshMatchStatus();
  loadAutoLinked();
}
$('#linked-unlink-selected').addEventListener('click', () => {
  const ids = [...linkedState.picked];
  if (!ids.length) { $('#linked-status').textContent = 'Tick at least one model first.'; return; }
  unlinkAndReload({ model_ids: ids }, ids.length);
});
$('#linked-unlink-all').addEventListener('click', () => {
  const f = linkedParams();
  const body = {};
  if (f.provider) body.provider = f.provider;
  if (f.linked_by) body.linked_by = f.linked_by;
  if (f.q) body.q = f.q;
  if (!Object.keys(body).length) body.all = true;
  unlinkAndReload(body, linkedState.total);
});

// ---------- Collections ----------
async function loadCollections() {
  const cols = await (await fetch('/api/collections')).json();
  $('#collections-list').innerHTML = cols.map(c => `<li data-id="${c.id}">${c.cover ? `<img class="col-cover" src="/api/library/thumbnails/${esc(c.cover)}" alt="">` : ''}<b class="col-name">${esc(c.name)}</b> <span class="muted">${c.models} model${c.models === 1 ? '' : 's'}</span>
    <button data-id="${c.id}" class="cover-col" title="Choose the picture that stands for this collection">cover</button> <button data-id="${c.id}" class="share-col">share</button> <button data-id="${c.id}" class="del-col">delete</button>
    <div class="panel hidden collection-cover-panel" id="collection-cover-${c.id}"></div>
    <div class="panel hidden collection-share-panel" id="collection-share-${c.id}"></div></li>`).join('');
  $$('.cover-col').forEach(b => b.onclick = async () => {
    const panel = $(`#collection-cover-${b.dataset.id}`);
    panel.classList.toggle('hidden');
    if (panel.classList.contains('hidden')) return;
    const col = cols.find(c => String(c.id) === b.dataset.id);
    const res = await fetch(`/api/library/models?collection_id=${b.dataset.id}&limit=60`);
    const models = res.ok ? await res.json() : [];
    panel.innerHTML = models.length ? `<div class="muted">Pick the model whose picture stands for the collection.</div>
      <div class="cover-choices">${models.filter(m => m.thumbnail_path).map(m => `<button data-model="${m.id}" class="${col && col.cover_model_id === m.id ? 'chosen' : ''}" title="${esc(m.filename)}"><img src="/api/library/thumbnails/${esc(m.thumbnail_path)}" alt="${esc(m.filename)}" loading="lazy"></button>`).join('')}</div>
      <p><button class="cover-reset">Use the first model's picture</button></p>` : '<p class="muted">Put a model in the collection first.</p>';
    const choose = async (modelId) => { await jsonRequest('PATCH', `/api/collections/${b.dataset.id}`, { cover_model_id: modelId }); loadCollections(); };
    panel.querySelectorAll('.cover-choices button').forEach(x => x.onclick = () => choose(parseInt(x.dataset.model)));
    const reset = panel.querySelector('.cover-reset');
    if (reset) reset.onclick = () => choose(null);
  });
  $$('.share-col').forEach(b => b.onclick = () => {
    const panel = $(`#collection-share-${b.dataset.id}`);
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) renderSharePanel(`#collection-share-${b.dataset.id}`, 'collection', parseInt(b.dataset.id));
  });
  $$('.del-col').forEach(b => b.onclick = async () => { await fetch(`/api/collections/${b.dataset.id}`, { method: 'DELETE' }); loadCollections(); });

  const smart = await (await fetch('/api/collections/smart')).json();
  $('#smart-collections-list').innerHTML = smart.map(s => `<li>${s.name} <button data-id="${s.id}" class="del-smart">delete</button></li>`).join('');
  $$('.del-smart').forEach(b => b.onclick = async () => { await fetch(`/api/collections/smart/${b.dataset.id}`, { method: 'DELETE' }); loadCollections(); });
}
$('#add-collection-btn').addEventListener('click', async () => {
  const name = $('#new-collection-name').value.trim();
  if (!name) return;
  await fetch('/api/collections', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
  $('#new-collection-name').value = '';
  loadCollections();
});
$('#add-smart-btn').addEventListener('click', async () => {
  const name = $('#smart-name').value.trim();
  const field = $('#smart-field').value;
  const op = $('#smart-op').value;
  const value = $('#smart-value').value.trim();
  if (!name || !value) return;
  await fetch('/api/collections/smart', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, rule: { match: 'all', conditions: [{ field, op, value }] } }),
  });
  loadCollections();
});

// ---------- Filament ----------
function dryingText(d) {
  if (!d) return '';
  const age = d.days_since === 0 ? 'today' : `${d.days_since} day${d.days_since === 1 ? '' : 's'} ago`;
  if (d.status === 'due') return `<br><small class="dry-due">Due for drying (${age}; every ${d.every_days} days for this material)</small>`;
  if (d.status === 'soon') return `<br><small class="muted">Drying due in ${d.days_left} day${d.days_left === 1 ? '' : 's'} (last opened or dried ${age})</small>`;
  return `<br><small class="muted">Opened or dried ${age}</small>`;
}

async function loadFilament() {
  const items = await (await fetch('/api/filament')).json();
  const loadedIn = {};
  const dry = await fetch('/api/filament/drying/overview').then(r => (r.ok ? r.json() : {}));
  const slotInfo = await fetch('/api/slots').then(r => (r.ok ? r.json() : { printers: [] }));
  for (const p of slotInfo.printers) for (const sl of p.slots) if (sl.filament_id) loadedIn[sl.filament_id] = `${p.name} slot ${sl.slot}${sl.label ? ' (' + sl.label + ')' : ''}`;
  $('#filament-table tbody').innerHTML = items.map(f => `
    <tr data-id="${f.id}">
      <td>${f.material}</td><td>${f.brand || ''}</td><td>${f.color || ''}${loadedIn[f.id] ? `<br><small class="muted">loaded in ${esc(loadedIn[f.id])}</small>` : ''}${dryingText(dry[f.id])}</td>
      <td>${f.remaining_g}g / ${f.spool_weight_g}g</td>
      <td><input class="fil-price" data-id="${f.id}" type="number" min="0" step="0.01" value="${f.cost ?? ''}" placeholder="price" aria-label="Spool price">
        ${f.cost != null && f.spool_weight_g ? `<small class="muted">$${(f.cost / f.spool_weight_g * 1000).toFixed(2)}/kg</small>` : ''}</td>
      <td><button data-id="${f.id}" class="fil-history">prices</button> <button data-id="${f.id}" class="fil-opened" title="Note that this spool was opened today">opened</button> <button data-id="${f.id}" class="fil-dried" title="Note that this spool was dried today">dried</button> <button data-id="${f.id}" class="del-fil">delete</button></td>
    </tr>
    <tr class="fil-history-row hidden" data-for="${f.id}"><td colspan="6"></td></tr>`).join('');
  if (!$('#fil-labels-link')) $('#filament-table').insertAdjacentHTML('beforebegin', '<p><a id="fil-labels-link" class="button-link" href="#/labels/filament">Print QR labels for spools</a></p>');
  $$('.fil-price').forEach(input => input.onchange = async () => {
    const value = input.value.trim();
    await jsonRequest('PATCH', `/api/filament/${input.dataset.id}`, { cost: value === '' ? null : parseFloat(value) });
    loadFilament();
  });
  $$('.fil-history').forEach(b => b.onclick = async () => {
    const row = document.querySelector(`.fil-history-row[data-for="${b.dataset.id}"]`);
    if (!row.classList.contains('hidden')) { row.classList.add('hidden'); return; }
    const res = await fetch(`/api/filament/${b.dataset.id}/prices`);
    const prices = res.ok ? await res.json() : [];
    row.firstElementChild.innerHTML = prices.length
      ? prices.map(p => `<div class="muted">${esc(String(p.at).slice(0, 10))}: $${Number(p.cost).toFixed(2)} ($${p.per_kg.toFixed(2)}/kg)</div>`).join('')
      : '<div class="muted">No prices recorded yet. Set a price on the spool to start the history.</div>';
    row.classList.remove('hidden');
  });
  for (const [cls, action] of [['.fil-opened', 'opened'], ['.fil-dried', 'dried']]) {
    $$(cls).forEach(b => b.onclick = async () => { await fetch(`/api/filament/${b.dataset.id}/${action}`, { method: 'POST' }); loadFilament(); });
  }
  $$('.del-fil').forEach(b => b.onclick = async () => { await fetch(`/api/filament/${b.dataset.id}`, { method: 'DELETE' }); loadFilament(); });
}
$('#add-filament-btn').addEventListener('click', async () => {
  await fetch('/api/filament', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      material: $('#fil-material').value, brand: $('#fil-brand').value,
      color: $('#fil-color').value, spool_weight_g: parseFloat($('#fil-weight').value) || 1000,
      remaining_g: parseFloat($('#fil-weight').value) || 1000,
      cost: $('#fil-cost').value.trim() === '' ? null : parseFloat($('#fil-cost').value),
    }),
  });
  loadFilament();
});

// ---------- Printers (administrator) ----------
let printerPoll = null;
let printerInfo = { printers: [], slicer_ready: false, slicer_note: '' };

function printerStatusText(st) {
  if (!st.online) return `<span class="error-text">${esc(st.message || 'offline')}</span>`;
  const bits = [`<span class="status-badge status-${st.state === 'printing' ? 'building' : 'done'}">${esc(st.state)}</span>`];
  if (st.file) bits.push(esc(st.file));
  if (st.progress != null) bits.push(`${st.progress}%`);
  if (st.nozzle != null) bits.push(`nozzle ${st.nozzle}\u00b0`);
  if (st.bed != null) bits.push(`bed ${st.bed}\u00b0`);
  if (st.message) bits.push(`<span class="muted">${esc(st.message)}</span>`);
  return bits.join(' &middot; ');
}

async function refreshPrinterStatuses() {
  for (const p of printerInfo.printers) {
    const res = await fetch(`/api/printers/${p.id}/status`);
    const cell = document.querySelector(`.printer-row[data-id="${p.id}"] .printer-state`);
    if (cell && res.ok) {
      const st = await res.json();
      cell.innerHTML = printerStatusText(st) + (st.online && ['printing', 'paused'].includes(st.state) ? ` <span class="printer-controls">
        ${st.state === 'printing' ? '<button class="printer-pause">Pause</button>' : '<button class="printer-resume">Resume</button>'}<button class="printer-cancel danger">Cancel print</button></span>` : '');
    }
  }
}

async function loadPrinters() {
  if (currentUser.role !== 'admin') return;
  const res = await fetch('/api/printers');
  if (!res.ok) return;
  printerInfo = await res.json();
  $('#printers-list').innerHTML = printerInfo.printers.length ? printerInfo.printers.map(p => `
    <div class="printer-row" data-id="${p.id}">
      <b>${esc(p.name)}</b> <span class="badge">${{ moonraker: 'Klipper', octoprint: 'OctoPrint', bambu: 'Bambu Lab' }[p.kind] || esc(p.kind)}</span>
      <span class="muted">${esc(p.url)}</span>
      <span class="printer-state muted">checking...</span>
      <button class="printer-delete">Remove</button>
      <details class="printer-bed"><summary>Bed size${p.bed_x && p.bed_y ? ` (${p.bed_x} x ${p.bed_y}${p.bed_z ? ' x ' + p.bed_z : ''})` : ''}</summary>
        <p class="muted">In mm. With it, Model Hub can say which models fit this printer and warn when a queued model is too big.</p>
        <div class="row"><input type="number" class="printer-bed-x" min="10" max="5000" value="${p.bed_x || ''}" placeholder="width" aria-label="Bed width"><input type="number" class="printer-bed-y" min="10" max="5000" value="${p.bed_y || ''}" placeholder="depth" aria-label="Bed depth"><input type="number" class="printer-bed-z" min="10" max="5000" value="${p.bed_z || ''}" placeholder="height" aria-label="Build height">
        <button class="printer-bed-save">Save</button><span class="printer-bed-result muted"></span></div>
      </details>
      <details class="printer-slotcount"><summary>Spool slots${p.slot_count ? ` (${p.slot_count})` : ''}</summary>
        <p class="muted">How many spools it can hold at once (an AMS has 4, a toolchanger one per tool). Leave 0 for a printer with one spool.</p>
        <div class="row"><input type="number" class="printer-slot-count" min="0" max="16" value="${p.slot_count || 0}" aria-label="Number of spool slots"><button class="printer-slot-save">Save</button><span class="printer-slot-result muted"></span></div>
      </details>
      <details class="printer-camera"><summary>Camera${p.snapshot_url ? ' (set)' : ''}</summary>
        <p class="muted">A still-picture address (Mainsail/Fluidd: <code>http://host/webcam/?action=snapshot</code>). When a print finishes, its picture is kept as the print's photo.</p>
        <div class="row"><input class="printer-snapshot-url" value="${esc(p.snapshot_url || '')}" placeholder="http://192.168.1.60/webcam/?action=snapshot" aria-label="Camera picture address">
          <button class="printer-snapshot-save">Save</button><button class="printer-snapshot-try">Take a picture now</button></div>
        <div class="printer-snapshot-result muted"></div>
        <label class="inline-check"><input type="checkbox" class="printer-watch" ${p.watch_failures ? 'checked' : ''}> Look at the camera now and then and warn me about a failed print</label>
        <p class="muted">Every two minutes while it prints, one picture goes to your own vision model (Settings, AI: local with a vision model). It only warns you and never stops the printer. A warning needs two bad looks in a row and the model can be wrong, so take it as a hint.</p>
        <div class="row"><button class="printer-watch-try">Ask the model about the picture now</button><span class="printer-watch-result muted"></span></div>
      </details>
      <details class="printer-plug"><summary>Smart plug${p.plug_kind ? ` (${esc(p.plug_kind)})` : ''}</summary>
        <p class="muted">A Tasmota or Shelly plug that counts energy, with the printer plugged into it. Model Hub reads its running total when a print starts and ends and keeps the difference (kWh) with the print. The address is the plug's own, like 192.168.1.70.</p>
        <div class="row"><select class="printer-plug-kind" aria-label="Plug make"><option value="">none</option><option value="tasmota" ${p.plug_kind === 'tasmota' ? 'selected' : ''}>Tasmota</option><option value="shelly" ${p.plug_kind === 'shelly' ? 'selected' : ''}>Shelly</option></select>
          <input class="printer-plug-host" value="${esc(p.plug_host || '')}" placeholder="192.168.1.70" aria-label="Plug address">
          <button class="printer-plug-save">Save</button><button class="printer-plug-try">Read it now</button><span class="printer-plug-result muted"></span></div>
      </details>
    </div>`).join('') : '<p class="muted">No printers yet.</p>';
  $('#send-printer').innerHTML = printerInfo.printers.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  refreshPrinterStatuses();
  if (!printerPoll) printerPoll = setInterval(() => {
    if (location.hash === '#/queue' && printerInfo.printers.length) refreshPrinterStatuses();
    else if (location.hash !== '#/queue') { clearInterval(printerPoll); printerPoll = null; }
  }, 5000);
}

function syncPrinterForm() {
  const bambu = $('#printer-kind').value === 'bambu';
  $('#printer-serial-row').classList.toggle('hidden', !bambu);
  $('#printer-url').placeholder = bambu ? '192.168.1.60 (its address on your network)' : 'http://192.168.1.60:7125 (OctoPrint: http://octopi.local)';
  $('#printer-key').placeholder = bambu ? 'LAN access code (printer screen: Settings, WLAN)' : 'API key (needed for OctoPrint)';
}
$('#printer-kind').addEventListener('change', syncPrinterForm);
$('#printer-add').addEventListener('click', async () => {
  const body = { name: $('#printer-name').value, kind: $('#printer-kind').value, url: $('#printer-url').value };
  if (body.kind === 'bambu') body.serial = $('#printer-serial').value.trim();
  if ($('#printer-snapshot').value.trim()) body.snapshot_url = $('#printer-snapshot').value.trim();
  if ($('#printer-key').value) body.api_key = $('#printer-key').value;
  const res = await jsonRequest('POST', '/api/printers', body);
  $('#printers-status').textContent = res.ok ? 'Added.' : await sourceErrorText(res);
  if (res.ok) { $('#printer-name').value = ''; $('#printer-url').value = ''; $('#printer-key').value = ''; $('#printer-snapshot').value = ''; loadPrinters(); }
});
$('#printers-list').addEventListener('click', async (e) => {
  const row = e.target.closest('.printer-row');
  if (row && e.target.classList.contains('printer-bed-save')) {
    const num = sel => (row.querySelector(sel).value ? parseFloat(row.querySelector(sel).value) : null);
    const res = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { bed_x: num('.printer-bed-x'), bed_y: num('.printer-bed-y'), bed_z: num('.printer-bed-z') });
    row.querySelector('.printer-bed-result').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    return;
  }
  if (row && ['printer-pause', 'printer-resume', 'printer-cancel'].some(c => e.target.classList.contains(c))) {
    const action = e.target.classList.contains('printer-pause') ? 'pause' : e.target.classList.contains('printer-resume') ? 'resume' : 'cancel';
    if (action === 'cancel' && !confirm('Cancel the print that is running? It cannot be continued afterwards, and it will be logged as a failed print.')) return;
    e.target.disabled = true;
    const res = await jsonRequest('POST', `/api/printers/${row.dataset.id}/control`, { action });
    showNotice(res.ok ? `Sent: ${action}.` : await sourceErrorText(res));
    refreshPrinterStatuses();
    return;
  }
  if (row && e.target.classList.contains('printer-slot-save')) {
    const res = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { slot_count: parseInt(row.querySelector('.printer-slot-count').value) || 0 });
    row.querySelector('.printer-slot-result').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    if (res.ok) loadQueue();
    return;
  }
  if (row && e.target.classList.contains('printer-snapshot-save')) {
    const res = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { snapshot_url: row.querySelector('.printer-snapshot-url').value.trim() });
    row.querySelector('.printer-snapshot-result').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    return;
  }
  if (row && e.target.classList.contains('printer-snapshot-try')) {
    const out = row.querySelector('.printer-snapshot-result');
    out.textContent = 'Asking the camera...';
    await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { snapshot_url: row.querySelector('.printer-snapshot-url').value.trim() });
    const res = await fetch(`/api/printers/${row.dataset.id}/snapshot`, { method: 'POST' });
    if (!res.ok) { out.textContent = await sourceErrorText(res); return; }
    const url = URL.createObjectURL(await res.blob());
    out.innerHTML = `<img src="${url}" alt="Picture from the printer camera" style="max-width:320px;border-radius:6px">`;
    return;
  }
  if (row && e.target.classList.contains('printer-plug-save')) {
    const res = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { plug_kind: row.querySelector('.printer-plug-kind').value, plug_host: row.querySelector('.printer-plug-host').value.trim() });
    row.querySelector('.printer-plug-result').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
    return;
  }
  if (row && e.target.classList.contains('printer-plug-try')) {
    const out = row.querySelector('.printer-plug-result');
    out.textContent = 'Asking the plug...';
    const saved = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { plug_kind: row.querySelector('.printer-plug-kind').value, plug_host: row.querySelector('.printer-plug-host').value.trim() });
    if (!saved.ok) { out.textContent = await sourceErrorText(saved); return; }
    const res = await fetch(`/api/printers/${row.dataset.id}/plug-test`, { method: 'POST' });
    out.textContent = res.ok ? `Its energy total is ${(await res.json()).total_kwh} kWh.` : await sourceErrorText(res);
    return;
  }
  if (row && e.target.classList.contains('printer-watch-try')) {
    const out = row.querySelector('.printer-watch-result');
    out.textContent = 'Asking... (a local model can take a minute)';
    const res = await fetch(`/api/printers/${row.dataset.id}/watch-test`, { method: 'POST' });
    if (!res.ok) { out.textContent = await sourceErrorText(res); return; }
    const d = await res.json();
    out.textContent = d.failed ? `It thinks the print has failed: ${d.reason || 'no reason given'}` : `It thinks the print is fine${d.reason ? ': ' + d.reason : ''}.`;
    return;
  }
  if (!e.target.classList.contains('printer-delete')) return;
  if (!confirm('Remove this printer from Model Hub? (The printer itself is not touched.)')) return;
  await fetch(`/api/printers/${e.target.closest('.printer-row').dataset.id}`, { method: 'DELETE' });
  loadPrinters();
});
$('#printers-list').addEventListener('change', async (e) => {
  const row = e.target.closest('.printer-row');
  if (!row || !e.target.classList.contains('printer-watch')) return;
  const out = row.querySelector('.printer-watch-result');
  const res = await jsonRequest('PATCH', `/api/printers/${row.dataset.id}`, { watch_failures: e.target.checked });
  if (!res.ok) { e.target.checked = !e.target.checked; out.textContent = await sourceErrorText(res); return; }
  out.textContent = e.target.checked ? 'Watching.' : 'Not watching.';
});
$('#send-go').addEventListener('click', async () => {
  const file = $('#send-file').files[0];
  const printer = $('#send-printer').value;
  if (!printer || !file) { $('#printers-status').textContent = 'Choose a printer and a G-code file.'; return; }
  const start = $('#send-start').checked;
  if (start && !confirm('Start printing as soon as the file arrives? Check that the bed is clear.')) return;
  $('#printers-status').textContent = 'Sending...';
  const form = new FormData();
  form.append('file', file);
  form.append('start', start ? 'true' : 'false');
  const res = await fetch(`/api/printers/${printer}/send`, { method: 'POST', body: form });
  $('#printers-status').textContent = res.ok
    ? (await res.json()).started ? 'Sent, and the printer started it.' : 'Sent. It is waiting on the printer.'
    : await sourceErrorText(res);
  refreshPrinterStatuses();
});

// "Slice and send" on a model's page
async function renderModelPrinter(model) {
  const panel = $('#model-printer-panel');
  if (currentUser.role !== 'admin') { panel.classList.add('hidden'); return; }
  const res = await fetch('/api/printers');
  if (!res.ok || !currentModel || currentModel.id !== model.id) return;
  const info = await res.json();
  if (!info.printers.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const sliceable = ['.stl', '.3mf', '.obj', '.step', '.stp'].includes(model.extension);
  panel.innerHTML = `
    <h3>Send to a printer</h3>
    ${info.slicer_ready && sliceable ? `
      <div class="row">
        <select id="model-printer">${info.printers.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
        <input id="model-infill" type="number" min="0" max="100" step="1" value="15" aria-label="Infill percent" title="Infill %">
      </div>
      <label class="inline-check"><input type="checkbox" id="model-print-start"> Start printing as soon as it arrives</label>
      <div class="row"><button id="model-slice-send" class="primary">Slice and send</button><span id="model-printer-status" class="muted"></span></div>`
      : `<p class="muted">${esc(info.slicer_note)} G-code files can be sent from the Print Queue tab.</p>`}`;
  const go = $('#model-slice-send');
  if (go) go.onclick = async () => {
    const start = $('#model-print-start').checked;
    if (start && !confirm('Start printing as soon as the file arrives? Check that the bed is clear and the printer is ready.')) return;
    $('#model-printer-status').textContent = 'Slicing and sending (this can take a minute)...';
    const form = new FormData();
    form.append('model_id', String(model.id));
    form.append('start', start ? 'true' : 'false');
    form.append('infill', String((parseFloat($('#model-infill').value) || 15) / 100));
    const r = await fetch(`/api/printers/${$('#model-printer').value}/send`, { method: 'POST', body: form });
    $('#model-printer-status').textContent = r.ok
      ? ((await r.json()).started ? 'Sent, and the printer started it.' : 'Sent. It is waiting on the printer.')
      : await sourceErrorText(r);
  };
}

// ---------- Printer maintenance ----------
async function loadMaintenance() {
  const panel = $('#maintenance-panel');
  const res = await fetch('/api/maintenance');
  if (!res.ok) { panel.classList.add('hidden'); return; }
  const data = await res.json();
  if (!data.printers.length && !data.tasks.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const readOnly = currentUser.role === 'viewer';
  const badge = t => `<span class="status-badge ${t.status === 'due' ? 'status-failed' : t.status === 'soon' ? 'status-printing' : ''}">${t.status === 'due' ? 'due' : t.status === 'soon' ? 'soon' : 'ok'}</span>`;
  const left = t => [t.hours_left != null ? `${t.hours_left} print hours` : '', t.days_left != null ? `${t.days_left} days` : ''].filter(Boolean).join(' or ');
  panel.innerHTML = `<h3>Printer maintenance</h3>
    <p class="muted">Tasks fall due after so many hours of printing (counted from the prints a printer reported) and/or days.</p>
    ${data.tasks.length ? data.tasks.map(t => `<div class="row maint-row" data-id="${t.id}">${badge(t)}
      <b>${esc(t.printer)}</b>: ${esc(t.name)}
      <span class="muted">${t.status === 'due' ? 'overdue by ' + esc(left({ hours_left: t.hours_left != null && t.hours_left < 0 ? -t.hours_left : null, days_left: t.days_left != null && t.days_left < 0 ? -t.days_left : null })) || 'now'
        : esc(left(t)) + ' left'}${t.note ? ' &middot; ' + esc(t.note) : ''}</span>
      ${readOnly ? '' : '<button class="maint-done">Done</button><button class="maint-delete">Remove</button>'}</div>`).join('') : '<p class="muted">No tasks yet.</p>'}
    ${readOnly || !data.printers.length ? '' : `<details><summary>Add a task</summary><div class="row">
      <select id="maint-printer">${data.printers.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
      <select id="maint-preset"><option value="">(a common task...)</option>${data.presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join('')}</select></div>
      <div class="row"><input id="maint-name" placeholder="What has to be done" maxlength="80"><input id="maint-hours" type="number" min="1" placeholder="every ... print hours"><input id="maint-days" type="number" min="1" placeholder="or every ... days">
      <button id="maint-add" class="primary">Add</button><span id="maint-status" class="muted"></span></div></details>`}`;
  const preset = $('#maint-preset');
  if (preset) preset.onchange = () => {
    const p = data.presets[parseInt(preset.value)];
    if (p) { $('#maint-name').value = p.name; $('#maint-hours').value = p.every_hours || ''; $('#maint-days').value = p.every_days || ''; }
  };
  const add = $('#maint-add');
  if (add) add.onclick = async () => {
    const body = { printer_id: parseInt($('#maint-printer').value), name: $('#maint-name').value };
    if ($('#maint-hours').value) body.every_hours = parseFloat($('#maint-hours').value);
    if ($('#maint-days').value) body.every_days = parseInt($('#maint-days').value);
    const res2 = await jsonRequest('POST', '/api/maintenance', body);
    if (res2.ok) loadMaintenance(); else $('#maint-status').textContent = await sourceErrorText(res2);
  };
  panel.querySelectorAll('.maint-done').forEach(b => b.onclick = async () => { await fetch(`/api/maintenance/${b.closest('.maint-row').dataset.id}/done`, { method: 'POST' }); loadMaintenance(); });
  panel.querySelectorAll('.maint-delete').forEach(b => b.onclick = async () => {
    if (!confirm('Remove this maintenance task?')) return;
    await fetch(`/api/maintenance/${b.closest('.maint-row').dataset.id}`, { method: 'DELETE' });
    loadMaintenance();
  });
}

// ---------- Which spool is in which slot of a printer ----------
async function renderSlotsPanel(printers) {
  const panel = $('#slots-panel');
  if (!printers.length) { panel.classList.add('hidden'); return; }
  const spools = await fetch('/api/filament').then(r => (r.ok ? r.json() : []));
  const readOnly = currentUser.role === 'viewer';
  panel.classList.remove('hidden');
  panel.innerHTML = `<h3>Spools in the printers</h3>
    <p class="muted">What is loaded in each slot (an AMS, an MMU, a toolchanger...). A queue entry that names a slot is counted against the spool in it when it finishes.</p>
    ${printers.map(p => `<div class="slot-printer" data-printer="${p.id}"><b>${esc(p.name)}</b>
      ${p.reads_slots ? `<span class="muted"> reads its slots from the printer</span>
        <div class="row">${currentUser.role === 'admin' ? '<button class="slot-refresh">Ask the printer now</button>' : ''}
          ${!readOnly && p.slots.some(sl => sl.suggested_filament_id) ? '<button class="slot-apply">Load the spools it points at</button>' : ''}
          ${!readOnly && p.slots.some(sl => sl.reported && sl.reported.remain != null && sl.spool) ? '<button class="slot-sync">Set remaining weights from the printer</button>' : ''}
          <span class="slot-note muted"></span></div>` : ''}
      ${p.slots.map(sl => `<div class="row slot-row" data-slot="${sl.slot}"><span>Slot ${sl.slot}</span>
        ${sl.reported ? `<span class="muted" title="what the printer reports">${sl.reported.color ? `<span class="swatch" style="background:${esc(sl.reported.color)}"></span>` : ''}${sl.reported.empty ? 'empty' : esc([sl.reported.material, sl.reported.name].filter(Boolean).join(' '))}${sl.reported.remain != null ? ` ${sl.reported.remain}%` : ''}</span>` : ''}
        <input class="slot-label" value="${esc(sl.label || '')}" placeholder="name" maxlength="40" aria-label="Slot name" ${readOnly ? 'disabled' : ''}>
        <select class="slot-spool" aria-label="Spool in slot ${sl.slot}" ${readOnly ? 'disabled' : ''}><option value="">(empty)</option>${spools.map(f =>
          `<option value="${f.id}" ${f.id === sl.filament_id ? 'selected' : ''}>${esc([f.material, f.brand, f.color].filter(Boolean).join(' '))} (${f.remaining_g} g)</option>`).join('')}</select></div>`).join('')}
    </div>`).join('')}`;
  const save = async (row) => {
    const printer = row.closest('.slot-printer').dataset.printer;
    const spool = row.querySelector('.slot-spool').value;
    const res = await jsonRequest('PUT', `/api/slots/${printer}/${row.dataset.slot}`, { filament_id: spool ? parseInt(spool) : null, label: row.querySelector('.slot-label').value });
    if (!res.ok) showNotice(await sourceErrorText(res));
    loadQueue();
  };
  const note = (button, text) => { const el = button.closest('.slot-printer').querySelector('.slot-note'); if (el) el.textContent = text; };
  panel.querySelectorAll('.slot-refresh').forEach(b => b.onclick = async () => {
    note(b, 'Asking...');
    const res = await fetch(`/api/printers/${b.closest('.slot-printer').dataset.printer}/status`);
    const data = res.ok ? await res.json() : {};
    if (!data.online) note(b, data.message || 'The printer did not answer.'); else loadQueue();
  });
  panel.querySelectorAll('.slot-apply').forEach(b => b.onclick = async () => {
    const res = await fetch(`/api/slots/${b.closest('.slot-printer').dataset.printer}/apply-suggestions`, { method: 'POST' });
    if (res.ok) loadQueue(); else note(b, await sourceErrorText(res));
  });
  panel.querySelectorAll('.slot-sync').forEach(b => b.onclick = async () => {
    const res = await fetch(`/api/slots/${b.closest('.slot-printer').dataset.printer}/sync-remaining`, { method: 'POST' });
    if (res.ok) loadQueue(); else note(b, await sourceErrorText(res));
  });
  panel.querySelectorAll('.slot-spool').forEach(sel => sel.onchange = () => save(sel.closest('.slot-row')));
  panel.querySelectorAll('.slot-label').forEach(input => input.onchange = () => save(input.closest('.slot-row')));
}

// ---------- Queue ----------
async function loadQueue() {
  loadPrinters();
  const [slotData, hints] = await Promise.all([
    fetch('/api/slots').then(r => (r.ok ? r.json() : { printers: [] })),
    loadHints(),
  ]);
  const slotsByPrinter = Object.fromEntries(slotData.printers.map(p => [p.id, p]));
  const fitData = await fetch('/api/fit/queue').then(r => (r.ok ? r.json() : {}));
  const suggestions = await fetch('/api/queue/suggestions').then(r => (r.ok ? r.json() : {}));
  const [items, models, printerData, summary] = await Promise.all([
    (await fetch('/api/queue')).json(),
    (await fetch('/api/library/models?limit=1000')).json(),
    currentUser.role === 'admin' ? fetch('/api/printers').then(r => (r.ok ? r.json() : { printers: [] })) : Promise.resolve({ printers: [] }),
    fetch('/api/queue/summary').then(r => (r.ok ? r.json() : { printers: [] })),
  ]);
  const printers = printerData.printers || [];
  const modelById = Object.fromEntries(models.map(m => [m.id, m]));
  $('#queue-summary').innerHTML = summary.printers.length
    ? summary.printers.map(r => `<span class="queue-total"><b>${esc(r.printer)}</b>: ${r.jobs} job${r.jobs === 1 ? '' : 's'}, about ${formatMinutes(r.minutes) || '0 min'}${r.without_estimate ? ` (+${r.without_estimate} without an estimate)` : ''}</span>`).join(' ')
    : '';
  const accuracy = await fetch('/api/estimates/accuracy').then(r => (r.ok ? r.json() : null));
  if (accuracy && accuracy.usable) {
    const off = Math.round(Math.abs(accuracy.factor - 1) * 100);
    $('#queue-summary').innerHTML += `<div class="muted">${off < 3 ? 'Estimates have matched the real print times' : `Prints usually take ${off}% ${accuracy.factor > 1 ? 'longer' : 'less time'} than estimated`} (from ${accuracy.samples} prints); new estimates for models you have not printed are corrected for it.</div>`;
  }
  $('#queue-list').innerHTML = items.map(i => {
    const m = modelById[i.model_id];
    const basis = { history: 'from your past prints', adjusted: 'adjusted by how long prints really take' }[i.estimate_basis];
    const est = [i.estimated_grams != null ? `${i.estimated_grams}g` : null,
                 i.estimated_minutes != null ? `${Math.round(i.estimated_minutes)}min${basis ? ` (${basis})` : ''}` : null,
                 i.actual_minutes != null ? `took ${Math.round(i.actual_minutes)}min` : null]
      .filter(Boolean).join(' · ');
    return `
    <li>
      <span>#${i.position} ${m ? m.filename : 'model ' + i.model_id}${est ? ' — ' + est : ''}
        ${fitData[i.id] && !fitData[i.id].fits ? `<span class="warn-text" title="model ${fitData[i.id].model.map(v => Math.round(v)).join(' x ')} mm, bed ${fitData[i.id].bed.filter(Boolean).join(' x ')} mm">&#9888; too big for ${esc(fitData[i.id].printer)}'s bed</span>` : ''}
        ${i.uses ? `<span class="muted">${JSON.parse(i.uses).length} spools counted</span>` : ''}
        ${suggestions[i.id] ? `<span class="muted" title="${esc(suggestions[i.id][0].reasons.join('; '))}">suggested: ${esc(suggestions[i.id][0].name)}${suggestions[i.id][0].slot ? ' slot ' + suggestions[i.id][0].slot : ''}
          <button class="queue-suggest" data-id="${i.id}" data-printer="${suggestions[i.id][0].printer_id}" data-slot="${suggestions[i.id][0].slot || ''}">Use</button></span>` : ''}
        ${hints[i.model_id] && ['queued', 'printing'].includes(i.status) ? `<span class="warn-text" title="${esc(hints[i.model_id].tip || '')}">&#9888; ${esc(hintText(hints[i.model_id]))}</span>` : ''}</span>
      <span>
        ${['queued', 'printing'].includes(i.status) ? `<input type="date" class="queue-date" data-id="${i.id}" value="${esc(i.planned_date || '')}" aria-label="Planned day" title="The day you plan to print this">` : ''}
        ${printers.length ? `<select data-id="${i.id}" class="queue-printer" aria-label="Printer">
          <option value="">any printer</option>${printers.map(p => `<option value="${p.id}" ${p.id === i.printer_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          ${slotsByPrinter[i.printer_id] ? `<select data-id="${i.id}" class="queue-slot" aria-label="Spool slot">
            <option value="">no slot</option>${slotsByPrinter[i.printer_id].slots.map(sl => `<option value="${sl.slot}" ${sl.slot === i.slot ? 'selected' : ''}>slot ${sl.slot}${sl.label ? ' ' + esc(sl.label) : ''}${sl.spool ? ': ' + esc([sl.spool.material, sl.spool.color].filter(Boolean).join(' ')) : ' (empty)'}</option>`).join('')}</select>` : ''}
          ${i.printer_id && ['queued', 'failed'].includes(i.status) ? `<button data-id="${i.id}" class="queue-send" title="Send the model's newest kept G-code file to this printer">Send</button>` : ''}` : ''}
        <select data-id="${i.id}" class="queue-status">
          ${['queued', 'printing', 'done', 'failed'].map(s => `<option value="${s}" ${s === i.status ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        ${['queued'].includes(i.status) ? `<button data-id="${i.id}" class="queue-colours" title="Count each colour of a sliced file against its spool">colours</button>` : ''}
        <button data-id="${i.id}" class="del-queue">remove</button>
      </span>
    </li>`;
  }).join('');
  $$('.queue-date').forEach(input => input.onchange = async () => {
    const res = await jsonRequest('PATCH', `/api/queue/${input.dataset.id}`, { planned_date: input.value || null });
    if (!res.ok) showNotice(await sourceErrorText(res));
  });
  $$('.queue-suggest').forEach(btn => btn.onclick = async () => {
    const body = { printer_id: parseInt(btn.dataset.printer) };
    const res = await jsonRequest('PATCH', `/api/queue/${btn.dataset.id}`, body);
    if (res.ok && btn.dataset.slot) await jsonRequest('PATCH', `/api/queue/${btn.dataset.id}`, { slot: parseInt(btn.dataset.slot) });
    loadQueue();
  });
  const waitingWithoutPrinter = Object.keys(suggestions).length;
  $('#queue-summary').insertAdjacentHTML('beforeend', waitingWithoutPrinter ? ` <div><button id="queue-assign-all">Give the ${waitingWithoutPrinter} waiting print${waitingWithoutPrinter === 1 ? '' : 's'} without a printer the best one</button> <span id="queue-assign-msg" class="muted"></span></div>` : '');
  const assignAll = $('#queue-assign-all');
  if (assignAll) assignAll.onclick = async () => {
    const res = await jsonRequest('POST', '/api/queue/auto-assign', {});
    if (!res.ok) { showNotice(await sourceErrorText(res)); return; }
    const r = await res.json();
    await loadQueue();
    $('#queue-summary').insertAdjacentHTML('beforeend', ` <div class="muted">Assigned ${r.assigned.length}. ${r.activity_id ? `<button id="queue-assign-undo" data-id="${r.activity_id}">Undo</button>` : ''}</div>`);
    const undo = $('#queue-assign-undo');
    if (undo) undo.onclick = async () => { if (await undoActivity(undo.dataset.id)) loadQueue(); };
  };
  $$('.queue-colours').forEach(btn => btn.onclick = async () => {
    const item = items.find(x => x.id === parseInt(btn.dataset.id));
    const li = btn.closest('li');
    if (li.querySelector('.queue-colours-box')) { li.querySelector('.queue-colours-box').remove(); return; }
    const res = await fetch(`/api/queue/uses-from-file/${item.model_id}${item.printer_id ? `?printer_id=${item.printer_id}` : ''}`);
    if (!res.ok) { showNotice(await sourceErrorText(res)); return; }
    const data = await res.json();
    const slots = (slotsByPrinter[item.printer_id] || { slots: [] }).slots;
    const spools = await fetch('/api/filament').then(r => (r.ok ? r.json() : []));
    const spoolLabel = f => [f.material, f.brand, f.color].filter(Boolean).join(' ');
    const box = document.createElement('div');
    box.className = 'queue-colours-box';
    box.innerHTML = `<p class="muted">The colours of <b>${esc(data.file.filename)}</b>. Say which ${slots.length ? 'slot' : 'spool'} each one is printed from; each gets its grams taken when the print finishes.</p>
      ${data.filaments.map(f => `<div class="row colour-row" data-grams="${f.grams}">
        <span>${f.color ? `<span class="swatch" style="background:${esc(f.color)}"></span>` : ''}${esc(f.type || 'filament ' + f.index)} ${f.grams} g</span>
        <select class="colour-pick" aria-label="Where ${esc(f.type || 'filament')} comes from">${slots.length
          ? `<option value="">(not counted)</option>${slots.map(sl => `<option value="s${sl.slot}" ${sl.slot === f.suggested_slot ? 'selected' : ''}>slot ${sl.slot}${sl.spool ? ': ' + esc(spoolLabel(sl.spool)) : ' (empty)'}</option>`).join('')}`
          : `<option value="">(not counted)</option>${spools.map(sp => `<option value="f${sp.id}">${esc(spoolLabel(sp))} (${sp.remaining_g} g)</option>`).join('')}`}</select></div>`).join('')}
      <div class="row"><button class="colours-save primary">Save</button><button class="colours-clear">Back to one spool</button></div>`;
    li.appendChild(box);
    const save = async (clear) => {
      const uses = clear ? null : [...box.querySelectorAll('.colour-row')].map(r => {
        const v = r.querySelector('.colour-pick').value;
        return v ? (v[0] === 's' ? { slot: parseInt(v.slice(1)), grams: parseFloat(r.dataset.grams) } : { filament_id: parseInt(v.slice(1)), grams: parseFloat(r.dataset.grams) }) : null;
      }).filter(Boolean);
      const r2 = await jsonRequest('PATCH', `/api/queue/${item.id}`, { uses: uses && uses.length ? uses : null });
      if (r2.ok) loadQueue(); else showNotice(await sourceErrorText(r2));
    };
    box.querySelector('.colours-save').onclick = () => save(false);
    box.querySelector('.colours-clear').onclick = () => save(true);
  });
  $$('.queue-slot').forEach(sel => sel.onchange = async () => {
    const res = await jsonRequest('PATCH', `/api/queue/${sel.dataset.id}`, { slot: sel.value ? parseInt(sel.value) : null });
    if (!res.ok) showNotice(await sourceErrorText(res));
    loadQueue();
  });
  renderSlotsPanel(slotData.printers);
  loadMaintenance();
  $$('.queue-printer').forEach(sel => sel.onchange = async () => {
    await jsonRequest('PATCH', `/api/queue/${sel.dataset.id}`, { printer_id: sel.value ? parseInt(sel.value) : null });
    loadQueue();
  });
  $$('.queue-send').forEach(b => b.onclick = async () => {
    const start = confirm('Start printing as soon as the file arrives? (Cancel sends it without starting.) Check that the bed is clear.');
    b.disabled = true;
    const res = await jsonRequest('POST', `/api/queue/${b.dataset.id}/send`, { start });
    showNotice(res.ok ? (start ? 'Sent, and the printer started it.' : 'Sent. It is waiting on the printer.') : await sourceErrorText(res));
    loadQueue();
  });
  $$('.del-queue').forEach(b => b.onclick = async () => { await fetch(`/api/queue/${b.dataset.id}`, { method: 'DELETE' }); loadQueue(); });
  $$('.queue-status').forEach(sel => sel.onchange = async () => {
    if (sel.value === 'failed') {                              // ask why before saving, so the log can say
      const reasons = await loadFailureReasons();
      const row = sel.closest('li');
      const ask = document.createElement('div');
      ask.className = 'queue-fail row';
      ask.innerHTML = `<span>Why did it fail?</span><select>${reasons.map(r => `<option value="${esc(r.key)}">${esc(r.label)}</option>`).join('')}</select>
        <button class="queue-fail-save primary">Save</button><button class="queue-fail-skip">Skip</button><button class="queue-fail-cancel">Cancel</button>`;
      row.appendChild(ask);
      const save = async (withReason) => {
        await jsonRequest('PATCH', `/api/queue/${sel.dataset.id}`, withReason ? { status: 'failed', failure_reason: ask.querySelector('select').value } : { status: 'failed' });
        loadQueue();
      };
      ask.querySelector('.queue-fail-save').onclick = () => save(true);
      ask.querySelector('.queue-fail-skip').onclick = () => save(false);
      ask.querySelector('.queue-fail-cancel').onclick = () => loadQueue();
      return;
    }
    await fetch(`/api/queue/${sel.dataset.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: sel.value }),
    });
    loadQueue();
    loadFilament();
  });
}

// ---------- Settings ----------
// ---------- Creators ----------
async function loadCreators(name) {
  const body = $('#creators-body');
  if (name) return loadCreator(name);
  $('#creators-q').parentElement.classList.remove('hidden');
  const res = await fetch('/api/creators?q=' + encodeURIComponent($('#creators-q').value.trim()));
  if (!res.ok) { body.innerHTML = `<p class="error-text">${esc(await sourceErrorText(res))}</p>`; return; }
  const list = (await res.json()).creators;
  body.innerHTML = list.length ? list.map(c => `<div class="creator-row"><a href="#/creators/${encodeURIComponent(c.name)}"><b>${esc(c.name)}</b></a>
      <span class="muted">${c.models} model${c.models === 1 ? '' : 's'} &middot; ${formatBytes(c.size_bytes)}</span></div>`).join('')
    : '<p class="muted">No model names a designer yet. Models imported from a listing get one automatically; you can also set it on a model.</p>';
}
$('#creators-q').addEventListener('input', debounce(() => { if ((location.hash || '').startsWith('#/creators')) loadCreators(''); }, 300));

async function loadCreator(name) {
  const body = $('#creators-body');
  $('#creators-q').parentElement.classList.add('hidden');
  const res = await fetch('/api/creators/detail?name=' + encodeURIComponent(name));
  if (!res.ok) { body.innerHTML = `<p><a href="#/creators">All creators</a></p><p class="error-text">${esc(await sourceErrorText(res))}</p>`; return; }
  const d = await res.json();
  body.innerHTML = `<p><a href="#/creators">All creators</a></p><h3>${esc(d.name)}</h3>
    <p class="muted">${d.total} model${d.total === 1 ? '' : 's'} here &middot; ${d.printed} printed (${d.prints} print${d.prints === 1 ? '' : 's'} in all) &middot; ${formatBytes(d.size_bytes)}
      ${d.providers.length ? ' &middot; from ' + d.providers.map(p => `${esc(PROVIDER_LABELS[p.provider] || p.provider)} (${p.models})`).join(', ') : ''}
      ${d.licenses.length ? ' &middot; licences: ' + d.licenses.map(esc).join(', ') : ''}</p>
    <div class="creator-models">${d.models.map(m => `<a href="#/model/${m.id}">${m.thumbnail_path ? `<img src="/api/library/thumbnails/${esc(m.thumbnail_path)}" alt="" loading="lazy">` : '<img alt="">'}<span>${esc(m.filename)}</span></a>`).join('')}</div>
    ${d.total > d.models.length ? `<p class="muted">Showing the first ${d.models.length}.</p>` : ''}`;
}

// ---------- Following designers ----------
async function refreshFollowBadge() {
  const res = await fetch('/api/designers');
  if (!res.ok) return;
  const data = await res.json();
  const button = document.querySelector('#tabs button[data-tab="following"]');
  if (button) button.textContent = data.new_total ? `Following (${data.new_total})` : 'Following';
  return data;
}

let followUploads = [];

function renderFollowing(data) {
  $('#follow-list').innerHTML = data.designers.length ? data.designers.map(d => `
    <div class="follow-row" data-id="${d.id}">
      <span class="badge">${esc(d.label)}</span>
      <b>${esc(d.name || d.handle)}</b>
      ${d.new_count ? `<span class="status-badge status-building">${d.new_count} new</span>` : ''}
      <span class="muted">${d.last_error ? `<span class="error-text">${esc(d.last_error)}</span>` : (d.last_checked_at ? 'checked ' + esc(String(d.last_checked_at).slice(0, 16).replace('T', ' ')) : '')}</span>
      <button class="follow-unfollow">Unfollow</button>
    </div>`).join('')
    : '<p class="muted">You are not following anyone yet. Open a listing and press <b>Follow</b> next to the designer.</p>';
}

function followUploadCard(u) {
  const open = `#/listing/${u.provider}/${encodeURIComponent(u.source_id)}`;
  return `
    <div class="result-card" data-id="${u.id}">
      <a href="${open}">${u.thumbnail && safeHttpUrl(u.thumbnail)
        ? `<img class="result-thumb" src="${esc(u.thumbnail)}" loading="lazy" referrerpolicy="no-referrer" alt="">` : '<div class="result-thumb"></div>'}</a>
      <div class="result-info">
        <a href="${open}"><b>${esc(u.title)}</b></a>
        <div class="muted">${esc(PROVIDER_LABELS[u.provider] || u.provider)} · ${esc(u.designer)}${u.license ? ' · ' + esc(u.license) : ''}</div>
        <div class="result-actions">
          ${u.in_library ? `<a class="button-link" href="#/model/${u.in_library}">In your library</a>`
            : (u.can_download ? '<button class="upload-add">Add to library</button>' : '')}
          <button class="upload-save ${u.wishlist_id ? 'saved' : ''}">${u.wishlist_id ? '&#9733; Saved' : '&#9734; Save'}</button>
          <button class="upload-seen">Seen</button>
        </div>
      </div>
    </div>`;
}

async function loadFollowing() {
  const status = $('#follow-status');
  let data = await refreshFollowBadge();
  if (!data) return;
  renderFollowing(data);
  // quietly look for anything new when it has been a while
  if (data.designers.some(d => !d.last_checked_at || (Date.now() - Date.parse(d.last_checked_at + 'Z')) > 6 * 3600 * 1000)) {
    status.textContent = 'Checking for new uploads...';
    const res = await jsonRequest('POST', '/api/designers/check', { stale_hours: 6 });
    status.textContent = res.ok ? '' : 'Could not check just now.';
    data = await refreshFollowBadge();
    renderFollowing(data);
  }
  const res = await fetch('/api/designers/uploads');
  followUploads = res.ok ? await res.json() : [];
  $('#follow-uploads').innerHTML = followUploads.length ? followUploads.map(followUploadCard).join('')
    : '<p class="muted">No new uploads.</p>';
}

$('#follow-check').addEventListener('click', async () => {
  const status = $('#follow-status');
  status.textContent = 'Checking...';
  const res = await jsonRequest('POST', '/api/designers/check', {});
  const data = await res.json().catch(() => ({}));
  const problems = Object.entries(data.errors || {}).map(([who, msg]) => `${who}: ${msg}`);
  await loadFollowing();
  status.textContent = res.ok ? `${data.new} new${problems.length ? ' — ' + problems.join('; ') : ''}` : 'Could not check.';
});
$('#follow-seen-all').addEventListener('click', async () => {
  await jsonRequest('POST', '/api/designers/uploads/seen', { all: true });
  loadFollowing();
});
$('#follow-list').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('follow-unfollow')) return;
  await fetch(`/api/designers/${e.target.closest('.follow-row').dataset.id}`, { method: 'DELETE' });
  loadFollowing();
});
$('#follow-uploads').addEventListener('click', async (e) => {
  const card = e.target.closest('.result-card');
  if (!card) return;
  const upload = followUploads.find(u => u.id === parseInt(card.dataset.id));
  if (!upload) return;
  if (e.target.classList.contains('upload-seen')) {
    await jsonRequest('POST', '/api/designers/uploads/seen', { ids: [upload.id] });
    loadFollowing();
  } else if (e.target.classList.contains('upload-save')) {
    const id = await toggleWishlist(upload.wishlist_id, upload);
    if (id !== false) { upload.wishlist_id = id; card.outerHTML = followUploadCard(upload); }
  } else if (e.target.classList.contains('upload-add')) {
    e.target.disabled = true;
    e.target.textContent = 'Queued...';
    const data = await queueDownloads([{ provider: upload.provider, source_id: upload.source_id, title: upload.title, thumbnail: upload.thumbnail }],
      { images: true, add_tags: false });
    if (data.error || (data.rejected && data.rejected.length)) {
      e.target.disabled = false;
      e.target.textContent = 'Add to library';
      $('#follow-status').textContent = data.error || data.rejected[0].reason;
    }
  }
});

// ---------- Duplicates ----------
const dupState = { shown: 0, total: 0 };

function dupGroupHtml(g) {
  const keep = g.suggested_keep;
  return `
    <div class="dup-group" data-hash="${esc(g.hash)}">
      <div class="dup-cards">${g.models.map(m => `
        <label class="dup-card ${m.id === keep ? 'keep' : ''}">
          <input type="radio" name="keep-${esc(g.hash)}" value="${m.id}" ${m.id === keep ? 'checked' : ''}>
          ${m.thumbnail_path ? `<img src="/api/library/thumbnails/${esc(m.thumbnail_path)}" loading="lazy" alt="">` : '<div class="dup-nothumb"></div>'}
          <div class="dup-info">
            <a href="#/model/${m.id}"><b>${esc(m.filename)}</b></a>
            <div class="muted" title="${esc(m.path)}">${esc(m.path)}</div>
            <div class="muted">${formatBytes(m.size_bytes)}${m.file_exists ? '' : ' &middot; <span class="warn-text">file missing</span>'}</div>
            <div class="muted">${[m.tags && plural(m.tags, 'tag'), m.collections && plural(m.collections, 'collection'), m.projects && plural(m.projects, 'project'),
              m.prints && plural(m.prints, 'print'), m.source_provider && 'linked to ' + (PROVIDER_LABELS[m.source_provider] || m.source_provider), m.has_notes && 'notes'].filter(Boolean).join(' · ') || 'nothing attached'}</div>
          </div>
        </label>`).join('')}</div>
      <div class="row">
        <button class="dup-combine">Combine info</button>
        <button class="dup-delete danger">Keep this, delete the others</button>
        <span class="muted">frees ${formatBytes(g.reclaimable_bytes)}</span><span class="dup-status muted"></span>
      </div>
    </div>`;
}

async function loadDuplicates(append) {
  if (!append) { dupState.shown = 0; $('#dup-groups').innerHTML = ''; }
  const res = await fetch(`/api/duplicates?offset=${dupState.shown}&limit=25`);
  if (!res.ok) return;
  const data = await res.json();
  dupState.total = data.total;
  $('#dup-groups').insertAdjacentHTML('beforeend', data.groups.map(dupGroupHtml).join(''));
  dupState.shown += data.groups.length;
  $('#dup-summary').innerHTML = data.total
    ? `<span><b>${data.total}</b> group${data.total === 1 ? '' : 's'} of identical files, ${formatBytes(data.bytes_in_duplicate_files)} in all.</span>
       <button id="dup-all-combine">Combine info in every group</button>
       <button id="dup-all-delete" class="danger">Clean up every group (keep the suggested file, delete the rest)</button>
       <span id="dup-all-status" class="muted"></span>`
    : '<span>No identical files in your library.</span>';
  $('#dup-more').classList.toggle('hidden', dupState.shown >= data.total);
  if (!append) loadSimilar();
}

async function loadSimilar() {
  const res = await fetch('/api/duplicates/similar');
  if (!res.ok) return;
  const groups = (await res.json()).groups;
  $('#dup-similar-box summary').textContent = `Same shape, different file (${groups.length})`;
  $('#dup-similar').innerHTML = groups.length ? groups.map(g => `<ul class="link-list">${g.map(m =>
    `<li><a href="#/model/${m.id}">${esc(m.filename)}</a><span class="muted">${esc(m.path)} &middot; ${formatBytes(m.size_bytes)}</span></li>`).join('')}</ul>`).join('')
    : '<p class="muted">None.</p>';
}

function dupResultText(r) {
  const bits = [];
  if (r.deleted_files) bits.push(`deleted ${plural(r.deleted_files, 'file')} (${formatBytes(r.freed_bytes)} freed)`);
  else if (r.merged) bits.push(`shared the information of ${plural(r.merged, 'copy')}`);
  if (r.skipped && r.skipped.length) bits.push(`left ${r.skipped.length} alone: ${r.skipped[0].filename} (${r.skipped[0].reason})`);
  return bits.join('; ') || 'nothing to do';
}

$('#dup-groups').addEventListener('click', async (e) => {
  const combine = e.target.classList.contains('dup-combine');
  if (!combine && !e.target.classList.contains('dup-delete')) return;
  const group = e.target.closest('.dup-group');
  const keep = parseInt(group.querySelector('input[type=radio]:checked').value);
  const others = [...group.querySelectorAll('input[type=radio]')].map(r => parseInt(r.value)).filter(id => id !== keep);
  if (!combine && !confirm(`Delete ${plural(others.length, 'file')} from your library folder and keep the selected one? This cannot be undone.`)) return;
  const status = group.querySelector('.dup-status');
  status.textContent = 'Working...';
  const res = await jsonRequest('POST', '/api/duplicates/merge', { keep_id: keep, remove_ids: others, delete_files: !combine });
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  const result = await res.json();
  if (combine) status.textContent = dupResultText(result);
  else { await loadDuplicates(); $('#dup-summary').insertAdjacentHTML('beforeend', `<span class="muted">Last cleanup: ${esc(dupResultText(result))}</span>`); }
});
$('#dup-groups').addEventListener('change', (e) => {
  if (e.target.type !== 'radio') return;
  e.target.closest('.dup-group').querySelectorAll('.dup-card').forEach(c => c.classList.toggle('keep', c.contains(e.target)));
});
$('#dup-summary').addEventListener('click', async (e) => {
  const del = e.target.id === 'dup-all-delete';
  if (!del && e.target.id !== 'dup-all-combine') return;
  if (del && !confirm('Delete every extra copy in every group from your library folder, keeping the suggested file of each? This cannot be undone.')) return;
  const status = $('#dup-all-status');
  status.textContent = 'Working...';
  const res = await jsonRequest('POST', '/api/duplicates/merge-all', del ? { delete_files: true, confirm: 'delete' } : {});
  if (!res.ok) { status.textContent = await sourceErrorText(res); return; }
  const result = await res.json();
  await loadDuplicates();
  $('#dup-summary').insertAdjacentHTML('beforeend', `<span class="muted">${esc(dupResultText(result))}</span>`);
});
$('#dup-more-btn').addEventListener('click', () => loadDuplicates(true));

// ---------- Backup (Settings) ----------
async function refreshBackups() {
  const res = await fetch('/api/backup/saved');
  if (!res.ok) return;
  const list = (await res.json()).backups;
  $('#backup-saved').innerHTML = list.length ? `<ul class="link-list">${list.map(b => `
    <li data-name="${esc(b.name)}"><a href="/api/backup/saved/${encodeURIComponent(b.name)}" download>${esc(b.name)}</a>
      <span class="muted">${formatBytes(b.size)} &middot; ${esc(b.created.slice(0, 16).replace('T', ' '))}</span>
      <button class="backup-restore-saved">Restore</button> <button class="backup-delete-saved">Delete</button></li>`).join('')}</ul>`
    : '<p class="muted">No copies saved on the server yet.</p>';
}

function restoreText(r) {
  const missing = r.missing_files ? ` ${plural(r.missing_files, 'model')} in the backup ${r.missing_files === 1 ? 'is' : 'are'} not in this library folder and will be dropped by the next scan.` : '';
  return `Restored (backup from ${String(r.created || '?').slice(0, 10)}, ${r.pictures} pictures). Your previous data was saved as ${r.safety_copy}.${missing}`;
}

$('#backup-save-btn').addEventListener('click', async () => {
  $('#backup-status').textContent = 'Saving...';
  const res = await fetch('/api/backup/save', { method: 'POST' });
  $('#backup-status').textContent = res.ok ? 'Saved on the server.' : await sourceErrorText(res);
  refreshBackups();
});
$('#backup-saved').addEventListener('click', async (e) => {
  const row = e.target.closest('li');
  if (!row) return;
  const name = encodeURIComponent(row.dataset.name);
  if (e.target.classList.contains('backup-delete-saved')) {
    await fetch(`/api/backup/saved/${name}`, { method: 'DELETE' });
    refreshBackups();
  } else if (e.target.classList.contains('backup-restore-saved')) {
    if (!confirm('Replace your current tags, projects, notes and so on with this copy? A copy of the current data is saved first.')) return;
    $('#backup-status').textContent = 'Restoring...';
    const res = await jsonRequest('POST', `/api/backup/saved/${name}/restore`, { confirm: 'replace' });
    $('#backup-status').textContent = res.ok ? restoreText(await res.json()) : await sourceErrorText(res);
    refreshBackups();
  }
});
$('#backup-restore-btn').addEventListener('click', async () => {
  const file = $('#backup-file').files[0];
  if (!file) { $('#backup-status').textContent = 'Choose a backup file first.'; return; }
  if (!confirm('Replace your current tags, projects, notes and so on with this backup? A copy of the current data is saved first.')) return;
  $('#backup-status').textContent = 'Restoring...';
  const form = new FormData();
  form.append('file', file);
  form.append('confirm', 'replace');
  const res = await fetch('/api/backup/restore', { method: 'POST', body: form });
  $('#backup-status').textContent = res.ok ? restoreText(await res.json()) : await sourceErrorText(res);
  refreshBackups();
});

// ---------- Scheduled jobs and notifications (Settings) ----------
async function saveSetting(values, statusSelector) {
  const res = await jsonRequest('PUT', '/api/settings', values);
  if (statusSelector) {
    $(statusSelector).textContent = res.ok ? 'Saved.' : 'Could not save.';
    setTimeout(() => { const el = $(statusSelector); if (el) el.textContent = ''; }, 2000);
  }
  return res.ok;
}

async function loadNotifyEvents() {
  const res = await fetch('/api/settings/notify-events');
  if (!res.ok) return;
  $('#notify-events').innerHTML = (await res.json()).events.map(e => `
    <label class="inline-check"><input type="checkbox" class="notify-event" data-event="${esc(e.id)}" ${e.enabled ? 'checked' : ''}> ${esc(e.label)}</label>`).join('');
}

$('#notify-events').addEventListener('change', (e) => {
  if (!e.target.classList.contains('notify-event')) return;
  saveSetting({ [`notify_${e.target.dataset.event}`]: e.target.checked ? 'true' : 'false' });
});
async function saveOffsite() {
  return saveSetting({ offsite_kind: $('#offsite-kind').value, offsite_path: $('#offsite-path').value.trim(), offsite_url: $('#offsite-url').value.trim(),
    offsite_user: $('#offsite-user').value.trim(), offsite_password: $('#offsite-password').value, offsite_keep: $('#offsite-keep').value });
}
$('#offsite-save').addEventListener('click', async () => { $('#offsite-status').textContent = (await saveOffsite()) ? 'Saved.' : 'Could not save.'; });
for (const [id, url, label] of [['#offsite-test', '/api/backup/offsite/test', 'Wrote a test file to'], ['#offsite-now', '/api/backup/offsite/now', 'Sent to']]) {
  $(id).addEventListener('click', async () => {
    await saveOffsite();
    $('#offsite-status').textContent = 'Working...';
    const res = await fetch(url, { method: 'POST' });
    $('#offsite-status').textContent = res.ok ? `${label} ${(await res.json()).where}` : await sourceErrorText(res);
  });
}
async function saveSpoolman() {
  return saveSetting({ spoolman_url: $('#spoolman-url').value.trim(), spoolman_sync_usage: $('#spoolman-usage').checked ? 'true' : '' });
}
$('#spoolman-save').addEventListener('click', async () => { $('#spoolman-status').textContent = (await saveSpoolman()) ? 'Saved.' : 'Could not save.'; });
for (const [id, path] of [['#spoolman-test', 'test'], ['#spoolman-import', 'import'], ['#spoolman-export', 'export']]) {
  $(id).addEventListener('click', async () => {
    await saveSpoolman();
    $('#spoolman-status').textContent = 'Working...';
    const res = await fetch(`/api/spoolman/${path}`, { method: 'POST' });
    if (!res.ok) { $('#spoolman-status').textContent = await sourceErrorText(res); return; }
    const d = await res.json();
    $('#spoolman-status').textContent = d.message || (path === 'import' ? `Added ${d.added}, refreshed ${d.updated} (of ${d.seen}).` : `Sent ${d.created} spool${d.created === 1 ? '' : 's'}.`);
  });
}
$('#weekly-summary').addEventListener('change', () => saveSetting({ weekly_summary: $('#weekly-summary').checked ? 'true' : '' }));
$('#weekly-test-btn').addEventListener('click', async () => {
  await saveSetting({ notify_webhook_url: $('#notify-webhook-url').value });
  $('#weekly-test-status').textContent = 'Sending...';
  const res = await fetch('/api/settings/weekly-test', { method: 'POST' });
  $('#weekly-test-status').textContent = res.ok ? 'Sent: ' + (await res.json()).message.replace(/\n/g, ' ') : await sourceErrorText(res);
});
$('#notify-test-btn').addEventListener('click', async () => {
  await saveSetting({ notify_webhook_url: $('#notify-webhook-url').value });
  $('#notify-test-status').textContent = 'Sending...';
  const res = await fetch('/api/settings/notify-test', { method: 'POST' });
  $('#notify-test-status').textContent = res.ok ? 'Sent. Check your phone or channel.' : await sourceErrorText(res);
});
$('#auto-backup').addEventListener('change', () => saveSetting({ auto_backup: $('#auto-backup').value }, '#auto-backup-status'));
$('#auto-backup-keep').addEventListener('change', () => saveSetting({ auto_backup_keep: $('#auto-backup-keep').value }, '#auto-backup-status'));
$('#low-filament-g').addEventListener('change', () => saveSetting({ low_filament_g: $('#low-filament-g').value }));
$('#auto-listing-check').addEventListener('change', () => saveSetting({ auto_listing_check: $('#auto-listing-check').checked ? 'true' : 'false' }));

// ---------- Stats ----------
function barChart(title, rows, valueKey, format) {
  const max = Math.max(...rows.map(r => r[valueKey]), 0);
  const width = 600, height = 120, gap = 4;
  const bar = rows.length ? (width - gap * (rows.length - 1)) / rows.length : 0;
  const bars = rows.map((r, i) => {
    const h = max ? Math.max(r[valueKey] ? 2 : 0, Math.round(r[valueKey] / max * (height - 18))) : 0;
    const x = i * (bar + gap);
    return `<g><title>${esc(r.month)}: ${esc(format(r[valueKey]))}</title>
      <rect x="${x}" y="${height - 14 - h}" width="${bar}" height="${h}" rx="2" class="bar"/>
      <text x="${x + bar / 2}" y="${height - 2}" text-anchor="middle" class="bar-label">${esc(r.month.slice(5))}</text></g>`;
  }).join('');
  return `<div class="chart"><h3>${esc(title)} <small class="muted">most in a month: ${esc(format(max))}</small></h3>
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}">${bars}</svg></div>`;
}

async function loadStats() {
  const months = $('#stats-months').value;
  const res = await fetch(`/api/stats?months=${months}`);
  if (!res.ok) { $('#stats-body').innerHTML = '<p class="error-text">Could not load the statistics.</p>'; return; }
  const d = await res.json();
  const t = d.totals;
  const card = (label, value) => `<div class="stat-card"><div class="stat-value">${value}</div><div class="muted">${esc(label)}</div></div>`;
  const money = v => `$${Number(v).toFixed(2)}`;
  $('#stats-body').innerHTML = `
    <div class="stat-cards">
      ${card('prints', t.prints)}${card('models printed', t.models_printed)}${card('filament used', `${t.grams.toLocaleString()} g`)}
      ${card('print time', `${t.hours} h`)}${card('filament cost', money(t.cost))}
      ${card('printer success rate', d.printer_jobs.success_rate == null ? 'n/a' : `${d.printer_jobs.success_rate}%`)}
      ${card('never printed', `${d.library.never_printed} of ${d.library.models}`)}${card('on spools', `${d.filament.remaining_g.toLocaleString()} g`)}
      ${card('failed prints', d.failures.rate == null ? 'n/a' : `${d.failures.total} (${d.failures.rate}%)`)}
    </div>
    ${barChart('Prints per month', d.per_month, 'prints', v => String(v))}
    ${barChart('Filament per month (g)', d.per_month, 'grams', v => `${v} g`)}
    ${barChart('Filament cost per month', d.per_month, 'cost', money)}
    ${barChart('Models added per month', d.per_month, 'models_added', v => String(v))}
    <div class="page-grid">
      <div class="panel"><h3>Most printed</h3>${d.top_models.length ? `<ol class="link-list">${d.top_models.map(m =>
        `<li><a href="#/model/${m.id}">${esc(m.filename)}</a> <span class="muted">${m.prints} print${m.prints === 1 ? '' : 's'}</span></li>`).join('')}</ol>` : '<p class="muted">Nothing printed yet.</p>'}</div>
      <div class="panel"><h3>By material</h3>${d.materials.length ? `<ul class="link-list">${d.materials.map(m =>
        `<li>${esc(m.material)} <span class="muted">${m.grams.toLocaleString()} g</span></li>`).join('')}</ul>` : '<p class="muted">No filament recorded on prints yet.</p>'}
        <h3>Printer jobs</h3><p class="muted">${d.printer_jobs.done} finished, ${d.printer_jobs.stopped} stopped or failed</p></div>
      <div class="panel"><h3>Failed prints</h3>${d.failures.total ? `<ul class="link-list">${d.failures.by_reason.map(r =>
        `<li>${esc(r.label)} <span class="muted">${r.count}</span></li>`).join('')}</ul>
        <p class="muted">${d.failures.grams ? `${d.failures.grams.toLocaleString()} g of filament${d.failures.cost ? ` (${money(d.failures.cost)})` : ''} went into them.` : 'Say how much filament a failed print used (its log entry) to count the waste.'}</p>`
        : '<p class="muted">No failed prints logged. Mark one on a model\'s print history, or in the Print Queue.</p>'}</div>
    </div>`;
}
$('#stats-months').addEventListener('change', loadStats);

// ---------- Updates and API tokens (Settings) ----------
function versionLine(v) {
  if (!v) return '';
  const when = v.checked_at ? ` Checked ${new Date(v.checked_at * 1000).toLocaleString()}.` : '';
  if (v.update_available) return `You have <b>${esc(v.current)}</b>; <b>${esc(v.latest)}</b> is out. <a href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">What's new</a>. On Unraid, update the container to get it.${when}`;
  return `You have <b>${esc(v.current)}</b>${v.latest ? ' (the newest release)' : ''}.${when}`;
}

async function loadVersion() {
  const res = await fetch('/api/system/version');
  if (!res.ok) return;
  const v = await res.json();
  $('#version-line').innerHTML = versionLine(v);
  $('#update-check-on').checked = v.enabled !== false;
  $('#update-badge').classList.toggle('hidden', !v.update_available);
  if (v.update_available) $('#update-badge').textContent = `Update ${v.latest}`;
}
async function refreshUpdateBadge() { if (currentUser.role === 'admin') await loadVersion(); }

$('#update-check-on').addEventListener('change', () => saveSetting({ update_check: $('#update-check-on').checked ? 'true' : 'false' }));
$('#update-check-now').addEventListener('click', async () => {
  $('#update-status').textContent = 'Checking...';
  const res = await fetch('/api/system/update-check', { method: 'POST' });
  $('#update-status').textContent = res.ok ? '' : await sourceErrorText(res);
  if (res.ok) $('#version-line').innerHTML = versionLine(await res.json());
  loadVersion();
});

async function loadTokens() {
  const res = await fetch('/api/tokens');
  if (!res.ok) return;
  const list = (await res.json()).tokens;
  $('#tokens-list').innerHTML = list.length ? list.map(t => `
    <div class="user-row" data-id="${t.id}"><b>${esc(t.name)}</b> <span class="badge">${esc(t.scope)}</span> <code>${esc(t.prefix)}...</code>
      <span class="muted">${t.last_used_at ? 'last used ' + esc(String(t.last_used_at).slice(0, 16).replace('T', ' ')) : 'never used'}${t.expires_at ? ' · expires ' + esc(String(t.expires_at).slice(0, 10)) : ''}</span>
      <button class="token-revoke danger">Revoke</button></div>`).join('') : '<p class="muted">No tokens yet.</p>';
}
$('#token-add').addEventListener('click', async () => {
  const body = { name: $('#token-name').value, scope: $('#token-scope').value };
  if ($('#token-expiry').value) body.expires_days = parseInt($('#token-expiry').value);
  const res = await jsonRequest('POST', '/api/tokens', body);
  if (!res.ok) { $('#token-status').textContent = await sourceErrorText(res); return; }
  const made = await res.json();
  $('#token-name').value = '';
  $('#token-status').textContent = '';
  const box = $('#token-new');
  box.classList.remove('hidden');
  box.innerHTML = `Copy this key now; it is not shown again:<br><input readonly class="share-url" value="${esc(made.token)}" aria-label="New token"> <button id="token-copy">Copy</button>`;
  $('#token-copy').onclick = async (e) => { e.target.textContent = (await copyText(made.token)) ? 'Copied' : 'Select + copy'; };
  loadTokens();
});
$('#tokens-list').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('token-revoke')) return;
  if (!confirm('Revoke this token? Anything using it stops working at once.')) return;
  await fetch(`/api/tokens/${e.target.closest('.user-row').dataset.id}`, { method: 'DELETE' });
  loadTokens();
});

// ---------- Filing rules (Collections tab) ----------
async function loadRules() {
  const res = await fetch('/api/filing-rules');
  if (!res.ok) return;
  const rules = (await res.json()).rules;
  $('#rules-list').innerHTML = rules.length ? rules.map(r => `
    <div class="user-row" data-id="${r.id}">
      <label class="inline-check"><input type="checkbox" class="rule-enabled" ${r.enabled ? 'checked' : ''} aria-label="Enabled"></label>
      <span>When the <b>${esc(r.field)}</b> contains <b>${esc(r.match)}</b>, ${r.action === 'add_tag' ? 'add the tag' : 'file in the collection'} <b>${esc(r.value)}</b></span>
      <button class="rule-delete danger">Delete</button></div>`).join('') : '<p class="muted">No rules yet.</p>';
}
$('#rule-add').addEventListener('click', async () => {
  const res = await jsonRequest('POST', '/api/filing-rules', { field: $('#rule-field').value, match: $('#rule-match').value, action: $('#rule-action').value, value: $('#rule-value').value });
  $('#rules-status').textContent = res.ok ? 'Rule added.' : await sourceErrorText(res);
  if (res.ok) { $('#rule-match').value = ''; $('#rule-value').value = ''; loadRules(); }
});
$('#rules-list').addEventListener('change', async (e) => {
  if (!e.target.classList.contains('rule-enabled')) return;
  await jsonRequest('PATCH', `/api/filing-rules/${e.target.closest('.user-row').dataset.id}`, { enabled: e.target.checked });
});
$('#rules-list').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('rule-delete')) return;
  await fetch(`/api/filing-rules/${e.target.closest('.user-row').dataset.id}`, { method: 'DELETE' });
  loadRules();
});
async function runRules(dry) {
  $('#rules-status').textContent = dry ? 'Checking...' : 'Running...';
  const res = await jsonRequest('POST', '/api/filing-rules/apply', { dry_run: dry });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $('#rules-status').textContent = data.detail || 'Could not run the rules.'; return; }
  $('#rules-status').innerHTML = `${dry ? 'Would change' : 'Changed'} ${data.changed} (${data.rules} rule${data.rules === 1 ? '' : 's'} over ${data.models_checked} linked models).` +
    (data.activity_id ? ` <button id="rules-undo" data-id="${data.activity_id}">Undo</button>` : '');
}
$('#rules-preview').addEventListener('click', () => runRules(true));
$('#rules-run').addEventListener('click', () => runRules(false));
$('#rules-status').addEventListener('click', async (e) => {
  if (e.target.id !== 'rules-undo') return;
  if (await undoActivity(e.target.dataset.id)) $('#rules-status').textContent = 'Undone.';
});

// ---------- Orders ----------
let ordersOpen = new Set();
async function loadOrders() {
  const res = await fetch('/api/orders');
  if (!res.ok) { $('#orders-list').innerHTML = '<p class="error-text">Could not load the orders.</p>'; return; }
  const data = await res.json();
  const readOnly = currentUser.role === 'viewer';
  $('#order-new-box').classList.toggle('hidden', readOnly);
  const badge = o => `<span class="status-badge ${o.status === 'delivered' ? '' : o.status === 'cancelled' ? 'status-failed' : 'status-printing'}">${esc(o.status)}</span>`;
  $('#orders-list').innerHTML = data.orders.length ? data.orders.map(o => `
    <div class="panel order-card" data-id="${o.id}">
      <div class="row order-head"><b>${esc(o.customer)}</b> ${badge(o)} ${o.paid ? '<span class="status-badge">paid</span>' : ''}
        ${o.due_date ? `<span class="${o.overdue ? 'warn-text' : 'muted'}">due ${esc(o.due_date)}${o.overdue ? ' (late)' : ''}</span>` : ''}
        <span class="muted">${o.units_done} of ${o.units} printed &middot; ${money2(o.price)} for ${money2(o.cost)} cost &middot; profit ${money2(o.profit)}</span>
        <button class="order-toggle">${ordersOpen.has(o.id) ? 'Close' : 'Open'}</button></div>
      <div class="order-body ${ordersOpen.has(o.id) ? '' : 'hidden'}"></div></div>`).join('') : '<p class="muted">No orders yet.</p>';
  for (const o of data.orders) if (ordersOpen.has(o.id)) renderOrderBody(o.id);
}

async function renderOrderBody(id) {
  const card = document.querySelector(`.order-card[data-id="${id}"]`);
  if (!card) return;
  const res = await fetch(`/api/orders/${id}`);
  if (!res.ok) return;
  const o = await res.json();
  const readOnly = currentUser.role === 'viewer';
  const statuses = ['quote', 'accepted', 'printing', 'ready', 'delivered', 'cancelled'];
  card.querySelector('.order-body').innerHTML = `
    ${o.contact ? `<div class="muted">${esc(o.contact)}</div>` : ''}${o.notes ? `<div>${esc(o.notes)}</div>` : ''}
    <table class="facts"><thead><tr><th>Model</th><th>How many</th><th>Price each</th><th>Line</th><th>Printed</th><th></th></tr></thead><tbody>
      ${o.items.map(i => `<tr data-item="${i.id}"><td><a href="#/model/${i.model_id}">${esc(i.filename || 'model ' + i.model_id)}</a></td>
        <td><input class="oi-qty" type="number" min="1" value="${i.quantity}" style="width:5em" ${readOnly ? 'disabled' : ''}></td>
        <td><input class="oi-price" type="number" min="0" step="0.01" value="${i.unit_price ?? ''}" placeholder="${i.suggested_price != null ? i.suggested_price + ' (suggested)' : 'price'}" style="width:8em" ${readOnly ? 'disabled' : ''}></td>
        <td>${money2(i.line_price)}</td><td>${i.units_done} / ${i.quantity}${i.units_queued < i.quantity ? ` <span class="muted">(${i.quantity - i.units_queued} not queued)</span>` : ''}</td>
        <td>${readOnly ? '' : '<button class="oi-remove">Remove</button>'}</td></tr>`).join('')}</tbody></table>
    ${readOnly ? '' : `<div class="row"><input class="oi-model" list="order-models-${id}" placeholder="Add a model (type to search)"><datalist id="order-models-${id}"></datalist>
      <input class="oi-new-qty" type="number" min="1" value="1" style="width:5em" aria-label="How many"><button class="oi-add">Add</button>
      <select class="order-status">${statuses.map(s => `<option value="${s}" ${s === o.status ? 'selected' : ''}>${s}</option>`).join('')}</select>
      <label class="inline-check"><input type="checkbox" class="order-paid" ${o.paid ? 'checked' : ''}> paid</label>
      <button class="order-queue primary">Put the prints in the queue</button><button class="order-delete danger">Delete</button><span class="order-msg muted"></span></div>
      ${o.suggested_status !== o.status ? `<div class="muted">Looks like it is <b>${esc(o.suggested_status)}</b> now.</div>` : ''}`}`;
  const body = card.querySelector('.order-body');
  const msg = text => { const el = body.querySelector('.order-msg'); if (el) el.textContent = text; };
  const reload = async () => { await loadOrders(); };
  body.querySelectorAll('.oi-qty, .oi-price').forEach(input => input.onchange = async () => {
    const row = input.closest('tr');
    const price = row.querySelector('.oi-price').value;
    const r = await jsonRequest('PATCH', `/api/orders/${id}/items/${row.dataset.item}`, { quantity: parseInt(row.querySelector('.oi-qty').value), unit_price: price === '' ? null : parseFloat(price) });
    if (r.ok) reload(); else msg(await sourceErrorText(r));
  });
  body.querySelectorAll('.oi-remove').forEach(b => b.onclick = async () => { await fetch(`/api/orders/${id}/items/${b.closest('tr').dataset.item}`, { method: 'DELETE' }); reload(); });
  const modelInput = body.querySelector('.oi-model');
  if (modelInput) modelInput.oninput = async () => {
    if (modelInput.value.length < 2) return;
    const found = await fetch(`/api/library/models?q=${encodeURIComponent(modelInput.value.replace(/\s*#\d+$/, ''))}&limit=15`).then(r => (r.ok ? r.json() : []));
    body.querySelector('datalist').innerHTML = found.map(m => `<option value="${esc(m.filename)} #${m.id}"></option>`).join('');
  };
  const add = body.querySelector('.oi-add');
  if (add) add.onclick = async () => {
    const m = /#(\d+)\s*$/.exec(modelInput.value);
    if (!m) { msg('Pick a model from the list.'); return; }
    const r = await jsonRequest('POST', `/api/orders/${id}/items`, { model_id: parseInt(m[1]), quantity: parseInt(body.querySelector('.oi-new-qty').value) || 1 });
    if (r.ok) reload(); else msg(await sourceErrorText(r));
  };
  const status = body.querySelector('.order-status');
  if (status) status.onchange = async () => { await jsonRequest('PATCH', `/api/orders/${id}`, { status: status.value }); reload(); };
  const paid = body.querySelector('.order-paid');
  if (paid) paid.onchange = async () => { await jsonRequest('PATCH', `/api/orders/${id}`, { paid: paid.checked }); reload(); };
  const queue = body.querySelector('.order-queue');
  if (queue) queue.onclick = async () => {
    const r = await jsonRequest('POST', `/api/orders/${id}/queue`, {});
    if (r.ok) { const d = await r.json(); showNotice(d.queued ? `Queued ${d.queued} print${d.queued === 1 ? '' : 's'}.` : 'Everything is already in the queue.'); reload(); } else msg(await sourceErrorText(r));
  };
  const del = body.querySelector('.order-delete');
  if (del) del.onclick = async () => { if (confirm('Delete this order? Its prints stay in the queue.')) { await fetch(`/api/orders/${id}`, { method: 'DELETE' }); ordersOpen.delete(id); reload(); } };
}

$('#orders-list').addEventListener('click', (e) => {
  if (!e.target.classList.contains('order-toggle')) return;
  const id = parseInt(e.target.closest('.order-card').dataset.id);
  if (ordersOpen.has(id)) ordersOpen.delete(id); else ordersOpen.add(id);
  loadOrders();
});
$('#order-create').addEventListener('click', async () => {
  const res = await jsonRequest('POST', '/api/orders', { customer: $('#order-customer').value, contact: $('#order-contact').value, due_date: $('#order-due').value || null, notes: $('#order-notes').value });
  if (!res.ok) { $('#order-status-msg').textContent = await sourceErrorText(res); return; }
  const o = await res.json();
  ordersOpen.add(o.id);
  ['#order-customer', '#order-contact', '#order-due', '#order-notes'].forEach(sel => { $(sel).value = ''; });
  $('#order-status-msg').textContent = '';
  loadOrders();
});

// ---------- Calendar ----------
function localMonth(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
}
let calMonth = localMonth(new Date());
let calSelected = null;
let calData = null;
let calPrinter = '';
let calHints = {};
let calRepeatHtml = '';              // the answer of the last 'Repeat this week', kept while the calendar redraws

function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  return localMonth(new Date(y, m - 1 + delta, 1));
}

function dayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
}

async function loadCalendar() {
  loadFailureReasons();
  const res = await fetch(`/api/calendar?month=${calMonth}${calPrinter ? `&printer=${encodeURIComponent(calPrinter)}` : ''}`);
  if (!res.ok) {
    if (calPrinter) { calPrinter = ''; return loadCalendar(); }             // that printer is gone: show them all
    $('#cal-status').textContent = await sourceErrorText(res);
    return;
  }
  calData = await res.json();
  calHints = await loadHints();
  renderCalendar();
}

function renderCalendar() {
  const data = calData;
  const [y, m] = data.month.split('-').map(Number);
  $('#cal-title').textContent = new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const offset = (new Date(y, m - 1, 1).getDay() + 6) % 7;                  // weeks start on Monday
  const last = parseInt(data.last.slice(8));
  const today = new Date();
  const todayIso = `${localMonth(today)}-${String(today.getDate()).padStart(2, '0')}`;
  $('#cal-printer-label').classList.toggle('hidden', !data.printers.length);
  $('#cal-printer').innerHTML = `<option value="">All printers</option>${data.printers.map(p => `<option value="${p.id}" ${String(p.id) === calPrinter ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}
    <option value="none" ${calPrinter === 'none' ? 'selected' : ''}>Not assigned</option>`;
  $('#cal-ics').href = `/api/calendar/export.ics${calPrinter ? `?printer=${encodeURIComponent(calPrinter)}` : ''}`;
  $('#cal-printer-note').classList.toggle('hidden', !calPrinter);
  const shortNote = $('#cal-short-note');
  shortNote.classList.toggle('hidden', !data.short_count);
  shortNote.textContent = data.short_count ? `${data.short_count} planned print${data.short_count === 1 ? '' : 's'} need${data.short_count === 1 ? 's' : ''} more filament than the spool will have left (marked below).` : '';
  const heads = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(d => `<div class="cal-head">${d}</div>`).join('');
  let cells = '<div class="cal-cell cal-empty"></div>'.repeat(offset);
  for (let d = 1; d <= last; d++) {
    const iso = `${data.month}-${String(d).padStart(2, '0')}`;
    const info = data.days[iso];
    const classes = ['cal-cell', iso === todayIso ? 'cal-today' : '', iso === calSelected ? 'cal-selected' : '', info && info.overbooked ? 'cal-over' : ''].join(' ');
    const planned = info ? info.planned.length : 0, printed = info ? info.printed.length : 0;
    cells += `<button type="button" class="${classes}" data-day="${iso}" aria-label="${esc(dayLabel(iso))}">
      <span class="cal-num">${d}</span>
      ${planned ? `<span class="cal-planned">${planned} planned<br>${formatMinutes(info.minutes)}</span>` : ''}
      ${info && info.short ? '<span class="cal-short">&#9888; filament</span>' : ''}
      ${printed ? `<span class="cal-printed">${printed} printed</span>` : ''}</button>`;
  }
  $('#cal-grid').innerHTML = heads + cells;
  renderCalendarDay();
  $('#cal-unplanned').innerHTML = data.unplanned.length ? data.unplanned.map(u => `
    <li data-id="${u.id}"><a href="#/model/${u.model_id}">${esc(u.filename || 'model ' + u.model_id)}</a>${u.minutes ? ` <span class="muted">${esc(formatMinutes(u.minutes))}</span>` : ''}
      <input type="date" class="cal-set-date" aria-label="Day for this print" value="${calSelected || ''}"> <button class="cal-set">Plan for this day</button></li>`).join('')
    + (data.unplanned_total > data.unplanned.length ? `<li class="muted">and ${data.unplanned_total - data.unplanned.length} more</li>` : '')
    : '<li class="muted">Nothing is waiting without a day.</li>';
}

function renderCalendarDay() {
  const panel = $('#cal-day');
  const info = calSelected && calData.days[calSelected];
  if (!calSelected) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const capacity = formatMinutes(calData.capacity_minutes);
  panel.innerHTML = `<h3>${esc(dayLabel(calSelected))}</h3>
    ${info && info.overbooked ? `<p class="warn-text">${esc(formatMinutes(info.minutes))} of prints, and your printers have about ${esc(capacity)} for a day.</p>` : ''}
    ${info && info.planned.length ? '<ul>' + info.planned.map(p => `
      <li data-id="${p.id}"><a href="#/model/${p.model_id}">${esc(p.filename || 'model ' + p.model_id)}</a>
        <span class="muted">${p.minutes ? esc(formatMinutes(p.minutes)) : 'no estimate'}${p.printer ? ' on ' + esc(p.printer) : ''}${p.status === 'printing' ? ' (printing)' : ''}</span>
        ${calHints[p.model_id] ? `<div class="warn-text" title="${esc(calHints[p.model_id].tip || '')}">&#9888; ${esc(hintText(calHints[p.model_id]))}</div>` : ''}
        ${p.short ? `<div class="warn-text">&#9888; ${esc(p.short.spool)} will be short by ${p.short.short_by} g (this print needs ${p.need ?? p.short.need} g)</div>` : ''}
        <input type="date" class="cal-move-date" aria-label="Move to another day" value="${calSelected}"> <button class="cal-move">Move</button> <button class="cal-unplan">Take off the calendar</button></li>`).join('') + '</ul>' : '<p class="muted">Nothing planned for this day.</p>'}
    <div class="cal-repeat"><h4>Repeat this week</h4>
      <p class="muted">Copy every print planned in the week of ${esc(dayLabel(calSelected))} onto the following weeks, as waiting prints.</p>
      <div class="row"><label>For <select id="cal-repeat-weeks">${[1, 2, 3, 4, 6, 8].map(n => `<option value="${n}">${n} week${n === 1 ? '' : 's'}</option>`).join('')}</select></label>
        <label class="inline-check"><input type="checkbox" id="cal-repeat-done" checked> include prints already done</label>
        <button id="cal-repeat-preview">Preview</button><button id="cal-repeat-go" class="primary">Repeat</button></div>
      <div id="cal-repeat-status" class="muted">${calRepeatHtml}</div></div>
    ${info && info.printed.length ? '<h4>Printed</h4><ul>' + info.printed.map(p => `
      <li><a href="#/model/${p.model_id}">${esc(p.filename || 'model ' + p.model_id)}</a>
        ${p.failed ? `<span class="status-badge status-failed">Failed${p.reason && failureReasons ? ': ' + esc((failureReasons.find(r => r.key === p.reason) || {}).label || '') : ''}</span>` : ''}
        <span class="muted">${p.minutes ? esc(formatMinutes(p.minutes)) : ''}${p.has_photo ? ' &middot; has a photo' : ''}</span>
        <label class="button-link cal-photo-btn">${p.has_photo ? 'New photo' : 'Take photo'}<input type="file" accept="image/*" capture="environment" class="cal-photo-input" data-id="${p.id}" hidden></label></li>`).join('') + '</ul>' : ''}`;
}

async function planCalendarItem(id, day) {
  const res = await jsonRequest('PATCH', `/api/queue/${id}`, { planned_date: day || null });
  if (!res.ok) { showNotice(await sourceErrorText(res)); return; }
  await loadCalendar();
}

$('#cal-prev').addEventListener('click', () => { calMonth = shiftMonth(calMonth, -1); calSelected = null; loadCalendar(); });
$('#cal-next').addEventListener('click', () => { calMonth = shiftMonth(calMonth, 1); calSelected = null; loadCalendar(); });
$('#cal-today').addEventListener('click', () => { calMonth = localMonth(new Date()); calSelected = null; loadCalendar(); });
async function repeatWeek(dry) {
  const out = $('#cal-repeat-status');
  const show = (html) => { calRepeatHtml = html; out.innerHTML = html; };
  const statuses = $('#cal-repeat-done').checked ? ['queued', 'printing', 'done'] : ['queued', 'printing'];
  show(dry ? 'Checking...' : 'Copying...');
  const res = await jsonRequest('POST', '/api/calendar/copy', { date: calSelected, weeks: parseInt($('#cal-repeat-weeks').value), statuses, dry_run: dry });
  if (!res.ok) { show(esc(await sourceErrorText(res))); return; }
  const r = await res.json();
  if (!r.created.length) { show('Nothing was planned that week.'); return; }
  show(`${dry ? 'Would add' : 'Added'} ${r.created.length} waiting print${r.created.length === 1 ? '' : 's'} (the week of ${esc(r.week)} and the days after).`
    + (!dry && r.activity_id ? ` <button id="cal-repeat-undo" data-id="${r.activity_id}">Undo</button>` : ''));
  if (!dry) loadCalendar();
}
$('#cal-day').addEventListener('click', async (e) => {
  if (e.target.id === 'cal-repeat-preview') repeatWeek(true);
  else if (e.target.id === 'cal-repeat-go') repeatWeek(false);
  else if (e.target.id === 'cal-repeat-undo' && await undoActivity(e.target.dataset.id)) { calRepeatHtml = 'Undone.'; loadCalendar(); }
});
$('#cal-printer').addEventListener('change', () => { calPrinter = $('#cal-printer').value; calSelected = null; loadCalendar(); });
$('#cal-day').addEventListener('change', async (e) => {
  if (!e.target.classList.contains('cal-photo-input') || !e.target.files.length) return;
  const form = new FormData();
  form.append('file', e.target.files[0]);
  const res = await fetch(`/api/prints/${e.target.dataset.id}/photo`, { method: 'POST', body: form });
  if (res.ok) { showNotice('Photo saved.'); loadCalendar(); } else showNotice(await sourceErrorText(res));
});
$('#cal-grid').addEventListener('click', (e) => {
  const cell = e.target.closest('.cal-cell[data-day]');
  if (!cell) return;
  calSelected = calSelected === cell.dataset.day ? null : cell.dataset.day;
  calRepeatHtml = '';
  renderCalendar();
});
$('#cal-day').addEventListener('click', async (e) => {
  const row = e.target.closest('li[data-id]');
  if (!row) return;
  if (e.target.classList.contains('cal-unplan')) await planCalendarItem(row.dataset.id, null);
  else if (e.target.classList.contains('cal-move')) await planCalendarItem(row.dataset.id, row.querySelector('.cal-move-date').value);
});
$('#cal-unplanned').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('cal-set')) return;
  const row = e.target.closest('li');
  const day = row.querySelector('.cal-set-date').value;
  if (!day) { $('#cal-status').textContent = 'Choose a day first (click one on the calendar, or pick a date).'; return; }
  await planCalendarItem(row.dataset.id, day);
});
async function planAutomatically(dry) {
  $('#cal-status').textContent = dry ? 'Checking...' : 'Planning...';
  const res = await jsonRequest('POST', '/api/calendar/plan', { dry_run: dry });
  if (!res.ok) { $('#cal-status').textContent = await sourceErrorText(res); return; }
  const r = await res.json();
  if (!r.assigned.length) { $('#cal-status').textContent = 'Nothing is waiting without a day.'; return; }
  const days = r.assigned.map(a => a.planned_date).sort();
  const guessed = r.assigned.filter(a => a.guessed).length;
  $('#cal-status').innerHTML = `${dry ? 'Would plan' : 'Planned'} ${r.assigned.length} print${r.assigned.length === 1 ? '' : 's'} between ${esc(days[0])} and ${esc(days[days.length - 1])}${guessed ? ` (${guessed} without an estimate counted as an hour each)` : ''}.`
    + (r.unplaced ? ` ${r.unplaced} did not fit in the next year.` : '')
    + (!dry && r.activity_id ? ` <button id="cal-undo" data-id="${r.activity_id}">Undo</button>` : '');
  if (!dry) loadCalendar();
}
$('#cal-plan-preview').addEventListener('click', () => planAutomatically(true));
$('#cal-plan-run').addEventListener('click', () => planAutomatically(false));
$('#cal-status').addEventListener('click', async (e) => {
  if (e.target.id !== 'cal-undo') return;
  if (await undoActivity(e.target.dataset.id)) { $('#cal-status').textContent = 'Undone.'; loadCalendar(); }
});

$('#calendar-hours-save').addEventListener('click', async () => {
  const ok = await saveSetting({ calendar_hours_per_day: $('#calendar-hours').value.trim() });
  $('#calendar-hours-status').textContent = ok ? 'Saved.' : 'Could not save.';
});

// ---------- Library information and MQTT (Settings) ----------
async function importLibraryInfo(dry) {
  const file = $('#io-file').files[0];
  if (!file) { $('#io-result').textContent = 'Choose a file first.'; return; }
  const form = new FormData();
  form.append('file', file);
  form.append('overwrite', $('#io-overwrite').checked ? 'true' : 'false');
  form.append('dry_run', dry ? 'true' : 'false');
  $('#io-result').textContent = dry ? 'Checking...' : 'Importing...';
  const res = await fetch('/api/library-io/import', { method: 'POST', body: form });
  if (!res.ok) { $('#io-result').textContent = await sourceErrorText(res); return; }
  const r = await res.json();
  $('#io-result').innerHTML = `${dry ? '<b>Preview (nothing changed):</b> ' : '<b>Done:</b> '}${r.rows} rows, ${r.matched} matched, ${r.unmatched} not found. `
    + `${r.models_changed} models changed: ${r.tags_added} tags, ${r.collections_added} collection links, ${r.projects_added} project links, ${r.fields_set} fields, ${r.sources_set} listing links.`
    + (r.problems.length ? `<br>${r.problems.map(esc).join('<br>')}` : '');
}
$('#io-preview').addEventListener('click', () => importLibraryInfo(true));
$('#io-import').addEventListener('click', () => importLibraryInfo(false));

function loadMqttSettings(s) {
  $('#mqtt-host').value = s.mqtt_host || '';
  $('#mqtt-port').value = s.mqtt_port || '';
  $('#mqtt-prefix').value = s.mqtt_prefix || '';
  $('#mqtt-user').value = s.mqtt_user || '';
  $('#mqtt-password').value = s.mqtt_password || '';
  $('#mqtt-tls').checked = s.mqtt_tls === 'true';
  $('#mqtt-discovery').checked = s.mqtt_discovery !== 'false';
  $('#mqtt-status').innerHTML = s.mqtt_last_error ? `<span class="error-text">Last problem: ${esc(s.mqtt_last_error)}</span>` : '';
}
async function saveMqtt() {
  return saveSetting({
    mqtt_host: $('#mqtt-host').value.trim(), mqtt_port: $('#mqtt-port').value, mqtt_prefix: $('#mqtt-prefix').value.trim(),
    mqtt_user: $('#mqtt-user').value.trim(), mqtt_password: $('#mqtt-password').value,
    mqtt_tls: $('#mqtt-tls').checked ? 'true' : 'false', mqtt_discovery: $('#mqtt-discovery').checked ? 'true' : 'false',
  });
}
$('#mqtt-save').addEventListener('click', async () => { $('#mqtt-status').textContent = (await saveMqtt()) ? 'Saved.' : 'Could not save.'; });
$('#mqtt-test').addEventListener('click', async () => {
  await saveMqtt();
  $('#mqtt-status').textContent = 'Sending...';
  const res = await fetch('/api/settings/mqtt-test', { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  $('#mqtt-status').textContent = res.ok ? data.message : (data.detail || 'Could not send.');
});

// ---------- Pictures for STEP files, rendered here and sent to the server ----------
async function stepThumbnailBlob(file, size) {
  const buffer = new Uint8Array(await (await fetch(file)).arrayBuffer());
  const occt = await getOcctModule();
  const result = occt.ReadStepFile(buffer, null);
  if (!result.success || !result.meshes.length) throw new Error('no geometry');
  const group = new THREE.Group();
  const fallback = new THREE.MeshStandardMaterial({ color: viewerModelColor });
  for (const m of result.meshes) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(m.attributes.position.array, 3));
    if (m.attributes.normal) geometry.setAttribute('normal', new THREE.Float32BufferAttribute(m.attributes.normal.array, 3));
    geometry.setIndex(m.index.array);
    if (!m.attributes.normal) geometry.computeVertexNormals();
    group.add(new THREE.Mesh(geometry, m.color ? new THREE.MeshStandardMaterial({ color: new THREE.Color(m.color[0], m.color[1], m.color[2]) }) : fallback));
  }
  const box = new THREE.Box3().setFromObject(group);
  group.position.sub(box.getCenter(new THREE.Vector3()));
  const radius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1);
  const canvas = document.createElement('canvas');
  canvas.width = size; canvas.height = Math.round(size * 0.75);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setSize(canvas.width, canvas.height, false);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d0f12);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 2));
  const light = new THREE.DirectionalLight(0xffffff, 1.5);
  light.position.set(1, 1, 1);
  scene.add(light, group);
  const camera = new THREE.PerspectiveCamera(45, canvas.width / canvas.height, radius / 100, radius * 100);
  camera.position.set(radius * 2, radius * 2, radius * 3);
  camera.lookAt(0, 0, 0);
  renderer.render(scene, camera);
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
  group.traverse(o => { if (o.geometry) o.geometry.dispose(); });
  renderer.dispose();
  renderer.forceContextLoss();
  return blob;
}

async function makeStepThumbnails() {
  const status = $('#step-thumbs-status');
  const res = await fetch('/api/library/models?limit=5000');
  if (!res.ok) return;
  const todo = (await res.json()).filter(m => ['.step', '.stp'].includes(m.extension) && !m.thumbnail_path);
  if (!todo.length) { status.textContent = 'Every STEP file already has a picture.'; return; }
  let done = 0, failed = 0;
  for (const m of todo) {
    status.textContent = `Making pictures for STEP files: ${done + failed + 1} of ${todo.length}...`;
    try {
      const blob = await stepThumbnailBlob(`/api/library/models/${m.id}/file`, 320);
      const form = new FormData();
      form.append('file', blob, 'thumb.png');
      const up = await fetch(`/api/library/models/${m.id}/thumbnail`, { method: 'POST', body: form });
      if (up.ok) done++; else failed++;
    } catch (e) { failed++; }
  }
  status.textContent = `Made ${done} picture${done === 1 ? '' : 's'}${failed ? `, ${failed} could not be made` : ''}.`;
  loadModels();
}
$('#step-thumbs-btn').addEventListener('click', makeStepThumbnails);

async function loadStorage() {
  const body = $('#storage-body');
  const res = await fetch('/api/storage');
  if (!res.ok) { body.textContent = await sourceErrorText(res); return; }
  const d = await res.json();
  const top = Math.max(1, d.database_bytes, ...d.by_type.map(t => t.bytes), ...d.folders.map(f => f.bytes));
  const bar = (label, bytes, note) => `<div class="bar-row"><span>${esc(label)}</span><span><span class="bar" style="display:block;width:${Math.max(1, Math.round(bytes / top * 100))}%"></span></span><span>${formatBytes(bytes)}${note ? ` <small class="muted">${note}</small>` : ''}</span></div>`;
  const disk = x => (x.total ? `${formatBytes(x.free)} free of ${formatBytes(x.total)}` : 'unknown');
  body.innerHTML = `<p><b>${d.models}</b> models, <b>${formatBytes(d.library_bytes)}</b> in the library &middot; library disk: ${disk(d.library_disk)} &middot; Model Hub's own disk: ${disk(d.config_disk)}</p>
    <h4>Library by file type</h4>${d.by_type.map(t => bar(t.extension, t.bytes, `${t.models} model${t.models === 1 ? '' : 's'}`)).join('')}
    <h4>Model Hub's own folders</h4>${d.folders.map(f => bar(f.name, f.bytes)).join('')}${bar('database', d.database_bytes)}
    <p class="muted">${d.duplicates.models ? `${d.duplicates.models} model${d.duplicates.models === 1 ? ' is a duplicate' : 's are duplicates'} taking ${formatBytes(d.duplicates.bytes)}: see the Duplicates page.` : 'No duplicate files.'}</p>
    <h4>Biggest models</h4>${d.biggest.map(b => `<div class="creator-row"><a href="#/model/${b.id}">${esc(b.filename)}</a><span class="muted">${formatBytes(b.bytes || 0)}</span></div>`).join('')}`;
}
$('#storage-refresh').addEventListener('click', loadStorage);

async function loadSettings() {
  loadStorage();
  const s = await (await fetch('/api/settings')).json();
  $('#ai-mode').value = s.ai_mode || 'local';
  $('#ollama-host').value = s.ollama_host || '';
  $('#ollama-vision-model').value = s.ollama_vision_model || '';
  $('#ollama-embed-model').value = s.ollama_embed_model || '';
  $('#ai-api-base').value = s.ai_api_base || '';
  $('#ai-api-key').value = s.ai_api_key || '';
  $('#ai-api-model').value = s.ai_api_model || '';
  $('#unraid-share-path').value = s.unraid_share_path || '';
  viewerModelColor = normalizeViewerModelColor(s.viewer_model_color);
  $('#viewer-model-color').value = viewerModelColor;
  $('#est-material').value = s.est_material || 'PLA';
  $('#est-density').value = s.est_density || '';
  $('#est-infill').value = s.est_infill || '15';
  $('#cost-kwh-price').value = s.cost_kwh_price || '';
  $('#cost-printer-watts').value = s.cost_printer_watts || '';
  $('#failed-deduct').checked = s.failed_deduct !== 'false';
  $('#cost-machine-hour').value = s.cost_machine_per_hour || '';
  $('#cost-failure-pct').value = s.cost_failure_pct || '';
  $('#cost-margin-pct').value = s.cost_margin_pct || '';
  $('#cost-default-kg').value = s.cost_default_per_kg || '';
  $('#spoolman-url').value = s.spoolman_url || '';
  $('#spoolman-usage').checked = s.spoolman_sync_usage === 'true';
  $('#bed-x').value = s.bed_x || '';
  $('#bed-y').value = s.bed_y || '';
  $('#bed-z').value = s.bed_z || '';
  $('#notify-webhook-url').value = s.notify_webhook_url || '';
  $('#weekly-summary').checked = s.weekly_summary === 'true';
  $('#offsite-kind').value = s.offsite_kind || '';
  $('#offsite-path').value = s.offsite_path || '';
  $('#offsite-url').value = s.offsite_url || '';
  $('#offsite-user').value = s.offsite_user || '';
  $('#offsite-password').value = s.offsite_password || '';
  $('#offsite-keep').value = s.offsite_keep || '';
  $('#offsite-status').innerHTML = s.offsite_last_error ? `<span class="error-text">Last problem: ${esc(s.offsite_last_error)}</span>` : (s.offsite_last ? `Last sent: ${esc(s.offsite_last)}` : '');
  await renderSiteSettings(s);
  refreshBackups();
  loadUsers();
  loadMqttSettings(s);
  $('#calendar-hours').value = s.calendar_hours_per_day || '';
  renderSharePanel('#library-share-panel', 'library', 0);
  loadVersion();
  loadTokens();
  $('#auto-backup').value = s.auto_backup || 'weekly';
  $('#auto-backup-keep').value = s.auto_backup_keep || '';
  $('#low-filament-g').value = s.low_filament_g || '';
  $('#auto-listing-check').checked = s.auto_listing_check === 'true';
  loadNotifyEvents();
  const keyRes = await fetch('/api/settings/extension-key');
  $('#ext-api-key').value = keyRes.ok ? (await keyRes.json()).extension_api_key : '';
}
$('#save-settings-btn').addEventListener('click', async () => {
  await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ai_mode: $('#ai-mode').value,
      ollama_host: $('#ollama-host').value,
      ollama_vision_model: $('#ollama-vision-model').value,
      ollama_embed_model: $('#ollama-embed-model').value,
      ai_api_base: $('#ai-api-base').value,
      ai_api_key: $('#ai-api-key').value,
      ai_api_model: $('#ai-api-model').value,
      unraid_share_path: $('#unraid-share-path').value,
    }),
  });
  $('#settings-status').textContent = 'Saved.';
  setTimeout(() => $('#settings-status').textContent = '', 2000);
});

$('#save-viewer-settings-btn').addEventListener('click', async () => {
  const color = normalizeViewerModelColor($('#viewer-model-color').value);
  const res = await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ viewer_model_color: color }),
  });
  if (!res.ok) {
    $('#viewer-settings-status').textContent = 'Failed to save.';
    return;
  }
  viewerModelColor = color;
  $('#viewer-model-color').value = color;
  $('#viewer-settings-status').textContent = 'Saved.';
  setTimeout(() => $('#viewer-settings-status').textContent = '', 2000);
});

$('#save-estimate-settings-btn').addEventListener('click', async () => {
  await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      est_material: $('#est-material').value,
      est_density: $('#est-density').value,
      est_infill: $('#est-infill').value,
      cost_kwh_price: $('#cost-kwh-price').value,
      cost_printer_watts: $('#cost-printer-watts').value,
      failed_deduct: $('#failed-deduct').checked ? '' : 'false',
      cost_machine_per_hour: $('#cost-machine-hour').value, cost_failure_pct: $('#cost-failure-pct').value,
      cost_margin_pct: $('#cost-margin-pct').value, cost_default_per_kg: $('#cost-default-kg').value,
      bed_x: $('#bed-x').value, bed_y: $('#bed-y').value, bed_z: $('#bed-z').value,
    }),
  });
});

$('#save-notify-btn').addEventListener('click', async () => {
  await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notify_webhook_url: $('#notify-webhook-url').value }),
  });
});

$('#nonmodel-check-btn').addEventListener('click', async () => {
  const res = await fetch('/api/library/non-model-files');
  if (!res.ok) return;
  const data = await res.json();
  $('#nonmodel-remove-btn').classList.toggle('hidden', data.count === 0);
  $('#nonmodel-status').textContent = data.count
    ? `${data.count} entr${data.count === 1 ? 'y' : 'ies'} for other file types (${Object.entries(data.by_extension).map(([k, v]) => `${k} x${v}`).join(', ')}). Examples: ${data.examples.join(', ')}`
    : 'Nothing to clean up: the index only has model files.';
});
$('#nonmodel-remove-btn').addEventListener('click', async () => {
  if (!confirm('Remove these entries from the index? The files themselves are not touched.')) return;
  const res = await fetch('/api/library/non-model-files/remove', { method: 'POST' });
  const data = res.ok ? await res.json() : { removed: 0 };
  $('#nonmodel-remove-btn').classList.add('hidden');
  $('#nonmodel-status').textContent = `Removed ${data.removed} entr${data.removed === 1 ? 'y' : 'ies'} from the index.`;
});

$('#regen-key-btn').addEventListener('click', async () => {
  const res = await fetch('/api/settings/regenerate-extension-key', { method: 'POST' });
  const data = await res.json();
  $('#ext-api-key').value = data.extension_api_key;
});

$('#change-password-btn').addEventListener('click', async () => {
  const res = await fetch('/api/auth/change-password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      current_password: $('#cur-password').value,
      new_password: $('#new-password').value,
    }),
  });
  const data = await res.json().catch(() => ({}));
  $('#account-status').textContent = res.ok ? 'Password changed.' : (data.detail || 'Failed.');
  if (res.ok) { $('#cur-password').value = ''; $('#new-password').value = ''; }
});

$('#logout-btn').addEventListener('click', async () => {
  await fetch('/api/auth/logout', { method: 'POST' });
  location.reload();
});

// ---------- Who is signed in ----------
let currentUser = { username: null, role: 'admin' };

function showNotice(message) {
  const bar = $('#notice-bar');
  bar.textContent = message;
  bar.classList.remove('hidden');
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => bar.classList.add('hidden'), 5000);
}

// a refused action explains itself instead of failing silently
const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await nativeFetch(...args);
  if (res.status === 403) {
    res.clone().json().then(d => showNotice(d.detail || 'Your account is not allowed to do that.')).catch(() => {});
  }
  return res;
};

function applyUser(user) {
  currentUser = user;
  const admin = user.role === 'admin';
  $('#whoami').classList.remove('hidden');
  $('#whoami-name').textContent = `${user.username}${admin ? '' : ' (' + user.role + ')'}`;
  $('#whoami-password').classList.toggle('hidden', admin);
  document.querySelector('#tabs button[data-tab="settings"]').classList.toggle('hidden', !admin);
  document.body.classList.toggle('read-only', user.role === 'viewer');
  document.body.classList.toggle('not-admin', !admin);
}

$('#whoami-logout').addEventListener('click', async () => {
  await fetch('/api/auth/logout', { method: 'POST' });
  location.reload();
});
$('#whoami-password').addEventListener('click', async () => {
  const current = prompt('Your current password:');
  if (current === null) return;
  const next = prompt('A new password (8 or more characters):');
  if (next === null) return;
  const res = await jsonRequest('POST', '/api/auth/me/password', { current_password: current, new_password: next });
  showNotice(res.ok ? 'Password changed.' : await sourceErrorText(res));
});

// ---------- People (Settings, administrator) ----------
async function loadUsers() {
  const res = await fetch('/api/users');
  if (!res.ok) return;
  const data = await res.json();
  $('#users-list').innerHTML = `
    <div class="user-row"><b>${esc(data.admin || '')}</b> <span class="status-badge status-done">administrator</span></div>
    ${data.users.map(u => `
      <div class="user-row" data-id="${u.id}">
        <b>${esc(u.username)}</b>
        <select class="user-role">${data.roles.map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}</select>
        <button class="user-reset">Set a new password</button>
        <button class="user-delete danger">Remove</button>
      </div>`).join('')}`;
}

$('#add-user-btn').addEventListener('click', async () => {
  const res = await jsonRequest('POST', '/api/users', {
    username: $('#new-user-name').value.trim(), password: $('#new-user-password').value, role: $('#new-user-role').value,
  });
  $('#users-status').textContent = res.ok ? 'Added.' : await sourceErrorText(res);
  if (res.ok) { $('#new-user-name').value = ''; $('#new-user-password').value = ''; loadUsers(); }
});
$('#users-list').addEventListener('change', async (e) => {
  if (!e.target.classList.contains('user-role')) return;
  const res = await jsonRequest('PATCH', `/api/users/${e.target.closest('.user-row').dataset.id}`, { role: e.target.value });
  $('#users-status').textContent = res.ok ? 'Saved.' : await sourceErrorText(res);
});
$('#users-list').addEventListener('click', async (e) => {
  const row = e.target.closest('.user-row');
  if (!row || !row.dataset.id) return;
  if (e.target.classList.contains('user-delete')) {
    if (!confirm('Remove this login? They are signed out at once.')) return;
    await fetch(`/api/users/${row.dataset.id}`, { method: 'DELETE' });
    loadUsers();
  } else if (e.target.classList.contains('user-reset')) {
    const password = prompt('A new password for this person (8 or more characters):');
    if (password === null) return;
    const res = await jsonRequest('PATCH', `/api/users/${row.dataset.id}`, { password });
    $('#users-status').textContent = res.ok ? 'Password changed.' : await sourceErrorText(res);
  }
});

// ---------- Auth gate ----------
async function boot() {
  const status = await (await fetch('/api/auth/status')).json();
  if (!status.configured) {
    showAuthOverlay('setup');
    return;
  }
  const who = await fetch('/api/auth/me');
  if (who.status === 401) {
    showAuthOverlay('login');
    return;
  }
  applyUser(await who.json());
  await loadViewerColor();
  route();
  refreshFollowBadge();
  refreshUpdateBadge();
}

async function loadViewerColor() {
  if (currentUser.role !== 'admin') return;           // the setting is part of Settings; others keep the default
  const res = await fetch('/api/settings');
  if (res.ok) viewerModelColor = normalizeViewerModelColor((await res.json()).viewer_model_color);
}

function showAuthOverlay(mode) {
  const overlay = $('#auth-overlay');
  overlay.classList.remove('hidden');
  $('#auth-title').textContent = mode === 'setup' ? 'Create admin account' : 'Sign in';
  $('#auth-submit-btn').textContent = mode === 'setup' ? 'Create account' : 'Sign in';

  $('#auth-submit-btn').onclick = async () => {
    const username = $('#auth-username').value.trim();
    const password = $('#auth-password').value;
    const endpoint = mode === 'setup' ? '/api/auth/setup' : '/api/auth/login';
    const res = await fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      $('#auth-status').textContent = data.detail || 'Failed.';
      return;
    }
    if (mode === 'setup') {
      // account created but not signed in yet -- log in immediately with the same creds
      await fetch('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
    }
    overlay.classList.add('hidden');
    const whoRes = await fetch('/api/auth/me');
    if (whoRes.ok) applyUser(await whoRes.json());
    await loadViewerColor();
    route();
    refreshFollowBadge();
  };
}

boot();

// installable as an app (phone home screen, desktop)
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => {}); });
}
