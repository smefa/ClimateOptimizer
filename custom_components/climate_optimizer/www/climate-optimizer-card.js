// ClimateOptimizer feature-test demo card.
//
// Not a polished production card: it exists to prove every sensor/number/
// switch/select this integration exposes is reachable and renderable from a
// dashboard, and to make Phase 2/3 shadow-mode output (RC model, MPC plan)
// inspectable at a glance. Point `entity` at any one entity created by a
// ClimateOptimizer zone and the rest of that zone's entities are discovered
// automatically via the frontend entity registry (falling back to entity_id
// suffix matching if the registry lookup isn't available).
//
// Layout mirrors the "Shared: / Control: / Auto-tune: / MPC:" entity-name
// prefixes (see sensor.py), so a number's section on the card and its name
// on the device page always agree, and the section notes say which group can
// actually reach the output.
//
// Plain custom element (no Lit/build step) so it ships as a single static
// file with zero bundling, matching how HACS-distributed cards are normally
// authored.

const FIELDS = [
  // Ordered longest-suffix-first: matching is "does entity_id end with this",
  // so a more specific suffix (e.g. compensated_outdoor_temperature) must be
  // tried before a shorter one it also happens to end with (outdoor_temperature).
  //
  // Suffixes deliberately exclude the name prefix. Existing installs keep the
  // entity_ids they were first registered with, so on those the id has no
  // prefix at all, while a fresh install gets e.g.
  // sensor.<zone>_auto_tune_rc_model_heat_pump_gain. Names were only ever
  // prefixed, never reworded, so an endsWith match still covers both.
  { key: "compensated_outdoor_temperature", suffix: "compensated_outdoor_temperature", label: "Compensated outdoor temp", kind: "temp", hero: true },
  { key: "indoor_temperature_error", suffix: "indoor_temperature_error", label: "Indoor temp error", kind: "temp" },
  { key: "indoor_temperature", suffix: "indoor_temperature", label: "Indoor temp", kind: "temp" },
  { key: "outdoor_temperature", suffix: "outdoor_temperature", label: "Outdoor temp (raw)", kind: "temp" },
  { key: "price_shift_applied", suffix: "price_shift_applied", label: "Price shift applied", kind: "temp" },
  { key: "power_draw", suffix: "power_draw", label: "Power draw", kind: "power" },
  { key: "status", suffix: "status", label: "Status", kind: "status" },
  { key: "rc_thermal_time_constant", suffix: "rc_model_thermal_time_constant", label: "RC time constant (τ)", kind: "hours" },
  { key: "rc_heat_pump_gain", suffix: "rc_model_heat_pump_gain", label: "RC heat-pump gain", kind: "coeff" },
  { key: "rc_solar_gain", suffix: "rc_model_solar_gain", label: "RC solar gain", kind: "coeff" },
  { key: "rc_wind_gain", suffix: "rc_model_wind_gain", label: "RC wind gain", kind: "coeff" },
  { key: "rc_model_confidence", suffix: "rc_model_confidence", label: "RC confidence", kind: "percent" },
  { key: "rc_prediction_error", suffix: "rc_model_prediction_error", label: "RC prediction error", kind: "temp" },
  { key: "autotune_blend", suffix: "auto_tune_blend", label: "Auto-tune blend", kind: "percent" },
  { key: "autotune_effective_k_indoor", suffix: "auto_tune_effective_k_indoor", label: "Effective k_indoor", kind: "coeff" },
  { key: "tuning_mode", suffix: "tuning_mode", label: "Tuning mode", kind: "tier" },
  { key: "mpc_recommended_delta", suffix: "mpc_recommended_compensation_delta", label: "MPC recommended delta", kind: "temp" },
  { key: "mpc_status", suffix: "mpc_status", label: "MPC status", kind: "mpc_status" },
  { key: "mpc_projected_savings", suffix: "mpc_projected_savings", label: "MPC projected savings", kind: "coeff" },
  { key: "mpc_planned_next_temperature", suffix: "mpc_planned_next_indoor_temperature", label: "MPC planned next temp", kind: "temp" },
  { key: "active", suffix: "compensation_active", label: "Compensation active", kind: "switch" },
  { key: "indoor_target_temperature", suffix: "indoor_target_temperature", label: "Indoor target", kind: "temp" },
  { key: "price_comfort_tier", suffix: "price_comfort_tier", label: "Price comfort tier", kind: "tier" },
].sort((a, b) => b.suffix.length - a.suffix.length);

