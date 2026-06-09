/**
 * streams.js — Streams page (CRUD + per-stream config)
 */

const STREAM_COLS = [
  { label: 'Stream'  },
  { label: 'Status'  },
  { label: 'Added'   },
  { label: 'Actions' },
]

function streamRow(r) {
  return `
    <tr class="group border-b border-gray-800/60 hover:bg-gray-800/40 transition-colors">
      <td class="px-6 py-4">
        <div class="flex items-center gap-3">
          <div class="w-9 h-9 rounded-xl bg-gray-800 border border-gray-700 flex items-center justify-center flex-shrink-0 text-base">
            📺
          </div>
          <div class="min-w-0">
            <div class="font-medium text-white text-sm truncate max-w-[300px]" title="${r.title || r.video_id}">
              ${r.title || '—'}
            </div>
            <a href="${r.url}" target="_blank"
               class="text-xs text-gray-500 hover:text-brand-400 font-mono transition-colors">
              ${r.video_id} ↗
            </a>
          </div>
        </div>
      </td>
      <td class="px-6 py-4">${statusBadge(r.status)}</td>
      <td class="px-6 py-4 text-gray-500 text-xs tabular-nums whitespace-nowrap">${fmtDate(r.added_at)}</td>
      <td class="px-6 py-4">
        <div class="flex items-center gap-1">
          <button onclick="openRunModal(${r.id}, '${r.title || r.video_id}')"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                         text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/10 hover:border-emerald-500/40">
            ▶ Run
          </button>
          <button onclick="openRerunModal(${r.id}, '${r.title || r.video_id}')"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                         text-amber-400 border border-amber-500/20 hover:bg-amber-500/10 hover:border-amber-500/40">
            ↺ Rerun
          </button>
          <a href="/results.html?id=${r.id}"
             class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                    text-brand-500 border border-brand-500/20 hover:bg-brand-500/10 hover:border-brand-500/40">
            Results
          </a>
          <button onclick="openConfigModal(${r.id})"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                         text-gray-400 border border-gray-700 hover:bg-gray-800 hover:text-white">
            Config
          </button>
          <button onclick="openEditModal(${r.id}, '${r.url}', '${r.status}')"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                         text-gray-400 border border-gray-700 hover:bg-gray-800 hover:text-white">
            Edit
          </button>
          <button onclick="openDeleteModal(${r.id})"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all
                         text-red-500/70 border border-red-500/20 hover:bg-red-500/10 hover:text-red-400">
            Delete
          </button>
        </div>
      </td>
    </tr>
  `
}

async function loadStreams() {
  document.getElementById('streams-table').innerHTML = `
    <div class="flex items-center justify-center py-16 gap-3 text-gray-600">
      <svg class="animate-spin h-4 w-4 text-brand-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
      </svg>
      <span class="text-sm">Loading streams…</span>
    </div>
  `
  try {
    const res = await apiFetch('/streams')
    const rows = res.data || []

    // Update count badge
    const badge = document.getElementById('stream-count-badge')
    if (badge) { badge.textContent = rows.length; badge.classList.remove('hidden') }

    if (!rows.length) {
      document.getElementById('streams-table').innerHTML = `
        <div class="flex flex-col items-center justify-center py-20 text-gray-600">
          <div class="text-5xl mb-4">📭</div>
          <p class="text-sm mb-3">No streams yet.</p>
          <button onclick="openAddModal()" class="text-xs text-brand-500 hover:text-brand-400 transition-colors">
            Add your first stream →
          </button>
        </div>
      `
      return
    }

    const thead = `
      <thead>
        <tr style="border-bottom:1px solid #1f2937;">
          ${STREAM_COLS.map(c => `
            <th class="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider"
                style="background:#111827;">${c.label}</th>
          `).join('')}
        </tr>
      </thead>
    `
    document.getElementById('streams-table').innerHTML =
      `<table class="w-full text-sm">${thead}<tbody>${rows.map(streamRow).join('')}</tbody></table>`
  } catch (e) {
    document.getElementById('streams-table').innerHTML = `
      <div class="flex items-center justify-center py-16 text-red-400 gap-2">
        <span>⚠</span><span class="text-sm">${e.message}</span>
      </div>
    `
  }
}

// ── Add Stream ────────────────────────────────────────────────────────────────

