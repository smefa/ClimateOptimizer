// TrueTemp holiday mode card.
//
// Ships as a second, independent custom element from truetemp-card.js on
// purpose — small files, load-size isolation, and this is the integration's
// first *interactive* card (the two dates, the setback number and the arm
// switch all write back through hass.callService, where the read-only
// truetemp-card.js only ever displays). The ~30-line device-based discovery
// helper below is duplicated from that file rather than shared, which is the
// smallest reasonable amount of duplication for the isolation this buys — a
// shared truetemp-card-common.js is the fallback if that ever grows
// unpleasant, not needed at this size.
//
// Add it with:
//     type: custom:truetemp-holiday-card
//     entity: sensor.<name>_holiday_status
//
// Any entity belonging to the zone works — the rest (the two dates, the
// setback number, the arm switch, the climate entity for the card title) are
// discovered through the frontend entity registry, so renaming entities never
// breaks it. See truetemp-card.js's own comment for why translation_key,
// not entity_id, is what's matched on first.

import { STRINGS as EN } from "./lang/en.js";
import { STRINGS as DE } from "./lang/de.js";
import { STRINGS as SV } from "./lang/sv.js";

const TRANSLATION_KEYS = {
  status: "holiday_status",
  start: "holiday_start",
  end: "holiday_end",
  target: "holiday_target_temperature",
  armed: "holiday_mode",
};

// Fallback for a registry entry lacking translation_key — English-only, see
// truetemp-card.js's own SUFFIXES for why this is a secondary path.
const SUFFIXES = {
  status: "holiday_status",
  start: "holiday_start",
  end: "holiday_end",
  target: "holiday_setback_temperature",
  armed: "holiday_mode",
};

const STRINGS = {
  en: EN,
  de: { ...EN, ...DE },
  sv: { ...EN, ...SV },
};

function pickLang(hass) {
  const raw = (hass && (hass.language || (hass.locale && hass.locale.language))) || "en";
  const base = String(raw).split("-")[0].toLowerCase();
  return STRINGS[base] ? base : "en";
}

const PHASE_KEYS = {
  inactive: "holidayPhaseInactive",
  invalid: "holidayPhaseInvalid",
  scheduled: "holidayPhaseScheduled",
  setback: "holidayPhaseSetback",
  ramping: "holidayPhaseRamping",
  done: "holidayPhaseDone",
};

// Phases where "on_track: false" means something worth surfacing (the ramp
// couldn't fit between the two dates). Not shown for "done"/"inactive" —
// on_track carries no news once the trip is over or was never armed.
const OFF_TRACK_PHASES = new Set(["scheduled", "setback", "ramping"]);

class TrueTempHolidayCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error(STRINGS[pickLang(this._hass)].holidaySetEntityError);
    }
    this._config = config;
    this._entities = null;
    this._built = false;
  }

  getCardSize() {
    return 5;
  }

  static getConfigElement() {
    return document.createElement("truetemp-holiday-card-editor");
  }

  // Picks any _holiday_status entity belonging to this integration, so
  // dropping the card from the picker works before the user has touched
  // YAML.
  static getStubConfig(hass) {
    const entityId = Object.keys(hass.entities || {}).find(
      (id) => hass.entities[id].platform === "truetemp" && id.endsWith("_holiday_status"),
    );
    return { entity: entityId || "" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entities) {
      this._entities = this._discover(hass);
    }
    this._render();
  }

  // Find the zone's other holiday entities via the device the configured one
  // belongs to. Falls back to the configured entity alone if the registry is
  // not reachable, so the card degrades to something rather than nothing.
  _discover(hass) {
    const found = { climate: null };
    Object.keys(SUFFIXES).forEach((key) => {
      found[key] = null;
    });

    const configured = this._config.entity;
    const entry = hass.entities && hass.entities[configured];
    const deviceId = entry && entry.device_id;
    if (!deviceId || !hass.entities) {
      found.status = configured;
      return found;
    }

    Object.keys(hass.entities).forEach((entityId) => {
      const info = hass.entities[entityId];
      if (info.device_id !== deviceId) return;
      if (entityId.startsWith("climate.")) {
        found.climate = entityId;
        return;
      }
      if (info.translation_key) {
        Object.entries(TRANSLATION_KEYS).forEach(([key, tKey]) => {
          if (info.translation_key === tKey) found[key] = entityId;
        });
        return;
      }
      Object.entries(SUFFIXES).forEach(([key, suffix]) => {
        if (entityId.endsWith(`_${suffix}`)) found[key] = entityId;
      });
    });
    if (!found.status) found.status = configured;
    return found;
  }

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    return this._hass.states[entityId] || null;
  }

  // Several of these strings are user-controlled (entity friendly names, the
  // free-text reason), and everything interpolated below reaches the DOM as
  // markup or textContent — see truetemp-card.js's identical helper.
  _esc(value) {
    return String(value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  _num(value, digits = 1, unit = "") {
    const parsed = Number(value);
    if (value === null || value === undefined || value === "" || Number.isNaN(parsed)) {
      return "—";
    }
    return `${parsed.toFixed(digits)}${unit}`;
  }

  _fmtDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  }

  // Builds the static DOM once (and again only on a language change).
  // Inputs are updated in place on every `_render` after that, never torn
  // down — rebuilding them every tick would blur an input mid-edit and drop
  // a keystroke, which the read-only card in truetemp-card.js never has to
  // worry about.
  _build(t) {
    this.innerHTML = `
      <ha-card header="${this._esc(t.holidayCardTitle)}">
        <div class="ho-body">
          <div class="ho-row">
            <label>${this._esc(t.holidayLabelStart)}</label>
            <input type="date" class="ho-start">
          </div>
          <div class="ho-row">
            <label>${this._esc(t.holidayLabelEnd)}</label>
            <input type="date" class="ho-end">
          </div>
          <div class="ho-row">
            <label>${this._esc(t.holidayLabelTarget)}</label>
            <input type="number" step="0.5" class="ho-target">
          </div>
          <div class="ho-row">
            <label>${this._esc(t.holidayLabelArmed)}</label>
            <label class="ho-switch">
              <input type="checkbox" class="ho-armed">
              <span class="ho-slider"></span>
            </label>
          </div>
          <div class="ho-status"><span class="ho-dot"></span><span class="ho-phase"></span></div>
          <div class="ho-grid">
            <div class="ho-cell"><span>${this._esc(t.holidayRowRampStarts)}</span><b class="ho-ramp-start">—</b></div>
            <div class="ho-cell"><span>${this._esc(t.holidayRowBackBy)}</span><b class="ho-return-at">—</b></div>
            <div class="ho-cell"><span>${this._esc(t.holidayRowHoursNeeded)}</span><b class="ho-hours-needed">—</b></div>
          </div>
          <div class="ho-off-track" hidden></div>
          <div class="ho-reason"></div>
        </div>
      </ha-card>
      <style>
        .ho-body { padding: 0 16px 16px; display:flex; flex-direction:column; gap:10px; }
        .ho-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
        .ho-row label { font-size:13px; color: var(--primary-text-color); }
        .ho-row input[type="date"], .ho-row input[type="number"] {
          font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
          border: 1px solid var(--divider-color); border-radius:4px; padding:5px 8px; width:150px;
        }
        .ho-switch { position:relative; display:inline-block; width:40px; height:22px; flex:none; }
        .ho-switch input { opacity:0; width:0; height:0; }
        .ho-slider { position:absolute; inset:0; background: var(--divider-color); border-radius:22px;
                     transition:.15s; cursor:pointer; }
        .ho-slider::before { content:""; position:absolute; width:16px; height:16px; left:3px; top:3px;
                              background:#fff; border-radius:50%; transition:.15s; }
        .ho-switch input:checked + .ho-slider { background: var(--primary-color); }
        .ho-switch input:checked + .ho-slider::before { transform: translateX(18px); }
        .ho-status { display:flex; align-items:center; gap:8px; font-size:14px; margin-top:4px; }
        .ho-dot { width:9px; height:9px; border-radius:50%; background: var(--disabled-text-color, #999); flex:none; }
        .ho-setback .ho-dot { background: var(--warning-color, #fa3); }
        .ho-ramping .ho-dot { background: var(--info-color, #39a); }
        .ho-scheduled .ho-dot, .ho-done .ho-dot { background: var(--success-color, #4c1); }
        .ho-invalid .ho-dot { background: var(--error-color, #d33); }
        .ho-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:8px; }
        .ho-cell { display:flex; flex-direction:column; gap:2px; padding:8px 10px; border-radius:4px;
                   background: var(--secondary-background-color); border:1px solid var(--divider-color); }
        .ho-cell span { font-size:11px; color: var(--secondary-text-color); }
        .ho-cell b { font-size:14px; font-weight:500; font-variant-numeric: tabular-nums; }
        .ho-off-track { font-size:12px; line-height:1.5; color: var(--primary-text-color);
                         border-left:3px solid var(--warning-color, #fa3); padding-left:10px; }
        .ho-reason { font-size:11px; line-height:1.5; color: var(--secondary-text-color); }
      </style>
    `;

    const els = {
      start: this.querySelector(".ho-start"),
      end: this.querySelector(".ho-end"),
      target: this.querySelector(".ho-target"),
      armed: this.querySelector(".ho-armed"),
      status: this.querySelector(".ho-status"),
      phase: this.querySelector(".ho-phase"),
      rampStart: this.querySelector(".ho-ramp-start"),
      returnAt: this.querySelector(".ho-return-at"),
      hoursNeeded: this.querySelector(".ho-hours-needed"),
      offTrack: this.querySelector(".ho-off-track"),
      reason: this.querySelector(".ho-reason"),
    };

    els.start.addEventListener("change", (ev) => this._setDate("start", ev.target.value));
    els.end.addEventListener("change", (ev) => this._setDate("end", ev.target.value));
    els.target.addEventListener("change", (ev) => this._setTarget(ev.target.value));
    els.armed.addEventListener("change", (ev) => this._setArmed(ev.target.checked));

    this._els = els;
    this._built = true;
  }

  _setDate(which, value) {
    const entityId = which === "start" ? this._entities.start : this._entities.end;
    if (!entityId || !value || !this._hass) return;
    this._hass.callService("date", "set_value", { entity_id: entityId, date: value });
  }

  _setTarget(value) {
    const entityId = this._entities.target;
    const parsed = Number(value);
    if (!entityId || Number.isNaN(parsed) || !this._hass) return;
    this._hass.callService("number", "set_value", { entity_id: entityId, value: parsed });
  }

  _setArmed(on) {
    const entityId = this._entities.armed;
    if (!entityId || !this._hass) return;
    this._hass.callService("switch", on ? "turn_on" : "turn_off", { entity_id: entityId });
  }

  _render() {
    if (!this._hass || !this._entities) return;
    const lang = pickLang(this._hass);
    const t = STRINGS[lang];
    if (!this._built || this._lastLang !== lang) {
      this._build(t);
      this._lastLang = lang;
    }

    const start = this._state(this._entities.start);
    const end = this._state(this._entities.end);
    const target = this._state(this._entities.target);
    const armed = this._state(this._entities.armed);
    const status = this._state(this._entities.status);
    const attrs = (status && status.attributes) || {};

    const els = this._els;
    // Never overwrite an input the user is actively editing — a periodic
    // coordinator refresh elsewhere in the zone must not steal a keystroke
    // or reset a half-typed value out from under them.
    const focused = this.contains(document.activeElement) ? document.activeElement : null;

    if (els.start !== focused) {
      els.start.value = start && start.state && start.state !== "unknown" ? start.state : "";
    }
    if (els.end !== focused) {
      els.end.value = end && end.state && end.state !== "unknown" ? end.state : "";
    }
    if (els.target !== focused) {
      els.target.value = target && target.state && target.state !== "unknown" ? target.state : "";
    }
    if (els.armed !== focused) {
      els.armed.checked = !!(armed && armed.state === "on");
    }

    const phaseKey = status ? status.state : "inactive";
    els.status.className = `ho-status ho-${this._esc(phaseKey)}`;
    els.phase.textContent = t[PHASE_KEYS[phaseKey]] || phaseKey;

    els.rampStart.textContent = this._fmtDateTime(attrs.ramp_start_at);
    els.returnAt.textContent = this._fmtDateTime(attrs.return_at);
    els.hoursNeeded.textContent = this._num(attrs.hours_needed, 1, " h");

    const offTrack = attrs.on_track === false && OFF_TRACK_PHASES.has(phaseKey);
    els.offTrack.hidden = !offTrack;
    els.offTrack.textContent = offTrack ? t.holidayNoteOffTrack : "";

    els.reason.textContent = attrs.reason || "";
  }
}

customElements.define("truetemp-holiday-card", TrueTempHolidayCard);

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "truetemp" } } },
];

// Thin wrapper around ha-form, same reasoning as TrueTempCardEditor in
// truetemp-card.js: the config is a single required entity, not worth a
// hand-rolled form.
class TrueTempHolidayCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const t = STRINGS[pickLang(this._hass)];
    const labels = { entity: t.holidayEditorEntity };
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        this._config = ev.detail.value;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config } }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = EDITOR_SCHEMA;
    this._form.computeLabel = (schema) => labels[schema.name] || schema.name;
  }
}

customElements.define("truetemp-holiday-card-editor", TrueTempHolidayCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "truetemp-holiday-card",
  name: "TrueTemp Holiday Mode",
  description: "Plan a holiday setback: pick leave/return dates and a setback temperature, then arm it.",
});