// The control law's per-term breakdown lives only in the main sensor's
// attributes, never on a sensor of its own. Surfaced here so the "Control"
// section shows what is actually being applied rather than a single tile —
// otherwise the card would look as though almost everything happens in
// auto-tune, purely because that side has more entities.
const CONTROL_TERMS = [
  { attr: "indoor_adjustment_c", label: "Indoor term" },
  { attr: "wind_adjustment_c", label: "Wind term" },
  { attr: "sun_adjustment_c", label: "Sun term" },
  { attr: "price_adjustment_c", label: "Price term" },
  { attr: "total_adjustment_c", label: "Total adjustment" },
];

const STATUS_COLOR = {
  ok: "var(--success-color, #4caf50)",
  degraded: "var(--warning-color, #ff9800)",
  not_trustworthy: "var(--warning-color, #ff9800)",
  infeasible: "var(--warning-color, #ff9800)",
  error: "var(--error-color, #f44336)",
  no_forecast: "var(--error-color, #f44336)",
  no_data: "var(--error-color, #f44336)",
  degenerate: "var(--error-color, #f44336)",
};

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function classify(entityId, hass) {
  const regEntry = hass.entities && hass.entities[entityId];
  // If the registry confirms this entity belongs to a different integration,
  // never suffix-guess it — plenty of unrelated integrations also have a
  // "*_status" or similar entity, and a whole-instance scan (getStubConfig)
  // would otherwise happily match someone else's sensor.
  if (regEntry && regEntry.platform && regEntry.platform !== "climate_optimizer") {
    return null;
  }
  const tkey = regEntry && regEntry.translation_key;
  if (tkey) {
    const byKey = FIELDS.find((f) => f.key === tkey);
    if (byKey) return byKey;
  }
  const lower = entityId.toLowerCase();
  return FIELDS.find((f) => lower.endsWith(f.suffix)) || null;
}

function fmtNumber(v, digits) {
  return Number.isFinite(v) ? v.toFixed(digits) : "–";
}

function chip(text, color) {
  return `<span class="chip" style="color:${color};background:color-mix(in srgb, ${color} 16%, transparent)">${esc(text)}</span>`;
}

