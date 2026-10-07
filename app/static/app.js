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

// ---------- Tabs ----------
$$('#tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('#tabs button').forEach(b => b.classList.remove('active'));
    $$('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    $(`#tab-${btn.dataset.tab}`).classList.add('active');
    if (btn.dataset.tab === 'collections') loadCollections();
    if (btn.dataset.tab === 'projects') loadProjects();
    if (btn.dataset.tab === 'filament') loadFilament();
    if (btn.dataset.tab === 'queue') loadQueue();
    if (btn.dataset.tab === 'settings') loadSettings();
  });
});

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
    const card = document.createElement('div');
    card.className = 'card' + (m.is_duplicate_of ? ' duplicate' : '');
    const thumb = m.thumbnail_path ? `/api/library/thumbnails/${m.thumbnail_path}` : '';
    card.innerHTML = `
      ${thumb ? `<img src="${thumb}" loading="lazy">` : `<div style="height:120px;display:flex;align-items:center;justify-content:center;color:#666">${m.extension}</div>`}
      <div class="meta">
        <div class="fname" title="${m.filename}">${m.filename}</div>
        <div class="tags">${(m.tags || []).map(t => t.name).join(', ')}</div>
      </div>`;
    card.addEventListener('click', () => openViewer(m));
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

function openViewer(model) {
  viewerSessionColor = viewerModelColor;
  $('#viewer-modal').classList.remove('hidden');
  $('#viewer-info').innerHTML = `
    <div><b>${model.filename}</b></div>
    <div>${model.ai_description || ''}</div>
    <div>Tags: ${(model.tags || []).map(t => t.name).join(', ') || '(none)'}</div>
    <div>Vertices: ${model.vertex_count ?? '?'} | Faces: ${model.face_count ?? '?'}</div>
    <div><a href="/api/library/models/${model.id}/file" download>Download original file</a></div>
    <div class="row" style="margin-top:8px">
      <button id="viewer-tag-btn">Tag with AI</button>
      <input id="viewer-designer" placeholder="Designer" value="${model.designer || ''}">
      <input id="viewer-license" placeholder="License" value="${model.license || ''}">
      <button id="viewer-save-meta-btn">Save</button>
    </div>`;

  $('#viewer-tag-btn').onclick = async () => {
    const res = await fetch(`/api/ai/tag/${model.id}`, { method: 'POST' });
    const result = await res.json();
    alert(result.status === 'ok' ? `Tagged: ${result.tags.join(', ')}` : (result.reason || 'skipped'));
  };
  $('#viewer-save-meta-btn').onclick = async () => {
    await fetch(`/api/library/models/${model.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        designer: $('#viewer-designer').value,
        license: $('#viewer-license').value,
      }),
    });
  };

  initViewer();
  ensureViewerColorControl();
  const fileUrl = `/api/library/models/${model.id}/file`;
  const loaders = {
    '.stl': loadSTL,
    '.obj': loadOBJ,
    '.3mf': load3MF,
    '.fbx': loadFBX,
    '.step': loadSTEP,
    '.stp': loadSTEP,
  };
  const loadFn = loaders[model.extension];
  if (loadFn && previewIsTooLarge(model)) {
    const faces = model.face_count ? `${(model.face_count / 1e6).toFixed(1)}M faces` : `${Math.round(model.size_bytes / 2 ** 20)} MB`;
    $('#viewer-info').insertAdjacentHTML('beforeend',
      `<div id="large-preview-note" style="color:#e0a800">This model is very large (${faces}) and may freeze or crash this browser tab to preview live. `
      + `<button id="large-preview-load-btn">Load anyway</button></div>`);
    $('#large-preview-load-btn').onclick = () => {
      $('#large-preview-note').remove();
      loadFn(fileUrl);
    };
  } else if (loadFn) {
    loadFn(fileUrl);
  } else {
    $('#viewer-info').insertAdjacentHTML('beforeend',
      `<div style="color:#e0a800">Live viewer not available for ${model.extension} yet -- see the thumbnail and download the original file above.</div>`);
  }

  loadFilamentOptionsForQueue();
  loadProjectOptionsForViewer();
  $('#viewer-project-result').textContent = '';
  $('#viewer-estimate-btn').onclick = () => runEstimate(model.id);
  $('#viewer-add-queue-btn').onclick = () => addToQueue(model.id);
  $('#viewer-add-project-btn').onclick = () => addModelToProject(model.id);
}

$('#close-viewer').addEventListener('click', () => {
  $('#viewer-modal').classList.add('hidden');
  if (animId) cancelAnimationFrame(animId);
  if (viewerResizeHandler) {
    window.removeEventListener('resize', viewerResizeHandler);
    viewerResizeHandler = null;
  }
});

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

function loadSTL(url) {
  const loader = new STLLoader();
  loader.load(url, (geometry) => {
    geometry.center();
    const mesh = new THREE.Mesh(geometry, createViewerMaterial());
    scene.add(mesh);
    frameCameraOn(mesh);
  });
}

function loadOBJ(url) {
  const loader = new OBJLoader();
  loader.load(url, (object) => {
    object.traverse((child) => {
      if (child.isMesh) child.material = createViewerMaterial();
    });
    scene.add(object);
    frameCameraOn(object);
  }, undefined, (err) => showViewerError(err));
}

function load3MF(url) {
  const loader = new ThreeMFLoader();
  loader.load(url, (object) => {
    scene.add(object);
    frameCameraOn(object);
  }, undefined, (err) => showViewerError(err));
}

function loadFBX(url) {
  const loader = new FBXLoader();
  loader.load(url, (object) => {
    object.traverse((child) => {
      if (child.isMesh) child.material = createViewerMaterial();
    });
    scene.add(object);
    frameCameraOn(object);
  }, undefined, (err) => showViewerError(err));
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
  $('#viewer-info').insertAdjacentHTML('beforeend',
    '<div id="step-loading-note">Loading STEP geometry (this can take a few seconds)…</div>');
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Could not fetch file (${res.status})`);
    const fileBuffer = new Uint8Array(await res.arrayBuffer());
    const occt = await getOcctModule();
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
    scene.add(group);
    frameCameraOn(group);
  } catch (err) {
    showViewerError(err);
  } finally {
    document.getElementById('step-loading-note')?.remove();
  }
}

