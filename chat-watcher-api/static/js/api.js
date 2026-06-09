/**
 * api.js — Shared API client + utilities
 */

const API = '/api/v1'

// ── API fetch wrapper ─────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error || `HTTP ${res.status}`)
  }
  if (res.status === 204) return null
  return res.json()
}

// ── Toast ─────────────────────────────────────────────────────────────────────

function showToast(message, type = 'success') {
  let toast = document.getElementById('toast')
  if (!toast) {
    toast = document.createElement('div')
    toast.id = 'toast'
    document.body.appendChild(toast)
  }
  toast.textContent = message
  toast.className = `toast ${type}`
  toast.classList.remove('hidden')
  clearTimeout(toast._timer)
  toast._timer = setTimeout(() => toast.classList.add('hidden'), 3000)
}

// ── Error element helper ──────────────────────────────────────────────────────

/**
 * Show or hide an error message element.
 * Pass null/undefined to hide it.
 */
function showError(id, message) {
  const el = document.getElementById(id)
  if (!el) return
  if (message) {
    el.textContent = message
    el.classList.remove('hidden')
  } else {
    el.textContent = ''
    el.classList.add('hidden')
  }
}

// ── Modal helpers ─────────────────────────────────────────────────────────────

function openModal(id) {
  document.getElementById(id)?.classList.remove('hidden')
}

function closeModal(id) {
  document.getElementById(id)?.classList.add('hidden')
}

// ── Formatters ────────────────────────────────────────────────────────────────

function fmtNum(val, decimals = 1) {
  if (val == null || isNaN(val)) return '—'
  return Number(val).toFixed(decimals)
}

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function statusBadge(status) {
  const map = {
    pending:    'badge-yellow',
    processing: 'badge-blue',
    done:       'badge-green',
    error:      'badge-red',
  }
  return `<span class="badge ${map[status] || 'badge-gray'}">${status}</span>`
}

// ── URL helpers ───────────────────────────────────────────────────────────────

function getStreamID() {
  return new URLSearchParams(window.location.search).get('id')
}