function openAddModal() {
  document.getElementById('add-url').value = ''
  showError('add-error', null)
  openModal('add-modal')
  setTimeout(() => document.getElementById('add-url').focus(), 100)
}

async function submitAddStream() {
  const url = document.getElementById('add-url').value.trim()
  showError('add-error', null)
  if (!url) { showError('add-error', 'URL is required.'); return }
  try {
    await apiFetch('/streams', { method: 'POST', body: JSON.stringify({ url }) })
    closeModal('add-modal')
    showToast('Stream added successfully.')
    loadStreams()
  } catch (e) {
    showError('add-error', e.message)
  }
}

// ── Edit Stream ───────────────────────────────────────────────────────────────

function openEditModal(id, url, status) {
  document.getElementById('edit-id').value = id
  document.getElementById('edit-url').value = url
  document.getElementById('edit-status').value = status
  showError('edit-error', null)
  openModal('edit-modal')
}

async function submitEditStream() {
  const id     = document.getElementById('edit-id').value
  const url    = document.getElementById('edit-url').value.trim()
  const status = document.getElementById('edit-status').value
  showError('edit-error', null)
  try {
    await apiFetch(`/streams/${id}`, { method: 'PATCH', body: JSON.stringify({ url, status }) })
    closeModal('edit-modal')
    showToast('Stream updated.')
    loadStreams()
  } catch (e) {
    showError('edit-error', e.message)
  }
}

// ── Run Watcher ───────────────────────────────────────────────────────────────

function openRunModal(id, title) {
  document.getElementById('run-id').value = id
  document.getElementById('run-stream-title').textContent = title
  showError('run-error', null)
  openModal('run-modal')
}

async function submitRunStream() {
  const id = document.getElementById('run-id').value
  showError('run-error', null)
  try {
    await apiFetch(`/streams/${id}/run`, { method: 'POST' })
    closeModal('run-modal')
    showToast('Watcher started — stream is now processing.')
    loadStreams()
  } catch (e) {
    showError('run-error', e.message)
  }
}

// ── Rerun Watcher ─────────────────────────────────────────────────────────────

function openRerunModal(id, title) {
  document.getElementById('rerun-id').value = id
  document.getElementById('rerun-stream-title').textContent = title
  showError('rerun-error', null)
  openModal('rerun-modal')
}

async function submitRerunStream() {
  const id = document.getElementById('rerun-id').value
  showError('rerun-error', null)
  try {
    await apiFetch(`/streams/${id}/rerun`, { method: 'POST' })
    closeModal('rerun-modal')
    showToast('Previous spikes cleared — watcher restarted.')
    loadStreams()
  } catch (e) {
    showError('rerun-error', e.message)
  }
}

// ── Delete Stream ─────────────────────────────────────────────────────────────

function openDeleteModal(id) {
  document.getElementById('delete-id').value = id
  openModal('delete-modal')
}

async function submitDeleteStream() {
  const id = document.getElementById('delete-id').value
  try {
    await apiFetch(`/streams/${id}`, { method: 'DELETE' })
    closeModal('delete-modal')
    showToast('Stream deleted.')
    loadStreams()
  } catch (e) {
    showToast(e.message, 'error')
  }
}

// ── Per-stream Config ─────────────────────────────────────────────────────────

async function openConfigModal(streamId) {
  document.getElementById('config-stream-id').value = streamId
  showError('config-error', null)
  try {
    const res = await apiFetch(`/streams/${streamId}/config`)
    document.getElementById('config-source').textContent =
      res.source === 'per_stream'
        ? '⚙️ Using per-stream config override'
        : '🌐 Using global default — saving will create an override'
    mountConfigForm('stream-config-form', res.data, 'scfg')
    openModal('config-modal')
  } catch (e) {
    showToast(e.message, 'error')
  }
}

async function submitStreamConfig() {
  const id = document.getElementById('config-stream-id').value
  showError('config-error', null)
  try {
    const body = readConfigForm('scfg')
    await apiFetch(`/streams/${id}/config`, { method: 'PUT', body: JSON.stringify(body) })
    closeModal('config-modal')
    showToast('Stream config saved.')
  } catch (e) {
    showError('config-error', e.message)
  }
}