function sparkline(values) {
  if (!Array.isArray(values) || values.length < 2) return "";
  const width = 280;
  const height = 44;
  const padY = 5;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const stepX = width / (values.length - 1);
  const pts = values.map((v, i) => [
    i * stepX,
    padY + (1 - (v - min) / span) * (height - padY * 2),
  ]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const dots = pts
    .map(([x, y], i) => (
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="7" fill="transparent">` +
      `<title>+${i}h: ${values[i].toFixed(2)} °C</title></circle>`
    ))
    .join("");
  const color = "var(--primary-color, #03a9f4)";
  return `
    <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}"
         preserveAspectRatio="none" role="img"
         aria-label="Predicted indoor temperature trajectory, ${fmtNumber(min, 1)} to ${fmtNumber(max, 1)} degC over ${values.length} steps">
      <polyline points="${line}" fill="none" stroke="${color}" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
      <circle cx="${pts[0][0].toFixed(1)}" cy="${pts[0][1].toFixed(1)}" r="3" fill="${color}" />
      <circle cx="${pts[pts.length - 1][0].toFixed(1)}" cy="${pts[pts.length - 1][1].toFixed(1)}" r="3" fill="${color}" />
      ${dots}
    </svg>
    <div class="spark-caption">${fmtNumber(min, 1)}–${fmtNumber(max, 1)} °C over ${values.length} planned steps</div>`;
}

class ClimateOptimizerCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("climate-optimizer-card: `entity` is required (any ClimateOptimizer entity for the zone)");
    }
    this._config = config;
    this._lastSig = null;
  }

  static getStubConfig(hass) {
    const guess = Object.keys(hass.states).find((id) => classify(id, hass)?.key === "status");
    return { entity: guess || "" };
  }

  getCardSize() {
    return 8;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    const ids = this._discoverEntityIds(hass);
    // last_updated, not last_changed: several tiles (the heuristic's per-term
    // breakdown, the MPC trajectory) are read from attributes, which move
    // without the state string changing. Re-rendering once per coordinator
    // cycle is cheap; showing a stale breakdown is not.
    const sig = ids
      .map((id) => {
        const s = hass.states[id];
        return s ? `${id}:${s.state}:${s.last_updated}` : `${id}:x`;
      })
      .join("|");
    if (sig === this._lastSig) return;
    this._lastSig = sig;
    this._render(ids);
  }

  _discoverEntityIds(hass) {
    const entry = hass.entities && hass.entities[this._config.entity];
    const deviceId = entry && entry.device_id;
    if (!deviceId) return [this._config.entity];
    return Object.values(hass.entities)
      .filter((e) => e.device_id === deviceId)
      .map((e) => e.entity_id);
  }

  _render(ids) {
    const hass = this._hass;
    const found = new Map();
    for (const id of ids) {
      const field = classify(id, hass);
      if (field) found.set(field.key, { field, state: hass.states[id], id });
    }

    const primaryState = hass.states[this._config.entity];
    if (!primaryState) {
      this._setContent(`<ha-card><div class="card-content">Entity not found: ${esc(this._config.entity)}</div></ha-card>`);
      return;
    }
    const deviceName = (hass.entities && hass.entities[this._config.entity]?.name)
      || primaryState.attributes.friendly_name
      || "ClimateOptimizer";

    const hero = found.get("compensated_outdoor_temperature");
    const heroValue = hero ? fmtNumber(Number(hero.state.state), 1) : "–";

    const statusEntry = found.get("status");
    const statusChip = statusEntry
      ? chip(statusEntry.state.state, STATUS_COLOR[statusEntry.state.state] || "var(--secondary-text-color)")
      : "";
    const activeEntry = found.get("active");
    const activeChip = activeEntry
      ? chip(activeEntry.state.state === "on" ? "compensation on" : "learn mode", activeEntry.state.state === "on" ? "var(--success-color, #4caf50)" : "var(--secondary-text-color)")
      : "";
    const tierEntry = found.get("price_comfort_tier");
    const tierChip = tierEntry ? chip(`tier: ${tierEntry.state.state}`, "var(--secondary-text-color)") : "";

    const statRow = (keys) => keys
      .map((k) => found.get(k))
      .filter(Boolean)
      .map(({ field, state }) => this._statTile(field, state))
      .join("");

    const mpcDelta = found.get("mpc_recommended_delta");
    const mpcStatus = found.get("mpc_status");
    const trajectory = mpcDelta?.state.attributes.predicted_trajectory;
    const mpcReason = mpcDelta?.state.attributes.reason || mpcStatus?.state.attributes.reason || "";

    this._setContent(`
      <ha-card>
        <style>${STYLE}</style>
        <div class="card-content">
          <div class="header-row">
            <div class="device-name">${esc(deviceName)}</div>
            <div class="chips">${statusChip}${activeChip}${tierChip}</div>
          </div>

          <div class="hero">
            <span class="hero-value">${heroValue}<span class="hero-unit">°C</span></span>
            <span class="hero-label">compensated outdoor temperature — the only published output</span>
          </div>

          <div class="section-title">Shared <span class="section-note">(inputs and health — identical in either tuning mode)</span></div>
          <div class="stat-grid">
            ${statRow(["indoor_temperature", "outdoor_temperature", "indoor_temperature_error", "indoor_target_temperature", "power_draw"])}
          </div>

          <div class="section-title">Control <span class="section-note">(what is actually being applied, in either mode)</span></div>
          <div class="stat-grid">
            ${statRow(["price_shift_applied"])}
            ${this._controlTermTiles(hero)}
          </div>

          <div class="section-title">Auto-tune · learned house model <span class="section-note">(the fit the coefficients are derived from)</span></div>
          <div class="stat-grid">
            ${statRow(["rc_thermal_time_constant", "rc_model_confidence", "rc_heat_pump_gain", "rc_solar_gain", "rc_wind_gain", "rc_prediction_error"])}
          </div>

          <div class="section-title">Auto-tune · derived coefficients <span class="section-note">(the only thing here that can reach the output, and only in Auto mode)</span></div>
          <div class="stat-grid">
            ${statRow(["tuning_mode", "autotune_blend", "autotune_effective_k_indoor"])}
          </div>

          <div class="section-title">MPC planner <span class="section-note">(advisory only — tunes nothing, drives nothing)</span></div>
          <div class="stat-grid">
            ${statRow(["mpc_recommended_delta", "mpc_status", "mpc_projected_savings", "mpc_planned_next_temperature"])}
          </div>
          ${trajectory ? `<div class="trajectory" title="${esc(mpcReason)}">${sparkline(trajectory)}</div>` : ""}

          <div class="footer">Feature test: ${found.size}/${FIELDS.length} known ClimateOptimizer entities discovered on this device.</div>
        </div>
      </ha-card>`);
  }

  _statTile(field, state) {
    const raw = state.state;
    let value;
    if (raw === "unavailable" || raw === "unknown") {
      value = "–";
    } else if (field.kind === "temp") {
      value = `${fmtNumber(Number(raw), 1)} °C`;
    } else if (field.kind === "hours") {
      value = `${fmtNumber(Number(raw), 1)} h`;
    } else if (field.kind === "power") {
      value = `${fmtNumber(Number(raw), 0)} W`;
    } else if (field.kind === "percent") {
      value = `${fmtNumber(Number(raw), 0)}%`;
    } else if (field.kind === "coeff") {
      value = fmtNumber(Number(raw), 4);
    } else if (field.kind === "mpc_status") {
      return `<div class="stat"><span class="stat-label">${esc(field.label)}</span>${chip(raw, STATUS_COLOR[raw] || "var(--secondary-text-color)")}</div>`;
    } else {
      value = esc(raw);
    }
    return `<div class="stat"><span class="stat-label">${esc(field.label)}</span><span class="stat-value">${value}</span></div>`;
  }

  // Per-term breakdown of the control law's recommendation, read off the main
  // sensor's attributes. These are always the *recommendation*: while the
  // activation switch is off they are computed but not applied, so the tiles
  // are marked as such rather than silently implying the house is being driven
  // by them.
  _controlTermTiles(hero) {
    if (!hero) return "";
    const attrs = hero.state.attributes || {};
    const suffix = attrs.active === false ? " (rec.)" : "";
    return CONTROL_TERMS
      .filter(({ attr }) => Number.isFinite(Number(attrs[attr])))
      .map(({ attr, label }) => (
        `<div class="stat"><span class="stat-label">${esc(label + suffix)}</span>` +
        `<span class="stat-value">${fmtNumber(Number(attrs[attr]), 2)} °C</span></div>`
      ))
      .join("");
  }

  _setContent(html) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = html;
  }
}

const STYLE = `
  .card-content { display: flex; flex-direction: column; gap: 10px; }
  .header-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .device-name { font-size: 16px; font-weight: 600; color: var(--primary-text-color); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .hero { display: flex; flex-direction: column; align-items: flex-start; padding: 4px 0 8px; }
  .hero-value { font-size: 34px; font-weight: 700; color: var(--primary-text-color); line-height: 1; }
  .hero-unit { font-size: 18px; font-weight: 500; color: var(--secondary-text-color); margin-left: 2px; }
  .hero-label { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--primary-text-color); border-top: 1px solid var(--divider-color); padding-top: 8px; }
  .section-note { font-weight: 400; color: var(--secondary-text-color); }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
  .stat { display: flex; flex-direction: column; gap: 2px; padding: 6px 8px; border-radius: 8px; background: var(--secondary-background-color, rgba(0,0,0,0.04)); }
  .stat-label { font-size: 11px; color: var(--secondary-text-color); }
  .stat-value { font-size: 15px; font-weight: 600; color: var(--primary-text-color); }
  .trajectory { padding-top: 4px; }
  .spark-caption { font-size: 11px; color: var(--secondary-text-color); text-align: right; }
  .footer { font-size: 11px; color: var(--secondary-text-color); border-top: 1px solid var(--divider-color); padding-top: 6px; }
`;

customElements.define("climate-optimizer-card", ClimateOptimizerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "climate-optimizer-card",
  name: "ClimateOptimizer (feature test)",
  description: "Diagnostic demo card: every ClimateOptimizer sensor/number/switch/select for one zone, including RC model and MPC shadow-mode output.",
  preview: false,
});
