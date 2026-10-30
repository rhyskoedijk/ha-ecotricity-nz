/**
 * A dashboard card that shows the Ecotricity NZ integration's cost statistics as a bill.
 *
 * The integration says which statistics make up each line (`ecotricity_nz/bill`), and the card
 * reads each one's change over the range from the recorder. With a `collection_key`, the range
 * follows the energy date picker that shares it; otherwise it's this month so far.
 */

import {
  LitElement,
  css,
  html,
  nothing,
  type PropertyValues,
  type TemplateResult,
} from "lit";

import {
  buildBill,
  type Bill,
  formatAmount,
  formatQuantity,
  formatRate,
  billingPeriodStart,
  statisticIds,
  wholeHours,
  type Changes,
  type BillLayout,
  type BillSection,
  type SectionKind,
} from "./bill";
import { CARD_TYPE } from "./card-type";

export { CARD_TYPE };

export interface BillCardConfig {
  type: string;
  title?: string;
  /** The ICP to show, as on a bill or in lower case, when more than one is set up. */
  icp?: string;
  /** The config entry to show, as an alternative to `icp`. */
  config_entry_id?: string;
  /** The energy date picker's collection key, starting "energy_". */
  collection_key?: string;
  /** Without a date picker, the day of the month bills start on (1 to 28); 1 by default. */
  billing_period_start_day?: number;
  /** Leave out the losses, "@" and unit price columns, and who charges each section. */
  compact?: boolean;
  /** The section heading bars' colour, as any CSS colour. */
  header_color?: string;
  /** List the plan's lines that charged nothing in the range, too. */
  show_empty_lines?: boolean;
}

/** The options that change what's fetched, rather than only how it's shown. */
const FETCH_OPTIONS = ["icp", "config_entry_id", "collection_key", "billing_period_start_day"] as const;

/** Check a config, throwing an error the card editor shows, as Home Assistant's cards do. */
export function validateConfig(config: BillCardConfig): void {
  if (config.collection_key !== undefined && !config.collection_key.startsWith(COLLECTION_PREFIX)) {
    throw new Error(`collection_key must start with "${COLLECTION_PREFIX}"`);
  }
  const day = config.billing_period_start_day;
  if (day !== undefined && !(Number.isInteger(day) && day >= 1 && day <= 28)) {
    throw new Error("billing_period_start_day must be a whole number from 1 to 28");
  }
}

/** The parts of Home Assistant's frontend object the card uses. */
export interface HomeAssistant {
  connection: Record<string, unknown>;
  callWS<T>(message: Record<string, unknown>): Promise<T>;
}

/** The energy date picker's collection: its range, and a way to hear when it changes. */
export interface EnergyCollection {
  start: Date;
  end?: Date;
  subscribe(callback: (data: unknown) => void): () => void;
}

const COLLECTION_PREFIX = "energy_";
/** How long to wait for the date picker to create its collection, and how often to look. */
const COLLECTION_WAIT_MS = 10_000;
const COLLECTION_POLL_MS = 250;

/** The supplier: every plan the integration prices is Ecotricity's. */
const SUPPLIER_NAME = "Ecotricity";

const SECTION_TITLES: Record<SectionKind, string> = {
  network: "Network Charges",
  energy: "Energy Charges",
  other: "Other Charges",
};

