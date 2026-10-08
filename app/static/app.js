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
  library: () => loadModels(), search: () => openSearchPage(''), wishlist: () => loadWishlist(), duplicates: () => loadDuplicates(), following: () => loadFollowing(),
  collections: () => loadCollections(), projects: () => loadProjects(),
  supplies: () => loadSupplies(), matches: () => loadMatches(), filament: () => loadFilament(),
  queue: () => loadQueue(), settings: () => loadSettings(),
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
  const listing = hash.match(/^#\/listing\/([a-z0-9]+)\/([A-Za-z0-9_-]+)$/);
  if (listing) {
    showSection('page-listing', 'search');
    return openListingPage(listing[1], listing[2], token);
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
  const q = $('#search-box').value;
  const dupOnly = $('#dup-only').checked;
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (dupOnly) params.set('duplicates_only', 'true');
  if ($('#printed-filter').value) params.set('printed', $('#printed-filter').value);
  const res = await fetch(`/api/library/models?${params}`);
  const models = await res.json();
  renderGrid(models);
}

// one library card (used by the Library grid and the Search page)
function libraryCard(m) {
  const card = document.createElement('a');
  card.className = 'card' + (m.is_duplicate_of ? ' duplicate' : '');
  card.href = `#/model/${m.id}`;
  // models the renderer couldn't thumbnail (STEP, FBX...) borrow the first picture from their site listing
  const siteImages = parseJsonList(m.source_images);
  const thumb = m.thumbnail_path ? `/api/library/thumbnails/${m.thumbnail_path}`
    : (siteImages.length ? `/api/library/models/${m.id}/source/images/${siteImages[0]}` : '');
  card.innerHTML = `
    ${thumb ? `<img src="${thumb}" loading="lazy" alt="">` : `<div style="height:120px;display:flex;align-items:center;justify-content:center;color:#666">${esc(m.extension)}</div>`}
    <div class="meta">
      <div class="fname" title="${esc(m.filename)}">${esc(m.filename)}</div>
      <div class="tags">${esc((m.tags || []).map(t => t.name).join(', '))}</div>
      ${m.print_count ? `<div class="printed-badge" title="Last printed ${esc(String(m.last_printed_at || '').slice(0, 10))}">&#10003; printed${m.print_count > 1 ? ` x${m.print_count}` : ''}</div>` : ''}
    </div>`;
  return card;
}

function renderGrid(models) {
  const grid = $('#grid');
  grid.innerHTML = '';
  for (const m of models) grid.appendChild(libraryCard(m));
}

$('#search-box').addEventListener('input', debounce(loadModels, 300));
$('#dup-only').addEventListener('change', loadModels);
$('#printed-filter').addEventListener('change', loadModels);
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
}

// ---------- Print history on a model's page ----------
function ratingText(n) { return n ? '\u2605'.repeat(n) + '\u2606'.repeat(5 - n) : ''; }

async function renderModelPrints(model) {
  const panel = $('#model-prints-panel');
  const [logs, spools] = await Promise.all([
    fetch(`/api/prints?model_id=${model.id}`).then(r => (r.ok ? r.json() : { total: 0, items: [] })),
    fetch('/api/filament').then(r => (r.ok ? r.json() : [])),
  ]);
  if (!currentModel || currentModel.id !== model.id) return;
  const spoolText = f => [f.material, f.brand, f.color].filter(Boolean).join(' ');
  const spoolById = new Map(spools.map(f => [f.id, f]));
  const today = new Date().toISOString().slice(0, 10);
  panel.innerHTML = `
    <h3>Print history${logs.total ? ` (${logs.total})` : ''}</h3>
    ${logs.items.length ? logs.items.map(l => `
      <div class="print-row" data-id="${l.id}">
        <div class="print-main">
          <b>${esc(String(l.printed_at).slice(0, 10))}</b>
          ${l.rating ? `<span class="stars" title="${l.rating} of 5">${ratingText(l.rating)}</span>` : ''}
          ${l.grams != null ? `<span class="muted">${l.grams} g${l.filament_id && spoolById.get(l.filament_id) ? ' of ' + esc(spoolText(spoolById.get(l.filament_id))) : ''}</span>` : ''}
          ${l.minutes != null ? `<span class="muted">${Math.round(l.minutes)} min</span>` : ''}
          ${l.source === 'queue' ? '<span class="muted">from the print queue</span>' : ''}
          ${l.notes ? `<div>${esc(l.notes)}</div>` : ''}
        </div>
        ${l.has_photo ? `<a href="/api/prints/${l.id}/photo" target="_blank" rel="noopener"><img class="print-photo" src="/api/prints/${l.id}/photo?v=${Date.now()}" loading="lazy" alt="Photo of the print"></a>` : ''}
        <div class="print-actions">
          <label class="button-link print-photo-btn">${l.has_photo ? 'Replace photo' : 'Add photo'}<input type="file" accept="image/*" class="print-photo-input" hidden></label>
          <button class="print-delete">Delete</button>
        </div>
      </div>`).join('') : '<p class="muted">Not printed yet.</p>'}
    <h3>Log a print</h3>
    <div class="stack">
      <div class="row">
        <label>Date <input id="print-date" type="date" value="${today}"></label>
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
    if ($('#print-rating').value) body.rating = parseInt($('#print-rating').value);
    if ($('#print-spool').value) body.filament_id = parseInt($('#print-spool').value);
    if ($('#print-grams').value) body.grams = parseFloat($('#print-grams').value);
    if ($('#print-minutes').value) body.minutes = parseFloat($('#print-minutes').value);
    const res = await jsonRequest('POST', '/api/prints', body);
    if (res.ok) refreshModelPage(); else $('#print-status').textContent = await sourceErrorText(res);
  };
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
  if (renderer) { renderer.dispose(); renderer.forceContextLoss(); }
  renderer = scene = camera = controls = null;
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
  camera.position.set(radius * 2, radius * 2, radius * 3);
  camera.near = radius / 100;
  camera.far = radius * 100;
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
}

