// Core helpers are loaded from static/app.core.js
// (escapeHtml, getCookie, apiFetch, render helpers, formatting, sorting).

const SHIFT_START_SECONDS = 6 * 3600;
let lastPlanData = null;
let lastPublishedPlan = null;
let customStopTarget = null;
let customDragContext = null;
let planMap = null;
let planMapReady = false;
let planMapMarkers = [];
let planMapUpdateTimer = null;
const MED_SKID_UNITS = 20;
const SMALL_SKID_UNITS = 10;
const LARGE_SKID_UNITS = 25;

function getMedSkid(delivery) {
  return Number(delivery?.med_skid ?? delivery?.skid ?? 0);
}

function getSmallSkid(delivery) {
  return Number(delivery?.small_skid ?? 0);
}

function getLargeSkid(delivery) {
  return Number(delivery?.large_skid ?? 0);
}

function getCapacityUnits(delivery) {
  return (
    (getMedSkid(delivery) * MED_SKID_UNITS)
    + (getSmallSkid(delivery) * SMALL_SKID_UNITS)
    + (getLargeSkid(delivery) * LARGE_SKID_UNITS)
  );
}

function extractScheduledIds(routes) {
  const ids = new Set();
  for (const route of (routes || [])) {
    for (const stop of (route.stops || [])) {
      for (const item of (stop || [])) {
        if (item && item.type === 'delivery' && item.id !== undefined && item.id !== null) {
          ids.add(String(item.id).trim());
        }
      }
    }
  }
  return ids;
}

// -----------------------------
// Route card rendering + editing
// -----------------------------
function buildRouteCards(routes, deliveries, options = {}) {
  if (!Array.isArray(routes) || routes.length === 0) {
    return `<div class="empty-state">No routes returned.</div>`;
  }

  const { draggable = true, driverEditable = true, allowCustomStops = true } = options;

  const byId = new Map();
  for (const d of (deliveries || [])) {
    const key = String(d?.id ?? d?.delivery_id ?? d?.record_id ?? '').trim();
    if (key) byId.set(key, d);
  }

  const cards = routes.map((route, idx) => {
    const stopCount = (route.stops || []).length;
    let timeIndex = 0;
    const routeStops = (route.stops || []).map((stop, stopIndex) => {
      let stopLat = null;
      let stopLon = null;
      const isCustomStop = Array.isArray(stop) && stop.length > 0 && stop.every(item => item && item.type === 'custom');
      const customItem = isCustomStop ? (stop[0] || {}) : null;
      const customTimeMeta = isCustomStop && customItem ? {
        arrival_s: customItem.arrival_s,
        departure_s: customItem.departure_s,
        arrival_hhmm: customItem.arrival_hhmm,
        departure_hhmm: customItem.departure_hhmm,
      } : null;
      const timeMeta = isCustomStop
        ? customTimeMeta
        : (Array.isArray(route.stop_times) ? route.stop_times[timeIndex] : null);
      if (!isCustomStop && Array.isArray(route.stop_times)) {
        timeIndex += 1;
      }
      const hasTimes = timeMeta && (timeMeta.arrival_s !== undefined || timeMeta.departure_s !== undefined || timeMeta.arrival_hhmm || timeMeta.departure_hhmm);
      const arrival = Number.isFinite(timeMeta?.arrival_s)
        ? formatClockTimeFromSeconds(timeMeta.arrival_s, SHIFT_START_SECONDS)
        : escapeHtml(timeMeta?.arrival_hhmm || '--:--');
      const departure = Number.isFinite(timeMeta?.departure_s)
        ? formatClockTimeFromSeconds(timeMeta.departure_s, SHIFT_START_SECONDS)
        : escapeHtml(timeMeta?.departure_hhmm || '--:--');
      const canInsert = allowCustomStops && stopIndex < stopCount - 1;
      const actionsHtml = canInsert ? `
        <div class="route-stop-actions">
          <button class="btn btn-secondary icon-only btn-stop-action" type="button" data-add-stop title="Add stop" aria-label="Add stop">
            <span class="btn-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M12 5v14" />
                <path d="M5 12h14" />
              </svg>
            </span>
            <span class="sr-only">Add stop</span>
          </button>
        </div>
      ` : '';
      const timeHtml = hasTimes ? `
        <div class="route-stop-time">
          Arr ${arrival} • Dep ${departure}
        </div>
      ` : '';
      const nodeId = Array.isArray(route.stop_nodes) ? route.stop_nodes[stopIndex] : null;
      const arrivalS = Number.isFinite(timeMeta?.arrival_s) ? Math.round(Number(timeMeta.arrival_s)) : null;
      const departureS = Number.isFinite(timeMeta?.departure_s) ? Math.round(Number(timeMeta.departure_s)) : null;
      const timeAttrs = (arrivalS !== null && departureS !== null)
        ? ` data-arrival-s="${arrivalS}" data-departure-s="${departureS}"`
        : '';
      if (Array.isArray(stop) && stop.length === 1 && stop[0].type === 'depot') {
      const depotLat = Number(window.DEPOT_LAT);
      const depotLon = Number(window.DEPOT_LON);
      if (Number.isFinite(depotLat) && Number.isFinite(depotLon)) {
        stopLat = depotLat;
        stopLon = depotLon;
      }
      const coordAttrs = (Number.isFinite(stopLat) && Number.isFinite(stopLon))
        ? ` data-lat="${stopLat}" data-lon="${stopLon}"`
        : '';
      return `
        <li class="route-stop is-depot" data-draggable="false" data-node="${nodeId ?? 0}"${coordAttrs}${timeAttrs}>
          <div class="route-stop-title">Stop ${stopIndex + 1}: Depot</div>
          ${timeHtml}
          ${actionsHtml}
        </li>
      `;
      }

      const items = (stop || []).map((item) => {
        if (!item || item.type === 'depot') {
          return `<div class="route-item">Depot</div>`;
        }
        if (item.type === 'custom') {
          if (Number.isFinite(item.lat) && Number.isFinite(item.lon)) {
            stopLat = Number(item.lat);
            stopLon = Number(item.lon);
          }
          const label = escapeHtml(item.label || 'Custom stop');
          const address = escapeHtml(item.address || '');
          const labelAttr = escapeHtml(item.label || '');
          const addrAttr = escapeHtml(item.address || '');
          const latAttr = Number.isFinite(item.lat) ? ` data-lat="${Number(item.lat)}"` : '';
          const lonAttr = Number.isFinite(item.lon) ? ` data-lon="${Number(item.lon)}"` : '';
          const svcAttr = Number.isFinite(item.service_minutes) ? ` data-service-minutes="${Number(item.service_minutes)}"` : '';
          return `
            <div class="route-item" data-custom="1" data-label="${labelAttr}" data-address="${addrAttr}"${latAttr}${lonAttr}${svcAttr}>
              <div class="route-main">${label}</div>
              ${address ? `<div class="route-sub">${address}</div>` : ''}
              <div class="route-metrics">
                <span>Custom stop</span>
              </div>
            </div>
          `;
        }
        const key = String(item.id || '').trim();
        const d = byId.get(key);
        if (d && Number.isFinite(Number(d.lat)) && Number.isFinite(Number(d.lon))) {
          stopLat = Number(d.lat);
          stopLon = Number(d.lon);
        }
        const medSkids = item.qty !== undefined ? item.qty : getMedSkid(d);
        const smallSkids = item.qty !== undefined ? 0 : getSmallSkid(d);
        const largeSkids = item.qty !== undefined ? 0 : getLargeSkid(d);
        const units = Number.isFinite(Number(item.units)) ? Number(item.units) : null;
        const salesOrderRaw = String(d?.sales_order || '').trim();
        const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
        const addressText = String(d?.job_address || '').trim();
        const partAttr = item.part_no !== undefined ? ` data-part-no="${Number(item.part_no)}"` : '';
        const qtyAttr = item.qty !== undefined ? ` data-qty="${Number(item.qty)}"` : '';
        const unitsAttr = Number.isFinite(units) ? ` data-units="${units}"` : '';
        const idAttr = key ? ` data-delivery-id="${escapeHtml(key)}"` : '';

        return `
          <div class="route-item"${idAttr}${partAttr}${qtyAttr}${unitsAttr}>
            <div class="route-main">${escapeHtml(d?.job_name || item.job_name || '')}</div>
            ${addressText ? `<div class="route-sub">${escapeHtml(addressText)}</div>` : ''}
            <div class="route-sub">${escapeHtml(salesOrderText)}</div>
            <div class="route-metrics">
              <span>Med skids: ${Number(medSkids || 0)}</span>
              <span>Small skids: ${Number(smallSkids || 0)}</span>
              <span>Large skids: ${Number(largeSkids || 0)}</span>
              ${Number.isFinite(units) ? `<span>Units: ${units}</span>` : ''}
              <span>Doors: ${Number(d?.door || 0)}</span>
              <span>Frames: ${Number(d?.frame || 0)}</span>
              <span>Exp Frames: ${Number(d?.exp_frame || 0)}</span>
              <span>Boxes: ${Number(d?.box || 0)}</span>
            </div>
          </div>
        `;
      }).join('');

      const dragAttr = draggable ? ' data-draggable="true" draggable="true"' : ' data-draggable="false"';
      const stopClass = isCustomStop ? 'route-stop custom-stop' : 'route-stop';
      const stopTitle = isCustomStop ? `Stop ${stopIndex + 1}: Custom` : `Stop ${stopIndex + 1}`;
      const coordAttrs = (Number.isFinite(stopLat) && Number.isFinite(stopLon))
        ? ` data-lat="${stopLat}" data-lon="${stopLon}"`
        : '';
      const serviceMinutes = Number.isFinite(customItem?.service_minutes) ? Number(customItem.service_minutes) : null;
      const serviceAttr = Number.isFinite(serviceMinutes) ? ` data-service-minutes="${serviceMinutes}"` : '';
      return `
        <li class="${stopClass}"${dragAttr} data-node="${nodeId ?? ''}"${coordAttrs}${timeAttrs}${serviceAttr}>
          <div class="route-stop-title">${stopTitle}</div>
          ${timeHtml}
          <div class="route-items">${items}</div>
          ${actionsHtml}
        </li>
      `;
    }).join('');

    const driverAttr = driverEditable ? '' : ' disabled aria-disabled="true"';
    const driverValue = typeof route.driver_profile === 'string'
      ? route.driver_profile
      : (route.driver_profile?.name || '');
    const drivers = Array.isArray(window.__DRIVERS__) ? window.__DRIVERS__ : [];
    const normalizedDrivers = drivers.map(d => String(d || '')).filter(Boolean);
    const driverValueNorm = String(driverValue || '').trim().toLowerCase();
    const hasDriverValue = driverValueNorm && normalizedDrivers.some(d => d.toLowerCase() === driverValueNorm);
    const skidBreakdown = computeSkidBreakdownFromStops(route.stops || [], byId);
    const driverOptions = [
      '<option value="">Unassigned</option>',
      ...(hasDriverValue
        ? []
        : (driverValue ? [`<option value="${escapeHtml(driverValue)}" selected>${escapeHtml(driverValue)}</option>`] : [])),
      ...normalizedDrivers.map((name) => {
        const nameNorm = String(name || '').trim();
        const selected = driverValueNorm && nameNorm.toLowerCase() === driverValueNorm ? ' selected' : '';
        return `<option value="${escapeHtml(nameNorm)}"${selected}>${escapeHtml(nameNorm)}</option>`;
      }),
    ].join('');
    return `
      <section class="route-card" data-route-id="${route.vehicle_id ?? (idx + 1)}" data-vehicle-type="${escapeHtml(route.vehicle_type || 'truck')}">
        <div class="route-header">
          <div>
            <h3>Vehicle ${route.vehicle_id ?? (idx + 1)}</h3>
            <p class="card-subtitle">${escapeHtml(route.vehicle_type || 'vehicle')}</p>
          </div>
          <div class="route-pill-group">
            <span class="pill">Med ${Number(skidBreakdown.med || 0)}</span>
            <span class="pill">Small ${Number(skidBreakdown.small || 0)}</span>
            <span class="pill">Large ${Number(skidBreakdown.large || 0)}</span>
            <span class="pill pill-muted" data-total-distance data-total-distance-m="${Number(route.total_distance_m || 0)}">${formatDistance(route.total_distance_m)}</span>
            <span class="pill pill-muted" data-total-time data-total-time-s="${Number(route.total_time_s || 0)}">${formatDuration(route.total_time_s)}</span>
            <span class="pill pill-muted route-time-note">Times stale — rerun plan</span>
          </div>
        </div>
        <div class="route-driver">
          <label>Driver</label>
          <select class="driver-input" data-driver-select${driverAttr}>
            ${driverOptions}
          </select>
        </div>
        <ol class="route-stops">${routeStops}</ol>
      </section>
    `;
  }).join('');

  return `<div class="routes-grid">${cards}</div>`;
}

function renderRoutes(deliveries, routes) {
  const target = document.getElementById('routesOutput');
  if (!target) return;
  target.innerHTML = buildRouteCards(routes, deliveries, { draggable: true, driverEditable: true, allowCustomStops: true });
  enableRouteDragAndDrop(target);
  initDriverOptions();
  refreshDriverSelects();
  initPlanMap();
  updatePlanMapFromDom();
}

function getNextVehicleId() {
  const ids = Array.from(document.querySelectorAll('.route-card'))
    .map(card => Number(card.dataset.routeId))
    .filter(id => Number.isFinite(id));
  if (!ids.length) return 1;
  return Math.max(...ids) + 1;
}

function addVehicleToPlan(vehicleType = 'truck') {
  const output = document.getElementById('routesOutput');
  if (!output) return;
  let grid = output.querySelector('.routes-grid');
  if (!grid) {
    alert('Run a plan first to add vehicles.');
    return;
  }

  const route = {
    vehicle_id: getNextVehicleId(),
    vehicle_type: vehicleType,
    skids: 0,
    capacity_units: 0,
    total_distance_m: 0,
    total_time_s: 0,
    stops: [
      [{ type: 'depot', label: 'Depot' }],
      [{ type: 'depot', label: 'Depot' }],
    ],
    stop_nodes: [0, 0],
    stop_times: [],
  };

  const temp = document.createElement('div');
  temp.innerHTML = buildRouteCards([route], [], { draggable: true, driverEditable: true, allowCustomStops: true });
  const card = temp.querySelector('.route-card');
  if (!card) return;
  card.classList.add('times-stale');
  grid.appendChild(card);
  setPublishStatus('Edits pending. Publish to update.', false);
  refreshDriverSelects();
  updatePlanMapFromDom();
}

function renderRoutesPreview(deliveries, routes, targetId = 'publishedPreviewRoutes') {
  const target = document.getElementById(targetId);
  if (!target) return;
  const normalized = ensureRouteSkids(routes, deliveries);
  target.innerHTML = buildRouteCards(normalized, deliveries, { draggable: false, driverEditable: false, allowCustomStops: false });
  initPlanMap();
  updatePlanMapFromDom();
}

function renderPublishedRoutesEditable(deliveries, routes) {
  const target = document.getElementById('publishedPreviewRoutes');
  if (!target) return;
  const normalized = ensureRouteSkids(routes, deliveries);
  target.innerHTML = buildRouteCards(normalized, deliveries, { draggable: true, driverEditable: true, allowCustomStops: true });
  enableRouteDragAndDrop(target);
  initDriverOptions();
  refreshDriverSelects();
  initPlanMap();
  updatePlanMapFromDom();
}

function createCustomStopElement(label, address, lat = null, lon = null, serviceMinutes = 0) {
  const safeLabel = escapeHtml(label || 'Custom stop');
  const safeAddress = escapeHtml(address || '');
  const li = document.createElement('li');
  li.className = 'route-stop custom-stop';
  li.setAttribute('data-draggable', 'true');
  li.setAttribute('draggable', 'true');
  li.setAttribute('data-node', '');
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    li.dataset.lat = String(lat);
    li.dataset.lon = String(lon);
  }
  if (Number.isFinite(serviceMinutes)) {
    li.dataset.serviceMinutes = String(serviceMinutes);
  }
  li.innerHTML = `
    <div class="route-stop-title">Custom Stop</div>
    <div class="route-items">
      <div class="route-item" data-custom="1" data-label="${safeLabel}" data-address="${safeAddress}"${Number.isFinite(lat) ? ` data-lat="${lat}"` : ''}${Number.isFinite(lon) ? ` data-lon="${lon}"` : ''}${Number.isFinite(serviceMinutes) ? ` data-service-minutes="${serviceMinutes}"` : ''}>
        <div class="route-main">${safeLabel}</div>
        ${safeAddress ? `<div class="route-sub">${safeAddress}</div>` : ''}
        <div class="route-metrics">
          <span>Custom stop</span>
        </div>
      </div>
    </div>
    <div class="route-stop-actions">
      <button class="btn btn-secondary icon-only btn-stop-action" type="button" data-add-stop title="Add stop" aria-label="Add stop">
        <span class="btn-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 5v14" />
            <path d="M5 12h14" />
          </svg>
        </span>
        <span class="sr-only">Add stop</span>
      </button>
      <button class="btn btn-secondary icon-only btn-stop-action" type="button" data-remove-stop title="Remove stop" aria-label="Remove stop">
        <span class="btn-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M3 6h18" />
            <path d="M8 6V4h8v2" />
            <path d="M6 6l1 14h10l1-14" />
          </svg>
        </span>
        <span class="sr-only">Remove stop</span>
      </button>
    </div>
  `;
  return li;
}