async function deleteStreamConfig() {
  const id = document.getElementById('config-stream-id').value
  try {
    await apiFetch(`/streams/${id}/config`, { method: 'DELETE' })
    closeModal('config-modal')
    showToast('Per-stream config removed. Reverted to global default.')
  } catch (e) {
    showToast(e.message, 'error')
  }
}

// Enter key submits add modal
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('add-url').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitAddStream()
  })
})

// ── Clip Jobs ─────────────────────────────────────────────────────────────────

// Active polling timers: { jobId: intervalId }
const _clipPollers = {}

async function triggerClip(streamId, btn) {
  btn.disabled = true
  btn.textContent = '⏳ Starting…'

  try {
    const res = await apiFetch(`/streams/${streamId}/clip`, { method: 'POST' })
    showToast(`Clip job #${res.job_id} started — downloading & cropping spikes…`)
    btn.textContent = '⏳ Processing…'
    btn.style.cssText = 'color:#a78bfa; border-color:rgba(167,139,250,0.3);'
    pollClipJob(res.job_id, streamId, btn)
  } catch (e) {
    showToast(e.message, 'error')
    btn.disabled = false
    btn.textContent = '✂ Clip'
  }
}

function pollClipJob(jobId, streamId, btn) {
  // Clear any existing poller for this job
  if (_clipPollers[jobId]) clearInterval(_clipPollers[jobId])

  _clipPollers[jobId] = setInterval(async () => {
    try {
      const job = await apiFetch(`/clips/${jobId}`)

      if (job.status === 'done') {
        clearInterval(_clipPollers[jobId])
        delete _clipPollers[jobId]

        btn.disabled = false
        btn.textContent = '✂ Clip'

        const driveLink = job.drive_folder_url
          ? `<a href="${job.drive_folder_url}" target="_blank"
                class="underline text-purple-300 hover:text-purple-200">
               Open in Drive →
             </a>`
          : ''

        showToast(`✅ ${job.clips_count} clip${job.clips_count !== 1 ? 's' : ''} uploaded to Google Drive!`)

        // Show drive link in a banner below the table
        showClipBanner(streamId, job.clips_count, job.drive_folder_url)

      } else if (job.status === 'error') {
        clearInterval(_clipPollers[jobId])
        delete _clipPollers[jobId]

        btn.disabled = false
        btn.textContent = '✂ Clip'
        btn.style.cssText = ''

        showToast(`Clip job failed: ${job.error_msg || 'unknown error'}`, 'error')
      }
      // still pending/processing — keep polling
    } catch (e) {
      // network error — keep polling silently
    }
  }, 4000) // poll every 4 seconds
}

function showClipBanner(streamId, clipsCount, driveUrl) {
  // Remove existing banner for this stream if any
  const existing = document.getElementById(`clip-banner-${streamId}`)
  if (existing) existing.remove()

  const banner = document.createElement('div')
  banner.id = `clip-banner-${streamId}`
  banner.style.cssText = `
    margin-top: 1rem;
    padding: 0.875rem 1.25rem;
    background: rgba(139,92,246,0.1);
    border: 1px solid rgba(139,92,246,0.25);
    border-radius: 0.75rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.875rem;
  `
  banner.innerHTML = `
    <div style="display:flex; align-items:center; gap:0.75rem;">
      <span style="font-size:1.25rem;">✅</span>
      <div>
        <div style="color:#e9d5ff; font-weight:500;">
          ${clipsCount} clip${clipsCount !== 1 ? 's' : ''} ready on Google Drive
        </div>
        <div style="color:#7c3aed; font-size:0.75rem; margin-top:0.125rem;">
          Stream #${streamId}
        </div>
      </div>
    </div>
    ${driveUrl
      ? `<a href="${driveUrl}" target="_blank"
            style="color:#a78bfa; border:1px solid rgba(167,139,250,0.3);
                   padding:0.375rem 0.875rem; border-radius:0.5rem; font-size:0.75rem;
                   text-decoration:none; transition:background 0.15s;"
            onmouseover="this.style.background='rgba(167,139,250,0.1)'"
            onmouseout="this.style.background='transparent'">
           Open in Drive →
         </a>`
      : ''
    }
  `

  // Insert after the streams table card
  const main = document.querySelector('main')
  if (main) main.appendChild(banner)
}

loadStreams()