// ---------- Match a model to its Printables / MakerWorld listing ----------
const PROVIDER_LABELS = {
  printables: 'Printables', makerworld: 'MakerWorld', sketchfab: 'Sketchfab', thingiverse: 'Thingiverse',
  myminifactory: 'MyMiniFactory', cults3d: 'Cults3D', commons: 'Wikimedia Commons', nasa3d: 'NASA 3D Resources',
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
  $('#viewer-estimate-result').textContent = `${grams} · ${mins} (${est.source})`;
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
      : '<p class="muted">Nothing to buy -- every project has the parts it needs.</p>'}`;
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
  $('#collections-list').innerHTML = cols.map(c => `<li>${c.name} <button data-id="${c.id}" class="del-col">delete</button></li>`).join('');
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
async function loadFilament() {
  const items = await (await fetch('/api/filament')).json();
  $('#filament-table tbody').innerHTML = items.map(f => `
    <tr>
      <td>${f.material}</td><td>${f.brand || ''}</td><td>${f.color || ''}</td>
      <td>${f.remaining_g}g / ${f.spool_weight_g}g</td>
      <td><input class="fil-price" data-id="${f.id}" type="number" min="0" step="0.01" value="${f.cost ?? ''}" placeholder="price" aria-label="Spool price">
        ${f.cost != null && f.spool_weight_g ? `<small class="muted">$${(f.cost / f.spool_weight_g * 1000).toFixed(2)}/kg</small>` : ''}</td>
      <td><button data-id="${f.id}" class="del-fil">delete</button></td>
    </tr>`).join('');
  $$('.fil-price').forEach(input => input.onchange = async () => {
    const value = input.value.trim();
    await jsonRequest('PATCH', `/api/filament/${input.dataset.id}`, { cost: value === '' ? null : parseFloat(value) });
    loadFilament();
  });
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

// ---------- Queue ----------
async function loadQueue() {
  const [items, models] = await Promise.all([
    (await fetch('/api/queue')).json(),
    (await fetch('/api/library/models?limit=1000')).json(),
  ]);
  const modelById = Object.fromEntries(models.map(m => [m.id, m]));
  $('#queue-list').innerHTML = items.map(i => {
    const m = modelById[i.model_id];
    const est = [i.estimated_grams != null ? `${i.estimated_grams}g` : null,
                 i.estimated_minutes != null ? `${Math.round(i.estimated_minutes)}min` : null]
      .filter(Boolean).join(' · ');
    return `
    <li>
      <span>#${i.position} ${m ? m.filename : 'model ' + i.model_id}${est ? ' — ' + est : ''}</span>
      <span>
        <select data-id="${i.id}" class="queue-status">
          ${['queued', 'printing', 'done', 'failed'].map(s => `<option value="${s}" ${s === i.status ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        <button data-id="${i.id}" class="del-queue">remove</button>
      </span>
    </li>`;
  }).join('');
  $$('.del-queue').forEach(b => b.onclick = async () => { await fetch(`/api/queue/${b.dataset.id}`, { method: 'DELETE' }); loadQueue(); });
  $$('.queue-status').forEach(sel => sel.onchange = async () => {
    await fetch(`/api/queue/${sel.dataset.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: sel.value }),
    });
    loadQueue();
    loadFilament();
  });
}

// ---------- Settings ----------
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

async function loadSettings() {
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
  $('#notify-webhook-url').value = s.notify_webhook_url || '';
  await renderSiteSettings(s);
  refreshBackups();
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

// ---------- Auth gate ----------
async function boot() {
  const status = await (await fetch('/api/auth/status')).json();
  if (!status.configured) {
    showAuthOverlay('setup');
    return;
  }
  const probe = await fetch('/api/settings');
  if (probe.status === 401) {
    showAuthOverlay('login');
    return;
  }
  const settings = await probe.json();
  viewerModelColor = normalizeViewerModelColor(settings.viewer_model_color);
  route();
  refreshFollowBadge();
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
    const settingsRes = await fetch('/api/settings');
    if (settingsRes.ok) {
      const settings = await settingsRes.json();
      viewerModelColor = normalizeViewerModelColor(settings.viewer_model_color);
    }
    route();
    refreshFollowBadge();
  };
}

boot();