function showViewerError(err) {
  console.error(err);
  $('#viewer-info').insertAdjacentHTML('beforeend',
    `<div style="color:#e0645a">Failed to load model preview: ${err.message || err}</div>`);
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

// ---------- Print estimate + add-to-queue (from viewer) ----------
let lastEstimate = null;

async function runEstimate(modelId) {
  const material = $('#viewer-est-material').value;
  const infillPct = parseFloat($('#viewer-est-infill').value);
  const infill = isNaN(infillPct) ? 0.15 : infillPct / 100;
  $('#viewer-estimate-result').textContent = 'Estimating…';
  const res = await fetch(`/api/library/models/${modelId}/estimate?material=${material}&infill=${infill}`);
  const est = await res.json();
  lastEstimate = est;
  if (est.estimated_grams == null && est.estimated_minutes == null) {
    $('#viewer-estimate-result').textContent = est.note || 'Estimate unavailable.';
    return;
  }
  const grams = est.estimated_grams != null ? `${est.estimated_grams} g` : '? g';
  const mins = est.estimated_minutes != null ? `${Math.round(est.estimated_minutes)} min` : '? min';
  $('#viewer-estimate-result').textContent = `${grams} · ${mins} (${est.source})`;
}

async function loadFilamentOptionsForQueue() {
  const items = await (await fetch('/api/filament')).json();
  const sel = $('#viewer-queue-filament');
  sel.innerHTML = '<option value="">(no filament selected)</option>' +
    items.map(f => `<option value="${f.id}">${f.material} ${f.color || ''} (${f.remaining_g}g left)</option>`).join('');
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
  $('#viewer-estimate-result').textContent = 'Added to print queue.';
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

async function loadProjectOptionsForViewer() {
  const projects = await (await fetch('/api/projects')).json();
  $('#viewer-project').innerHTML = '<option value="">(choose a project)</option>' +
    projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
}

async function addModelToProject(modelId) {
  const projectId = $('#viewer-project').value;
  if (!projectId) { $('#viewer-project-result').textContent = 'Choose a project first.'; return; }
  const res = await fetch(`/api/projects/${projectId}/models/${modelId}`, { method: 'POST' });
  $('#viewer-project-result').textContent = res.ok ? 'Added to project.' : 'Could not add to project.';
}

function renderPartRow(projectId, part) {
  const url = safeUrl(part.purchase_url);
  const cost = part.unit_cost != null ? `$${part.unit_cost.toFixed(2)}` : '';
  return `
    <tr class="${part.quantity_needed ? '' : 'part-complete'}" data-project="${projectId}" data-part="${part.id}">
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
  return `
    <div class="project-model" data-model="${m.id}">
      <div class="project-model-head">
        <b>${esc(m.filename)}</b>
        ${m.filament_grams ? `<span class="muted">${m.filament_grams}g</span>` : ''}
        ${locked ? '' : `<button class="model-estimate" title="Estimate filament use for this model">Estimate</button>
        <button class="model-unlink" title="Remove from project">&times;</button>`}
        <span class="model-estimate-note muted"></span>
      </div>
      ${lines}
      ${locked ? '' : `
      <div class="filament-line filament-add">
        <select class="new-line-spool">${projectSpools.length ? spoolOptions(null) : '<option value="">(add filament in the Filament tab first)</option>'}</select>
        <input class="new-line-grams" type="number" min="0" step="0.1" placeholder="grams" aria-label="Grams">
        <button class="line-add" ${projectSpools.length ? '' : 'disabled'}>Add filament</button>
      </div>`}
    </div>`;
}

function renderProject(p) {
  const cost = p.cost_needed ? ` · $${p.cost_needed.toFixed(2)} to buy` : '';
  const summary = p.parts_total
    ? (p.parts_missing ? `${p.parts_missing} of ${p.parts_total} parts needed${cost}` : 'all parts on hand')
    : 'no parts listed';
  const locked = p.filament_deducted;
  const models = p.models.map(m => renderProjectModel(m, locked)).join('');
  const totals = p.filament_totals.map(t => `
    <span class="${t.short ? 'filament-short' : ''}">${esc(t.filament_label)}: ${t.grams}g${t.remaining_g != null ? ` (spool has ${t.remaining_g}g)` : ''}${t.short ? ' &mdash; not enough!' : ''}</span>`).join(' · ');
  const filamentSummary = p.filament_totals.length
    ? `<div class="project-summary">Filament: <b>${p.filament_grams}g</b> &mdash; ${totals}
        ${locked ? '<br><small>Already subtracted from inventory. Move the project back to planning/building to undo.</small>' : ''}</div>`
    : '';
  return `
    <div class="project-card" data-project="${p.id}">
      <div class="project-head">
        <h3>${esc(p.name)}</h3>
        <select class="project-status">
          ${['planning', 'building', 'printed', 'done'].map(s => `<option value="${s}" ${s === p.status ? 'selected' : ''}>${s}</option>`).join('')}
        </select>
        <button class="project-del">delete project</button>
      </div>
      ${p.description ? `<div class="project-desc">${esc(p.description)}</div>` : ''}
      <div class="project-summary">${summary}</div>
      ${filamentSummary}
      ${models ? `<div class="project-models">${models}</div>` : ''}
      ${p.parts.length ? `
      <div class="table-scroll">
        <table class="parts-table"><thead><tr>
          <th>Part</th><th>Type</th><th>Need</th><th>Own</th><th>To get</th><th>Unit cost</th><th></th><th></th>
        </tr></thead><tbody>${p.parts.map(part => renderPartRow(p.id, part)).join('')}</tbody></table>
      </div>` : ''}
      <div class="row part-add">
        <input class="new-part-name" placeholder="Part name (e.g. ESP32, M3x8 screws, solder)">
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
    </div>`;
}

async function loadProjects() {
  const onlyNeeding = $('#projects-needing-only').checked;
  const [res, spools] = await Promise.all([
    fetch(`/api/projects?only_needing_parts=${onlyNeeding}`),
    fetch('/api/filament').then(r => r.json()),
  ]);
  projectSpools = spools;
  const projects = await res.json();
  $('#projects-list').innerHTML = projects.length
    ? projects.map(renderProject).join('')
    : `<p class="muted">${onlyNeeding ? 'No projects need parts right now.' : 'No projects yet. Add one above.'}</p>`;
  if (!$('#shopping-list').classList.contains('hidden')) loadShoppingList();
}

async function loadShoppingList() {
  const data = await (await fetch('/api/projects/shopping-list')).json();
  const box = $('#shopping-list');
  box.classList.remove('hidden');
  const rows = data.items.map(i => `
    <tr>
      <td>${esc(i.name)}</td><td>${esc(i.category)}</td><td>${i.quantity_needed}</td>
      <td>${esc(i.project_name)}</td>
      <td>${i.cost_needed != null ? `$${i.cost_needed.toFixed(2)}` : ''}</td>
      <td>${safeUrl(i.purchase_url) ? `<a href="${esc(safeUrl(i.purchase_url))}" target="_blank" rel="noopener noreferrer">buy</a>` : ''}</td>
    </tr>`).join('');
  box.innerHTML = `
    <h3>Shopping List <small>(parts still needed for projects that aren't done)</small></h3>
    ${data.items.length ? `
    <div class="table-scroll"><table class="parts-table"><thead><tr>
      <th>Part</th><th>Type</th><th>Qty</th><th>Project</th><th>Cost</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table></div>
    <div class="project-summary">Estimated total: $${data.total_cost.toFixed(2)}</div>`
      : '<p class="muted">Nothing to buy -- every project has the parts it needs.</p>'}`;
}

$('#add-project-btn').addEventListener('click', async () => {
  const name = $('#new-project-name').value.trim();
  if (!name) return;
  await jsonRequest('POST', '/api/projects', { name, description: $('#new-project-description').value });
  $('#new-project-name').value = '';
  $('#new-project-description').value = '';
  loadProjects();
});
$('#projects-needing-only').addEventListener('change', loadProjects);
$('#shopping-list-btn').addEventListener('click', () => {
  const box = $('#shopping-list');
  if (box.classList.contains('hidden')) loadShoppingList();
  else box.classList.add('hidden');
});

$('#projects-list').addEventListener('click', async (e) => {
  const card = e.target.closest('.project-card');
  if (!card) return;
  const projectId = card.dataset.project;
  const target = e.target;

  if (target.classList.contains('project-del')) {
    if (!confirm('Delete this project and its parts list?')) return;
    await fetch(`/api/projects/${projectId}`, { method: 'DELETE' });
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
  loadProjects();
});

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
    loadProjects();
  } else {
    box.querySelector('.new-line-grams').value = est.estimated_grams;
    note.textContent = `~${est.estimated_grams}g (${est.source}) -- pick a spool and click Add filament.`;
  }
}

$('#projects-list').addEventListener('change', async (e) => {
  const card = e.target.closest('.project-card');
  if (!card) return;
  const projectId = card.dataset.project;
  if (e.target.classList.contains('project-status')) {
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}`, { status: e.target.value });
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || 'Could not change status.');
    loadProjects();
  } else if (e.target.classList.contains('line-grams') || e.target.classList.contains('line-spool')) {
    const row = e.target.closest('.filament-line');
    const body = e.target.classList.contains('line-grams')
      ? { grams: e.target.value || 0 } : { filament_id: e.target.value };
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}/filament/${row.dataset.line}`, body);
    if (!res.ok) alert((await res.json().catch(() => ({}))).detail || 'Could not update filament.');
    loadProjects();
  } else if (e.target.classList.contains('part-owned')) {
    const res = await jsonRequest('PATCH', `/api/projects/${projectId}/parts/${e.target.closest('tr').dataset.part}`,
      { quantity_owned: e.target.value });
    if (!res.ok) alert('Quantity owned must be a whole number, 0 or more.');
    loadProjects();
  }
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
  loadModels();
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
    loadModels();
  };
}

boot();