function getStopCoords(stopEl) {
  if (!stopEl) return null;
  const lat = Number(stopEl.dataset.lat);
  const lon = Number(stopEl.dataset.lon);
  if (Number.isFinite(lat) && Number.isFinite(lon)) {
    return { lat, lon };
  }
  return null;
}

function getStopTimes(stopEl) {
  if (!stopEl) return null;
  const arrival = Number(stopEl.dataset.arrivalS);
  const departure = Number(stopEl.dataset.departureS);
  if (Number.isFinite(arrival) && Number.isFinite(departure)) {
    return { arrival, departure };
  }
  return null;
}

function getAdjacentStops(stopEl) {
  if (!stopEl) return { prev: null, next: null };
  let prev = stopEl.previousElementSibling;
  while (prev && !prev.classList.contains('route-stop')) {
    prev = prev.previousElementSibling;
  }
  let next = stopEl.nextElementSibling;
  while (next && !next.classList.contains('route-stop')) {
    next = next.nextElementSibling;
  }
  return { prev, next };
}

async function recomputeCustomStops(card) {
  if (!card) return;
  const customStops = Array.from(card.querySelectorAll('.route-stop.custom-stop'));
  if (!customStops.length) return;
  const vehicleType = card.dataset?.vehicleType || 'truck';
  for (const stopEl of customStops) {
    const { prev, next } = getAdjacentStops(stopEl);
    if (!prev || !next) {
      card.classList.add('times-stale');
      continue;
    }
    const prevCoords = getStopCoords(prev);
    const nextCoords = getStopCoords(next);
    const customCoords = getStopCoords(stopEl);
    if (!prevCoords || !nextCoords || !customCoords) {
      card.classList.add('times-stale');
      continue;
    }
    const serviceMinutes = getCustomServiceMinutes(stopEl);
    try {
      const estimate = await estimateCustomStop(
        prevCoords,
        { lat: customCoords.lat, lon: customCoords.lon },
        nextCoords,
        vehicleType,
        serviceMinutes
      );
      applyCustomStopTiming({
        prevStop: prev,
        nextStop: next,
        newStop: stopEl,
        estimate,
        vehicleType,
        serviceMinutes,
      });
    } catch (e) {
      console.error(e);
      card.classList.add('times-stale');
      return;
    }
  }
}

function getCustomServiceMinutes(stopEl) {
  if (!stopEl) return null;
  const direct = Number(stopEl.dataset.serviceMinutes);
  if (Number.isFinite(direct)) return direct;
  const item = stopEl.querySelector('.route-item[data-custom="1"]');
  if (!item) return null;
  const fromItem = Number(item.dataset.serviceMinutes);
  return Number.isFinite(fromItem) ? fromItem : null;
}

function setStopTimes(stopEl, arrival, departure) {
  if (!stopEl) return;
  const arr = Math.round(Number(arrival));
  const dep = Math.round(Number(departure));
  stopEl.dataset.arrivalS = String(arr);
  stopEl.dataset.departureS = String(dep);
  let timeEl = stopEl.querySelector('.route-stop-time');
  if (!timeEl) {
    timeEl = document.createElement('div');
    timeEl.className = 'route-stop-time';
    const titleEl = stopEl.querySelector('.route-stop-title');
    if (titleEl) {
      titleEl.insertAdjacentElement('afterend', timeEl);
    } else {
      stopEl.insertAdjacentElement('afterbegin', timeEl);
    }
  }
  const arrivalLabel = formatClockTimeFromSeconds(arr, SHIFT_START_SECONDS);
  const departureLabel = formatClockTimeFromSeconds(dep, SHIFT_START_SECONDS);
  timeEl.textContent = `Arr ${arrivalLabel} • Dep ${departureLabel}`;
}

function updateRouteTotals(card, deltaTimeS, deltaDistanceM) {
  if (!card) return;
  const timeEl = card.querySelector('[data-total-time]');
  const distEl = card.querySelector('[data-total-distance]');
  if (timeEl) {
    const current = Number(timeEl.dataset.totalTimeS || 0);
    const next = Math.max(0, current + deltaTimeS);
    timeEl.dataset.totalTimeS = String(next);
    timeEl.textContent = formatDuration(next);
  }
  if (distEl && Number.isFinite(deltaDistanceM)) {
    const current = Number(distEl.dataset.totalDistanceM || 0);
    const next = Math.max(0, current + deltaDistanceM);
    distEl.dataset.totalDistanceM = String(next);
    distEl.textContent = formatDistance(next);
  }
}

