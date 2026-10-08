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
  library: () => loadModels(), collections: () => loadCollections(), projects: () => loadProjects(),
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
  const res = await fetch(`/api/library/models?${params}`);
  const models = await res.json();
  renderGrid(models);
}

function renderGrid(models) {
  const grid = $('#grid');
  grid.innerHTML = '';
  for (const m of models) {
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
      </div>`;
    grid.appendChild(card);
  }
}

$('#search-box').addEventListener('input', debounce(loadModels, 300));
$('#dup-only').addEventListener('change', loadModels);
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
const PROVIDER_LABELS = { printables: 'Printables', makerworld: 'MakerWorld', sketchfab: 'Sketchfab', thingiverse: 'Thingiverse' };

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

async function loadMatches() {
  await refreshMatchStatus();
  await loadMatchQueue();
  await loadAutoLinked();
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
      <td><button data-id="${f.id}" class="del-fil">delete</button></td>
    </tr>`).join('');
  $$('.del-fil').forEach(b => b.onclick = async () => { await fetch(`/api/filament/${b.dataset.id}`, { method: 'DELETE' }); loadFilament(); });
}
$('#add-filament-btn').addEventListener('click', async () => {
  await fetch('/api/filament', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      material: $('#fil-material').value, brand: $('#fil-brand').value,
      color: $('#fil-color').value, spool_weight_g: parseFloat($('#fil-weight').value) || 1000,
      remaining_g: parseFloat($('#fil-weight').value) || 1000,
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
  $('#notify-webhook-url').value = s.notify_webhook_url || '';
  $('#thingiverse-token').value = s.thingiverse_token || '';
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
    }),
  });
});

$('#save-notify-btn').addEventListener('click', async () => {
  await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ notify_webhook_url: $('#notify-webhook-url').value }),
  });
});

$('#save-thingiverse-btn').addEventListener('click', async () => {
  const res = await jsonRequest('PUT', '/api/settings', { thingiverse_token: $('#thingiverse-token').value.trim() });
  $('#thingiverse-status').textContent = res.ok ? 'Saved.' : 'Could not save.';
  setTimeout(() => { $('#thingiverse-status').textContent = ''; }, 2500);
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
  };
}

boot();
