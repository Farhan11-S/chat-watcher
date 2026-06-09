/**
 * config.js — Global config page
 */

async function loadGlobalConfig() {
  try {
    const cfg = await apiFetch('/config')
    mountConfigForm('global-config-form', cfg, 'cfg')
  } catch (e) {
    showToast('Failed to load config: ' + e.message, 'error')
  }
}

async function saveGlobalConfig() {
  showError('cfg-error', null)
  document.getElementById('cfg-success').classList.add('hidden')

  try {
    const body = readConfigForm('cfg')
    await apiFetch('/config', { method: 'PUT', body: JSON.stringify(body) })
    document.getElementById('cfg-success').classList.remove('hidden')
    setTimeout(() => document.getElementById('cfg-success').classList.add('hidden'), 3000)
    showToast('Global config saved.')
  } catch (e) {
    showError('cfg-error', e.message)
  }
}

// Init
mountPageHeader('page-header', {
  title:    'Global Spike Config',
  subtitle: 'Default thresholds applied to all streams unless overridden per-stream.',
})
loadGlobalConfig()
