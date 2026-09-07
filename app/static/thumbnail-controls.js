(() => {
  const button = document.querySelector('#regenerate-thumbnails-btn');
  const status = document.querySelector('#thumbnail-regeneration-status');
  if (!button || !status) return;

  let pollHandle = null;
  let wasRunning = false;

  function refreshVisibleThumbnails() {
    const stamp = Date.now();
    document.querySelectorAll('img[src^="/api/library/thumbnails/"]').forEach((img) => {
      const url = new URL(img.src, window.location.origin);
      url.searchParams.set('v', String(stamp));
      img.src = url.pathname + url.search;
    });
  }

  function renderState(state) {
    if (state.running) {
      wasRunning = true;
      button.disabled = true;
      button.textContent = 'Regenerating Thumbnails...';
      const total = state.total || 0;
      status.textContent = total > 0
        ? `${state.done || 0} / ${total} complete${state.failed ? ` · ${state.failed} failed` : ''}`
        : 'Preparing thumbnail regeneration...';
      return;
    }

    button.disabled = false;
    button.textContent = 'Regenerate Thumbnails';
    if ((state.done || 0) > 0 || (state.failed || 0) > 0) {
      status.textContent = `Complete: ${state.regenerated || 0} regenerated${state.failed ? ` · ${state.failed} failed` : ''}`;
    }
    if (wasRunning) {
      wasRunning = false;
      refreshVisibleThumbnails();
    }
  }

  async function fetchStatus() {
    const response = await fetch('/api/library/thumbnails/regenerate/status');
    if (!response.ok) return null;
    return response.json();
  }

  function startPolling() {
    if (pollHandle) return;
    pollHandle = setInterval(async () => {
      try {
        const state = await fetchStatus();
        if (!state) return;
        renderState(state);
        if (!state.running) {
          clearInterval(pollHandle);
          pollHandle = null;
        }
      } catch (error) {
        console.error('Could not read thumbnail regeneration status', error);
      }
    }, 1000);
  }

  button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Starting thumbnail regeneration...';
    try {
      const response = await fetch('/api/library/thumbnails/regenerate', { method: 'POST' });
      const result = await response.json().catch(() => ({}));
      if (response.status === 409) {
        status.textContent = result.detail || 'Another library maintenance task is already running.';
        button.disabled = false;
        return;
      }
      if (!response.ok) {
        status.textContent = result.detail || 'Could not start thumbnail regeneration.';
        button.disabled = false;
        return;
      }
      renderState(result);
      startPolling();
    } catch (error) {
      console.error(error);
      status.textContent = 'Could not start thumbnail regeneration.';
      button.disabled = false;
    }
  });

  fetchStatus()
    .then((state) => {
      if (!state) return;
      renderState(state);
      if (state.running) startPolling();
    })
    .catch((error) => console.error('Could not read thumbnail regeneration status', error));
})();
