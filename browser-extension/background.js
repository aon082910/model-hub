// Downloads the chosen files in the browser (so they are fetched with your own
// logged-in session, which MakerWorld, MyMiniFactory and others require) and
// posts them to your Model Hub. Model Hub keeps only model files; a .zip is opened
// there and just the models inside are kept.
const ALLOWED_EXTENSIONS = /\.(stl|3mf|step|stp|obj|fbx|zip)$/i;

function fileNameFrom(response, wantedName, url) {
  if (wantedName && ALLOWED_EXTENSIONS.test(wantedName)) return wantedName;
  const disposition = response.headers.get('content-disposition') || '';
  const match = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(disposition) || /filename="?([^";]+)"?/i.exec(disposition);
  if (match) {
    try { return decodeURIComponent(match[1].trim().replace(/^"|"$/g, '')); } catch (e) { return match[1].trim(); }
  }
  try { return decodeURIComponent(new URL(response.url || url).pathname.split('/').pop() || ''); } catch (e) { return ''; }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type !== 'modelhub-import') return false;

  (async () => {
    const { serverUrl, apiKey } = await chrome.storage.sync.get(['serverUrl', 'apiKey']);
    if (!serverUrl) {
      sendResponse({ ok: false, error: 'Set the Model Hub server URL in the extension popup first.' });
      return;
    }
    if (!apiKey) {
      sendResponse({ ok: false, error: 'Set the Model Hub extension API key in the extension popup first (copy it from Model Hub\'s Settings tab).' });
      return;
    }

    // older versions of the content script sent plain URLs
    const files = msg.files || (msg.fileUrls || []).map((url) => ({ url, name: '' }));
    let imported = 0;
    const errors = [];

    for (const file of files) {
      const label = file.name || file.url.split('/').pop().split('?')[0] || file.url;
      try {
        const fileResp = await fetch(file.url, { credentials: 'include' });
        if (!fileResp.ok) throw new Error(`download HTTP ${fileResp.status}`);
        if ((fileResp.headers.get('content-type') || '').includes('text/html')) {
          throw new Error('that link opened a web page, not a file (you may need to log in)');
        }
        const filename = fileNameFrom(fileResp, file.name, file.url) || 'model.stl';
        if (!ALLOWED_EXTENSIONS.test(filename)) throw new Error(`${filename} is not a model file or zip`);
        const blob = await fileResp.blob();

        const form = new FormData();
        form.append('file', blob, filename);
        if (msg.sourceUrl) form.append('source_url', msg.sourceUrl);
        if (msg.designer) form.append('designer', msg.designer);
        if (msg.license) form.append('license', msg.license);

        const importResp = await fetch(`${serverUrl}/api/library/import`, {
          method: 'POST',
          headers: { 'X-Model-Hub-Api-Key': apiKey },
          body: form,
        });
        if (!importResp.ok) {
          const body = await importResp.text().catch(() => '');
          throw new Error(`import HTTP ${importResp.status} ${body.slice(0, 120)}`);
        }
        imported++;
      } catch (e) {
        errors.push(`${label}: ${e.message}`);
      }
    }

    sendResponse({ ok: true, imported, errors });
  })();

  return true; // keep the message channel open for the async response
});
