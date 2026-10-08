(function () {
  // Model files (and zips, which Model Hub opens to keep just the models inside).
  const FILE_EXT_RE = /\.(stl|3mf|step|stp|obj|fbx|zip)(\?|#|$)/i;

  // What each supported site's model pages and download links look like.
  const SITES = [
    { name: 'Printables', host: /(^|\.)printables\.com$/i, model: /\/model\/\d+/i },
    { name: 'MakerWorld', host: /(^|\.)makerworld\.com$/i, model: /\/models\/\d+/i },
    { name: 'Thingiverse', host: /(^|\.)thingiverse\.com$/i, model: /\/thing:\d+/i, download: /\/download:\d+/i },
    { name: 'MyMiniFactory', host: /(^|\.)myminifactory\.com$/i, model: /\/object\/[^/?#]*\d+/i, download: /\/(download|files?)\//i },
    { name: 'Cults3D', host: /(^|\.)cults3d\.com$/i, model: /\/3d-model\//i, download: /\/(download|files?)\b/i },
    { name: 'Sketchfab', host: /(^|\.)sketchfab\.com$/i, model: /\/3d-models\//i },
  ];

  const site = SITES.find((s) => s.host.test(location.hostname));

  function isModelPage() {
    return !!site && site.model.test(location.pathname);
  }

  // JSON-LD is a standardized way sites embed structured metadata (schema.org).
  // Reading it is far more resilient to markup/redesign changes than CSS selectors.
  function readJsonLd() {
    const out = { name: null, author: null, license: null };
    document.querySelectorAll('script[type="application/ld+json"]').forEach((script) => {
      let data;
      try { data = JSON.parse(script.textContent); } catch (e) { return; }
      const items = Array.isArray(data) ? data : [data];
      for (const item of items) {
        if (!item || typeof item !== 'object') continue;
        if (item.name && !out.name) out.name = item.name;
        if (item.author) {
          out.author = typeof item.author === 'string' ? item.author : (item.author.name || out.author);
        }
        if (item.creator) {
          out.author = out.author || (typeof item.creator === 'string' ? item.creator : item.creator.name);
        }
        if (item.license && !out.license) out.license = item.license;
      }
    });
    return out;
  }

  function readMeta(name) {
    const el = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
    return el ? el.content : null;
  }

  function fileNameFromUrl(url) {
    try {
      return decodeURIComponent(new URL(url).pathname.split('/').pop() || '') || '';
    } catch (e) {
      return '';
    }
  }

  // Links on the page that look like model files or a site's download buttons.
  // The name is what the page calls the link (or the file name from the address);
  // when it is only "Download", the background worker takes the real name from the
  // download's own headers.
  function findFileLinks() {
    const found = new Map();
    document.querySelectorAll('a[href]').forEach((a) => {
      const href = a.href;
      if (!/^https?:/i.test(href)) return;
      const looksLikeFile = FILE_EXT_RE.test(href) || (site && site.download && site.download.test(href));
      if (!looksLikeFile || found.has(href)) return;
      const label = (a.textContent || '').trim().replace(/\s+/g, ' ');
      const fromUrl = fileNameFromUrl(href);
      const name = FILE_EXT_RE.test(fromUrl) ? fromUrl : (/\.[a-z0-9]{2,4}$/i.test(label) ? label : '');
      found.set(href, { url: href, name, label: name || label || fromUrl || 'download' });
    });
    return Array.from(found.values());
  }

  function collectMetadata() {
    const ld = readJsonLd();
    return {
      title: ld.name || readMeta('og:title') || document.title,
      designer: ld.author || readMeta('author') || '',
      license: ld.license || '',
      sourceUrl: location.href,
      files: findFileLinks(),
    };
  }

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    for (const child of children) node.append(child);
    return node;
  }

  function buildButton() {
    const btn = el('button', { id: 'modelhub-import-btn', textContent: '📦 Send to Model Hub' });
    btn.addEventListener('click', openPanel);
    document.body.appendChild(btn);
  }

  function openPanel() {
    const existing = document.getElementById('modelhub-panel');
    if (existing) { existing.remove(); return; }

    const meta = collectMetadata();
    const panel = el('div', { id: 'modelhub-panel' });
    panel.append(el('h3', { textContent: `Send to Model Hub${site ? ` (${site.name})` : ''}` }));

    const filesBox = el('div', { id: 'modelhub-files' });
    if (meta.files.length === 0) {
      panel.append(el('p', {
        className: 'modelhub-warn',
        textContent: 'No direct model file links found on this page. Some sites only reveal them after you click their '
          + 'Download button (and some require you to be logged in). Open the page that lists the files, then use this button there.',
      }));
    } else {
      panel.append(el('label', { textContent: 'Files' }));
      meta.files.forEach((file, i) => {
        const row = el('label', { className: 'modelhub-file-row' });
        const box = el('input', { type: 'checkbox', checked: true });
        box.dataset.index = String(i);
        row.append(box, el('span', { textContent: file.label }));
        filesBox.append(row);
      });
      panel.append(filesBox);
    }

    const designer = el('input', { id: 'modelhub-designer', type: 'text', value: meta.designer || '' });
    const license = el('input', { id: 'modelhub-license', type: 'text', value: meta.license || '' });
    const status = el('div', { id: 'modelhub-status' });
    const cancel = el('button', { id: 'modelhub-cancel', textContent: 'Cancel' });
    const send = el('button', { id: 'modelhub-send', textContent: 'Import', disabled: meta.files.length === 0 });
    panel.append(
      el('label', { textContent: 'Designer' }), designer,
      el('label', { textContent: 'License' }), license,
      status,
      el('div', { className: 'modelhub-actions' }, cancel, send),
    );
    document.body.appendChild(panel);

    cancel.addEventListener('click', () => panel.remove());
    send.addEventListener('click', () => sendImport(panel, meta, status, designer.value, license.value));
  }

  function sendImport(panel, meta, status, designer, license) {
    const chosen = Array.from(panel.querySelectorAll('#modelhub-files input:checked'))
      .map((box) => meta.files[parseInt(box.dataset.index, 10)]);
    if (chosen.length === 0) {
      status.textContent = 'Select at least one file.';
      status.className = 'modelhub-err';
      return;
    }

    status.textContent = `Importing ${chosen.length} file(s)…`;
    status.className = '';

    chrome.runtime.sendMessage(
      {
        type: 'modelhub-import',
        files: chosen.map((f) => ({ url: f.url, name: f.name })),
        sourceUrl: meta.sourceUrl, designer, license,
      },
      (response) => {
        if (!response) {
          status.textContent = 'No response from extension background worker.';
          status.className = 'modelhub-err';
          return;
        }
        if (response.ok) {
          status.textContent = `Imported ${response.imported}/${chosen.length}.` +
            (response.errors.length ? ` Problems: ${response.errors.join('; ')}` : '');
          status.className = response.errors.length ? 'modelhub-err' : 'modelhub-ok';
        } else {
          status.textContent = response.error || 'Import failed.';
          status.className = 'modelhub-err';
        }
      }
    );
  }

  if (isModelPage()) {
    buildButton();
  }
})();
