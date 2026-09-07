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
  if (loadFn) {
    loadFn(fileUrl);
  } else {
    $('#viewer-info').insertAdjacentHTML('beforeend',
      `<div style="color:#e0a800">Live viewer not available for ${model.extension} yet -- see the thumbnail and download the original file above.</div>`);
  }

  loadFilamentOptionsForQueue();
  $('#viewer-estimate-btn').onclick = () => runEstimate(model.id);
  $('#viewer-add-queue-btn').onclick = () => addToQueue(model.id);
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