export class EcotricityBillCard extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _config: { state: true },
    _layout: { state: true },
    _data: { state: true },
    _range: { state: true },
    _error: { state: true },
  };

  declare hass?: HomeAssistant;
  declare _config?: BillCardConfig;
  declare _layout?: BillLayout;
  /** Each statistic's change over the range, and the days of daily charges in it. */
  declare _data?: { changes: Changes; days: number };
  declare _range?: { start: Date; end: Date };
  declare _error?: string;

  private _unsubscribe?: () => void;
  private _waitTimer?: ReturnType<typeof setTimeout>;
  private _loading = false;
  /** Counts fetches, so a slow one doesn't overwrite a newer range's bill. */
  private _fetches = 0;
  /** The bill worked out from `_data`, kept so a `hass` update alone doesn't rebuild it. */
  private _bill?: Bill;

  /** Return the visual editor, loaded only when it's opened. */
  static async getConfigElement(): Promise<HTMLElement> {
    const { EDITOR_TYPE } = await import("./editor");
    return document.createElement(EDITOR_TYPE);
  }

  static getStubConfig(): BillCardConfig {
    return { type: `custom:${CARD_TYPE}`, collection_key: "energy_bill" };
  }

  setConfig(config: BillCardConfig): void {
    validateConfig(config);
    const previous = this._config;
    this._config = config;
    // Only what's fetched needs fetching again; how it's shown is worked out as it's drawn.
    if (previous && FETCH_OPTIONS.every((option) => previous[option] === config[option])) {
      return;
    }
    this._layout = undefined;
    this._data = undefined;
    this._range = undefined;
    this._error = undefined;
    this._stopListening();
  }

  getCardSize(): number {
    return 10;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    if (this._layout) {
      // Moved in the page, such as when the dashboard's view changes: follow the range again,
      // as leaving stopped it.
      this._followRange();
    } else if (this.hass && this._config) {
      void this._start();
    }
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this._stopListening();
  }

  protected override willUpdate(changed: PropertyValues): void {
    if (changed.has("_data") || changed.has("_layout") || changed.has("_config")) {
      this._bill =
        this._data && this._layout
          ? buildBill(this._layout, this._data.changes, this._data.days, {
              showEmptyLines: this._config?.show_empty_lines,
            })
          : undefined;
    }
    if (changed.has("_config")) {
      // The heading bars read this variable, so a theme can set it for every card too.
      const color = this._config?.header_color;
      if (color) {
        this.style.setProperty("--ecotricity-bill-header-color", color);
      } else {
        this.style.removeProperty("--ecotricity-bill-header-color");
      }
    }
  }

  protected override updated(changed: PropertyValues): void {
    if (
      (changed.has("hass") || changed.has("_config")) &&
      !this._layout &&
      !this._loading
    ) {
      void this._start();
    }
  }

  /** Load the bill's layout, then follow the date range. */
  private async _start(): Promise<void> {
    if (!this.hass || !this._config || this._loading || this._layout) {
      return;
    }
    this._loading = true;
    try {
      this._layout = await this.hass.callWS<BillLayout>({
        type: "ecotricity_nz/bill",
        ...this._target(),
      });
      this._error = undefined;
    } catch (err) {
      this._error = errorMessage(err);
      return;
    } finally {
      this._loading = false;
    }
    this._followRange();
  }

  /** Which ICP the integration's commands are about, when the card says. */
  private _target(): Record<string, string> {
    const target: Record<string, string> = {};
    if (this._config?.icp) {
      target.icp = this._config.icp;
    }
    if (this._config?.config_entry_id) {
      target.config_entry_id = this._config.config_entry_id;
    }
    return target;
  }

  private _followRange(): void {
    this._stopListening();
    const key = this._config?.collection_key;
    if (!key) {
      const now = new Date();
      void this._setRange(billingPeriodStart(now, this._config?.billing_period_start_day ?? 1), now);
      return;
    }
    const deadline = Date.now() + COLLECTION_WAIT_MS;
    const look = (): void => {
      const collection = this.hass?.connection[`_${key}`] as
        | EnergyCollection
        | undefined;
      if (collection) {
        // The collection also refreshes on its own as time passes, which picks up statistics
        // imported since, so each update reads them again.
        this._unsubscribe = collection.subscribe(() => {
          void this._setRange(
            collection.start,
            collection.end ?? new Date(),
            true,
          );
        });
        if (collection.start) {
          void this._setRange(collection.start, collection.end ?? new Date());
        }
      } else if (Date.now() < deadline) {
        this._waitTimer = setTimeout(look, COLLECTION_POLL_MS);
      } else {
        this._error = `No energy date picker with collection_key "${key}" on this dashboard`;
      }
    };
    look();
  }

  private _stopListening(): void {
    this._unsubscribe?.();
    this._unsubscribe = undefined;
    clearTimeout(this._waitTimer);
  }

  private async _setRange(
    start: Date,
    end: Date,
    refresh = false,
  ): Promise<void> {
    if (
      !refresh &&
      this._range &&
      this._range.start.getTime() === start.getTime() &&
      this._range.end.getTime() === end.getTime()
    ) {
      return;
    }
    this._range = { start, end };
    await this._fetch();
  }

  private async _fetch(): Promise<void> {
    const { hass, _layout: layout, _range: range } = this;
    if (!hass || !layout || !range) {
      return;
    }
    const fetch = ++this._fetches;
    try {
      const ids = statisticIds(layout);
      const hours = wholeHours(range.start, range.end);
      const period = {
        start_time: hours.start.toISOString(),
        end_time: hours.end.toISOString(),
      };
      const charged = hass.callWS<{ days: number }>({
        type: "ecotricity_nz/bill_days",
        ...this._target(),
        ...period,
      });
      const values = await Promise.all(
        ids.map((statistic_id) =>
          hass.callWS<{ change?: number | null }>({
            type: "recorder/statistic_during_period",
            statistic_id,
            fixed_period: period,
            types: ["change"],
          }),
        ),
      );
      if (fetch !== this._fetches) {
        return;
      }
      const changes = new Map(
        ids.map((id, index) => [id, values[index]?.change ?? null]),
      );
      const { days } = await charged;
      if (fetch !== this._fetches) {
        return;
      }
      this._data = { changes, days };
      this._error = undefined;
    } catch (err) {
      if (fetch === this._fetches) {
        this._error = errorMessage(err);
      }
    }
  }

  protected override render(): TemplateResult {
    const title = this._config?.title;
    return html`
      <ha-card .header=${title}>
        <div class="card-content">${this._renderBody()}</div>
      </ha-card>
    `;
  }

  private _renderBody(): TemplateResult {
    if (this._error) {
      return html`<p class="message">${this._error}</p>`;
    }
    const bill = this._bill;
    if (!bill || !this._layout) {
      return html`<p class="message">Loading…</p>`;
    }
    if (!bill.sections.length) {
      return html`<p class="message">No charges for this period.</p>`;
    }
    return html`
      <table class=${this._config?.compact ? "compact" : ""}>
        ${bill.sections.map((section) => this._renderSection(section))}
        <tbody class="totals">
          <tr class="header">
            <th colspan="6">Total Charges</th>
          </tr>
          <tr>
            <td colspan="5">Charges</td>
            <td class="amount">${formatAmount(bill.charges)}</td>
          </tr>
          <tr>
            <td colspan="5">GST</td>
            <td class="amount">${formatAmount(bill.gst)}</td>
          </tr>
          <tr class="total">
            <td colspan="5">Total</td>
            <td class="amount">${formatAmount(bill.total)}</td>
          </tr>
        </tbody>
      </table>
    `;
  }

  private _renderSection(section: BillSection): TemplateResult {
    // Who charges each section: the network by name, and Ecotricity for its own rates. A
    // compact card leaves them out.
    const by = this._config?.compact
      ? undefined
      : { network: this._layout?.network_name, energy: SUPPLIER_NAME, other: undefined }[
          section.kind
        ];
    const title = by
      ? `${SECTION_TITLES[section.kind]} [${by}]`
      : SECTION_TITLES[section.kind];
    return html`
      <tbody>
        <tr class="header">
          <th colspan="6">${title}</th>
        </tr>
        ${section.lines.map(
          (line) => html`
            <tr>
              <td class="label">${line.label}</td>
              <td class="quantity">
                ${formatQuantity(line.quantity, line.unit)} ${line.unit}
              </td>
              <td class="losses">
                ${line.losses === null
                  ? nothing
                  : html`<span>plus ${formatQuantity(line.losses, "kWh")} kWh losses</span>`}
              </td>
              <td class="at">${line.rate === null ? nothing : html`<span>@</span>`}</td>
              <td class="rate">
                ${line.rate === null
                  ? nothing
                  : html`<span
                      >${formatRate(line.rate)} $ per ${line.unit === "days" ? "day" : "kWh"}</span
                    >`}
              </td>
              <td class="amount">${formatAmount(line.amount)}</td>
            </tr>
          `,
        )}
        <tr class="subtotal">
          <td colspan="5">Total ${SECTION_TITLES[section.kind]}</td>
          <td class="amount">${formatAmount(section.total)}</td>
        </tr>
      </tbody>
    `;
  }

  static override styles = css`
    :host {
      --bill-header-background: var(--ecotricity-bill-header-color, #0b4a6b);
      --bill-header-text: var(--ecotricity-bill-header-text-color, #ffffff);
      display: block;
      container-type: inline-size;
    }
    .card-content {
      padding: 0 16px 16px;
    }
    ha-card:not([header]) .card-content,
    ha-card .card-content:first-child {
      padding-top: 16px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-variant-numeric: tabular-nums;
    }
    tbody + tbody tr.header > * {
      border-top: 12px solid
        var(--ha-card-background, var(--card-background-color, transparent));
    }
    tr.header > * {
      background: var(--bill-header-background);
      color: var(--bill-header-text);
      font-weight: 600;
      text-align: left;
      padding: 6px 8px;
    }
    tr.header > .amount {
      text-align: right;
    }
    td {
      padding: 6px 8px;
      vertical-align: top;
      border-bottom: 1px solid var(--divider-color, rgba(127, 127, 127, 0.2));
    }
    td.quantity,
    td.losses,
    td.rate,
    td.amount {
      text-align: right;
      white-space: nowrap;
    }
    td.losses,
    td.at,
    td.rate {
      color: var(--secondary-text-color);
    }
    td.at {
      text-align: center;
      padding: 6px 0;
    }
    tr.subtotal td,
    tr.total td {
      font-weight: 600;
      border-bottom: none;
    }
    .totals tr.total td {
      border-bottom: none;
    }
    .message {
      color: var(--secondary-text-color);
      margin: 0;
    }
    /*
     * Hidden columns keep their cells, empty and without padding, so every row still has all six
     * columns and the totals rows' spans line up with them. Hiding the cells themselves would
     * leave line rows short of columns, with their amounts under the wrong heading.
     */
    table.compact td.losses,
    table.compact td.at,
    table.compact td.rate {
      padding: 0;
    }
    table.compact td.losses > span,
    table.compact td.at > span,
    table.compact td.rate > span {
      display: none;
    }
    @container (max-width: 460px) {
      td.losses,
      td.at,
      td.rate {
        padding: 0;
      }
      td.losses > span,
      td.at > span,
      td.rate > span {
        display: none;
      }
    }
  `;
}

function errorMessage(err: unknown): string {
  if (
    err &&
    typeof err === "object" &&
    "message" in err &&
    typeof err.message === "string"
  ) {
    return err.message;
  }
  return String(err);
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, EcotricityBillCard);
}

declare global {
  interface Window {
    customCards?: {
      type: string;
      name: string;
      description: string;
      preview?: boolean;
    }[];
  }
}

window.customCards = window.customCards ?? [];
if (!window.customCards.some((card) => card.type === CARD_TYPE)) {
  window.customCards.push({
    type: CARD_TYPE,
    name: "Ecotricity bill",
    description:
      "Your Ecotricity NZ costs over a date range, laid out like your bill.",
  });
}
