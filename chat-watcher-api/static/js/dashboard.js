/**
 * dashboard.js — Dashboard page
 */

const SUMMARY_COLS = [
  { label: 'Stream'       },
  { label: 'Status'       },
  { label: 'Spikes'       },
  { label: 'Avg Baseline' },
  { label: 'Peak Rate'    },
  { label: 'Processed'    },
  { label: ''             },
]

// Stat card definitions — using inline styles for dynamic colors (Tailwind JIT can't scan JS strings)
const STAT_CARDS = [
  { key: 'streams', icon: '📡', label: 'Total Streams', accentColor: '#0ea5e9' },
  { key: 'done',    icon: '✅', label: 'Done',          accentColor: '#22c55e' },
  { key: 'pending', icon: '⏳', label: 'Pending',       accentColor: '#eab308' },
  { key: 'spikes',  icon: '🔥', label: 'Total Spikes',  accentColor: '#ef4444' },
]

function StatCard({ icon, label, value, accentColor }) {
  return `
    <div class="relative bg-gray-900 rounded-2xl p-5 overflow-hidden transition-all hover:scale-[1.02]"
         style="border: 1px solid ${accentColor}33;">
      <!-- Top accent line -->
      <div class="absolute top-0 left-0 right-0 h-0.5 rounded-t-2xl"
           style="background: linear-gradient(90deg, ${accentColor}99, transparent);"></div>
      <div class="flex items-start justify-between mb-4">
        <div class="w-10 h-10 rounded-xl flex items-center justify-center text-xl"
             style="background: ${accentColor}18;">
          ${icon}
        </div>
        <div class="w-2 h-2 rounded-full mt-1" style="background: ${accentColor}; box-shadow: 0 0 6px ${accentColor};"></div>
      </div>
      <div class="text-3xl font-bold text-white mb-1">${value ?? '—'}</div>
      <div class="text-xs text-gray-500 uppercase tracking-wider font-medium">${label}</div>
    </div>
  `
}

function summaryRow(r) {
  const hasSpikes = r.total_spikes > 0
  return `
    <tr class="group border-b border-gray-800/60 hover:bg-gray-800/40 transition-colors cursor-default">
      <!-- Stream info -->
      <td class="px-6 py-4">
        <div class="flex items-center gap-3">
          <div class="w-9 h-9 rounded-xl bg-gray-800 border border-gray-700 flex items-center justify-center flex-shrink-0 text-base">
            📺
          </div>
          <div class="min-w-0">
            <div class="font-medium text-white text-sm leading-snug truncate max-w-[260px]" title="${r.title || r.video_id}">
              ${r.title || '—'}
            </div>
            <a href="https://youtube.com/watch?v=${r.video_id}" target="_blank"
               class="text-xs text-gray-500 hover:text-brand-400 font-mono transition-colors">
              ${r.video_id} ↗
            </a>
          </div>
        </div>
      </td>

      <!-- Status -->
      <td class="px-6 py-4">${statusBadge(r.status)}</td>

      <!-- Spikes -->
      <td class="px-6 py-4">
        ${hasSpikes
          ? `<span class="inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full"
                  style="background:#ef444418; color:#f87171; border:1px solid #ef444430;">
               🔥 ${r.total_spikes} spike${r.total_spikes !== 1 ? 's' : ''}
             </span>`
          : `<span class="text-gray-600 text-xs">—</span>`
        }
      </td>

      <!-- Avg baseline -->
      <td class="px-6 py-4">
        <span class="text-gray-300 text-sm tabular-nums">
          ${r.avg_baseline_rate ? fmtNum(r.avg_baseline_rate) : '—'}
          ${r.avg_baseline_rate ? '<span class="text-gray-600 text-xs">/min</span>' : ''}
        </span>
      </td>

      <!-- Peak rate -->
      <td class="px-6 py-4">
        ${r.peak_spike_rate
          ? `<span class="text-orange-400 font-semibold text-sm tabular-nums">
               ${fmtNum(r.peak_spike_rate)}<span class="text-orange-700 text-xs font-normal">/min</span>
             </span>`
          : `<span class="text-gray-600 text-xs">—</span>`
        }
      </td>

      <!-- Processed at -->
      <td class="px-6 py-4 text-gray-500 text-xs tabular-nums whitespace-nowrap">
        ${fmtDate(r.processed_at)}
      </td>

      <!-- Action -->
      <td class="px-6 py-4">
        <a href="/results.html?id=${r.id}"
           class="inline-flex items-center gap-1 text-xs font-medium px-3 py-1.5 rounded-lg transition-all
                  text-brand-500 border border-brand-500/20 hover:bg-brand-500/10 hover:border-brand-500/40">
          View Spikes
          <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
          </svg>
        </a>
      </td>
    </tr>
  `
}

async function loadSummary() {
  // Loading skeleton
  document.getElementById('summary-table').innerHTML = `
    <div class="flex items-center justify-center py-16 gap-3 text-gray-600">
      <svg class="animate-spin h-4 w-4 text-brand-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
      </svg>
      <span class="text-sm">Loading streams…</span>
    </div>
  `

  try {
    const res = await apiFetch('/summary')
    const rows = res.data || []

    // Subtitle
    document.getElementById('page-subtitle').textContent =
      'Updated ' + new Date().toLocaleTimeString()

    // Stat cards
    const totalSpikes = rows.reduce((s, r) => s + (r.total_spikes || 0), 0)
    const statValues  = [rows.length, rows.filter(r => r.status === 'done').length,
                         rows.filter(r => r.status === 'pending').length, totalSpikes]
    document.getElementById('stats-row').innerHTML =
      STAT_CARDS.map((c, i) => StatCard({ ...c, value: statValues[i] })).join('')

    // Stream count badge
    const badge = document.getElementById('stream-count-badge')
    if (badge) { badge.textContent = rows.length; badge.classList.remove('hidden') }

    // Empty state
    if (!rows.length) {
      document.getElementById('summary-table').innerHTML = `
        <div class="flex flex-col items-center justify-center py-20 text-gray-600">
          <div class="text-5xl mb-4">📭</div>
          <p class="text-sm mb-3">No streams yet.</p>
          <a href="/streams.html" class="text-xs text-brand-500 hover:text-brand-400 transition-colors">
            Add your first stream →
          </a>
        </div>
      `
      return
    }

    // Table
    const thead = `
      <thead>
        <tr style="border-bottom: 1px solid #1f2937;">
          ${SUMMARY_COLS.map(c => `
            <th class="px-6 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider"
                style="background:#111827;">
              ${c.label}
            </th>
          `).join('')}
        </tr>
      </thead>
    `
    document.getElementById('summary-table').innerHTML =
      `<table class="w-full text-sm">${thead}<tbody>${rows.map(summaryRow).join('')}</tbody></table>`

  } catch (e) {
    document.getElementById('summary-table').innerHTML = `
      <div class="flex items-center justify-center py-16 text-red-400 gap-2">
        <span>⚠</span><span class="text-sm">${e.message}</span>
      </div>
    `
  }
}

loadSummary()
