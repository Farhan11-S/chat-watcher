/**
 * results.js — Spike results page
 */

const RESULTS_COLS = [
  { label: '#'              },
  { label: 'Timestamp'      },
  { label: 'Stream Seconds' },
  { label: 'Rate / min'     },
  { label: 'Baseline / min' },
  { label: 'Spike Ratio'    },
  { label: 'Msgs in Window' },
  { label: 'Detected At'    },
  { label: 'Clip'           },
]

// Holds the video_id once the summary is loaded — used to build YouTube links
let _videoId = null

function ytLink(streamSeconds) {
  if (!_videoId) return ''
  const t = Math.max(0, Math.floor(streamSeconds) - 5) // 5s before spike
  return `https://www.youtube.com/watch?v=${_videoId}&t=${t}s`
}

const SPIKE_STAT_CARDS = [
  { key: 'total_spikes',       icon: '🔥', label: 'Total Spikes',  accentColor: '#ef4444' },
  { key: 'avg_baseline_rate',  icon: '📊', label: 'Avg Baseline',  accentColor: '#0ea5e9', suffix: '/min' },
  { key: 'peak_spike_rate',    icon: '⚡', label: 'Peak Rate',     accentColor: '#f97316', suffix: '/min' },
  { key: 'status',             icon: '🎯', label: 'Status',        accentColor: '#22c55e' },
]

function SpikeStatCard({ icon, label, value, accentColor }) {
  return `
    <div class="relative bg-gray-900 rounded-2xl p-5 overflow-hidden transition-all hover:scale-[1.02]"
         style="border: 1px solid ${accentColor}33;">
      <div class="absolute top-0 left-0 right-0 h-0.5 rounded-t-2xl"
           style="background: linear-gradient(90deg, ${accentColor}99, transparent);"></div>
      <div class="flex items-start justify-between mb-4">
        <div class="w-10 h-10 rounded-xl flex items-center justify-center text-xl"
             style="background: ${accentColor}18;">
          ${icon}
        </div>
        <div class="w-2 h-2 rounded-full mt-1" style="background:${accentColor}; box-shadow:0 0 6px ${accentColor};"></div>
      </div>
      <div class="text-2xl font-bold text-white mb-1 truncate">${value ?? '—'}</div>
      <div class="text-xs text-gray-500 uppercase tracking-wider font-medium">${label}</div>
    </div>
  `
}

function resultRow(s, i) {
  const ratioColor =
    s.spike_ratio >= 4   ? '#f87171' :
    s.spike_ratio >= 2.5 ? '#fb923c' : '#facc15'

  const yt = ytLink(s.stream_seconds)

  let summaryHTML = ''
  if (s.context_summary) {
    summaryHTML = `
      <div class="flex items-start gap-2 mt-2 pt-2" style="border-top: 1px solid #1f2937;">
        <span class="text-xs" style="color:#818cf8;">💬</span>
        <span class="text-xs italic" style="color:#94a3b8;">${s.context_summary}</span>
      </div>`
  } else {
    summaryHTML = `
      <div class="flex items-start gap-2 mt-2 pt-2" style="border-top: 1px solid #1f2937;">
        <button onclick="triggerSummarize(${s.id}, this)"
                id="summary-btn-${s.id}"
                class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-lg transition-all"
                style="color:#818cf8; border:1px solid rgba(129,140,248,0.2);">
          🔍 Summarize
        </button>
        <span id="summary-status-${s.id}" class="text-xs hidden" style="color:#94a3b8;"></span>
      </div>`
  }

  return `
    <tr class="group border-b border-gray-800/60 hover:bg-gray-800/40 transition-colors">
      <td class="px-6 py-4 text-gray-600 text-xs tabular-nums">${i + 1}</td>
      <td class="px-6 py-4">
        <div class="flex items-center gap-2">
          <span class="font-mono font-bold text-white text-lg tracking-wide">${s.stream_timestamp}</span>
          ${yt ? `
            <a href="${yt}" target="_blank"
               title="Open in YouTube at this timestamp"
               class="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-lg transition-all opacity-0 group-hover:opacity-100"
               style="color:#ff4444; border:1px solid rgba(255,68,68,0.25); background:rgba(255,68,68,0.08);">
              ▶ YT
            </a>
          ` : ''}
        </div>
        ${summaryHTML}
      </td>
      <td class="px-6 py-4 text-gray-500 text-xs tabular-nums">${fmtNum(s.stream_seconds, 0)}s</td>
      <td class="px-6 py-4">
        <span class="font-semibold tabular-nums" style="color:#fb923c;">${fmtNum(s.current_rate_per_min)}</span>
        <span class="text-gray-600 text-xs">/min</span>
      </td>
      <td class="px-6 py-4 text-gray-400 text-sm tabular-nums">
        ${fmtNum(s.baseline_rate_per_min)}<span class="text-gray-600 text-xs">/min</span>
      </td>
      <td class="px-6 py-4">
        <span class="inline-flex items-center gap-1 text-xs font-bold px-2.5 py-1 rounded-full tabular-nums"
              style="background:${ratioColor}18; color:${ratioColor}; border:1px solid ${ratioColor}30;">
          ${fmtNum(s.spike_ratio, 2)}×
        </span>
      </td>
      <td class="px-6 py-4 text-gray-400 text-sm tabular-nums">${s.messages_in_window}</td>
      <td class="px-6 py-4 text-gray-600 text-xs tabular-nums whitespace-nowrap">${fmtDate(s.detected_at)}</td>
      <td class="px-6 py-4">
        <div id="clip-cell-${s.id}">
          <button id="clip-btn-${s.id}" onclick="triggerClip(${s.id}, this)"
                  class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all"
                  style="color:#a78bfa; border:1px solid rgba(167,139,250,0.2);">
            ✂ Clip
          </button>
        </div>
      </td>
    </tr>
  `
}

