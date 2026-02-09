/*
 * Shared frontend utilities for Logistics Neon.
 * This file is intentionally framework-free and exposes a small global API
 * consumed by page-specific code in `app.js`.
 */

// -----------------------------
// HTML + cookie helpers
// -----------------------------
function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function getCookie(name) {
  const cookie = `; ${document.cookie || ''}`;
  const parts = cookie.split(`; ${name}=`);
  if (parts.length !== 2) return '';
  return parts.pop().split(';').shift() || '';
}

// -----------------------------
// Auth-aware fetch wrapper
// -----------------------------
const CSRF_COOKIE_NAME = window.CSRF_COOKIE_NAME || 'csrf_token';
const CSRF_HEADER_NAME = window.CSRF_HEADER_NAME || 'X-CSRF-Token';
const nativeFetch = window.fetch.bind(window);

function apiFetch(input, init = {}) {
  const opts = { ...init };
  const inputMethod = typeof input === 'object' && input?.method ? input.method : 'GET';
  const method = String(opts.method || inputMethod || 'GET').toUpperCase();

  if (!opts.credentials) {
    opts.credentials = 'same-origin';
  }

  // Attach CSRF header for unsafe methods.
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    const token = getCookie(CSRF_COOKIE_NAME);
    const headers = new Headers(opts.headers || {});
    if (token && !headers.has(CSRF_HEADER_NAME)) {
      headers.set(CSRF_HEADER_NAME, token);
    }
    opts.headers = headers;
  }

  return nativeFetch(input, opts);
}

function isManagerOrAdminUser() {
  const role = String(window.USER_ROLE || '').toLowerCase();
  return role === 'manager' || role === 'admin';
}

