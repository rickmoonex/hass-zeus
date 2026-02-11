import {
  LitElement,
  html,
  css,
  nothing,
} from "https://unpkg.com/lit-element@4.1.1/lit-element.js?module";

/**
 * Zeus Scheduler Card v2.0.0 — LitElement + HA native components.
 *
 * Page 1: Device list — tap a device to open it.
 * Page 2: If dynamic cycle duration -> hours:minutes input + confirm.
 *         Slot list (tap to reserve) OR reserved countdown + cancel.
 *
 * Resource URL: /zeus/zeus-scheduler-card.js  (type: module)
 * Card type:    custom:zeus-scheduler-card
 */

const CARD_VERSION = "2.0.0";

/* MDI icon SVG paths */
const mdiArrowLeft =
  "M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z";
const mdiChevronRight =
  "M8.59 16.59L13.17 12 8.59 7.41 10 6l6 6-6 6z";
const mdiPowerPlugOutline =
  "M16 7V3h-2v4h-4V3H8v4H6v7.5L9.5 18v3h5v-3l3.5-3.5V7h-2m0 7.17l-3 3V20h-2v-2.83l-3-3V9h8v5.17Z";

class ZeusSchedulerCard extends LitElement {
  static get properties() {
    return {
      hass: { attribute: false },
      _config: { state: true },
      _page: { state: true },
      _devices: { state: true },
    };
  }

  constructor() {
    super();
    this._config = {};
    this._page = null; // null = device list, number = device index
    this._devices = [];
    this._tickInterval = null;
  }

  setConfig(config) {
    this._config = config;
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {};
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;
    this._devices = this._findDevices();
    this.requestUpdate("hass", oldHass);
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    super.connectedCallback();
    this._startTick();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stopTick();
  }

  /* ---------------------------------------------------------------- */
  /*  Countdown tick (for reserved state)                              */
  /* ---------------------------------------------------------------- */

  _startTick() {
    if (this._tickInterval) return;
    this._tickInterval = setInterval(() => {
      const el = this.shadowRoot?.querySelector(".countdown");
      if (!el) return;
      const dev = this._devices[this._page];
      if (dev?.attrs?.reserved && dev.attrs.reservation_start) {
        el.textContent = this._countdown(
          dev.attrs.reservation_start,
          dev.attrs.reservation_end,
        );
      }
    }, 1000);
  }

  _stopTick() {
    if (this._tickInterval) {
      clearInterval(this._tickInterval);
      this._tickInterval = null;
    }
  }

  /* ---------------------------------------------------------------- */
  /*  Device discovery                                                 */
  /* ---------------------------------------------------------------- */

  _findDevices() {
    if (!this._hass) return [];
    const out = [];
    for (const [id, s] of Object.entries(this._hass.states)) {
      if (!id.startsWith("sensor.")) continue;
      const a = s.attributes || {};
      if (a.subentry_id === undefined || a.ranked_windows === undefined)
        continue;
      // Derive the device name from HA's device registry, not friendly_name
      // (friendly_name includes the entity suffix like "recommended start")
      let devName = null;
      const entReg = this._hass.entities?.[id];
      if (entReg?.device_id && this._hass.devices) {
        const dev = this._hass.devices[entReg.device_id];
        if (dev) devName = dev.name || dev.name_by_user;
      }
      if (!devName) {
        // Fallback: strip " recommended start" suffix from friendly_name
        devName = (a.friendly_name || id.replace("sensor.", "").replace(/_/g, " "))
          .replace(/\s+recommended start$/i, "");
      }

      out.push({
        entityId: id,
        subentryId: a.subentry_id,
        name: devName,
        attrs: a,
        numberEntityId: a.number_entity_id || null,
      });
    }
    return out;
  }

  /* ---------------------------------------------------------------- */
  /*  Actions                                                          */
  /* ---------------------------------------------------------------- */

  async _reserve(subId, startIso) {
    if (!this._hass) return;
    try {
      await this._hass.callService("zeus", "reserve_manual_device", {
        subentry_id: subId,
        start_time: startIso,
      });
    } catch (e) {
      console.error("Zeus: reserve failed", e);
    }
  }