async function loadResults() {
  const id = getStreamID()

  document.getElementById('page-title').textContent = 'Loading…'

  if (!id) {
    document.getElementById('page-title').textContent = 'No stream selected'
    return
  }

  document.getElementById('results-table').innerHTML = `
    <div class="flex items-center justify-center py-16 gap-3 text-gray-600">
      <svg class="animate-spin h-4 w-4 text-brand-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
      </svg>
      <span class="text-sm">Loading results…</span>
    </div>
  `

  try {
    const summary = await apiFetch(`/summary/${id}`)

    document.getElementById('page-title').textContent = summary.title || summary.video_id
    document.getElementById('page-subtitle').textContent =
      `Video ID: ${summary.video_id}  ·  Started: ${fmtDate(summary.stream_start)}`

    // Store video_id for YouTube timestamp links
    _videoId = summary.video_id

    // Stat cards
    const cardValues = [
      summary.total_spikes,
      summary.avg_baseline_rate ? fmtNum(summary.avg_baseline_rate) + '/min' : '—',
      summary.peak_spike_rate   ? fmtNum(summary.peak_spike_rate)   + '/min' : '—',
      summary.status,
    ]
    document.getElementById('stats-row').innerHTML =
      SPIKE_STAT_CARDS.map((c, i) => SpikeStatCard({ ...c, value: cardValues[i] })).join('')

    // Spike results
    const res = await apiFetch(`/streams/${id}/results`)
    const spikes = res.data || []

    if (!spikes.length) {
      document.getElementById('results-table').innerHTML = `
        <div class="flex flex-col items-center justify-center py-20 text-gray-600">
          <div class="text-5xl mb-4">🔍</div>
          <p class="text-sm">No spikes detected for this stream.</p>
        </div>
      `
      return
    }

    const thead = `
      <thead>
        <tr style="border-bottom:1px solid #1f2937;">
          ${RESULTS_COLS.map(c => `
            <th class="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider"
                style="background:#111827;">${c.label}</th>
          `).join('')}
        </tr>
      </thead>
    `
    document.getElementById('results-table').innerHTML =
      `<table class="w-full text-sm">${thead}<tbody>${spikes.map(resultRow).join('')}</tbody></table>`

    // Show Summarize All button if any spike lacks a summary
    const unsummarized = spikes.filter(s => !s.context_summary).length
    const summarizeAllBtn = document.getElementById('summarize-all-btn')
    if (summarizeAllBtn) {
      if (unsummarized > 0) {
        summarizeAllBtn.classList.remove('hidden')
        summarizeAllBtn.textContent = `🔍 Summarize All (${unsummarized})`
      } else {
        summarizeAllBtn.classList.add('hidden')
      }
    }

  } catch (e) {
    document.getElementById('page-title').textContent = 'Error loading results'
    document.getElementById('results-table').innerHTML = `
      <div class="flex items-center justify-center py-16 text-red-400 gap-2">
        <span>⚠</span><span class="text-sm">${e.message}</span>
      </div>
    `
  }
}

loadResults()

// ── Per-spike Clip ────────────────────────────────────────────────────────────

const _clipPollers = {}

function clipProgressHTML(msg) {
  return `
    <div class="flex flex-col gap-1.5 min-w-[160px]">
      <div class="flex items-center gap-2">
        <svg class="animate-spin h-3 w-3 shrink-0" style="color:#a78bfa;" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
        </svg>
        <span class="text-xs truncate max-w-[140px]" style="color:#a78bfa;" title="${msg}">${msg}</span>
      </div>
      <div class="w-full rounded-full overflow-hidden" style="height:3px; background:rgba(167,139,250,0.15);">
        <div class="h-full rounded-full animate-pulse" style="width:100%; background:linear-gradient(90deg,#a78bfa,#7c3aed);"></div>
      </div>
    </div>
  `
}

async function triggerClip(spikeId, btn) {
  const cell = document.getElementById(`clip-cell-${spikeId}`)
  cell.innerHTML = clipProgressHTML('⏳ Starting…')

  try {
    const res = await apiFetch(`/spikes/${spikeId}/clip`, { method: 'POST' })
    showToast(`Clip job #${res.job_id} started for ${res.stream_timestamp}`)
    pollClipJob(res.job_id, spikeId)
  } catch (e) {
    showToast(e.message, 'error')
    cell.innerHTML = `
      <button id="clip-btn-${spikeId}" onclick="triggerClip(${spikeId}, this)"
              class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all"
              style="color:#f87171; border:1px solid rgba(248,113,113,0.2);">
        ✂ Retry
      </button>
    `
  }
}