async function estimateCustomStop(prev, custom, next, vehicleType, serviceMinutes) {
  const payload = {
    prev,
    custom,
    next,
    vehicle_type: vehicleType || 'truck',
    service_minutes: Math.max(0, Number(serviceMinutes || 0)),
  };
  const res = await apiFetch('/plan/custom-stop/estimate', {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || !data?.ok) {
    throw new Error(data?.error || 'Estimate failed');
  }
  return data;
}

function applyCustomStopTiming({ prevStop, nextStop, newStop, estimate, vehicleType, serviceMinutes }) {
  const prevTimes = getStopTimes(prevStop);
  const nextTimes = getStopTimes(nextStop);
  if (!prevTimes || !nextTimes) {
    return false;
  }

  const truckFactor = 1.10;
  const timeFactor = (vehicleType || 'truck') === 'truck' ? truckFactor : 1.0;
  const tPrevCustom = Number(estimate.t_prev_custom || 0) * timeFactor;
  const tCustomNext = Number(estimate.t_custom_next || 0) * timeFactor;
  const tPrevNext = Number(estimate.t_prev_next || 0) * timeFactor;
  const svcSeconds = Math.max(0, Number(serviceMinutes || 0)) * 60;

  const oldTravel = nextTimes.arrival - prevTimes.departure;
  const newTravel = tPrevCustom + svcSeconds + tCustomNext;
  const delta = newTravel - oldTravel;

  const newArrival = prevTimes.departure + tPrevCustom;
  const newDeparture = newArrival + svcSeconds;
  setStopTimes(newStop, newArrival, newDeparture);

  let cursor = nextStop;
  while (cursor) {
    const times = getStopTimes(cursor);
    if (times) {
      setStopTimes(cursor, times.arrival + delta, times.departure + delta);
    }
    cursor = cursor.nextElementSibling;
  }

  const deltaDist = Number(estimate.d_prev_custom || 0)
    + Number(estimate.d_custom_next || 0)
    - Number(estimate.d_prev_next || 0);
  const card = prevStop.closest('.route-card');
  updateRouteTotals(card, delta, deltaDist);
  if (card) card.classList.remove('times-stale');
  return true;
}

function applyCustomStopRemoval({ prevStop, nextStop, estimate, vehicleType, serviceMinutes }) {
  const prevTimes = getStopTimes(prevStop);
  const nextTimes = getStopTimes(nextStop);
  if (!prevTimes || !nextTimes) {
    return false;
  }

  const truckFactor = 1.10;
  const timeFactor = (vehicleType || 'truck') === 'truck' ? truckFactor : 1.0;
  const tPrevCustom = Number(estimate.t_prev_custom || 0) * timeFactor;
  const tCustomNext = Number(estimate.t_custom_next || 0) * timeFactor;
  const tPrevNext = Number(estimate.t_prev_next || 0) * timeFactor;
  const svcSeconds = Math.max(0, Number(serviceMinutes || 0)) * 60;

  const oldTravel = tPrevCustom + svcSeconds + tCustomNext;
  const newTravel = tPrevNext;
  const delta = newTravel - oldTravel;

  let cursor = nextStop;
  while (cursor) {
    const times = getStopTimes(cursor);
    if (times) {
      setStopTimes(cursor, times.arrival + delta, times.departure + delta);
    }
    cursor = cursor.nextElementSibling;
  }

  const deltaDist = Number(estimate.d_prev_next || 0)
    - (Number(estimate.d_prev_custom || 0) + Number(estimate.d_custom_next || 0));
  const card = prevStop.closest('.route-card');
  updateRouteTotals(card, delta, deltaDist);
  if (card) card.classList.remove('times-stale');
  return true;
}

async function handleCustomStopMove(stopEl) {
  if (!stopEl) return false;
  const ctx = customDragContext;
  if (!ctx || ctx.stop !== stopEl) return false;
  const { prev: oldPrev, next: oldNext } = ctx;
  const { prev: newPrev, next: newNext } = getAdjacentStops(stopEl);
  if (!oldPrev || !oldNext || !newPrev || !newNext) return false;
  if (oldPrev === newPrev && oldNext === newNext) return true;

  const customCoords = getStopCoords(stopEl);
  if (!customCoords) return false;
  const oldPrevCoords = getStopCoords(oldPrev);
  const oldNextCoords = getStopCoords(oldNext);
  const newPrevCoords = getStopCoords(newPrev);
  const newNextCoords = getStopCoords(newNext);
  if (!oldPrevCoords || !oldNextCoords || !newPrevCoords || !newNextCoords) return false;

  const vehicleType = ctx.vehicleType || stopEl.closest('.route-card')?.dataset?.vehicleType || 'truck';
  const serviceMinutes = Number.isFinite(ctx.serviceMinutes) ? ctx.serviceMinutes : getCustomServiceMinutes(stopEl);

  let removalEstimate = null;
  try {
    removalEstimate = await estimateCustomStop(
      oldPrevCoords,
      { lat: customCoords.lat, lon: customCoords.lon },
      oldNextCoords,
      vehicleType,
      serviceMinutes
    );
  } catch (e) {
    console.error(e);
    return false;
  }
  if (!applyCustomStopRemoval({
    prevStop: oldPrev,
    nextStop: oldNext,
    estimate: removalEstimate,
    vehicleType,
    serviceMinutes,
  })) {
    return false;
  }

  let insertEstimate = null;
  try {
    insertEstimate = await estimateCustomStop(
      newPrevCoords,
      { lat: customCoords.lat, lon: customCoords.lon },
      newNextCoords,
      vehicleType,
      serviceMinutes
    );
  } catch (e) {
    console.error(e);
    return false;
  }

  if (!applyCustomStopTiming({
    prevStop: newPrev,
    nextStop: newNext,
    newStop: stopEl,
    estimate: insertEstimate,
    vehicleType,
    serviceMinutes,
  })) {
    return false;
  }
  return true;
}

function renderUnscheduled(deliveries, routes) {
  const scheduledIds = extractScheduledIds(routes);
  const unscheduled = (deliveries || []).filter((d) => {
    const key = String(d?.id ?? d?.delivery_id ?? d?.record_id ?? '').trim();
    return key ? !scheduledIds.has(key) : true;
  });
  renderDeliveriesTable(document.getElementById('unscheduled'), unscheduled, { showActions: false });
}

function renderPublishedPlansTable(target, items) {
  if (!target) return;
  if (!Array.isArray(items) || items.length === 0) {
    target.innerHTML = `<div class="empty-state">No plans found.</div>`;
    return;
  }

  const rows = items.map(item => {
    const date = escapeHtml(item.plan_date || '--');
    const updated = escapeHtml(item.updated_at || item.created_at || '--');
    const by = escapeHtml(item.published_by || '--');
    const status = escapeHtml(item.status || 'Draft');
    return `
      <tr>
        <td data-label="Plan Date">${date}</td>
        <td data-label="Plan Type">published</td>
        <td data-label="Updated">${updated}</td>
        <td data-label="Published By">${by}</td>
        <td data-label="Status">${status}</td>
        <td data-label="Actions">
          <button class="btn btn-secondary icon-only" type="button" data-view-plan="${date}" title="View" aria-label="View">
            <span class="btn-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d="M2 12s4-6 10-6 10 6 10 6-4 6-10 6-10-6-10-6" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            </span>
            <span class="sr-only">View</span>
          </button>
        </td>
      </tr>
    `;
  }).join('');

  target.innerHTML = `
    <table data-responsive="true">
      <thead>
        <tr>
          <th>Plan Date</th>
          <th>Plan Type</th>
          <th>Updated</th>
          <th>Published By</th>
          <th>Status</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function loadPublishedPlans() {
  const target = document.getElementById('publishedPlans');
  if (!target) return;
  try {
    const res = await apiFetch('/plan/published/list', { cache: 'no-store' });
    const data = await res.json();
    renderPublishedPlansTable(target, data.items || []);
  } catch (e) {
    console.error(e);
    target.innerHTML = `<div class="empty-state">Failed to load published plans.</div>`;
  }
}

async function savePublishedPlanEdits() {
  if (!lastPublishedPlan || !lastPublishedPlan.published) {
    alert('No published plan loaded.');
    return { ok: false };
  }
  const routes = buildPublishedRoutesFromDom(lastPublishedPlan.deliveries || []);
  const driverProfiles = collectDriverProfiles(routes);
  const payload = {
    plan_date: lastPublishedPlan.plan_date,
    routes,
    deliveries: lastPublishedPlan.deliveries || [],
    meta: lastPublishedPlan.meta || {},
    fleet: lastPublishedPlan.fleet || {},
    driver_profiles: driverProfiles,
  };
  const res = await apiFetch('/plan/publish', {
    method: 'POST',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!data || !data.ok) {
    throw new Error(data?.error || 'Save failed');
  }
  return { ok: true, plan_date: data.plan_date };
}

// -----------------------------
// Published plans page behavior
// -----------------------------
function initPublishedPlans() {
  const target = document.getElementById('publishedPlans');
  if (!target) return;

  const modal = document.getElementById('publishedPlanModal');
  const payloadEl = document.getElementById('publishedPlanPayload');
  const metaEl = document.getElementById('publishedPlanMeta');
  const previewMetaEl = document.getElementById('publishedPreviewMeta');
  const previewRoutes = document.getElementById('publishedPreviewRoutes');
  const jsonBtn = document.getElementById('btnPublishedJson');
  const listView = document.getElementById('publishedListView');
  const previewView = document.getElementById('publishedPreviewView');
  const backBtn = document.getElementById('btnPublishedBack');
  const editBtn = document.getElementById('btnPublishedEdit');
  const saveBtn = document.getElementById('btnPublishedSave');
  const cancelBtn = document.getElementById('btnPublishedCancel');
  const recalcBtn = document.getElementById('btnPublishedRecalc');
  let currentPlanDate = null;
  let editMode = false;

  const setView = (mode) => {
    if (listView) listView.classList.toggle('is-hidden', mode === 'preview');
    if (previewView) previewView.classList.toggle('is-hidden', mode !== 'preview');
  };

  const setEditMode = (enabled) => {
    editMode = Boolean(enabled);
    editBtn?.classList.toggle('is-hidden', editMode);
    saveBtn?.classList.toggle('is-hidden', !editMode);
    cancelBtn?.classList.toggle('is-hidden', !editMode);
    recalcBtn?.classList.toggle('is-hidden', !editMode);
    if (editMode && lastPublishedPlan?.published) {
      renderPublishedRoutesEditable(lastPublishedPlan.deliveries || [], lastPublishedPlan.routes || []);
    } else if (lastPublishedPlan?.published) {
      renderRoutesPreview(lastPublishedPlan.deliveries || [], lastPublishedPlan.routes || []);
    }
  };

  const close = () => {
    if (!modal) return;
    modal.classList.add('is-hidden');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
  };

  const open = () => {
    if (!modal) return;
    modal.classList.remove('is-hidden');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
  };

  const showPlan = async (planDate) => {
    try {
      const res = await apiFetch(`/plan/published?date=${encodeURIComponent(planDate)}`, { cache: 'no-store' });
      const data = await res.json();
      lastPublishedPlan = data;
      currentPlanDate = planDate;
      if (payloadEl) {
        payloadEl.textContent = JSON.stringify(data, null, 2);
      }
      if (metaEl) {
        metaEl.textContent = data.published
          ? `published • ${data.plan_date} • ${data.published_by || 'manager/admin'}`
          : 'Not published';
      }
      if (previewMetaEl) {
        previewMetaEl.textContent = data.published
          ? `published • ${data.plan_date} • ${data.published_by || 'manager/admin'}`
          : 'Not published';
      }
      if (!data.published) {
        if (previewRoutes) previewRoutes.innerHTML = `<div class="empty-state">No plan for this date/type.</div>`;
        setView('preview');
        setEditMode(false);
        return;
      }
      renderRoutesPreview(data.deliveries || [], data.routes || []);
      setView('preview');
      setEditMode(false);
    } catch (e) {
      console.error(e);
    }
  };

  target.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-view-plan]');
    if (!btn) return;
    const planDate = btn.getAttribute('data-view-plan');
    if (planDate) {
      showPlan(planDate);
    }
  });

  modal?.addEventListener('click', (event) => {
    if (event.target && event.target.matches('[data-modal-close]')) {
      close();
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal && !modal.classList.contains('is-hidden')) {
      close();
    }
  });

  backBtn?.addEventListener('click', () => {
    setView('list');
  });
  editBtn?.addEventListener('click', () => {
    if (!lastPublishedPlan?.published) return;
    setEditMode(true);
  });
  cancelBtn?.addEventListener('click', () => {
    if (!currentPlanDate) return;
    showPlan(currentPlanDate);
  });
  recalcBtn?.addEventListener('click', async () => {
    if (!editMode) return;
    recalcBtn.disabled = true;
    try {
      await recalcPlanTotals({ silent: false });
    } finally {
      recalcBtn.disabled = false;
    }
  });
  saveBtn?.addEventListener('click', async () => {
    if (!editMode) return;
    saveBtn.disabled = true;
    try {
      const result = await savePublishedPlanEdits();
      if (result.ok) {
        await showPlan(result.plan_date || currentPlanDate);
      }
    } catch (e) {
      console.error(e);
      alert('Save failed. Check DevTools Console/Network.');
    } finally {
      saveBtn.disabled = false;
    }
  });

  document.getElementById('btnPublishedRefresh')?.addEventListener('click', loadPublishedPlans);
  jsonBtn?.addEventListener('click', () => {
    if (!lastPublishedPlan) return;
    if (payloadEl) {
      payloadEl.textContent = JSON.stringify(lastPublishedPlan, null, 2);
    }
    if (metaEl) {
      metaEl.textContent = lastPublishedPlan.published
        ? `Published by ${lastPublishedPlan.published_by || 'manager/admin'} - ${lastPublishedPlan.plan_date}`
        : 'Not published';
    }
    open();
  });
  loadPublishedPlans();
  setView('list');
}

// -----------------------------
// Plan generation + publish flow
// -----------------------------

function setRoutesVisible(show) {
  const routesCard = document.getElementById('routesCard');
  if (routesCard) routesCard.classList.toggle('is-hidden', !show);
}

function setUnscheduledVisible(show) {
  const unscheduledCard = document.getElementById('unscheduledCard');
  if (unscheduledCard) unscheduledCard.classList.toggle('is-hidden', !show);
}

function setPlanMode(enabled) {
  document.body.classList.toggle('plan-mode', enabled);
}

function getRoutingMode() {
  return 'auto';
}

function applyRoutingModeUI() {
  const publishBtn = document.getElementById('btnPlanPublish');

  if (publishBtn) {
    publishBtn.title = 'Publish Plan';
    publishBtn.setAttribute('aria-label', 'Publish Plan');
    publishBtn.disabled = !(lastPlanData?.routes?.length);
  }
  if (lastPlanData?.routes?.length) {
    setRoutesVisible(true);
    setUnscheduledVisible(true);
    setPlanMode(true);
    setPublishStatus('Draft ready. Publish when confirmed.', false);
  } else {
    setRoutesVisible(false);
    setUnscheduledVisible(false);
    setPlanMode(false);
    setPublishStatus('Not published.', false);
  }
}

function setPublishStatus(message, isPublished = false) {
  const statusEl = document.getElementById('publishStatus');
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.classList.toggle('is-published', Boolean(isPublished));
  statusEl.classList.toggle('pill-muted', !isPublished);
}

function setLoading(isLoading) {
  const loadingEl = document.getElementById('routesLoading');
  const outputEl = document.getElementById('routesOutput');
  setRoutesVisible(true);
  if (loadingEl) loadingEl.classList.toggle('is-hidden', !isLoading);
  if (outputEl) outputEl.classList.toggle('is-hidden', isLoading);
  if (isLoading && outputEl) outputEl.innerHTML = '';
  setUnscheduledVisible(false);
}

function updateStopLabels(listEl) {
  if (!listEl) return;
  const stops = listEl.querySelectorAll('.route-stop');
  stops.forEach((stop, index) => {
    const title = stop.querySelector('.route-stop-title');
    if (!title) return;
    if (stop.classList.contains('is-depot')) {
      title.textContent = `Stop ${index + 1}: Depot`;
    } else if (stop.classList.contains('custom-stop')) {
      title.textContent = `Stop ${index + 1}: Custom`;
    } else {
      title.textContent = `Stop ${index + 1}`;
    }
  });
}

let recalcTimer = null;

function scheduleRecalc() {
  if (recalcTimer) {
    clearTimeout(recalcTimer);
  }
  recalcTimer = setTimeout(() => {
    recalcPlanTotals({ silent: true });
  }, 250);
}

function enableRouteDragAndDrop(root) {
  let dragging = null;

  const pruneEmptyRoutes = () => {
    root.querySelectorAll('.route-card').forEach((card) => {
      const hasDelivery = card.querySelector('.route-item[data-delivery-id]');
      if (!hasDelivery) {
        card.remove();
      }
    });
  };

  root.addEventListener('dragstart', (event) => {
    const item = event.target.closest('.route-stop[data-draggable="true"]');
    if (!item) return;
    dragging = item;
    item.classList.add('is-dragging');
    event.dataTransfer.effectAllowed = 'move';
    if (item.classList.contains('custom-stop')) {
      const adj = getAdjacentStops(item);
      const card = item.closest('.route-card');
      customDragContext = {
        stop: item,
        prev: adj.prev,
        next: adj.next,
        card,
        vehicleType: card?.dataset?.vehicleType || 'truck',
        serviceMinutes: getCustomServiceMinutes(item),
      };
    } else {
      customDragContext = null;
    }
  });

  root.addEventListener('dragend', () => {
    if (dragging) {
      dragging.classList.remove('is-dragging');
    }
    dragging = null;
    customDragContext = null;
    root.querySelectorAll('.route-stop.is-over').forEach(el => el.classList.remove('is-over'));
  });

  root.addEventListener('dragover', (event) => {
    if (!dragging) return;
    const list = event.target.closest('.route-stops');
    if (!list) return;
    event.preventDefault();
    const after = event.target.closest('.route-stop');
    if (after && after !== dragging) {
      const rect = after.getBoundingClientRect();
      const shouldInsertAfter = event.clientY > rect.top + rect.height / 2;
      list.insertBefore(dragging, shouldInsertAfter ? after.nextSibling : after);
      root.querySelectorAll('.route-stop.is-over').forEach(el => el.classList.remove('is-over'));
      after.classList.add('is-over');
    } else if (!after) {
      list.appendChild(dragging);
    }
  });

  root.addEventListener('drop', (event) => {
    if (!dragging) return;
    event.preventDefault();
    const list = event.target.closest('.route-stops');
    if (!list) return;
    updateStopLabels(list);
    const otherLists = root.querySelectorAll('.route-stops');
    otherLists.forEach(updateStopLabels);
    const card = list.closest('.route-card');
    if (dragging.classList.contains('custom-stop')) {
      handleCustomStopMove(dragging).then((handled) => {
        if (handled) {
          if (card) card.classList.remove('times-stale');
          setPublishStatus('Edits pending. Publish to update.', false);
        } else {
          if (card) card.classList.add('times-stale');
          scheduleRecalc();
          setPublishStatus('Edits pending. Publish to update.', false);
        }
        pruneEmptyRoutes();
        updatePlanMapFromDom();
      });
      return;
    }
    pruneEmptyRoutes();
    if (card) card.classList.add('times-stale');
    scheduleRecalc();
    setPublishStatus('Edits pending. Publish to update.', false);
    updatePlanMapFromDom();
  });
}

function getPlanParams() {
  const params = new URLSearchParams(window.location.search || '');
  return {
    trucks: Number(params.get('trucks') || 0),
    vans: Number(params.get('vans') || 0),
    hasParams: params.has('trucks') || params.has('vans'),
  };
}

function readFleetInputs() {
  return {
    trucks: Number(document.getElementById('fleetTrucks')?.value || 0),
    vans: Number(document.getElementById('fleetVans')?.value || 0),
  };
}

async function makePlanWithFleet(trucks, vans) {
  try {
    setPlanMode(true);
    setLoading(true);
    const payload = { trucks, vans };
    const res = await apiFetch('/plan/tomorrow', {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    lastPlanData = data;
    renderRoutes(data.deliveries || [], data.routes || []);
    renderUnscheduled(data.deliveries || [], data.routes || []);
    setLoading(false);
    setUnscheduledVisible(true);
    const publishBtn = document.getElementById('btnPlanPublish');
    if (publishBtn) publishBtn.disabled = false;
    setPublishStatus('Draft ready. Publish when confirmed.');
    applyRoutingModeUI();
    initPlanMap();
    updatePlanMapFromDom();
  } catch (e) {
    console.error(e);
    setLoading(false);
    setRoutesVisible(false);
    setUnscheduledVisible(false);
    setPlanMode(false);
    alert("Generate Plan failed. Check DevTools Console/Network.");
  }
}

function goToPlanPage() {
  window.location.href = '/plan';
}

async function runPlanRefreshAction() {
  const { trucks, vans } = readFleetInputs();
  await makePlanWithFleet(trucks, vans);
}

async function runPlanSaveAction() {
  await publishPlan();
}

function getRecalcPayload(options = {}) {
  const { excludeCustomStops = false } = options;
  const cards = document.querySelectorAll('.route-card');
  const payload = [];
  const skipped = [];
  Array.from(cards).forEach(card => {
    const vehicleId = card.dataset.routeId ? Number(card.dataset.routeId) : null;
    const vehicleType = (card.dataset.vehicleType || 'truck').toLowerCase();
    const nodes = Array.from(card.querySelectorAll('.route-stop'))
      .map(stop => Number(stop.dataset.node))
      .filter(n => Number.isFinite(n) && n !== 0);
    if (!nodes.length) {
      skipped.push(card);
      return;
    }
    payload.push({ vehicle_id: vehicleId, vehicle_type: vehicleType, stop_nodes: nodes });
  });
  return { payload, skipped };
}

function buildPublishedRoutesFromDom(deliveriesOverride = null) {
  const deliveriesSource = deliveriesOverride || lastPlanData?.deliveries || lastPublishedPlan?.deliveries || [];
  const deliveriesById = new Map(
    (deliveriesSource || [])
      .map((d) => [String(d?.id ?? d?.delivery_id ?? d?.record_id ?? '').trim(), d])
      .filter(([key]) => Boolean(key))
  );
  const cards = document.querySelectorAll('.route-card');
  return Array.from(cards).map(card => {
    const vehicleId = card.dataset.routeId ? Number(card.dataset.routeId) : null;
    const vehicleType = (card.dataset.vehicleType || 'truck').toLowerCase();
    const driverName = card.querySelector('.driver-input')?.value?.trim() || '';
    const timeEl = card.querySelector('[data-total-time]');
    const distEl = card.querySelector('[data-total-distance]');
    const totalTimeS = Number(timeEl?.dataset.totalTimeS || 0);
    const totalDistanceM = Number(distEl?.dataset.totalDistanceM || 0);
    const stops = Array.from(card.querySelectorAll('.route-stop')).map(stop => {
      const stopArr = Number(stop.dataset.arrivalS);
      const stopDep = Number(stop.dataset.departureS);
      const stopArrVal = Number.isFinite(stopArr) ? stopArr : null;
      const stopDepVal = Number.isFinite(stopDep) ? stopDep : null;
      if (stop.classList.contains('is-depot')) {
        return [{ type: 'depot', label: 'Depot' }];
      }
      const items = Array.from(stop.querySelectorAll('.route-item')).map(itemEl => {
        if (itemEl.dataset.custom === '1') {
          const entry = {
            type: 'custom',
            label: String(itemEl.dataset.label || '').trim(),
            address: String(itemEl.dataset.address || '').trim(),
            lat: itemEl.dataset.lat ? Number(itemEl.dataset.lat) : undefined,
            lon: itemEl.dataset.lon ? Number(itemEl.dataset.lon) : undefined,
            service_minutes: itemEl.dataset.serviceMinutes ? Number(itemEl.dataset.serviceMinutes) : undefined,
          };
          if (stopArrVal !== null) {
            entry.arrival_s = stopArrVal;
          }
          if (stopDepVal !== null) {
            entry.departure_s = stopDepVal;
          }
          return entry;
        }
        if (itemEl.dataset.deliveryId) {
          const id = String(itemEl.dataset.deliveryId || '').trim();
          const partNo = itemEl.dataset.partNo ? Number(itemEl.dataset.partNo) : undefined;
          const qty = itemEl.dataset.qty ? Number(itemEl.dataset.qty) : undefined;
          const units = itemEl.dataset.units ? Number(itemEl.dataset.units) : undefined;
          const entry = { type: 'delivery', id };
          if (Number.isFinite(partNo)) {
            entry.part_no = partNo;
          }
          if (Number.isFinite(qty)) {
            entry.qty = qty;
          }
          if (Number.isFinite(units)) {
            entry.units = units;
          }
          return entry;
        }
        return null;
      }).filter(Boolean);
      return items;
    });

    const stopTimes = Array.from(card.querySelectorAll('.route-stop'))
      .filter(stop => !stop.classList.contains('custom-stop'))
      .map(stop => {
        const arr = Number(stop.dataset.arrivalS);
        const dep = Number(stop.dataset.departureS);
        return {
          arrival_s: Number.isFinite(arr) ? arr : null,
          departure_s: Number.isFinite(dep) ? dep : null,
        };
      });

    const stopNodes = Array.from(card.querySelectorAll('.route-stop'))
      .map(stop => Number(stop.dataset.node))
      .filter(n => Number.isFinite(n) && n !== 0);

    const capacityUnits = computeSkidsFromStops(stops, deliveriesById);

    return {
      vehicle_id: vehicleId,
      vehicle_type: vehicleType,
      driver_profile: driverName ? { name: driverName } : null,
      stops,
      stop_nodes: stopNodes,
      skids: capacityUnits,
      capacity_units: capacityUnits,
      total_time_s: Number.isFinite(totalTimeS) ? totalTimeS : 0,
      total_distance_m: Number.isFinite(totalDistanceM) ? totalDistanceM : 0,
      stop_times: stopTimes,
    };
  });
}

function computeSkidsFromStops(stops, deliveriesById) {
  let total = 0;
  for (const stop of stops || []) {
    for (const item of stop || []) {
      if (!item || item.type === 'depot') continue;
      if (item.type === 'custom') continue;
      const itemUnits = Number(item.units);
      if (Number.isFinite(itemUnits)) {
        total += itemUnits;
        continue;
      }
      const itemQty = Number(item.qty);
      if (Number.isFinite(itemQty)) {
        total += itemQty * MED_SKID_UNITS;
        continue;
      }
      const d = deliveriesById.get(String(item.id || '').trim());
      if (d) {
        total += getCapacityUnits(d);
      }
    }
  }
  return total;
}

function computeSkidBreakdownFromStops(stops, deliveriesById) {
  const totals = { med: 0, small: 0, large: 0 };
  for (const stop of stops || []) {
    for (const item of stop || []) {
      if (!item || item.type === 'depot' || item.type === 'custom') continue;
      const itemQty = Number(item.qty);
      if (Number.isFinite(itemQty)) {
        totals.med += itemQty;
        continue;
      }
      const d = deliveriesById.get(String(item.id || '').trim());
      if (d) {
        totals.med += getMedSkid(d);
        totals.small += getSmallSkid(d);
        totals.large += getLargeSkid(d);
      }
    }
  }
  return totals;
}

function ensureRouteSkids(routes, deliveries) {
  const byId = new Map(
    (deliveries || [])
      .map((d) => [String(d?.id ?? d?.delivery_id ?? d?.record_id ?? '').trim(), d])
      .filter(([key]) => Boolean(key))
  );
  return (routes || []).map(route => {
    if (Number.isFinite(route?.capacity_units) && Number(route.capacity_units) > 0) {
      return route;
    }
    if (Number.isFinite(route?.skids) && Number(route.skids) > 0) {
      return route;
    }
    const stops = route?.stops || [];
    const total = computeSkidsFromStops(stops, byId);
    return Object.assign({}, route, { skids: total, capacity_units: total });
  });
}

function collectDriverProfiles(routes) {
  const seen = new Set();
  const profiles = [];
  for (const route of routes || []) {
    const profile = route?.driver_profile;
    const name = typeof profile === 'string' ? profile : profile?.name;
    const key = String(name || '').trim().toLowerCase();
    if (!name || seen.has(key)) continue;
    seen.add(key);
    profiles.push({ name });
  }
  return profiles;
}

function refreshDriverSelects() {
  const drivers = Array.isArray(window.__DRIVERS__) ? window.__DRIVERS__ : [];
  const normalizedDrivers = drivers.map(d => String(d || '')).filter(Boolean);
  const selects = document.querySelectorAll('select.driver-input[data-driver-select]');
  selects.forEach((select) => {
    const current = String(select.value || '').trim();
    const currentNorm = current.toLowerCase();
    const hasCurrent = currentNorm && normalizedDrivers.some(d => d.toLowerCase() === currentNorm);
    const options = [];
    options.push(new Option('Unassigned', ''));
    if (current && !hasCurrent) {
      options.push(new Option(current, current, true, true));
    }
    normalizedDrivers.forEach((name) => {
      const opt = new Option(name, name, false, currentNorm && name.toLowerCase() === currentNorm);
      options.push(opt);
    });
    select.innerHTML = '';
    options.forEach(opt => select.appendChild(opt));
    if (current) select.value = current;
  });
}

async function initDriverOptions() {
  const needsDrivers = document.getElementById('routesOutput') || document.getElementById('publishedPreviewRoutes');
  if (!needsDrivers) return;
  if (window.__DRIVERS_LOADED__) {
    refreshDriverSelects();
    return;
  }
  try {
    const res = await apiFetch('/drivers/list', { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    const drivers = Array.isArray(data.drivers) ? data.drivers : [];
    window.__DRIVERS__ = drivers;
    window.__DRIVERS_LOADED__ = true;
    refreshDriverSelects();
  } catch (e) {
    console.warn('Driver list load failed', e);
  }
}

async function publishPlan() {
  const publishBtn = document.getElementById('btnPlanPublish');
  if (!publishBtn) return;
  if (!lastPlanData || !Array.isArray(lastPlanData.routes) || lastPlanData.routes.length === 0) {
    alert('Generate a plan before publishing.');
    return;
  }

  publishBtn.disabled = true;
  try {
    const recalcResult = getRecalcPayload({ excludeCustomStops: true });
    const recalcPayload = recalcResult.payload;
    let recalcData = null;
    if (recalcPayload.length) {
      const recalcRes = await apiFetch('/plan/recalculate', {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(recalcPayload),
      });
      recalcData = await recalcRes.json();
    }

    const routes = buildPublishedRoutesFromDom();
    if (recalcData && Array.isArray(recalcData.routes)) {
      const skippedIds = new Set(
        recalcResult.skipped.map(card => String(card.dataset.routeId ?? ''))
      );
      const byVehicle = new Map(recalcData.routes.map(r => [String(r.vehicle_id), r]));
      routes.forEach(route => {
        const key = String(route.vehicle_id ?? '');
        if (skippedIds.has(key)) return;
        const match = byVehicle.get(key);
        if (!match) return;
        route.total_distance_m = match.total_distance_m;
        route.total_time_s = match.total_time_s;
        route.stop_times = match.stop_times;
      });
    }

    const driverProfiles = collectDriverProfiles(routes);
    const meta = Object.assign({}, lastPlanData.meta || {});
    if (Array.isArray(lastPlanData.geocode_failures)) {
      meta.geocode_failures = lastPlanData.geocode_failures;
    }

    const payload = {
      routes,
      deliveries: lastPlanData.deliveries || [],
      meta,
      fleet: lastPlanData.meta?.fleet || {},
      driver_profiles: driverProfiles,
    };

    const res = await apiFetch('/plan/publish', {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data || !data.ok) {
      throw new Error(data?.error || 'Publish failed');
    }
    setPublishStatus(`Published for ${data.plan_date}.`, true);
  } catch (e) {
    console.error(e);
    alert('Publish failed. Check DevTools Console/Network.');
  } finally {
    publishBtn.disabled = false;
  }
}

function applyRecalcResults(routes) {
  for (const route of (routes || [])) {
    const card = document.querySelector(`.route-card[data-route-id="${route.vehicle_id}"]`);
    if (!card) continue;
    const distEl = card.querySelector('[data-total-distance]');
    const timeEl = card.querySelector('[data-total-time]');
    if (distEl) distEl.textContent = formatDistance(route.total_distance_m);
    if (timeEl) timeEl.textContent = formatDuration(route.total_time_s);

    if (Array.isArray(route.stop_times) && route.stop_times.length) {
      const stops = Array.from(card.querySelectorAll('.route-stop'));
      let metaIdx = 0;
      stops.forEach((stopEl) => {
        if (stopEl.classList.contains('custom-stop')) {
          return;
        }
        const meta = route.stop_times[metaIdx];
        if (!meta) {
          metaIdx += 1;
          return;
        }
        metaIdx += 1;
        const arrival = Number.isFinite(meta.arrival_s)
          ? formatClockTimeFromSeconds(meta.arrival_s, SHIFT_START_SECONDS)
          : '--:--';
        const departure = Number.isFinite(meta.departure_s)
          ? formatClockTimeFromSeconds(meta.departure_s, SHIFT_START_SECONDS)
          : '--:--';
        stopEl.dataset.arrivalS = Number.isFinite(meta.arrival_s) ? String(Math.round(meta.arrival_s)) : '';
        stopEl.dataset.departureS = Number.isFinite(meta.departure_s) ? String(Math.round(meta.departure_s)) : '';
        let timeEl = stopEl.querySelector('.route-stop-time');
        if (!timeEl) {
          timeEl = document.createElement('div');
          timeEl.className = 'route-stop-time';
          const titleEl = stopEl.querySelector('.route-stop-title');
          if (titleEl) {
            titleEl.insertAdjacentElement('afterend', timeEl);
          } else {
            stopEl.insertAdjacentElement('afterbegin', timeEl);
          }
        }
        timeEl.textContent = `Arr ${arrival} • Dep ${departure}`;
      });
      card.classList.remove('times-stale');
    }
  }
}

async function recalcPlanTotals(options = {}) {
  const { silent = false } = options;
  const btn = document.getElementById('btnPlanRecalc');
  if (btn) btn.disabled = true;
  try {
    const recalcResult = getRecalcPayload({ excludeCustomStops: true });
    const payload = recalcResult.payload;
    if (!payload.length) {
      if (!silent && recalcResult.skipped.length) {
        alert('Routes without delivery stops are skipped from recalculation.');
      }
      return;
    }
    const res = await apiFetch('/plan/recalculate', {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    applyRecalcResults(data.routes || []);
    const cardsWithCustomStops = Array.from(document.querySelectorAll('.route-card'))
      .filter(card => card.querySelector('.custom-stop'));
    for (const card of cardsWithCustomStops) {
      await recomputeCustomStops(card);
    }
  } catch (e) {
    console.error(e);
    alert("Recalculate failed. Check DevTools Console/Network.");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadPlanFromQuery() {
  if (!document.getElementById('routesOutput')) return;
  setRoutesVisible(false);
  setUnscheduledVisible(false);
  const { trucks, vans, hasParams } = getPlanParams();
  if (!hasParams) return;
  await makePlanWithFleet(trucks, vans);
}

function initCustomStopModal() {
  const modal = document.getElementById('customStopModal');
  const form = document.getElementById('customStopForm');
  if (!modal || !form) return;

  const close = () => {
    modal.classList.add('is-hidden');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    customStopTarget = null;
  };

  const open = (target) => {
    customStopTarget = target;
    modal.classList.remove('is-hidden');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    form.reset();
    const first = form.querySelector('input');
    if (first) first.focus();
  };

  document.addEventListener('click', (event) => {
    const addBtn = event.target.closest('[data-add-stop]');
    if (addBtn) {
      const stopEl = addBtn.closest('.route-stop');
      if (!stopEl) return;
      open(stopEl);
      return;
    }

    const removeBtn = event.target.closest('[data-remove-stop]');
    if (removeBtn) {
      const stopEl = removeBtn.closest('.route-stop');
      if (!stopEl) return;
      const list = stopEl.closest('.route-stops');
      stopEl.remove();
      if (list) updateStopLabels(list);
      const card = list?.closest('.route-card');
      if (card) card.classList.add('times-stale');
      scheduleRecalc();
      setPublishStatus('Edits pending. Publish to update.', false);
      return;
    }

    if (event.target && event.target.matches('[data-modal-close]')) {
      if (modal.contains(event.target)) {
        close();
      }
    }
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!customStopTarget) return;
    const formData = new FormData(form);
    const label = String(formData.get('label') || '').trim();
    const address = String(formData.get('address') || '').trim();
    const serviceMinutes = Number(formData.get('service_minutes') || 0);
    if (!label) {
      alert('Stop label is required.');
      return;
    }
    const prevStop = customStopTarget;
    let nextStop = customStopTarget.nextElementSibling;
    if (nextStop && !nextStop.classList.contains('route-stop')) {
      nextStop = nextStop.nextElementSibling;
    }
    if (!nextStop) {
      alert('Cannot insert after the final stop.');
      return;
    }

    const vehicleType = prevStop.closest('.route-card')?.dataset?.vehicleType || 'truck';
    const prevCoords = getStopCoords(prevStop);
    const nextCoords = getStopCoords(nextStop);

    const insertStop = (lat, lon, estimate) => {
      const newStop = createCustomStopElement(label, address, lat, lon, serviceMinutes);
      prevStop.insertAdjacentElement('afterend', newStop);
      const list = newStop.closest('.route-stops');
      if (list) updateStopLabels(list);

      if (estimate && applyCustomStopTiming({
        prevStop,
        nextStop,
        newStop,
        estimate,
        vehicleType,
        serviceMinutes,
      })) {
        setPublishStatus('Edits pending. Publish to update.', false);
      } else {
        const card = list?.closest('.route-card');
        if (card) card.classList.add('times-stale');
        scheduleRecalc();
        setPublishStatus('Edits pending. Publish to update.', false);
      }
      close();
    };

    if (address && prevCoords && nextCoords) {
      estimateCustomStop(prevCoords, { address }, nextCoords, vehicleType, serviceMinutes)
        .then((estimate) => {
          insertStop(estimate.custom_lat, estimate.custom_lon, estimate);
        })
        .catch((e) => {
          console.error(e);
          alert('Could not estimate travel time for this stop. Added without timing.');
          insertStop(null, null, null);
        });
      return;
    }

    if (!address) {
      alert('Add an address to calculate timing. Stop will be added without timing.');
    }
    insertStop(null, null, null);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.classList.contains('is-hidden')) {
      close();
    }
  });
}

// -----------------------------
// Schedule views (board/calendar/list)
// -----------------------------
function formatCalendarDateKey(dt) {
  const yyyy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, '0');
  const dd = String(dt.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function getPsStatus(delivery, dateKey) {
  const delivered = Boolean(delivery?.ps_delivered);
  const todayKey = formatCalendarDateKey(new Date());
  const isOverdue = Boolean(dateKey && dateKey < todayKey && !delivered);
  const status = delivered ? 'complete' : (isOverdue ? 'overdue' : 'pending');
  return { delivered, status, isOverdue };
}

function filterCalendarDeliveries(deliveries) {
  const query = String(document.getElementById('calendarSearch')?.value || '').trim().toLowerCase();
  if (!query) return deliveries || [];
  return (deliveries || []).filter((d) => {
    const haystack = `${d.delivery_id || ''} ${d.job_name || ''} ${d.sales_order || ''}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderCalendarUnscheduled(items) {
  const target = document.getElementById('calendarUnscheduled');
  if (!target) return;
  if (!items || items.length === 0) {
    target.classList.add('is-hidden');
    target.innerHTML = '';
    return;
  }

  const canEdit = isManagerOrAdminUser();
  const rows = items.map((d) => {
    const deliveryId = String(d.delivery_id || '').trim();
    const dateKey = extractScheduleDateKey(d.scheduled_date);
    const { status } = getPsStatus(d, dateKey);
    const statusClass = ` ps-status--${status}`;
    const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
    const editHref = (canEdit && deliveryId) ? `/admin/deliveries/${encodeURIComponent(deliveryId)}?next=${nextParam}` : '';
    const inlineAttrs = (canEdit && deliveryId) ? ` data-inline-edit="1" data-delivery-id="${escapeHtml(deliveryId)}"` : '';
    const title = escapeHtml(d.job_name || 'Untitled Job');
    const salesOrderRaw = String(d.sales_order || '').trim();
    const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
    const meta = `Delivery: ${escapeHtml(deliveryId || '--')}`;
    return `
      <div class="calendar-unscheduled-item${statusClass}"${inlineAttrs}${(canEdit && deliveryId) ? ' draggable="true"' : ''}>
        ${editHref ? `<a class="calendar-item-title" href="${editHref}"${inlineAttrs}>${title}</a>` : `<div class="calendar-item-title">${title}</div>`}
        <div class="calendar-item-sub">${escapeHtml(salesOrderText)}</div>
        <div class="calendar-item-sub">${meta}</div>
      </div>
    `;
  }).join('');

  target.classList.remove('is-hidden');
  target.innerHTML = `
    <h3>Unscheduled</h3>
    <div class="calendar-unscheduled-list" data-dropzone="calendar" data-date="">${rows}</div>
  `;
}

function renderScheduleCalendar(deliveries, year, month) {
  const target = document.getElementById('scheduleCalendar');
  if (!target) return;

  const canEdit = isManagerOrAdminUser();
  const first = new Date(year, month, 1);
  const start = new Date(first);
  start.setDate(first.getDate() - first.getDay());
  const todayKey = formatCalendarDateKey(new Date());

  const MAX_VISIBLE = 1;

  const deliveriesByDate = new Map();
  const unscheduled = [];
  (deliveries || []).forEach((d) => {
    const key = extractScheduleDateKey(d.scheduled_date);
    if (!key) {
      unscheduled.push(d);
      return;
    }
    if (!deliveriesByDate.has(key)) deliveriesByDate.set(key, []);
    deliveriesByDate.get(key).push(d);
  });

  const weekdayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    .map((name) => `<div class="calendar-weekday">${name}</div>`).join('');

  let dayCells = '';
  for (let i = 0; i < 42; i += 1) {
    const curr = new Date(start);
    curr.setDate(start.getDate() + i);
    const inMonth = curr.getMonth() === month;
    const key = formatCalendarDateKey(curr);
    const items = deliveriesByDate.get(key) || [];
    const visible = items.slice(0, MAX_VISIBLE);
    const remaining = items.length - visible.length;
    const list = visible.map((d) => {
      const deliveryId = String(d.delivery_id || '').trim();
      const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
      const editHref = (canEdit && deliveryId) ? `/admin/deliveries/${encodeURIComponent(deliveryId)}?next=${nextParam}` : '';
      const inlineAttrs = (canEdit && deliveryId) ? ` data-inline-edit="1" data-delivery-id="${escapeHtml(deliveryId)}"` : '';
      const { status } = getPsStatus(d, key);
      const statusClass = ` ps-status--${status}`;
      const title = escapeHtml(d.job_name || 'Untitled Job');
      const salesOrderRaw = String(d.sales_order || '').trim();
      const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
      if (editHref) {
        return `
          <a class="calendar-item${statusClass}" href="${editHref}"${inlineAttrs}${canEdit ? ' draggable="true"' : ''}>
            <div class="calendar-item-title">${title}</div>
            <div class="calendar-item-sub">${escapeHtml(salesOrderText)}</div>
          </a>
        `;
      }
      return `
        <div class="calendar-item${statusClass}"${inlineAttrs}${canEdit ? ' draggable="true"' : ''}>
          <div class="calendar-item-title">${title}</div>
          <div class="calendar-item-sub">${escapeHtml(salesOrderText)}</div>
        </div>
      `;
    }).join('');

    const overflow = items.slice(MAX_VISIBLE);
    const overflowList = overflow.map((d) => {
      const deliveryId = String(d.delivery_id || '').trim();
      const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
      const editHref = (canEdit && deliveryId) ? `/admin/deliveries/${encodeURIComponent(deliveryId)}?next=${nextParam}` : '';
      const inlineAttrs = (canEdit && deliveryId) ? ` data-inline-edit="1" data-delivery-id="${escapeHtml(deliveryId)}"` : '';
      const { status } = getPsStatus(d, key);
      const statusClass = ` ps-status--${status}`;
      const title = escapeHtml(d.job_name || 'Untitled Job');
      const salesOrderRaw = String(d.sales_order || '').trim();
      const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
      if (editHref) {
        return `
          <a class="calendar-item${statusClass}" href="${editHref}"${inlineAttrs}${canEdit ? ' draggable="true"' : ''}>
            <div class="calendar-item-title">${title}</div>
            <div class="calendar-item-sub">${escapeHtml(salesOrderText)}</div>
          </a>
        `;
      }
      return `
        <div class="calendar-item${statusClass}"${inlineAttrs}${canEdit ? ' draggable="true"' : ''}>
          <div class="calendar-item-title">${title}</div>
          <div class="calendar-item-sub">${escapeHtml(salesOrderText)}</div>
        </div>
      `;
    }).join('');

    const more = remaining > 0
      ? `
        <button class="calendar-more" type="button" data-more-toggle data-more-count="${remaining}">
          +${remaining} more
        </button>
        <div class="calendar-more-list is-hidden" data-more-list>
          ${overflowList}
        </div>
      `
      : '';
    const count = items.length > 0 ? `<div class="calendar-day-count">${items.length}</div>` : '';
    const isToday = key === todayKey;
    dayCells += `
      <div class="calendar-day${inMonth ? '' : ' is-outside'}${isToday ? ' is-today' : ''}" data-dropzone="calendar" data-date="${key}" data-more-scope="1">
        <div class="calendar-day-header">
          <div>${curr.getDate()}</div>
          ${count}
        </div>
        <div class="calendar-items">
          ${list}
          ${more}
        </div>
      </div>
    `;
  }

  target.innerHTML = `
    <div class="calendar-weekdays">${weekdayLabels}</div>
    <div class="calendar-grid">${dayCells}</div>
  `;

  renderCalendarUnscheduled(unscheduled);
}

function initScheduleCalendar(options = {}) {
  const calendarEl = document.getElementById('scheduleCalendar');
  if (!calendarEl) return;

  window.__CALENDAR_STATE__ = window.__CALENDAR_STATE__ || {};

  const { dataOverride = null, skipInitialLoad = false } = options;
  const titleEl = document.getElementById('calendarTitle');
  const countEl = document.getElementById('calendarCount');
  const searchEl = document.getElementById('calendarSearch');
  const prevBtn = document.getElementById('btnCalPrev');
  const nextBtn = document.getElementById('btnCalNext');

  const state = {
    year: new Date().getFullYear(),
    month: new Date().getMonth(),
    deliveries: [],
  };
  window.__CALENDAR_STATE__ = state;

  const updateTitle = () => {
    const dt = new Date(state.year, state.month, 1);
    const label = dt.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    if (titleEl) titleEl.textContent = label;
  };

  const applyRender = () => {
    const filtered = filterCalendarDeliveries(state.deliveries);
    renderScheduleCalendar(filtered, state.year, state.month);
    updateTitle();
    if (countEl) {
      countEl.textContent = `${filtered.length} deliveries loaded`;
    }
  };

  window.__CALENDAR_RENDER__ = (deliveries) => {
    state.deliveries = Array.isArray(deliveries) ? deliveries : [];
    applyRender();
  };

  const load = async (loadOptions = {}) => {
    const { forceRefresh = false } = loadOptions;
    if (countEl) countEl.textContent = 'Loading deliveries...';
    try {
      if (!forceRefresh && Array.isArray(window.__SCHEDULE_DELIVERIES__)) {
        state.deliveries = window.__SCHEDULE_DELIVERIES__;
        applyRender();
        return;
      }
      const res = await apiFetch('/deliveries/all', { cache: 'no-store' });
      const data = await res.json();
      state.deliveries = Array.isArray(data.deliveries) ? data.deliveries : [];
      window.__CALENDAR_DELIVERIES__ = state.deliveries;
      window.__SCHEDULE_DELIVERIES__ = state.deliveries;
      applyRender();
    } catch (e) {
      console.error(e);
      if (countEl) countEl.textContent = 'Load failed.';
      calendarEl.innerHTML = `<div class="empty-state">Failed to load deliveries.</div>`;
    }
  };

  prevBtn?.addEventListener('click', () => {
    state.month -= 1;
    if (state.month < 0) {
      state.month = 11;
      state.year -= 1;
    }
    applyRender();
  });

  nextBtn?.addEventListener('click', () => {
    state.month += 1;
    if (state.month > 11) {
      state.month = 0;
      state.year += 1;
    }
    applyRender();
  });

  searchEl?.addEventListener('input', applyRender);

  if (Array.isArray(dataOverride)) {
    state.deliveries = dataOverride;
    applyRender();
    return;
  }

  if (!skipInitialLoad) {
    load({ forceRefresh: true });
  }
}

function extractScheduleDateKey(raw) {
  if (!raw) return '';
  const text = String(raw).trim();
  if (!text) return '';
  const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
  if (match) return match[1];
  const parsed = new Date(text);
  if (!Number.isNaN(parsed.getTime())) {
    const yyyy = parsed.getFullYear();
    const mm = String(parsed.getMonth() + 1).padStart(2, '0');
    const dd = String(parsed.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }
  return text;
}

function parseDateFromInput(value) {
  if (!value) return null;
  const parts = String(value).split('-');
  if (parts.length !== 3) return null;
  const yyyy = Number(parts[0]);
  const mm = Number(parts[1]) - 1;
  const dd = Number(parts[2]);
  const dt = new Date(yyyy, mm, dd);
  if (Number.isNaN(dt.getTime())) return null;
  return dt;
}

function startOfWeek(date) {
  const dt = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const day = dt.getDay();
  const offset = (day + 6) % 7;
  dt.setDate(dt.getDate() - offset);
  return dt;
}

function addDays(date, days) {
  const dt = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  dt.setDate(dt.getDate() + days);
  return dt;
}

function formatDateLabel(dt) {
  return dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}

function formatScheduleDateLabel(dateKey) {
  if (!dateKey) return 'Unscheduled';
  const parts = dateKey.split('-');
  if (parts.length === 3) {
    const dt = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    if (!Number.isNaN(dt.getTime())) {
      const today = new Date();
      const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
      const tomorrow = new Date(today);
      tomorrow.setDate(today.getDate() + 1);
      const tomorrowKey = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`;
      const baseLabel = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
      if (dateKey === todayKey) return `Today - ${baseLabel}`;
      if (dateKey === tomorrowKey) return `Tomorrow - ${baseLabel}`;
      return baseLabel;
    }
  }
  return dateKey;
}

function formatWindowLabel(start, end) {
  const s = String(start || '').trim();
  const e = String(end || '').trim();
  if (!s && !e) return '--';
  if (!s) return `By ${e}`;
  if (!e) return `From ${s}`;
  return `${s}-${e}`;
}

function filterBoardDeliveries(deliveries) {
  const query = String(document.getElementById('boardSearch')?.value || '').trim().toLowerCase();

  return (deliveries || []).filter((d) => {
    if (!query) return true;
    const haystack = `${d.delivery_id || ''} ${d.job_name || ''} ${d.sales_order || ''}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderScheduleBoard(deliveries, weekStart) {
  const target = document.getElementById('scheduleBoard');
  if (!target) return;
  if (!Array.isArray(deliveries)) {
    target.innerHTML = `<div class="empty-state">No deliveries found.</div>`;
    return;
  }

  const canEdit = isManagerOrAdminUser();
  const MAX_VISIBLE = 1;

  const buildBoardCard = (d) => {
    const deliveryId = String(d.delivery_id || '').trim();
    const dateKey = extractScheduleDateKey(d.scheduled_date);
    const { status } = getPsStatus(d, dateKey);
    const statusClass = ` ps-status--${status}`;
    const inlineAttrs = (canEdit && deliveryId)
      ? ` data-inline-edit="1" data-delivery-id="${escapeHtml(deliveryId)}" role="button" tabindex="0" aria-label="Edit delivery ${escapeHtml(deliveryId)}" draggable="true"`
      : '';
    const salesOrderRaw = String(d.sales_order || '').trim();
    const salesOrderText = salesOrderRaw ? `Sales Order: ${salesOrderRaw}` : 'Sales Order: --';
    const cargo = `Med skids: ${getMedSkid(d)} | Small skids: ${getSmallSkid(d)} | Large skids: ${getLargeSkid(d)} | Doors: ${Number(d.door || 0)} | Frames: ${Number(d.frame || 0)} | Exp Frames: ${Number(d.exp_frame || 0)} | Boxes: ${Number(d.box || 0)}`;
    const windowLabel = formatWindowLabel(d.window_start, d.window_end);
    return `
      <div class="board-card${statusClass}"${inlineAttrs}>
        <div class="board-card-title">${escapeHtml(d.job_name || 'Untitled Job')}</div>
        <div class="board-card-sub">${escapeHtml(salesOrderText)}</div>
        <div class="board-card-meta">
          <span>Delivery: ${escapeHtml(deliveryId || '--')}</span>
          <span>Window: ${escapeHtml(windowLabel)}</span>
          <span>${escapeHtml(cargo)}</span>
        </div>
      </div>
    `;
  };

  const grouped = new Map();
  const unscheduled = [];
  deliveries.forEach((d) => {
    const key = extractScheduleDateKey(d.scheduled_date);
    if (!key) {
      unscheduled.push(d);
      return;
    }
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(d);
  });

  const weekStartDate = weekStart || startOfWeek(new Date());
  const columns = [];

  for (let i = 0; i < 7; i += 1) {
    const day = addDays(weekStartDate, i);
    const key = formatCalendarDateKey(day);
    const items = grouped.get(key) || [];
    const label = formatDateLabel(day);
    const meta = key;
    const visible = items.slice(0, MAX_VISIBLE);
    const overflow = items.slice(MAX_VISIBLE);
    const cards = visible.map(buildBoardCard).join('');
    const overflowList = overflow.map(buildBoardCard).join('');
    const more = overflow.length
      ? `
        <button class="board-more" type="button" data-more-toggle data-more-count="${overflow.length}">
          +${overflow.length} more
        </button>
        <div class="board-more-list is-hidden" data-more-list>
          ${overflowList}
        </div>
      `
      : '';

    columns.push(`
      <section class="board-column" data-date="${escapeHtml(key)}" data-dropzone="board" data-more-scope="1">
        <div class="board-column-header">
          <div>
            <div class="board-column-title">${escapeHtml(label)}</div>
            <div class="board-column-meta">${escapeHtml(meta)}</div>
          </div>
          <span class="pill">${items.length}</span>
        </div>
        ${cards || `<div class="empty-state">No jobs.</div>`}
        ${more}
      </section>
    `);
  }

  const unscheduledVisible = unscheduled.slice(0, MAX_VISIBLE);
  const unscheduledOverflow = unscheduled.slice(MAX_VISIBLE);
  const unscheduledCards = unscheduledVisible.map(buildBoardCard).join('');
  const unscheduledOverflowList = unscheduledOverflow.map(buildBoardCard).join('');
  const unscheduledMore = unscheduledOverflow.length
    ? `
      <button class="board-more" type="button" data-more-toggle data-more-count="${unscheduledOverflow.length}">
        +${unscheduledOverflow.length} more
      </button>
      <div class="board-more-list is-hidden" data-more-list>
        ${unscheduledOverflowList}
      </div>
    `
    : '';

  columns.push(`
    <section class="board-column" data-date="" data-dropzone="board" data-more-scope="1">
      <div class="board-column-header">
        <div>
          <div class="board-column-title">Unscheduled</div>
          <div class="board-column-meta">No date</div>
        </div>
        <span class="pill">${unscheduled.length}</span>
      </div>
      ${unscheduledCards || `<div class="empty-state">No jobs.</div>`}
      ${unscheduledMore}
    </section>
  `);

  target.innerHTML = columns.join('');
}

function updateBoardView(deliveries) {
  const filtered = filterBoardDeliveries(deliveries || []);
  const weekInput = document.getElementById('boardWeek')?.value || '';
  const baseDate = parseDateFromInput(weekInput) || new Date();
  const weekStart = startOfWeek(baseDate);
  renderScheduleBoard(filtered, weekStart);
  const countEl = document.getElementById('boardCount');
  if (countEl) {
    const startKey = formatCalendarDateKey(weekStart);
    const endKey = formatCalendarDateKey(addDays(weekStart, 6));
    const inWeek = filtered.filter((d) => {
      const key = extractScheduleDateKey(d.scheduled_date);
      if (!key) return false;
      return key >= startKey && key <= endKey;
    }).length;
    const unscheduled = filtered.filter(d => !extractScheduleDateKey(d.scheduled_date)).length;
    countEl.textContent = `Week of ${startKey} - ${endKey} · ${inWeek} scheduled · ${unscheduled} unscheduled`;
  }
}

function filterListDeliveries(deliveries) {
  const query = String(document.getElementById('listSearch')?.value || '').trim().toLowerCase();
  const dateFilter = String(document.getElementById('listDate')?.value || '').trim();
  return (deliveries || []).filter((d) => {
    if (dateFilter) {
      const key = extractScheduleDateKey(d.scheduled_date);
      if (key !== dateFilter) return false;
    }
    if (!query) return true;
    const haystack = `${d.delivery_id || ''} ${d.job_name || ''} ${d.sales_order || ''}`.toLowerCase();
    return haystack.includes(query);
  });
}

function renderScheduleList(deliveries) {
  const target = document.getElementById('scheduleList');
  if (!target) return;
  if (!Array.isArray(deliveries) || deliveries.length === 0) {
    target.innerHTML = `<div class="empty-state">No deliveries found.</div>`;
    return;
  }

  const canEdit = isManagerOrAdminUser();
  const editEnabled = canEdit && Boolean(window.__LIST_EDIT_ENABLED__);
  const timeOptionsFor = (selected, placeholder) => {
    const steps = [];
    for (let h = 6; h <= 18; h += 1) {
      for (let m = 0; m < 60; m += 30) {
        const hh = String(h).padStart(2, '0');
        const mm = String(m).padStart(2, '0');
        steps.push(`${hh}:${mm}`);
      }
    }
    const normalized = String(selected || '').trim();
    let opts = `<option value="">${escapeHtml(placeholder || '')}</option>`;
    if (normalized && !steps.includes(normalized)) {
      opts += `<option value="${escapeHtml(normalized)}" selected>${escapeHtml(normalized)}</option>`;
    }
    opts += steps.map((val) => {
      const sel = val === normalized ? ' selected' : '';
      return `<option value="${val}"${sel}>${val}</option>`;
    }).join('');
    return opts;
  };
  const rows = deliveries.map((d) => {
    const deliveryId = String(d.delivery_id || '').trim();
    const dateKey = extractScheduleDateKey(d.scheduled_date);
    const dateLabel = formatScheduleDateLabel(dateKey);
    const sortKey = dateKey || '9999-99-99';
    const salesOrder = String(d.sales_order || '').trim();
    const salesText = salesOrder || '--';
    const windowStart = String(d.window_start || '').trim();
    const windowEnd = String(d.window_end || '').trim();
    const medSkidVal = getMedSkid(d);
    const smallSkidVal = getSmallSkid(d);
    const largeSkidVal = getLargeSkid(d);
    const pickupVal = Boolean(d.pickup);
    const requiresEmptyVal = Boolean(d.requires_empty_truck);
    const doorVal = Number(d.door || 0);
    const frameVal = Number(d.frame || 0);
    const expFrameVal = Number(d.exp_frame || 0);
    const boxVal = Number(d.box || 0);
    const cargoDisabled = editEnabled ? '' : ' disabled';
    const windowInputs = `
      <div class="window-edit">
        <select class="edit-input time-input" data-inline-edit-skip data-field="window_start" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${escapeHtml(windowStart)}" title="Window Start" aria-label="Window Start"${cargoDisabled}>
          ${timeOptionsFor(windowStart, 'Start')}
        </select>
        <select class="edit-input time-input" data-inline-edit-skip data-field="window_end" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${escapeHtml(windowEnd)}" title="Window End" aria-label="Window End"${cargoDisabled}>
          ${timeOptionsFor(windowEnd, 'End')}
        </select>
      </div>
    `;
    const windowLabel = formatWindowLabel(windowStart, windowEnd);
    const cargoMedSkidInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="med_skid" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${medSkidVal}" type="number" min="0" value="${medSkidVal}" title="Med Skid" aria-label="Med Skid"${cargoDisabled} />`;
    const cargoSmallSkidInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="small_skid" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${smallSkidVal}" type="number" min="0" value="${smallSkidVal}" title="Small Skid" aria-label="Small Skid"${cargoDisabled} />`;
    const cargoLargeSkidInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="large_skid" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${largeSkidVal}" type="number" min="0" value="${largeSkidVal}" title="Large Skid" aria-label="Large Skid"${cargoDisabled} />`;
    const cargoDoorInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="door" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${doorVal}" type="number" min="0" value="${doorVal}" title="Door" aria-label="Door"${cargoDisabled} />`;
    const cargoFrameInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="frame" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${frameVal}" type="number" min="0" value="${frameVal}" title="Frame" aria-label="Frame"${cargoDisabled} />`;
    const cargoExpFrameInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="exp_frame" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${expFrameVal}" type="number" min="0" value="${expFrameVal}" title="Exp Frame" aria-label="Exp Frame"${cargoDisabled} />`;
    const cargoBoxInput = `<input class="edit-input cargo-input" data-inline-edit-skip data-field="box" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${boxVal}" type="number" min="0" value="${boxVal}" title="Box" aria-label="Box"${cargoDisabled} />`;
    const pickupInput = `<input class="edit-input toggle-input" data-inline-edit-skip data-field="pickup" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${pickupVal ? '1' : '0'}" type="checkbox" value="1"${pickupVal ? ' checked' : ''}${cargoDisabled} aria-label="Pickup" />`;
    const requiresEmptyInput = `<input class="edit-input toggle-input" data-inline-edit-skip data-field="requires_empty_truck" data-delivery-id="${escapeHtml(deliveryId)}" data-original="${requiresEmptyVal ? '1' : '0'}" type="checkbox" value="1"${requiresEmptyVal ? ' checked' : ''}${cargoDisabled} aria-label="Requires Empty Truck" />`;
    const { delivered: psDelivered, status } = getPsStatus(d, dateKey);
    const statusClass = `is-${status}`;
    const rowStatusClass = `ps-row--${status}`;
    const toggleAttrs = canEdit ? ` data-ps-toggle data-delivery-id="${escapeHtml(deliveryId)}"` : '';
    const dueAttr = dateKey ? ` data-due-date="${escapeHtml(dateKey)}"` : '';
    const toggle = `
      <input type="checkbox" class="ps-toggle ${statusClass}"${toggleAttrs}${dueAttr}${psDelivered ? ' checked' : ''}${canEdit ? '' : ' disabled'}
        aria-label="Paperwork received for ${escapeHtml(deliveryId || d.job_name || 'delivery')}" />
    `;
    const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
    const deleteAction = `/admin/deliveries/${encodeURIComponent(deliveryId)}/delete?next=${nextParam}`;
    const actions = canEdit ? `
      <div class="table-actions">
        <form method="post" action="${deleteAction}">
          <input type="hidden" name="csrf_token" value="${escapeHtml(getCookie(CSRF_COOKIE_NAME))}">
          <button class="btn btn-secondary icon-only btn-compact" type="submit" title="Delete" aria-label="Delete">
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
      </div>
    ` : '';
    const inlineAttrs = (editEnabled && deliveryId)
      ? ` data-inline-edit="1" data-delivery-id="${escapeHtml(deliveryId)}" role="button" tabindex="0" aria-label="Edit delivery ${escapeHtml(deliveryId)}"`
      : '';
    const rowClass = ` class="ps-row ${rowStatusClass}"`;
    return `
      <tr${rowClass}${inlineAttrs}>
        <td class="col-ps" data-label="PS Delivered">${toggle}</td>
        <td class="col-sales" data-label="Sales Order">${escapeHtml(salesText)}</td>
        <td class="col-date" data-label="Scheduled Date"><span class="sr-only">${escapeHtml(sortKey)}</span>${escapeHtml(dateLabel)}</td>
        <td class="col-job" data-label="Job">${escapeHtml(d.job_name || 'Untitled Job')}</td>
        <td class="col-window" data-label="Time Window">${editEnabled ? windowInputs : escapeHtml(windowLabel)}</td>
        <td class="col-med-skid" data-label="Med Skid">${editEnabled ? cargoMedSkidInput : escapeHtml(String(medSkidVal))}</td>
        <td class="col-small-skid" data-label="Small Skid">${editEnabled ? cargoSmallSkidInput : escapeHtml(String(smallSkidVal))}</td>
        <td class="col-large-skid" data-label="Large Skid">${editEnabled ? cargoLargeSkidInput : escapeHtml(String(largeSkidVal))}</td>
        <td class="col-door" data-label="Door">${editEnabled ? cargoDoorInput : escapeHtml(String(doorVal))}</td>
        <td class="col-frame" data-label="Frame">${editEnabled ? cargoFrameInput : escapeHtml(String(frameVal))}</td>
        <td class="col-exp-frame" data-label="Exp Frame">${editEnabled ? cargoExpFrameInput : escapeHtml(String(expFrameVal))}</td>
        <td class="col-box" data-label="Box">${editEnabled ? cargoBoxInput : escapeHtml(String(boxVal))}</td>
        <td class="col-pickup" data-label="Pickup" data-inline-edit-skip>${editEnabled ? pickupInput : (pickupVal ? 'Yes' : 'No')}</td>
        <td class="col-empty-truck" data-label="Empty Truck" data-inline-edit-skip>${editEnabled ? requiresEmptyInput : (requiresEmptyVal ? 'Yes' : 'No')}</td>
        ${canEdit ? `<td class="col-actions" data-label="Actions" data-inline-edit-skip>${actions}</td>` : ''}
      </tr>
    `;
  }).join('');

  const actionsHeader = canEdit ? `<th class="col-actions">Actions</th>` : '';
  target.innerHTML = `
    <table data-responsive="true" data-sortable="true" class="table-clickable schedule-list-table">
      <thead>
        <tr>
          <th class="col-ps">PS Delivered</th>
          <th class="col-sales sortable" data-type="text">Sales Order</th>
          <th class="col-date sortable" data-type="text">Scheduled Date</th>
          <th class="col-job sortable" data-type="text">Job</th>
          <th class="col-window sortable" data-type="text">Time Window</th>
          <th class="col-med-skid sortable" data-type="number">Med Skid</th>
          <th class="col-small-skid sortable" data-type="number">Small Skid</th>
          <th class="col-large-skid sortable" data-type="number">Large Skid</th>
          <th class="col-door sortable" data-type="number">Door</th>
          <th class="col-frame sortable" data-type="number">Frame</th>
          <th class="col-exp-frame sortable" data-type="number">Exp Frame</th>
          <th class="col-box sortable" data-type="number">Box</th>
          <th class="col-pickup">Pickup</th>
          <th class="col-empty-truck">Empty Truck</th>
          ${actionsHeader}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function updateListView(deliveries) {
  const filtered = filterListDeliveries(deliveries || []);
  const sorted = filtered.slice().sort((a, b) => {
    const aKey = extractScheduleDateKey(a.scheduled_date) || '9999-99-99';
    const bKey = extractScheduleDateKey(b.scheduled_date) || '9999-99-99';
    if (aKey < bKey) return -1;
    if (aKey > bKey) return 1;
    return String(a.delivery_id || '').localeCompare(String(b.delivery_id || ''));
  });
  renderScheduleList(sorted);
}

async function loadScheduleList(options = {}) {
  const { forceRefresh = false, preferCache = true } = options;
  const target = document.getElementById('scheduleList');
  if (!target) return;
  if (!forceRefresh && preferCache && Array.isArray(window.__SCHEDULE_DELIVERIES__)) {
    window.__LIST_DELIVERIES__ = window.__SCHEDULE_DELIVERIES__;
    updateListView(window.__SCHEDULE_DELIVERIES__);
    return;
  }
  if (forceRefresh) {
    target.innerHTML = `<div class="loading"><div class="spinner"></div><div><div class="loading-title">Refreshing list</div><div class="loading-subtitle">Fetching deliveries.</div></div></div>`;
  }
  try {
    const res = await apiFetch('/deliveries/all', { cache: 'no-store' });
    const data = await res.json();
    const deliveries = Array.isArray(data.deliveries) ? data.deliveries : [];
    updateListView(deliveries);
    window.__LIST_DELIVERIES__ = deliveries;
    window.__SCHEDULE_DELIVERIES__ = deliveries;
  } catch (e) {
    console.error(e);
    target.innerHTML = `<div class="empty-state">Failed to load deliveries.</div>`;
  }
}

function initScheduleList(options = {}) {
  const list = document.getElementById('scheduleList');
  if (!list) return;

  window.__LIST_RENDER__ = (deliveries) => {
    window.__LIST_DELIVERIES__ = deliveries;
    updateListView(deliveries);
  };

  const { dataOverride = null, skipInitialLoad = false } = options;
  const searchInput = document.getElementById('listSearch');
  const dateInput = document.getElementById('listDate');
  const saveBtn = document.getElementById('listSaveCargo');
  const saveStatus = document.getElementById('listSaveStatus');
  const editToggle = document.getElementById('listEditToggle');
  const printBtn = document.getElementById('listPrintOrders');

  const showSaveStatus = (message, isError = false) => {
    if (!saveStatus) return;
    saveStatus.textContent = message || '';
    saveStatus.dataset.state = isError ? 'error' : 'ok';
  };

  const applyFilters = () => {
    const deliveries = Array.isArray(window.__LIST_DELIVERIES__)
      ? window.__LIST_DELIVERIES__
      : (Array.isArray(window.__SCHEDULE_DELIVERIES__) ? window.__SCHEDULE_DELIVERIES__ : []);
    updateListView(deliveries);
  };

  const getVisibleDeliveries = () => {
    const deliveries = Array.isArray(window.__LIST_DELIVERIES__)
      ? window.__LIST_DELIVERIES__
      : (Array.isArray(window.__SCHEDULE_DELIVERIES__) ? window.__SCHEDULE_DELIVERIES__ : []);
    const filtered = filterListDeliveries(deliveries || []);
    return filtered.slice().sort((a, b) => {
      const aKey = extractScheduleDateKey(a.scheduled_date) || '9999-99-99';
      const bKey = extractScheduleDateKey(b.scheduled_date) || '9999-99-99';
      if (aKey < bKey) return -1;
      if (aKey > bKey) return 1;
      return String(a.delivery_id || '').localeCompare(String(b.delivery_id || ''));
    });
  };

  const syncEditToggle = () => {
    if (!editToggle) return;
    const enabled = Boolean(window.__LIST_EDIT_ENABLED__);
    editToggle.checked = enabled;
    if (saveBtn) {
      saveBtn.disabled = !enabled;
      saveBtn.classList.toggle('is-hidden', !enabled);
    }
  };

  if (editToggle) {
    if (window.__LIST_EDIT_ENABLED__ === undefined) {
      window.__LIST_EDIT_ENABLED__ = false;
    }
    syncEditToggle();
    if (saveBtn) saveBtn.classList.add('is-hidden');
    editToggle.addEventListener('change', () => {
      window.__LIST_EDIT_ENABLED__ = Boolean(editToggle.checked);
      syncEditToggle();
      applyFilters();
    });
  }

  printBtn?.addEventListener('click', () => {
    const rows = getVisibleDeliveries().slice().sort((a, b) => {
      const aJob = String(a.job_name || '').trim().toLowerCase();
      const bJob = String(b.job_name || '').trim().toLowerCase();
      if (aJob < bJob) return -1;
      if (aJob > bJob) return 1;
      return String(a.sales_order || '').localeCompare(String(b.sales_order || ''));
    });
    if (!rows.length) {
      showSaveStatus('No deliveries to print.', true);
      return;
    }

    const now = new Date().toLocaleString();
    const tableRows = rows.map((d) => {
      const sales = String(d.sales_order || '').trim();
      const job = String(d.job_name || '').trim();
      const packing = String(d.packing_slip || '').trim();
      const driver = String(d.driver || '').trim();
      return `<tr><td>${escapeHtml(sales)}</td><td>${escapeHtml(job)}</td><td>${escapeHtml(packing)}</td><td>${escapeHtml(driver)}</td></tr>`;
    }).join('');

    const html = `
      <!doctype html>
      <html>
        <head>
          <meta charset="utf-8" />
          <title>Sales Orders</title>
          <style>
            * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
            body { font-family: "Manrope", "Segoe UI", sans-serif; padding: 24px; color: #1a1917; }
            h1 { font-size: 20px; margin: 0 0 6px; }
            .meta { font-size: 12px; color: #5a534b; margin-bottom: 16px; }
            table { width: 100%; border-collapse: collapse; border: 1px solid #c9c0b5; }
            th, td { border: 1px solid #c9c0b5; padding: 8px 10px; font-size: 13px; text-align: left; }
            th { background: #efe7dc; text-transform: uppercase; font-size: 11px; letter-spacing: 0.6px; }
            @media print {
              body { padding: 0; }
              th, td { font-size: 12px; }
              table { border: 1px solid #000; }
              th, td { border: 1px solid #000; }
            }
          </style>
        </head>
        <body>
          <h1>Sales Orders</h1>
          <div class="meta">${escapeHtml(now)} • ${rows.length} deliveries</div>
          <table>
            <thead><tr><th>Sales Order</th><th>Job Name</th><th>Packing Slip</th><th>Driver</th></tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
        </body>
      </html>
    `;

    const win = window.open('', '_blank', 'width=900,height=700');
    if (!win) {
      showSaveStatus('Popup blocked. Allow popups to print.', true);
      return;
    }
    win.document.open();
    win.document.write(html);
    win.document.close();
    win.focus();
    win.print();
  });

  searchInput?.addEventListener('input', applyFilters);
  searchInput?.addEventListener('change', applyFilters);
  dateInput?.addEventListener('change', applyFilters);
  dateInput?.addEventListener('input', applyFilters);

  saveBtn?.addEventListener('click', async () => {
    if (!isManagerOrAdminUser()) return;
    if (!window.__LIST_EDIT_ENABLED__) {
      showSaveStatus('Enable edit mode to save changes.', true);
      return;
    }

    const inputs = Array.from(document.querySelectorAll('.edit-input[data-delivery-id]'));
    const updatesById = new Map();
    const windowById = new Map();
    let hasInvalid = false;
    let windowErrorMessage = '';

    inputs.forEach((input) => {
      const deliveryId = input.dataset.deliveryId || '';
      const field = input.dataset.field || '';
      const original = String(input.dataset.original ?? '');
      const raw = input.type === 'checkbox'
        ? (input.checked ? '1' : '0')
        : String(input.value ?? '').trim();

      input.classList.remove('is-invalid');
      if (!deliveryId || !field) return;

    const isCargoField = ['med_skid', 'large_skid', 'small_skid', 'door', 'frame', 'exp_frame', 'box'].includes(field);
      const isBoolField = ['pickup', 'requires_empty_truck'].includes(field);
      const isWindowField = field === 'window_start' || field === 'window_end';

      if (raw === '' && isCargoField) {
        return;
      }

      if (isCargoField) {
        const num = Number(raw);
        if (!Number.isFinite(num) || num < 0) {
          input.classList.add('is-invalid');
          hasInvalid = true;
          return;
        }

        const value = Math.floor(num);
        if (String(value) === original) return;

        if (!updatesById.has(deliveryId)) {
          updatesById.set(deliveryId, { delivery_id: deliveryId });
        }
        updatesById.get(deliveryId)[field] = value;
        return;
      }

      if (isBoolField) {
        if (raw === original) return;
        if (!updatesById.has(deliveryId)) {
          updatesById.set(deliveryId, { delivery_id: deliveryId });
        }
        updatesById.get(deliveryId)[field] = raw === '1';
        return;
      }

      if (isWindowField) {
        const entry = windowById.get(deliveryId) || {};
        entry[field] = raw;
        entry[`${field}Original`] = original;
        entry[`${field}Input`] = input;
        windowById.set(deliveryId, entry);
        if (raw === original) return;
        if (!updatesById.has(deliveryId)) {
          updatesById.set(deliveryId, { delivery_id: deliveryId });
        }
        updatesById.get(deliveryId)[field] = raw;
      }
    });

    windowById.forEach((entry) => {
      const startVal = entry.window_start ?? '';
      const endVal = entry.window_end ?? '';
      const startOriginal = entry.window_startOriginal ?? '';
      const endOriginal = entry.window_endOriginal ?? '';
      const changed = String(startVal) !== String(startOriginal) || String(endVal) !== String(endOriginal);
      if (!changed) return;
      const result = validateWindowValues(startVal, endVal);
      if (result.ok) return;
      hasInvalid = true;
      if (!windowErrorMessage) {
        windowErrorMessage = result.message || 'Fix highlighted time windows before saving.';
      }
      if (entry.window_startInput) entry.window_startInput.classList.add('is-invalid');
      if (entry.window_endInput) entry.window_endInput.classList.add('is-invalid');
    });

    if (hasInvalid) {
      showSaveStatus(windowErrorMessage || 'Fix highlighted fields before saving.', true);
      return;
    }

    const updates = Array.from(updatesById.values());
    if (updates.length === 0) {
      showSaveStatus('No changes to save.', false);
      return;
    }

    saveBtn.disabled = true;
    showSaveStatus(`Saving ${updates.length} deliveries...`, false);
    try {
      const res = await apiFetch('/deliveries/bulk-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deliveries: updates }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        showSaveStatus(data.detail || data.error || 'Save failed.', true);
        return;
      }
      const errorCount = Array.isArray(data.errors) ? data.errors.length : 0;
      const suffix = errorCount ? ` (${errorCount} failed)` : '';
      showSaveStatus(`Updated ${data.updated || 0} deliveries.${suffix}`, false);
      inputs.forEach((input) => {
        input.dataset.original = String(input.value ?? '');
      });
      loadScheduleList({ forceRefresh: true, preferCache: false });
    } catch (e) {
      console.error(e);
      showSaveStatus('Save failed. Try again.', true);
    } finally {
      saveBtn.disabled = false;
    }
  });

  if (Array.isArray(dataOverride)) {
    window.__LIST_DELIVERIES__ = dataOverride;
    updateListView(dataOverride);
    return;
  }

  if (!skipInitialLoad) {
    loadScheduleList({ forceRefresh: true });
  }
}

async function loadScheduleBoard(options = {}) {
  const { forceRefresh = false, preferCache = true } = options;
  const target = document.getElementById('scheduleBoard');
  if (!target) return;
  if (!forceRefresh && preferCache && Array.isArray(window.__SCHEDULE_DELIVERIES__)) {
    window.__BOARD_DELIVERIES__ = window.__SCHEDULE_DELIVERIES__;
    updateBoardView(window.__SCHEDULE_DELIVERIES__);
    return;
  }
  if (forceRefresh) {
    target.innerHTML = `<div class="loading"><div class="spinner"></div><div><div class="loading-title">Refreshing board</div><div class="loading-subtitle">Fetching deliveries.</div></div></div>`;
  }
  try {
    const res = await apiFetch('/deliveries/all', { cache: 'no-store' });
    const data = await res.json();
    const deliveries = Array.isArray(data.deliveries) ? data.deliveries : [];
    updateBoardView(deliveries);
    window.__BOARD_DELIVERIES__ = deliveries;
    window.__SCHEDULE_DELIVERIES__ = deliveries;
  } catch (e) {
    console.error(e);
    target.innerHTML = `<div class="empty-state">Failed to load deliveries.</div>`;
    const countEl = document.getElementById('boardCount');
    if (countEl) countEl.textContent = 'Load failed.';
  }
}

function initScheduleBoard(options = {}) {
  const board = document.getElementById('scheduleBoard');
  if (!board) return;

  window.__BOARD_RENDER__ = (deliveries) => {
    window.__BOARD_DELIVERIES__ = deliveries;
    updateBoardView(deliveries);
  };

  const { dataOverride = null, skipInitialLoad = false } = options;
  const weekInput = document.getElementById('boardWeek');
  const clearBtn = document.getElementById('btnBoardClear');
  const refreshBtn = document.getElementById('btnBoardRefresh');
  const prevBtn = document.getElementById('btnBoardPrev');
  const nextBtn = document.getElementById('btnBoardNext');
  const todayBtn = document.getElementById('btnBoardToday');
  const inputs = [
    weekInput,
    document.getElementById('boardSearch'),
  ];

  const applyFilters = () => {
    const deliveries = Array.isArray(window.__BOARD_DELIVERIES__)
      ? window.__BOARD_DELIVERIES__
      : (Array.isArray(window.__SCHEDULE_DELIVERIES__) ? window.__SCHEDULE_DELIVERIES__ : []);
    if (weekInput) {
      const base = parseDateFromInput(weekInput.value) || new Date();
      const weekStart = startOfWeek(base);
      const weekKey = formatCalendarDateKey(weekStart);
      if (weekInput.value !== weekKey) {
        weekInput.value = weekKey;
      }
    }
    updateBoardView(deliveries);
  };

  clearBtn?.addEventListener('click', () => {
    inputs.forEach((el) => {
      if (!el) return;
      if (el.type === 'search') {
        el.value = '';
      }
    });
    applyFilters();
  });
  refreshBtn?.addEventListener('click', () => loadScheduleBoard({ forceRefresh: true, preferCache: false }));
  prevBtn?.addEventListener('click', () => {
    const base = parseDateFromInput(weekInput?.value) || new Date();
    const weekStart = startOfWeek(base);
    const prev = addDays(weekStart, -7);
    if (weekInput) weekInput.value = formatCalendarDateKey(prev);
    applyFilters();
  });
  nextBtn?.addEventListener('click', () => {
    const base = parseDateFromInput(weekInput?.value) || new Date();
    const weekStart = startOfWeek(base);
    const next = addDays(weekStart, 7);
    if (weekInput) weekInput.value = formatCalendarDateKey(next);
    applyFilters();
  });
  todayBtn?.addEventListener('click', () => {
    const today = new Date();
    if (weekInput) weekInput.value = formatCalendarDateKey(today);
    applyFilters();
  });
  inputs.forEach((el) => {
    el?.addEventListener('change', applyFilters);
  });
  inputs.forEach((el) => {
    el?.addEventListener('input', applyFilters);
  });

  if (weekInput && !weekInput.value) {
    weekInput.value = formatCalendarDateKey(new Date());
  }

  if (Array.isArray(dataOverride)) {
    window.__BOARD_DELIVERIES__ = dataOverride;
    updateBoardView(dataOverride);
    return;
  }

  if (!skipInitialLoad) {
    loadScheduleBoard({ forceRefresh: true });
  }
}

function initScheduleToggle() {
  const toggle = document.getElementById('scheduleViewToggle');
  const toolbar = document.getElementById('scheduleToolbar');
  const boardView = document.getElementById('boardView');
  const calendarView = document.getElementById('calendarView');
  const listView = document.getElementById('listView');
  if (!toggle || !boardView || !calendarView) return;

  const boardCount = document.getElementById('boardCount');
  const calendarCount = document.getElementById('calendarCount');
  const boardToolbar = document.getElementById('boardToolbarControls');
  const calendarToolbar = document.getElementById('calendarToolbarControls');
  const listToolbar = document.getElementById('listToolbarControls');
  const buttons = Array.from(toggle.querySelectorAll('[data-view]'));

  const setView = (view) => {
    const isBoard = view === 'board';
    const isCalendar = view === 'calendar';
    const isList = view === 'list';
    boardView.classList.toggle('is-hidden', !isBoard);
    calendarView.classList.toggle('is-hidden', !isCalendar);
    if (listView) listView.classList.toggle('is-hidden', !isList);
    if (boardCount) boardCount.classList.toggle('is-hidden', !isBoard);
    if (calendarCount) calendarCount.classList.toggle('is-hidden', !isCalendar);
    if (boardToolbar) boardToolbar.classList.toggle('is-hidden', !isBoard);
    if (calendarToolbar) calendarToolbar.classList.toggle('is-hidden', !isCalendar);
    if (listToolbar) listToolbar.classList.toggle('is-hidden', !isList);
    if (toolbar) {
      toolbar.classList.toggle('is-board', isBoard);
      toolbar.classList.toggle('is-calendar', isCalendar);
      toolbar.classList.toggle('is-list', isList);
    }
    buttons.forEach((btn) => {
      const active = btn.dataset.view === view;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    try {
      localStorage.setItem('schedule_view', view);
    } catch (e) {
      // ignore storage errors
    }
  };

  toggle.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-view]');
    if (!btn) return;
    setView(btn.dataset.view);
  });

  const stored = (() => {
    try {
      return localStorage.getItem('schedule_view');
    } catch (e) {
      return null;
    }
  })();

  if (stored === 'calendar' || stored === 'list') {
    setView(stored);
  } else {
    setView('board');
  }
}

async function refreshScheduleData() {
  try {
    const res = await apiFetch('/deliveries/all', { cache: 'no-store' });
    const data = await res.json();
    const deliveries = Array.isArray(data.deliveries) ? data.deliveries : [];
    window.__SCHEDULE_DELIVERIES__ = deliveries;
    if (typeof window.__BOARD_RENDER__ === 'function') {
      window.__BOARD_RENDER__(deliveries);
    }
    if (typeof window.__CALENDAR_RENDER__ === 'function') {
      window.__CALENDAR_RENDER__(deliveries);
    }
    if (typeof window.__LIST_RENDER__ === 'function') {
      window.__LIST_RENDER__(deliveries);
    }
    return true;
  } catch (e) {
    console.error(e);
    return false;
  }
}

function initScheduleDragAndDrop() {
  const root = document;
  let draggingId = null;

  root.addEventListener('dragstart', (event) => {
    const item = event.target.closest('[data-delivery-id]');
    if (!item) return;
    draggingId = item.dataset.deliveryId || null;
    if (draggingId && event.dataTransfer) {
      event.dataTransfer.setData('text/plain', draggingId);
      event.dataTransfer.effectAllowed = 'move';
    }
    item.classList.add('is-dragging');
    window.__INLINE_DRAG_SUPPRESS__ = true;
  });

  root.addEventListener('dragend', (event) => {
    const item = event.target.closest('[data-delivery-id]');
    if (item) item.classList.remove('is-dragging');
    draggingId = null;
    document.querySelectorAll('[data-dropzone].is-over').forEach((el) => el.classList.remove('is-over'));
    setTimeout(() => {
      window.__INLINE_DRAG_SUPPRESS__ = false;
    }, 150);
  });

  root.addEventListener('dragover', (event) => {
    const zone = event.target.closest('[data-dropzone]');
    if (!zone) return;
    event.preventDefault();
    zone.classList.add('is-over');
  });

  root.addEventListener('dragleave', (event) => {
    const zone = event.target.closest('[data-dropzone]');
    if (!zone) return;
    if (zone.contains(event.relatedTarget)) return;
    zone.classList.remove('is-over');
  });

  root.addEventListener('drop', async (event) => {
    const zone = event.target.closest('[data-dropzone]');
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove('is-over');

    const deliveryId = (event.dataTransfer && event.dataTransfer.getData('text/plain')) || draggingId;
    if (!deliveryId) return;

    const scheduledDate = zone.dataset.date || '';
    zone.classList.add('is-updating');
    try {
      const res = await apiFetch(`/deliveries/${encodeURIComponent(deliveryId)}/reschedule`, {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scheduled_date: scheduledDate || null }),
      });
      if (!res.ok) {
        throw new Error('Reschedule failed');
      }
      await refreshScheduleData();
    } catch (e) {
      console.error(e);
      alert('Could not reschedule delivery. Please try again.');
    } finally {
      zone.classList.remove('is-updating');
    }
  });
}

function initBoardMoreToggle() {
  document.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-more-toggle]');
    if (!btn) return;
    event.preventDefault();
    const scope = btn.closest('[data-more-scope]') || btn.parentElement;
    const list = scope?.querySelector('[data-more-list]');
    if (!list) return;
    const isHidden = list.classList.contains('is-hidden');
    if (isHidden) {
      list.classList.remove('is-hidden');
      btn.textContent = 'Hide';
      btn.dataset.moreOpen = '1';
      list.after(btn);
      return;
    }
    list.classList.add('is-hidden');
    btn.textContent = `+${btn.dataset.moreCount || 0} more`;
    btn.dataset.moreOpen = '';
    list.before(btn);
  });
}

function initInlineEditDrawer() {
  const drawer = document.getElementById('inlineEditDrawer');
  const body = document.getElementById('inlineEditBody');
  const titleEl = document.getElementById('inlineEditTitle');
  if (!drawer || !body) return;

  const open = async (deliveryId) => {
    if (!deliveryId) return;
    if (titleEl) titleEl.textContent = `Delivery ${deliveryId}`;
    drawer.classList.remove('is-hidden');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    body.innerHTML = `<div class="loading"><div class="spinner"></div><div><div class="loading-title">Loading delivery</div><div class="loading-subtitle">Fetching details.</div></div></div>`;

    try {
      const nextParam = encodeURIComponent(`${window.location.pathname}${window.location.search || ''}`);
      const res = await apiFetch(`/admin/deliveries/${encodeURIComponent(deliveryId)}?embed=1&next=${nextParam}`, { cache: 'no-store' });
      const html = await res.text();
      body.innerHTML = html;

      const form = body.querySelector('form[data-inline-form="1"]') || body.querySelector('form');
      if (form) {
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          const submitBtn = form.querySelector('button[type="submit"]');
          const startInput = form.querySelector('[name="window_start"]');
          const endInput = form.querySelector('[name="window_end"]');
          if (startInput) startInput.classList.remove('is-invalid');
          if (endInput) endInput.classList.remove('is-invalid');
          if (startInput || endInput) {
            const result = validateWindowValues(startInput?.value ?? '', endInput?.value ?? '');
            if (!result.ok) {
              if (startInput) startInput.classList.add('is-invalid');
              if (endInput) endInput.classList.add('is-invalid');
              alert(result.message || 'Fix time window values before saving.');
              return;
            }
          }
          if (submitBtn) submitBtn.disabled = true;
          try {
            const formData = new FormData(form);
            const resp = await apiFetch(form.action, {
              method: form.method || 'POST',
              body: formData,
              cache: 'no-store',
            });
            const responseText = resp.ok ? null : await resp.text();
            if (!resp.redirected) {
              const html = responseText || await resp.text();
              body.innerHTML = html;
              return;
            }
            await refreshScheduleData();
            close();
          } catch (e) {
            console.error(e);
            alert('Update failed. Please try again.');
          } finally {
            if (submitBtn) submitBtn.disabled = false;
          }
        });
      }
    } catch (e) {
      console.error(e);
      body.innerHTML = `<div class="empty-state">Failed to load delivery.</div>`;
    }
  };

  const close = () => {
    drawer.classList.add('is-hidden');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    body.innerHTML = '';
  };

  drawer.addEventListener('click', (event) => {
    if (event.target && event.target.matches('[data-drawer-close]')) {
      close();
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !drawer.classList.contains('is-hidden')) {
      close();
    }
  });

  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-ps-toggle]')) return;
    if (event.target.closest('[data-inline-edit-skip]')) return;
    const target = event.target.closest('[data-inline-edit="1"][data-delivery-id]');
    if (!target) return;
    if (window.__INLINE_DRAG_SUPPRESS__) return;
    event.preventDefault();
    open(target.dataset.deliveryId);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (event.target.closest('[data-ps-toggle]')) return;
    if (event.target.closest('[data-inline-edit-skip]')) return;
    const target = event.target.closest('[data-inline-edit="1"][data-delivery-id]');
    if (!target) return;
    if (window.__INLINE_DRAG_SUPPRESS__) return;
    event.preventDefault();
    open(target.dataset.deliveryId);
  });
}

function initPsDeliveredToggle() {
  document.addEventListener('change', async (event) => {
    const toggle = event.target.closest('[data-ps-toggle]');
    if (!toggle) return;
    if (!isManagerOrAdminUser()) {
      toggle.checked = !toggle.checked;
      return;
    }
    event.stopPropagation();
    const deliveryId = toggle.dataset.deliveryId || '';
    if (!deliveryId) return;
    const delivered = Boolean(toggle.checked);
    const dueDate = toggle.dataset.dueDate || '';
    const todayKey = formatCalendarDateKey(new Date());
    toggle.disabled = true;
    toggle.classList.add('is-updating');
    toggle.classList.remove('is-pending', 'is-overdue', 'is-complete');
    if (delivered) {
      toggle.classList.add('is-complete');
    } else if (dueDate && dueDate < todayKey) {
      toggle.classList.add('is-overdue');
    } else {
      toggle.classList.add('is-pending');
    }
    try {
      const res = await apiFetch(`/deliveries/${encodeURIComponent(deliveryId)}/ps-delivered`, {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delivered }),
      });
      if (!res.ok) {
        throw new Error('Update failed');
      }
      await refreshScheduleData();
    } catch (e) {
      console.error(e);
      toggle.checked = !delivered;
      alert('Could not update paperwork status. Please try again.');
      await refreshScheduleData();
    } finally {
      toggle.disabled = false;
      toggle.classList.remove('is-updating');
    }
  });
}

function initScheduleHub() {
  const hasBoard = document.getElementById('scheduleBoard');
  const hasCalendar = document.getElementById('scheduleCalendar');
  const hasList = document.getElementById('scheduleList');
  if (!hasBoard && !hasCalendar && !hasList) return;

  const canEdit = isManagerOrAdminUser();
  if (canEdit) {
    initInlineEditDrawer();
    initScheduleDragAndDrop();
  }
  initBoardMoreToggle();
  initPsDeliveredToggle();

  if (hasBoard && hasCalendar) {
    initScheduleToggle();
    const boardCount = document.getElementById('boardCount');
    const calendarCount = document.getElementById('calendarCount');
    const listTarget = document.getElementById('scheduleList');
    if (boardCount) boardCount.textContent = 'Loading deliveries...';
    if (calendarCount) calendarCount.textContent = 'Loading deliveries...';
    const loading = `<div class="loading"><div class="spinner"></div><div><div class="loading-title">Loading schedule</div><div class="loading-subtitle">Fetching deliveries.</div></div></div>`;
    hasBoard.innerHTML = loading;
    hasCalendar.innerHTML = loading;
    if (listTarget) listTarget.innerHTML = loading;

    apiFetch('/deliveries/all', { cache: 'no-store' })
      .then(res => res.json())
      .then((data) => {
        const deliveries = Array.isArray(data.deliveries) ? data.deliveries : [];
        window.__SCHEDULE_DELIVERIES__ = deliveries;
        initScheduleBoard({ dataOverride: deliveries, skipInitialLoad: true });
        initScheduleCalendar({ dataOverride: deliveries, skipInitialLoad: true });
        if (hasList) initScheduleList({ dataOverride: deliveries, skipInitialLoad: true });
      })
      .catch((e) => {
        console.error(e);
        initScheduleBoard({ dataOverride: [], skipInitialLoad: true });
        initScheduleCalendar({ dataOverride: [], skipInitialLoad: true });
        if (hasList) initScheduleList({ dataOverride: [], skipInitialLoad: true });
        if (boardCount) boardCount.textContent = 'Load failed.';
        if (calendarCount) calendarCount.textContent = 'Load failed.';
      });
    return;
  }

  if (hasBoard) {
    initScheduleBoard();
  }

  if (hasCalendar) {
    initScheduleCalendar();
  }

  if (hasList) {
    initScheduleList();
  }
}

function initPlanMap() {
  const mapEl = document.getElementById('routeMap');
  const mapCard = document.getElementById('routeMapCard');
  if (!mapEl || !mapCard) return;
  if (planMapReady) {
    mapCard.classList.toggle('is-hidden', false);
    return;
  }
  if (!window.mapboxgl || !window.MAPBOX_TOKEN) {
    mapEl.innerHTML = '<div class="empty-state">Map preview requires a Mapbox token.</div>';
    mapCard.classList.toggle('is-hidden', false);
    planMapReady = false;
    return;
  }
  mapboxgl.accessToken = window.MAPBOX_TOKEN;
  planMap = new mapboxgl.Map({
    container: mapEl,
    style: 'mapbox://styles/mapbox/streets-v12',
    center: [Number(window.DEPOT_LON) || -79.38, Number(window.DEPOT_LAT) || 43.64],
    zoom: 10,
  });
  planMap.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right');
  planMap.on('load', () => {
    planMapReady = true;
    mapCard.classList.toggle('is-hidden', false);
    updatePlanMapFromDom();
  });
}

function clearPlanMapMarkers() {
  for (const marker of planMapMarkers) {
    marker.remove();
  }
  planMapMarkers = [];
}

function schedulePlanMapUpdate() {
  if (planMapUpdateTimer) {
    clearTimeout(planMapUpdateTimer);
  }
  planMapUpdateTimer = setTimeout(() => {
    planMapUpdateTimer = null;
    updatePlanMapFromDom();
  }, 120);
}

function updatePlanMapFromDom() {
  if (!planMapReady || !planMap) return;
  const cards = Array.from(document.querySelectorAll('.route-card'));
  const mapCard = document.getElementById('routeMapCard');
  if (!cards.length) {
    if (mapCard) mapCard.classList.add('is-hidden');
    return;
  }
  if (mapCard) mapCard.classList.remove('is-hidden');

  const select = document.getElementById('routeMapSelect');
  if (select) {
    const current = select.value || 'all';
    select.innerHTML = [
      '<option value="all">All routes</option>',
      ...cards.map(card => {
        const id = escapeHtml(card.dataset.routeId || '');
        const label = escapeHtml(`Vehicle ${card.dataset.routeId || ''}`.trim());
        return `<option value="${id}">${label}</option>`;
      }),
    ].join('');
    select.value = current && Array.from(select.options).some(opt => opt.value === current) ? current : 'all';
  }

  const selected = (select && select.value !== 'all') ? select.value : null;
  const routes = cards
    .filter(card => !selected || String(card.dataset.routeId || '') === selected)
    .map(card => {
      const stops = Array.from(card.querySelectorAll('.route-stop'))
        .map(stop => {
          const lat = Number(stop.dataset.lat);
          const lon = Number(stop.dataset.lon);
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
          let label = '';
          if (stop.classList.contains('is-depot')) {
            label = 'Depot';
          } else if (stop.classList.contains('custom-stop')) {
            label = 'Custom stop';
          } else {
            const names = Array.from(stop.querySelectorAll('.route-item .route-main'))
              .map(el => String(el.textContent || '').trim())
              .filter(Boolean);
            if (names.length) {
              label = names.join(' / ');
            }
          }
          if (!label) {
            label = String(stop.querySelector('.route-stop-title')?.textContent || '').trim() || 'Stop';
          }
          if (label.length > 48) {
            label = `${label.slice(0, 45)}...`;
          }
          return { coord: [lon, lat], label };
        })
        .filter(Boolean);
      return { id: card.dataset.routeId || '', stops };
    })
    .filter(r => r.stops.length > 0);

  const routeColors = ['#1f6f66', '#d07a45', '#506fdd', '#c33c3c', '#4c9e8f', '#8b5a2b'];
  const lineFeatures = routes.map((route, idx) => ({
    type: 'Feature',
    geometry: { type: 'LineString', coordinates: route.stops.map(stop => stop.coord) },
    properties: { color: routeColors[idx % routeColors.length] },
  }));
  const pointFeatures = routes.flatMap((route) =>
    route.stops.map((stop, idx) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: stop.coord },
      properties: { index: idx + 1, label: stop.label },
    }))
  );

  if (!planMap.getSource('plan-routes')) {
    planMap.addSource('plan-routes', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: lineFeatures },
    });
    planMap.addLayer({
      id: 'plan-routes-line',
      type: 'line',
      source: 'plan-routes',
      paint: {
        'line-color': ['get', 'color'],
        'line-width': 3,
      },
    });
  } else {
    planMap.getSource('plan-routes').setData({ type: 'FeatureCollection', features: lineFeatures });
  }

  if (!planMap.getSource('plan-stops')) {
    planMap.addSource('plan-stops', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: pointFeatures },
    });
    planMap.addLayer({
      id: 'plan-stops-circle',
      type: 'circle',
      source: 'plan-stops',
      paint: {
        'circle-color': '#1a1917',
        'circle-radius': 4,
        'circle-stroke-width': 2,
        'circle-stroke-color': '#fff',
      },
    });
    planMap.addLayer({
      id: 'plan-stops-labels',
      type: 'symbol',
      source: 'plan-stops',
      layout: {
        'text-field': ['get', 'label'],
        'text-size': 12,
        'text-anchor': 'top',
        'text-offset': [0, 1.1],
        'text-allow-overlap': false,
      },
      paint: {
        'text-color': '#1a1917',
        'text-halo-color': '#ffffff',
        'text-halo-width': 1,
      },
    });
  } else {
    planMap.getSource('plan-stops').setData({ type: 'FeatureCollection', features: pointFeatures });
  }

  if (routes.length === 0) {
    return;
  }
  planMap.resize();
  const bounds = new mapboxgl.LngLatBounds();
  routes.forEach(route => route.stops.forEach(stop => bounds.extend(stop.coord)));
  if (!bounds.isEmpty()) {
    planMap.fitBounds(bounds, { padding: 40, maxZoom: 13, duration: 500 });
  }
}

// -----------------------------
// Meeting intake workflow
// -----------------------------
function initMeetingIntake() {
  const root = document.getElementById('meetingIntake');
  if (!root) return;
  const rowsEl = document.getElementById('meetingRows');
  const addJobsiteBtn = document.getElementById('btnMeetingAddJobsite');
  const publishBtn = document.getElementById('btnMeetingPublish');
  const statusEl = document.getElementById('meetingStatus');
  const modal = document.getElementById('jobsiteModal');
  const modalForm = document.getElementById('jobsiteForm');
  const formErrorEl = document.getElementById('jobsiteFormError');

  let activeRow = null;
  const jobsites = Array.isArray(window.__JOBSITES__) ? window.__JOBSITES__ : [];
  const formatJobsiteLabel = (j) => {
    const name = String(j?.name || '').trim();
    return name || String(j?.id || 'Jobsite');
  };

  const renderJobsiteOptions = (datalist) => {
    if (!datalist) return;
    const options = jobsites.map((j) => {
      const label = formatJobsiteLabel(j);
      return `<option value="${escapeHtml(label)}"></option>`;
    });
    datalist.innerHTML = options.join('');
  };

  const matchJobsiteByLabel = (value) => {
    const q = String(value || '').trim().toLowerCase();
    if (!q) return null;
    const exact = jobsites.find((j) => formatJobsiteLabel(j).toLowerCase() === q);
    if (exact) return exact;
    const starts = jobsites.filter((j) => formatJobsiteLabel(j).toLowerCase().startsWith(q));
    if (starts.length === 1) return starts[0];
    const contains = jobsites.filter((j) => formatJobsiteLabel(j).toLowerCase().includes(q));
    if (contains.length === 1) return contains[0];
    return null;
  };

  const createRow = () => {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td data-label="Sales Order">
        <input class="meeting-sales" placeholder="SO #" />
      </td>
      <td data-label="Job Name">
        <div class="jobsite-picker">
          <input class="jobsite-input" type="search" placeholder="Start typing job name" list="jobsite-list" />
          <input type="hidden" class="jobsite-id" />
        </div>
      </td>
      <td data-label="Scheduled Date">
        <input class="meeting-date" type="date" />
      </td>
      <td data-label="Door">
        <input class="meeting-door" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Frame">
        <input class="meeting-frame" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Med Skid">
        <input class="meeting-med-skid" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Small Skid">
        <input class="meeting-small-skid" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Large Skid">
        <input class="meeting-large-skid" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Box">
        <input class="meeting-box" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Exp Frame">
        <input class="meeting-exp-frame" type="number" min="0" step="1" value="0" />
      </td>
      <td data-label="Actions">
        <button class="btn btn-secondary icon-only meeting-remove" type="button" title="Remove row" aria-label="Remove row">
          <span class="btn-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M18 6l-12 12" />
              <path d="M6 6l12 12" />
            </svg>
          </span>
          <span class="sr-only">Remove</span>
        </button>
      </td>
    `;
    const datalist = document.getElementById('jobsite-list');
    const input = row.querySelector('.jobsite-input');
    const hidden = row.querySelector('.jobsite-id');
    const dateInput = row.querySelector('.meeting-date');
    renderJobsiteOptions(datalist);
    const syncHidden = () => {
      const match = matchJobsiteByLabel(input?.value || '');
      if (hidden) hidden.value = match?.id || '';
    };
    input?.addEventListener('input', syncHidden);
    input?.addEventListener('change', syncHidden);
    input?.addEventListener('blur', syncHidden);

    const openDatePicker = () => {
      if (!dateInput || dateInput.disabled || dateInput.readOnly) return;
      if (typeof dateInput.showPicker === 'function') {
        try {
          dateInput.showPicker();
        } catch (_) {
          // Ignore browsers that restrict programmatic picker opening.
        }
      }
    };

    dateInput?.addEventListener('click', () => {
      openDatePicker();
    });
    dateInput?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDatePicker();
      }
    });

    row.querySelector('.meeting-remove')?.addEventListener('click', () => {
      row.remove();
      ensureTrailingBlankRow();
    });
    return row;
  };

  const rowHasData = (row) => {
    if (!row) return false;
    const sales = String(row.querySelector('.meeting-sales')?.value || '').trim();
    const jobLabel = String(row.querySelector('.jobsite-input')?.value || '').trim();
    const scheduled = String(row.querySelector('.meeting-date')?.value || '').trim();
    const qtyFields = [
      '.meeting-door',
      '.meeting-frame',
      '.meeting-med-skid',
      '.meeting-small-skid',
      '.meeting-large-skid',
      '.meeting-box',
      '.meeting-exp-frame',
    ];
    const hasQty = qtyFields.some((selector) => Number(row.querySelector(selector)?.value || 0) > 0);
    return Boolean(sales || jobLabel || scheduled || hasQty);
  };

  const ensureTrailingBlankRow = () => {
    if (!rowsEl) return;
    const rows = Array.from(rowsEl.querySelectorAll('tr'));
    if (rows.length === 0) {
      rowsEl.appendChild(createRow());
      return;
    }
    const last = rows[rows.length - 1];
    if (rowHasData(last)) {
      rowsEl.appendChild(createRow());
    }
  };

  const showStatus = (message, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = message || '';
    statusEl.dataset.state = isError ? 'error' : 'ok';
  };

  const showFormError = (message = '') => {
    if (!formErrorEl) return;
    formErrorEl.textContent = message || '';
    formErrorEl.classList.toggle('is-hidden', !message);
  };

  const openModal = () => {
    if (!modal) return;
    modal.classList.remove('is-hidden');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
    showFormError('');
    const firstField = modal.querySelector('input, select, button, textarea');
    if (firstField) firstField.focus();
  };

  const closeModal = () => {
    if (!modal) return;
    modal.classList.add('is-hidden');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    if (modalForm) modalForm.reset();
    showFormError('');
  };

  modal?.addEventListener('click', (event) => {
    if (event.target && event.target.matches('[data-modal-close]')) {
      closeModal();
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal && !modal.classList.contains('is-hidden')) {
      closeModal();
    }
  });

  modalForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const formData = new FormData(modalForm);
    const name = String(formData.get('name') || '').trim();
    const address = String(formData.get('address') || '').trim();
    if (!name || !address) {
      showFormError('Jobsite name and address are required.');
      return;
    }
    try {
      const res = await apiFetch('/admin/jobsites/create-json', {
        method: 'POST',
        body: formData,
        cache: 'no-store',
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        const existing = data?.existing;
        const existingLabel = existing ? ` (${existing.name || 'Existing'} - ${existing.address || ''})` : '';
        showFormError(`${data.error || 'Jobsite creation failed'}${existingLabel}`.trim());
        return;
      }
      if (data.jobsite) {
        jobsites.push(data.jobsite);
      }
      if (rowsEl) {
        const list = document.getElementById('jobsite-list');
        renderJobsiteOptions(list);
      }
      if (activeRow) {
        const input = activeRow.querySelector('.jobsite-input');
        const hidden = activeRow.querySelector('.jobsite-id');
        if (input) {
          input.value = formatJobsiteLabel(data.jobsite);
        }
        if (hidden) {
          hidden.value = data.jobsite?.id || '';
        }
      }
      showStatus('Jobsite added.', false);
      closeModal();
    } catch (e) {
      console.error(e);
      showFormError('Could not add jobsite. Try again.');
    }
  });

  const collectRows = () => {
    const rows = Array.from(rowsEl?.querySelectorAll('tr') || []);
    const deliveries = [];
    let hasError = false;
    const errorRows = [];
    rows.forEach((row) => {
      const salesEl = row.querySelector('.meeting-sales');
      const jobLabelEl = row.querySelector('.jobsite-input');
      const hiddenEl = row.querySelector('.jobsite-id');
      const dateEl = row.querySelector('.meeting-date');
      const doorEl = row.querySelector('.meeting-door');
      const frameEl = row.querySelector('.meeting-frame');
      const medSkidEl = row.querySelector('.meeting-med-skid');
      const smallSkidEl = row.querySelector('.meeting-small-skid');
      const largeSkidEl = row.querySelector('.meeting-large-skid');
      const boxEl = row.querySelector('.meeting-box');
      const expFrameEl = row.querySelector('.meeting-exp-frame');

      const parseQty = (el) => {
        const raw = String(el?.value || '').trim();
        if (!raw) return 0;
        const num = Number(raw);
        if (!Number.isFinite(num) || num < 0) return null;
        return Math.floor(num);
      };

      const sales = String(salesEl?.value || '').trim();
      let jobsiteId = String(hiddenEl?.value || '').trim();
      const jobLabel = String(jobLabelEl?.value || '').trim();
      const scheduled = String(dateEl?.value || '').trim();
      const doorVal = parseQty(doorEl);
      const frameVal = parseQty(frameEl);
      const medSkidVal = parseQty(medSkidEl);
      const smallSkidVal = parseQty(smallSkidEl);
      const largeSkidVal = parseQty(largeSkidEl);
      const boxVal = parseQty(boxEl);
      const expFrameVal = parseQty(expFrameEl);
      if ([doorVal, frameVal, medSkidVal, smallSkidVal, largeSkidVal, boxVal, expFrameVal].some((x) => x === null)) {
        row.classList.add('is-invalid');
        showStatus('Door/Frame/Skids/Box/Exp Frame must be whole numbers >= 0.', true);
        hasError = true;
        return;
      }
      const cargoHasData = [doorVal, frameVal, medSkidVal, smallSkidVal, largeSkidVal, boxVal, expFrameVal]
        .some((x) => Number(x || 0) > 0);
      const isEmpty = !sales && !jobLabel && !scheduled && !cargoHasData;
      const isPlaceholder = !sales && !jobLabel && scheduled && !cargoHasData;
      if (isEmpty) {
        row.classList.remove('is-invalid');
        salesEl?.classList.remove('is-invalid');
        jobLabelEl?.classList.remove('is-invalid');
        dateEl?.classList.remove('is-invalid');
        return;
      }
      if (isPlaceholder) {
        row.classList.remove('is-invalid');
        salesEl?.classList.remove('is-invalid');
        jobLabelEl?.classList.remove('is-invalid');
        dateEl?.classList.remove('is-invalid');
        return;
      }
      if (!jobsiteId && jobLabel) {
        const match = matchJobsiteByLabel(jobLabel);
        if (match && hiddenEl) {
          hiddenEl.value = match.id || '';
          jobsiteId = String(hiddenEl.value || '').trim();
        }
      }
      const missing = [];
      if (!sales) missing.push('Sales Order');
      if (!jobsiteId) missing.push('Job Name');
      if (!scheduled) missing.push('Scheduled Date');
      const valid = missing.length === 0;
      row.classList.toggle('is-invalid', !valid);
      salesEl?.classList.toggle('is-invalid', !sales);
      jobLabelEl?.classList.toggle('is-invalid', !jobsiteId);
      dateEl?.classList.toggle('is-invalid', !scheduled);
      if (!valid) {
        hasError = true;
        const rowIndex = rows.indexOf(row) + 1;
        errorRows.push(`Row ${rowIndex}: ${missing.join(', ')}`);
        return;
      }
      deliveries.push({
        sales_order: sales,
        jobsite_id: jobsiteId,
        scheduled_date: scheduled,
        door: Number(doorVal || 0),
        frame: Number(frameVal || 0),
        med_skid: Number(medSkidVal || 0),
        small_skid: Number(smallSkidVal || 0),
        large_skid: Number(largeSkidVal || 0),
        box: Number(boxVal || 0),
        exp_frame: Number(expFrameVal || 0),
      });
    });
    if (hasError) {
      const preview = errorRows.slice(0, 3).join(' | ');
      const more = errorRows.length > 3 ? ` (+${errorRows.length - 3} more)` : '';
      showStatus(`Please complete required fields. ${preview}${more}`, true);
      return null;
    }
    return deliveries;
  };

  addJobsiteBtn?.addEventListener('click', () => {
    const rows = Array.from(rowsEl?.querySelectorAll('tr') || []);
    activeRow = rows[rows.length - 1] || activeRow;
    openModal();
  });

  publishBtn?.addEventListener('click', async () => {
    const deliveries = collectRows();
    if (deliveries === null) {
      return;
    }
    if (deliveries.length === 0) {
      showStatus('Add at least one row before publishing.', true);
      return;
    }
    publishBtn.disabled = true;
    showStatus('Publishing...', false);
    try {
      const res = await apiFetch('/deliveries/bulk-create', {
        method: 'POST',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ deliveries }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || 'Publish failed');
      }
      if (!data.ok) {
        const failures = Array.isArray(data.results)
          ? data.results.filter((r) => !r.ok)
          : [];
        const first = failures[0];
        const detail = first?.detail ? ` (${typeof first.detail === 'string' ? first.detail : JSON.stringify(first.detail)})` : '';
        showStatus(`Publish incomplete. ${data.count || 0} added, ${failures.length} failed. ${first?.error || ''}${detail}`, true);
        return;
      }
      showStatus(`Published ${data.count || deliveries.length} deliveries.`, false);
      if (rowsEl) {
        rowsEl.innerHTML = '';
        rowsEl.appendChild(createRow());
      }
    } catch (e) {
      console.error(e);
      showStatus('Publish failed. Please try again.', true);
    } finally {
      publishBtn.disabled = false;
    }
  });

  if (rowsEl && rowsEl.children.length === 0) {
    rowsEl.appendChild(createRow());
  }

  rowsEl?.addEventListener('input', (event) => {
    const row = event.target?.closest('tr');
    if (row) {
      activeRow = row;
    }
    ensureTrailingBlankRow();
  });

  rowsEl?.addEventListener('change', (event) => {
    const row = event.target?.closest('tr');
    if (row) {
      activeRow = row;
    }
    ensureTrailingBlankRow();
  });

  ensureTrailingBlankRow();
}

enableSorting();
const planBtn = document.getElementById('btnPlan');
if (planBtn) {
  const isPrivileged = isManagerOrAdminUser();
  if (!isPrivileged) {
    planBtn.disabled = true;
    planBtn.textContent = 'Generate Plan (Manager/Admin only)';
  } else {
    planBtn.addEventListener('click', goToPlanPage);
  }
}
document.getElementById('btnPlanRefresh')?.addEventListener('click', runPlanRefreshAction);
document.getElementById('btnPlanRecalc')?.addEventListener('click', recalcPlanTotals);
document.getElementById('btnAddVehicle')?.addEventListener('click', (event) => {
  const type = event?.altKey ? 'van' : 'truck';
  addVehicleToPlan(type);
});
document.getElementById('routeMapSelect')?.addEventListener('change', () => {
  updatePlanMapFromDom();
});
document.getElementById('routeMapFit')?.addEventListener('click', () => {
  updatePlanMapFromDom();
});
const publishBtn = document.getElementById('btnPlanPublish');
if (publishBtn) {
  publishBtn.disabled = true;
  publishBtn.addEventListener('click', runPlanSaveAction);
}

loadPlanFromQuery();
initCustomStopModal();
initMeetingIntake();
initPublishedPlans();
initScheduleHub();
initDriverOptions();
if (document.getElementById('publishStatus')) {
  applyRoutingModeUI();
}

const routesOutput = document.getElementById('routesOutput');
if (routesOutput && typeof MutationObserver !== 'undefined') {
  const observer = new MutationObserver(() => schedulePlanMapUpdate());
  observer.observe(routesOutput, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['data-lat', 'data-lon'],
  });
}