  async _cancel(subId) {
    if (!this._hass) return;
    try {
      await this._hass.callService("zeus", "cancel_reservation", {
        subentry_id: subId,
      });
    } catch (e) {
      console.error("Zeus: cancel failed", e);
    }
  }

  async _setCycleDuration(entityId, minutes) {
    if (!this._hass || !entityId) return;
    try {
      await this._hass.callService("number", "set_value", {
        entity_id: entityId,
        value: minutes,
      });
    } catch (e) {
      console.error("Zeus: set cycle duration failed", e);
    }
  }

  /* ---------------------------------------------------------------- */
  /*  Formatting helpers                                               */
  /* ---------------------------------------------------------------- */

  _countdown(startIso, endIso) {
    const now = Date.now();
    const start = new Date(startIso).getTime();
    const diff = start - now;
    if (diff <= 0) {
      if (endIso && now < new Date(endIso).getTime()) return "Running now";
      return "Completed";
    }
    const h = Math.floor(diff / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    return h > 0 ? `${h}h ${m}m` : `${m}m ${s}s`;
  }

  _time(iso) {
    if (!iso) return "--:--";
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  _cost(v) {
    if (v == null) return "--";
    return "\u20AC" + v.toFixed(2).replace(".", ",");
  }

  /* ---------------------------------------------------------------- */
  /*  Navigation                                                       */
  /* ---------------------------------------------------------------- */

  _goBack() {
    this._page = null;
  }

  _openDevice(idx) {
    this._page = idx;
    this._startTick();
  }

  /* ---------------------------------------------------------------- */
  /*  Cycle duration handler                                           */
  /* ---------------------------------------------------------------- */

  _handleSetCycle(entityId) {
    const hInput = this.shadowRoot?.querySelector(".cycle-h");
    const mInput = this.shadowRoot?.querySelector(".cycle-m");
    if (!hInput || !mInput) return;
    const hours = parseInt(hInput.value, 10) || 0;
    const mins = parseInt(mInput.value, 10) || 0;
    const total = hours * 60 + mins;
    if (total > 0 && entityId) {
      this._setCycleDuration(entityId, total);
    }
  }

  /* ---------------------------------------------------------------- */
  /*  Render                                                           */
  /* ---------------------------------------------------------------- */

  render() {
    if (this._page === null) {
      return this._renderDeviceList();
    }
    const dev = this._devices[this._page];
    if (!dev) {
      this._page = null;
      return this._renderDeviceList();
    }
    return dev.attrs.reserved
      ? this._renderReserved(dev)
      : this._renderSchedule(dev);
  }

  /* ---- Page 1: Device list --------------------------------------- */

  _renderDeviceList() {
    return html`
      <ha-card>
        <div class="card-header">Zeus Scheduler</div>
        <div class="card-content">
          ${this._devices.length === 0
            ? html`<p class="secondary">No manual devices configured.</p>`
            : this._devices.map(
                (d, i) => html`
                  <div
                    class="list-item"
                    @click=${() => this._openDevice(i)}
                  >
                    <ha-icon
                      class="list-icon"
                      icon="mdi:power-plug-outline"
                    ></ha-icon>
                    <span class="list-text">${d.name}</span>
                    <ha-icon
                      class="list-chevron"
                      icon="mdi:chevron-right"
                    ></ha-icon>
                  </div>
                `,
              )}
        </div>
      </ha-card>
    `;
  }

  /* ---- Page 2a: Reserved state ----------------------------------- */

  _renderReserved(dev) {
    const a = dev.attrs;
    return html`
      <ha-card>
        <div class="header-row">
          <ha-icon-button
            .path=${mdiArrowLeft}
            @click=${this._goBack}
          ></ha-icon-button>
          <span class="header-title">${dev.name}</span>
        </div>
        <div class="card-content center-block">
          <div class="countdown">
            ${this._countdown(a.reservation_start, a.reservation_end)}
          </div>
          <p class="secondary">Scheduled</p>
          <p class="time-range">
            ${this._time(a.reservation_start)} &ndash;
            ${this._time(a.reservation_end)}
          </p>
          <ha-button
            class="cancel-btn"
            @click=${() => this._cancel(dev.subentryId)}
          >
            Cancel
          </ha-button>
        </div>
      </ha-card>
    `;
  }

  /* ---- Page 2b: Schedule / slot list ----------------------------- */

  _renderSchedule(dev) {
    const a = dev.attrs;
    const wins = a.ranked_windows || [];
    const dynamic = a.dynamic_cycle_duration === true;

    let durMin = a.cycle_duration_min || 0;
    if (dynamic && dev.numberEntityId && this._hass) {
      const ns = this._hass.states[dev.numberEntityId];
      if (ns && ns.state !== "unknown" && ns.state !== "unavailable")
        durMin = parseFloat(ns.state);
    }
    const h = Math.floor(durMin / 60);
    const m = Math.round(durMin % 60);

    return html`
      <ha-card>
        <div class="header-row">
          <ha-icon-button
            .path=${mdiArrowLeft}
            @click=${this._goBack}
          ></ha-icon-button>
          <span class="header-title">${dev.name}</span>
        </div>
        <div class="card-content">
          ${dynamic
            ? html`
                <div class="cycle-section">
                  <p class="overline">Cycle duration</p>
                  <div class="cycle-row">
                    <ha-textfield
                      class="cycle-h"
                      type="number"
                      .value=${String(h)}
                      min="0"
                      max="23"
                      suffix="h"
                    ></ha-textfield>
                    <span class="time-sep">:</span>
                    <ha-textfield
                      class="cycle-m"
                      type="number"
                      .value=${String(m)}
                      min="0"
                      max="59"
                      suffix="m"
                    ></ha-textfield>
                    <ha-button
                      raised
                      @click=${() =>
                        this._handleSetCycle(dev.numberEntityId)}
                    >
                      Set
                    </ha-button>
                  </div>
                </div>
              `
            : nothing}

          <p class="overline">Available slots</p>

          ${wins.length === 0
            ? html`<p class="secondary">No slots available</p>`
            : html`
                <div class="slot-list">
                  ${wins.map((w, i) => this._renderSlot(w, i, dev))}
                </div>
              `}
        </div>
      </ha-card>
    `;
  }

  _renderSlot(win, idx, dev) {
    const best = idx === 0;
    const solar = win.solar_pct || 0;
    const delay = win.delay_hours !== undefined ? win.delay_hours : null;

    return html`
      <div
        class="slot ${best ? "slot--best" : ""}"
        @click=${() => this._reserve(dev.subentryId, win.start)}
      >
        <div class="slot-left">
          <span class="slot-time">
            ${this._time(win.start)} &ndash; ${this._time(win.end)}
          </span>
          ${delay !== null
            ? html`<span class="chip chip--outline">${delay}h delay</span>`
            : nothing}
        </div>
        <div class="slot-right">
          ${solar > 0
            ? html`<span class="chip chip--solar">${solar}%</span>`
            : nothing}
          <span class="slot-cost">${this._cost(win.cost)}</span>
        </div>
      </div>
    `;
  }

  /* ---------------------------------------------------------------- */
  /*  Styles                                                           */
  /* ---------------------------------------------------------------- */

  static get styles() {
    return css`
      :host {
        --zeus-primary: var(--primary-color, #03a9f4);
      }

      ha-card {
        overflow: hidden;
      }

      .card-header {
        padding: 16px 16px 0;
        font-size: 1.1em;
        font-weight: 500;
        color: var(--primary-text-color);
      }

      .card-content {
        padding: 8px 16px 16px;
      }

      /* Header with back button */
      .header-row {
        display: flex;
        align-items: center;
        padding: 4px 4px 0;
      }

      .header-title {
        font-size: 1.1em;
        font-weight: 500;
        color: var(--primary-text-color);
        margin-left: 4px;
      }

      /* Device list items */
      .list-item {
        display: flex;
        align-items: center;
        padding: 12px 4px;
        cursor: pointer;
        border-radius: 12px;
        transition: background 0.15s;
      }

      .list-item:active {
        background: var(
          --table-row-alternative-background-color,
          rgba(0, 0, 0, 0.04)
        );
      }

      .list-icon {
        color: var(--secondary-text-color);
        margin-right: 16px;
        --mdc-icon-size: 24px;
      }

      .list-text {
        flex: 1;
        font-size: 0.95em;
        font-weight: 500;
        color: var(--primary-text-color);
      }

      .list-chevron {
        color: var(--secondary-text-color);
        --mdc-icon-size: 24px;
      }

      /* Overline label */
      .overline {
        font-size: 0.75em;
        font-weight: 500;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--secondary-text-color);
        margin: 16px 0 8px;
        padding: 0;
      }

      .secondary {
        font-size: 0.875em;
        color: var(--secondary-text-color);
        margin: 4px 0;
      }

      /* Reserved page center block */
      .center-block {
        text-align: center;
        padding: 24px 16px 16px;
      }

      .countdown {
        font-size: 2.2em;
        font-weight: 700;
        color: var(--zeus-primary);
        margin-bottom: 4px;
      }

      .time-range {
        font-size: 1.1em;
        font-weight: 500;
        color: var(--primary-text-color);
        margin: 4px 0 20px;
      }

      .cancel-btn {
        --mdc-theme-primary: var(--error-color, #b00020);
        --ha-button-background-color: var(--error-color, #b00020);
        --ha-button-text-color: #fff;
      }

      /* Buttons */
      ha-button {
        --mdc-theme-primary: var(--primary-color);
        --ha-button-background-color: var(--primary-color);
        --ha-button-text-color: #fff;
      }

      /* Cycle duration section */
      .cycle-section {
        padding-bottom: 8px;
        margin-bottom: 4px;
        border-bottom: 1px solid var(--divider-color);
      }

      .cycle-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .cycle-row ha-textfield {
        width: 90px;
      }

      .time-sep {
        font-size: 1.2em;
        font-weight: 600;
        color: var(--secondary-text-color);
      }

      /* Slot list */
      .slot-list {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .slot {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 16px;
        border: 1px solid var(--divider-color);
        border-radius: 12px;
        cursor: pointer;
        transition: background 0.15s;
      }

      .slot:active {
        background: var(
          --table-row-alternative-background-color,
          rgba(0, 0, 0, 0.04)
        );
      }

      .slot--best {
        border-color: var(--zeus-primary);
        background: color-mix(
          in srgb,
          var(--zeus-primary) 6%,
          transparent
        );
      }

      .slot-left {
        display: flex;
        align-items: center;
        gap: 8px;
        flex: 1;
      }

      .slot-time {
        font-weight: 500;
        font-size: 0.875em;
        color: var(--primary-text-color);
      }

      .slot-right {
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .slot-cost {
        font-weight: 600;
        font-size: 0.875em;
        color: var(--primary-text-color);
      }

      /* Chips */
      .chip {
        display: inline-flex;
        align-items: center;
        font-size: 0.7em;
        font-weight: 500;
        padding: 2px 8px;
        border-radius: 8px;
        white-space: nowrap;
      }

      .chip--outline {
        border: 1px solid var(--divider-color);
        color: var(--secondary-text-color);
      }

      .chip--solar {
        background: rgba(255, 152, 0, 0.12);
        color: #e65100;
      }
    `;
  }
}

/* ==================================================================== */
/*  Register                                                             */
/* ==================================================================== */

customElements.define("zeus-scheduler-card", ZeusSchedulerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "zeus-scheduler-card",
  name: "Zeus Scheduler",
  description: "Schedule manual devices at the cheapest energy times",
  preview: true,
});

console.info(
  `%c ZEUS SCHEDULER %c v${CARD_VERSION} `,
  "background:#03a9f4;color:#fff;font-weight:bold;padding:2px 6px;border-radius:4px 0 0 4px",
  "background:#333;color:#fff;padding:2px 6px;border-radius:0 4px 4px 0",
);