function pollClipJob(jobId, spikeId) {
  if (_clipPollers[spikeId]) clearInterval(_clipPollers[spikeId])

  _clipPollers[spikeId] = setInterval(async () => {
    try {
      const job = await apiFetch(`/clips/${jobId}`)
      const cell = document.getElementById(`clip-cell-${spikeId}`)
      if (!cell) return

      if (job.status === 'processing') {
        const msg = job.progress_msg || 'Processing…'
        cell.innerHTML = clipProgressHTML(msg)

      } else if (job.status === 'done') {
        clearInterval(_clipPollers[spikeId])
        delete _clipPollers[spikeId]

        const parts = job.clips_count > 1 ? `${job.clips_count} parts` : '1 clip'
        cell.innerHTML = job.drive_folder_url
          ? `<a href="${job.drive_folder_url}" target="_blank"
                class="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg transition-all"
                style="color:#4ade80; border:1px solid rgba(74,222,128,0.2); background:rgba(74,222,128,0.06);">
               ✅ Drive (${parts})
             </a>`
          : `<span class="text-xs" style="color:#4ade80;">✅ Done (${parts})</span>`

        showToast(`✅ ${parts} ready on Google Drive!`)

      } else if (job.status === 'error') {
        clearInterval(_clipPollers[spikeId])
        delete _clipPollers[spikeId]

        cell.innerHTML = `
          <div class="flex flex-col gap-1">
            <button onclick="triggerClip(${spikeId}, this)"
                    class="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg transition-all"
                    style="color:#f87171; border:1px solid rgba(248,113,113,0.2);">
              ✂ Retry
            </button>
            <span class="text-xs truncate max-w-[160px]" style="color:#f87171;" title="${job.error_msg || ''}">
              ⚠ ${(job.error_msg || 'unknown error').slice(0, 40)}
            </span>
          </div>
        `
        showToast(`Clip failed: ${job.error_msg || 'unknown error'}`, 'error')
      }
    } catch (e) {
      // network hiccup — keep polling
    }
  }, 3000)
}

// ── Per-spike Summarize ─────────────────────────────────────────────────────────

async function triggerSummarize(spikeId, btn) {
  btn.disabled = true
  btn.textContent = '⏳ Summarizing...'
  const statusEl = document.getElementById(`summary-status-${spikeId}`)
  if (statusEl) { statusEl.classList.remove('hidden'); statusEl.textContent = 'Calling LLM…' }

  try {
    const resp = await apiFetch(`/spikes/${spikeId}/summarize`, { method: 'POST' })
    if (resp.context_summary) {
      // Replace button + status with the summary text (textContent to avoid XSS)
      const parent = btn.parentElement
      const icon = document.createElement('span')
      icon.className = 'text-xs'
      icon.style.color = '#818cf8'
      icon.textContent = '💬'
      const text = document.createElement('span')
      text.className = 'text-xs italic'
      text.style.color = '#94a3b8'
      text.textContent = resp.context_summary
      parent.replaceChildren(icon, text)
    } else {
      btn.textContent = '🔍 Summarize'
      btn.disabled = false
      if (statusEl) { statusEl.textContent = 'Failed. Try again.' }
    }
  } catch (e) {
    btn.textContent = '🔍 Summarize'
    btn.disabled = false
    if (statusEl) { statusEl.textContent = 'Error: ' + e.message }
  }
}

// ── Summarize All ───────────────────────────────────────────────────────────────

async function triggerSummarizeAll(btn) {
  const id = getStreamID()
  if (!id) return

  btn.disabled = true
  btn.textContent = '⏳ Summarizing all...'

  try {
    const resp = await apiFetch(`/streams/${id}/summarize-all`, { method: 'POST' })

    // Update each spike row inline with the returned summaries
    if (resp.results && resp.results.length) {
      for (const r of resp.results) {
        if (!r.summary) continue
        const rowBtn = document.getElementById(`summary-btn-${r.spike_id}`)
        if (!rowBtn) continue
        const parent = rowBtn.parentElement
        const icon = document.createElement('span')
        icon.className = 'text-xs'
        icon.style.color = '#818cf8'
        icon.textContent = '💬'
        const text = document.createElement('span')
        text.className = 'text-xs italic'
        text.style.color = '#94a3b8'
        text.textContent = r.summary
        parent.replaceChildren(icon, text)
      }
    }

    const done = (resp.summarized || 0) + (resp.cached || 0)
    const failed = resp.failed || 0
    showToast(
      failed > 0
        ? `✅ ${done} summarized, ⚠ ${failed} failed (no chat messages?)`
        : `✅ All ${done} spikes summarized`,
      failed > 0 ? 'error' : 'success'
    )
    btn.classList.add('hidden')
  } catch (e) {
    showToast('Summarize all failed: ' + e.message, 'error')
    btn.disabled = false
    btn.textContent = '🔍 Summarize All'
  }
}
