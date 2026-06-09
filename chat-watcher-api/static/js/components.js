/**
 * components.js — Reusable UI components
 *
 * All functions return HTML strings unless they accept a containerId,
 * in which case they mount directly into that element.
 */

// ── Navbar ────────────────────────────────────────────────────────────────────

const NAV_LINKS = [
  { label: 'Dashboard',     href: '/',             key: 'dashboard' },
  { label: 'Streams',       href: '/streams.html', key: 'streams'   },
  { label: 'Global Config', href: '/config.html',  key: 'config'    },
]

function mountNavbar() {
  const mount = document.getElementById('navbar-mount')
  if (!mount) return

  const path = window.location.pathname
  const activeKey =
    path === '/' || path === '/index.html' ? 'dashboard' :
    path.includes('streams')               ? 'streams'   :
    path.includes('config')                ? 'config'    : ''

  mount.innerHTML = `
    <nav class="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center gap-6">
      <a href="/" class="text-brand-500 font-bold text-lg tracking-tight no-underline">📡 Chat Watcher</a>
      ${NAV_LINKS.map(l => `
        <a href="${l.href}" class="nav-link ${l.key === activeKey ? 'active' : ''}">${l.label}</a>
      `).join('')}
    </nav>
  `
}

// ── Page header ───────────────────────────────────────────────────────────────

/**
 * Mount a standard page header.
 * @param {string} containerId
 * @param {{ title: string, subtitle?: string, action?: { label: string, onclick: string } }} opts
 */
function mountPageHeader(containerId, { title, subtitle = '', action = null }) {
  const el = document.getElementById(containerId)
  if (!el) return
  el.innerHTML = `
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="text-2xl font-semibold" id="page-title">${title}</h1>
        ${subtitle ? `<p class="text-gray-400 text-sm mt-1" id="page-subtitle">${subtitle}</p>` : ''}
      </div>
      ${action ? `<button onclick="${action.onclick}" class="btn-primary">${action.label}</button>` : ''}
    </div>
  `
}

/**
 * Update the page title and/or subtitle after mountPageHeader has been called.
 */
function updatePageHeader(title, subtitle) {
  if (title != null) {
    const el = document.getElementById('page-title')
    if (el) el.textContent = title
  }
  if (subtitle != null) {
    const el = document.getElementById('page-subtitle')
    if (el) el.innerHTML = subtitle
  }
}

// ── Stat Cards ────────────────────────────────────────────────────────────────

/**
 * Render a row of stat cards.
 * @param {string} containerId
 * @param {Array<{ label: string, value: any, colorClass?: string }>} stats
 */