// -----------------------------
// Time formatting + validation
// -----------------------------
function parseTimeToMinutes(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const parts = raw.split(':');
  if (parts.length < 2) return null;
  const hh = Number(parts[0]);
  const mm = Number(parts[1]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return null;
  if (hh < 0 || hh > 23 || mm < 0 || mm > 59) return null;
  return (hh * 60) + mm;
}

function validateWindowValues(startVal, endVal) {
  const startRaw = String(startVal || '').trim();
  const endRaw = String(endVal || '').trim();
  const startMin = startRaw ? parseTimeToMinutes(startRaw) : null;
  const endMin = endRaw ? parseTimeToMinutes(endRaw) : null;

  if (startRaw && startMin === null) {
    return { ok: false, message: 'Invalid window start time.' };
  }
  if (endRaw && endMin === null) {
    return { ok: false, message: 'Invalid window end time.' };
  }
  if (startMin !== null && endMin !== null && startMin > endMin) {
    return { ok: false, message: 'Window start must be earlier than or equal to window end.' };
  }
  return { ok: true, message: '' };
}

function formatDistance(meters) {
  const km = Number(meters || 0) / 1000;
  return `${km.toFixed(1)} km`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  if (hours <= 0) return `${minutes} min`;
  return `${hours}h ${minutes}m`;
}

function formatClockTimeFromSeconds(seconds, offsetSeconds = 0) {
  if (!Number.isFinite(seconds)) return '--:--';
  const total = Math.max(0, Number(seconds) + offsetSeconds);
  const hours = Math.floor(total / 3600) % 24;
  const minutes = Math.floor((total % 3600) / 60);
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
}

// -----------------------------
// Table rendering helpers
// -----------------------------
function renderDeliveriesTable(target, deliveries, options = {}) {
  if (!target) return;
  if (!Array.isArray(deliveries) || deliveries.length === 0) {
    target.innerHTML = `<div class="empty-state">No deliveries found.</div>`;
    return;
  }

  const { showActions = false } = options;
  const formatWindow = (start, end) => {
    const s = String(start || '').trim();
    const e = String(end || '').trim();
    if (!s && !e) return '--';
    if (!s) return `By ${e}`;
    if (!e) return `From ${s}`;
    return `${s}-${e}`;
  };

  const rows = deliveries.map(d => {
    const deliveryId = String(d.id || '').trim();
    const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
    const editHref = `/deliveries/${encodeURIComponent(deliveryId)}?next=${nextParam}`;
    const deleteAction = `/deliveries/${encodeURIComponent(deliveryId)}/delete?next=${nextParam}`;
    const salesOrderRaw = String(d.sales_order || '').trim();
    const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
    const actions = showActions ? `
      <td data-label="Actions">
        <a class="btn btn-secondary icon-only" href="${editHref}" title="Edit" aria-label="Edit">
          <span class="btn-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" />
            </svg>
          </span>
          <span class="sr-only">Edit</span>
        </a>
        <form method="post" action="${deleteAction}" style="display:inline;" onsubmit="return confirm('Delete delivery ${escapeHtml(deliveryId)}?');">
          <input type="hidden" name="csrf_token" value="${escapeHtml(getCookie(CSRF_COOKIE_NAME))}">
          <button class="btn btn-secondary icon-only" type="submit" title="Delete" aria-label="Delete">
            <span class="btn-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M3 6h18" />
                <path d="M8 6V4h8v2" />
                <path d="M6 6l1 14h10l1-14" />
              </svg>
            </span>
            <span class="sr-only">Delete</span>
          </button>
        </form>
      </td>
    ` : '';

    return `
    <tr>
      <td data-label="Delivery ID">${escapeHtml(deliveryId)}</td>
      <td data-label="Jobsite">
        <div class="cell-stack">
          <div class="cell-title">${escapeHtml(d.job_name)}</div>
          <div class="cell-sub">${escapeHtml(salesOrderText)}</div>
        </div>
      </td>
      <td data-label="Time Window">${escapeHtml(formatWindow(d.window_start, d.window_end))}</td>
      <td data-label="Cargo">
        <div class="cell-metrics">
          <span>Med skids: ${Number(d.med_skid ?? d.skid ?? 0)}</span>
          <span>Small skids: ${Number(d.small_skid || 0)}</span>
          <span>Large skids: ${Number(d.large_skid || 0)}</span>
          <span>Doors: ${Number(d.door || 0)}</span>
          <span>Frames: ${Number(d.frame || 0)}</span>
          <span>Exp Frames: ${Number(d.exp_frame || 0)}</span>
          <span>Boxes: ${Number(d.box || 0)}</span>
        </div>
      </td>
      ${actions}
    </tr>
  `;
  }).join('');

  const actionsHeader = showActions ? `<th>Actions</th>` : '';
  target.innerHTML = `
    <table data-sortable="true" data-responsive="true">
      <thead>
        <tr>
          <th class="sortable" data-key="delivery_id" data-type="text">Delivery ID</th>
          <th class="sortable" data-key="job_name" data-type="text">Jobsite</th>
          <th class="sortable" data-key="window" data-type="text">Time Window</th>
          <th class="sortable" data-key="med_skid" data-type="number">Cargo</th>
          ${actionsHeader}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function sortTable(table, colIndex, type, direction) {
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  const multiplier = direction === 'desc' ? -1 : 1;

  rows.sort((a, b) => {
    const aText = a.cells[colIndex]?.textContent?.trim() ?? '';
    const bText = b.cells[colIndex]?.textContent?.trim() ?? '';
    if (type === 'number') {
      const aNum = Number(aText || 0);
      const bNum = Number(bText || 0);
      return (aNum - bNum) * multiplier;
    }
    return aText.localeCompare(bText) * multiplier;
  });

  for (const row of rows) {
    tbody.appendChild(row);
  }
}

function enableSorting(root = document) {
  root.addEventListener('click', (event) => {
    const th = event.target.closest('th.sortable');
    if (!th) return;
    const table = th.closest('table');
    if (!table) return;

    const colIndex = th.cellIndex;
    const type = th.dataset.type || 'text';
    const currentCol = table.dataset.sortCol;
    const currentDir = table.dataset.sortDir || 'asc';
    const nextDir = (String(currentCol) === String(colIndex) && currentDir === 'asc') ? 'desc' : 'asc';

    table.dataset.sortCol = String(colIndex);
    table.dataset.sortDir = nextDir;
    sortTable(table, colIndex, type, nextDir);
  }, { passive: true });
}
