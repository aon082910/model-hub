(() => {
  const PAGE_SIZE = 200;
  let offset = 0;
  let total = 0;
  let suppressReset = false;

  function ensurePager() {
    let pager = document.querySelector('#library-pagination');
    if (pager) return pager;

    pager = document.createElement('div');
    pager.id = 'library-pagination';
    pager.className = 'pagination';
    pager.innerHTML = `
      <button id="library-prev" type="button">Previous</button>
      <span id="library-page-info">0 models</span>
      <label class="pagination-jump" for="library-page-jump">
        Page
        <input id="library-page-jump" type="number" min="1" value="1" inputmode="numeric" aria-label="Page number">
        <span id="library-page-count">of 1</span>
      </label>
      <button id="library-page-go" type="button">Go</button>
      <button id="library-next" type="button">Next</button>`;

    const grid = document.querySelector('#grid');
    if (grid) grid.insertAdjacentElement('afterend', pager);
    return pager;
  }

  function updatePager() {
    ensurePager();
    const prev = document.querySelector('#library-prev');
    const next = document.querySelector('#library-next');
    const info = document.querySelector('#library-page-info');
    const jump = document.querySelector('#library-page-jump');
    const pageCountLabel = document.querySelector('#library-page-count');
    const go = document.querySelector('#library-page-go');
    if (!prev || !next || !info || !jump || !pageCountLabel || !go) return;

    const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const page = total === 0 ? 1 : Math.floor(offset / PAGE_SIZE) + 1;
    const first = total === 0 ? 0 : offset + 1;
    const last = Math.min(offset + PAGE_SIZE, total);

    info.textContent = total === 0
      ? '0 models'
      : `${first}-${last} of ${total}`;
    prev.disabled = offset <= 0;
    next.disabled = offset + PAGE_SIZE >= total;

    jump.max = String(pageCount);
    jump.value = String(page);
    jump.disabled = total === 0;
    pageCountLabel.textContent = `of ${pageCount}`;
    go.disabled = total === 0;
  }

  function requestLibraryReload() {
    const search = document.querySelector('#search-box');
    if (!search) return;
    suppressReset = true;
    search.dispatchEvent(new Event('input', { bubbles: true }));
    suppressReset = false;
  }

  function jumpToPage() {
    const jump = document.querySelector('#library-page-jump');
    if (!jump || total === 0) return;

    const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const requested = Number.parseInt(jump.value, 10);
    const page = Number.isFinite(requested)
      ? Math.min(pageCount, Math.max(1, requested))
      : 1;

    jump.value = String(page);
    const newOffset = (page - 1) * PAGE_SIZE;
    if (newOffset === offset) return;

    offset = newOffset;
    requestLibraryReload();
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const rawUrl = typeof input === 'string' ? input : input.url;
    let url;
    try {
      url = new URL(rawUrl, window.location.origin);
    } catch (_) {
      return nativeFetch(input, init);
    }

    const isModelList = url.pathname === '/api/library/models';
    const isMainLibraryRequest = isModelList && !url.searchParams.has('limit') && !url.searchParams.has('offset');

    if (isMainLibraryRequest) {
      url.searchParams.set('limit', String(PAGE_SIZE));
      url.searchParams.set('offset', String(offset));
      input = url.pathname + url.search;
    }

    const response = await nativeFetch(input, init);

    if (isMainLibraryRequest && response.ok) {
      total = Number.parseInt(response.headers.get('X-Total-Count') || '0', 10) || 0;
      if (total > 0 && offset >= total) {
        offset = Math.floor((total - 1) / PAGE_SIZE) * PAGE_SIZE;
        requestLibraryReload();
      } else {
        updatePager();
      }
    }

    return response;
  };

  // This script is loaded at the end of <body>, immediately before app.js, so
  // the Library DOM already exists and these handlers register first.
  ensurePager();
  updatePager();

  const search = document.querySelector('#search-box');
  const dupOnly = document.querySelector('#dup-only');
  const scanButton = document.querySelector('#scan-btn');

  search?.addEventListener('input', () => {
    if (!suppressReset) offset = 0;
  });
  dupOnly?.addEventListener('change', () => {
    offset = 0;
  });

  document.querySelector('#library-prev')?.addEventListener('click', () => {
    if (offset <= 0) return;
    offset = Math.max(0, offset - PAGE_SIZE);
    requestLibraryReload();
  });

  document.querySelector('#library-next')?.addEventListener('click', () => {
    if (offset + PAGE_SIZE >= total) return;
    offset += PAGE_SIZE;
    requestLibraryReload();
  });

  document.querySelector('#library-page-go')?.addEventListener('click', jumpToPage);
  document.querySelector('#library-page-jump')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      jumpToPage();
    }
  });

  // Register before app.js so a scan collision can be handled cleanly instead
  // of letting the legacy handler display undefined counters for a 409 response.
  scanButton?.addEventListener('click', async (event) => {
    event.stopImmediatePropagation();
    scanButton.textContent = 'Scanning...';
    scanButton.disabled = true;
    try {
      const response = await nativeFetch('/api/library/scan', { method: 'POST' });
      const result = await response.json().catch(() => ({}));
      if (response.status === 409) {
        alert(result.detail || 'A library scan is already in progress.');
        return;
      }
      if (!response.ok) {
        alert(result.detail || 'Library scan failed.');
        return;
      }
      alert(`Scan complete: ${result.found} found, ${result.added} added, ${result.updated} updated, ${result.duplicates} duplicates.`);
      offset = 0;
      requestLibraryReload();
    } finally {
      scanButton.textContent = 'Rescan Library';
      scanButton.disabled = false;
    }
  });
})();