function mountStatCards(containerId, stats) {
  const el = document.getElementById(containerId)
  if (!el) return
  el.innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="stat-label">${s.label}</div>
      <div class="stat-value ${s.colorClass || ''}">${s.value ?? '—'}</div>
    </div>
  `).join('')
}

// ── Table ─────────────────────────────────────────────────────────────────────

/**
 * Render a full table into a container.
 * @param {string} containerId
 * @param {Array<{ label: string }>} columns
 * @param {Array<object>} rows
 * @param {function(row, index): string} rowFn  — returns a <tr> HTML string
 * @param {{ empty?: string, loading?: boolean, error?: string }} opts
 */
function renderTable(containerId, columns, rows, rowFn, opts = {}) {
  const el = document.getElementById(containerId)
  if (!el) return

  const thead = `<tr class="table-head">${columns.map(c =>
    `<th class="${c.align === 'right' ? 'text-right' : ''}">${c.label}</th>`
  ).join('')}</tr>`

  let tbody
  if (opts.error) {
    tbody = ErrorRow(columns.length, opts.error)
  } else if (opts.loading) {
    tbody = LoadingRow(columns.length)
  } else if (!rows.length) {
    tbody = EmptyRow(columns.length, opts.empty || 'No data.')
  } else {
    tbody = rows.map((r, i) => rowFn(r, i)).join('')
  }

  el.innerHTML = `
    <table class="w-full text-sm">
      <thead>${thead}</thead>
      <tbody>${tbody}</tbody>
    </table>
  `
}

// ── Table row states ──────────────────────────────────────────────────────────

function LoadingRow(colspan) {
  return `<tr><td colspan="${colspan}" class="text-center py-10 text-gray-500">
    <div class="inline-flex items-center gap-2">
      <svg class="animate-spin h-4 w-4 text-brand-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
      </svg>
      Loading...
    </div>
  </td></tr>`
}

function ErrorRow(colspan, message) {
  return `<tr><td colspan="${colspan}" class="text-center py-8 text-red-400">⚠ ${message}</td></tr>`
}

function EmptyRow(colspan, message) {
  return `<tr><td colspan="${colspan}" class="text-center py-10 text-gray-500">${message}</td></tr>`
}

// ── Empty State ───────────────────────────────────────────────────────────────

function EmptyState(message, linkHref = null, linkText = null) {
  const link = linkHref
    ? `<a href="${linkHref}" class="text-brand-500 hover:underline mt-2 inline-block">${linkText} →</a>`
    : ''
  return `
    <div class="flex flex-col items-center justify-center py-16 text-gray-500">
      <div class="text-4xl mb-3">📭</div>
      <p>${message}</p>
      ${link}
    </div>
  `
}

// ── Config Form ───────────────────────────────────────────────────────────────

const CONFIG_FIELDS = [
  { id: 'window',           label: 'Window Seconds',   hint: 'sliding window size',  key: 'window_seconds',   step: '1',   min: '5',   type: 'number' },
  { id: 'baseline-windows', label: 'Baseline Windows', hint: 'history depth',        key: 'baseline_windows', step: '1',   min: '1',   type: 'number' },
  { id: 'multiplier',       label: 'Spike Multiplier', hint: 'e.g. 2.5× baseline',  key: 'spike_multiplier', step: '0.1', min: '1.1', type: 'number' },
  { id: 'min-messages',     label: 'Min Messages',     hint: 'in window to trigger', key: 'min_messages',     step: '1',   min: '1',   type: 'number' },
  { id: 'cooldown',         label: 'Cooldown Seconds', hint: 'gap between spikes',   key: 'cooldown_seconds', step: '1',   min: '1',   type: 'number' },
  { id: 'clip-window',      label: 'Clip Duration',    hint: 'seconds to download around spike', key: 'clip_window_secs', step: '30', min: '30', type: 'number' },
]

const RESOLUTION_OPTIONS = ['360p', '480p', '720p', '1080p', 'best']

/**
 * Render spike config fields into a container.
 * @param {string} containerId
 * @param {object} values  — config object from API
 * @param {string} prefix  — id prefix to avoid collisions (e.g. 'cfg' or 'scfg')
 */
function mountConfigForm(containerId, values = {}, prefix = 'cfg') {
  const el = document.getElementById(containerId)
  if (!el) return

  const currentRes = values['clip_resolution'] || '720p'

  el.innerHTML = `
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      ${CONFIG_FIELDS.map(f => `
        <div>
          <label class="form-label" for="${prefix}-${f.id}">
            ${f.label}
            <span class="text-gray-500 font-normal ml-1">— ${f.hint}</span>
          </label>
          <input
            id="${prefix}-${f.id}"
            type="number"
            step="${f.step}"
            min="${f.min}"
            value="${values[f.key] ?? ''}"
            class="form-input"
          />
        </div>
      `).join('')}
      <div>
        <label class="form-label" for="${prefix}-clip-resolution">
          Clip Resolution
          <span class="text-gray-500 font-normal ml-1">— video quality for downloads</span>
        </label>
        <select id="${prefix}-clip-resolution" class="form-input">
          ${RESOLUTION_OPTIONS.map(r => `
            <option value="${r}" ${r === currentRes ? 'selected' : ''}>${r}</option>
          `).join('')}
        </select>
      </div>
    </div>
  `
}

/**
 * Read config form values back into an object.
 * @param {string} prefix — same prefix used in mountConfigForm
 */
function readConfigForm(prefix = 'cfg') {
  const get = id => document.getElementById(`${prefix}-${id}`)
  return {
    window_seconds:   parseInt(get('window').value),
    baseline_windows: parseInt(get('baseline-windows').value),
    spike_multiplier: parseFloat(get('multiplier').value),
    min_messages:     parseInt(get('min-messages').value),
    cooldown_seconds: parseInt(get('cooldown').value),
    clip_window_secs: parseInt(get('clip-window').value),
    clip_resolution:  get('clip-resolution').value,
  }
}

// ── Auto-init ─────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', mountNavbar)
