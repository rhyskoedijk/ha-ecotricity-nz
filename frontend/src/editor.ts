/**
 * The bill card's visual editor.
 *
 * It's a form of Home Assistant's own (`ha-form`), as the built-in cards' editors are, so it looks
 * and behaves like them. The ICPs to choose from come from the integration (`ecotricity_nz/icps`);
 * the ICP field only shows when there's a choice to make.
 */

import { LitElement, html, nothing, type TemplateResult } from "lit";

import type { BillCardConfig, HomeAssistant } from "./ecotricity-bill-card";

export const EDITOR_TYPE = "ecotricity-bill-card-editor";

/** An ICP set up in the integration, as `ecotricity_nz/icps` lists it. */
export interface IcpChoice {
  icp: string;
  title: string;
}

/** One field of an `ha-form` schema. */
export interface FormField {
  name: keyof BillCardConfig;
  selector: Record<string, unknown>;
}

const LABELS: Partial<Record<keyof BillCardConfig, string>> = {
  title: "Title",
  icp: "ICP",
  collection_key: "Date picker collection key",
  billing_period_start_day: "Billing period start day",
  compact: "Compact",
  show_empty_lines: "Show lines with no charges",
  header_color: "Heading colour",
};

/** Each option's value when it's left out, so the form shows it and the config leaves it out. */
export const DEFAULTS: Partial<BillCardConfig> = {
  billing_period_start_day: 1,
  compact: false,
  show_empty_lines: false,
};


const HELPERS: Partial<Record<keyof BillCardConfig, string>> = {
  icp: "Which ICP's bill to show.",
  collection_key:
    "The collection_key of an Energy date picker card on this dashboard, starting energy_. " +
    "Leave it empty to show the current billing period.",
  billing_period_start_day: "Without a date picker, the day of the month your bills start on.",
  compact: "Leave out the losses and unit price columns, and the names in the section headings.",
  header_color: "Any CSS colour, such as #0b4a6b. Leave it empty for the default.",
};

/** Return the editor's fields: the ICP only when there's more than one, or one is set already. */
export function editorSchema(icps: readonly IcpChoice[], config: BillCardConfig): FormField[] {
  const fields: FormField[] = [{ name: "title", selector: { text: {} } }];
  if (icps.length > 1 || config.icp) {
    const options = icps.map((choice) => ({ value: choice.icp, label: choice.title }));
    // An ICP typed in YAML that isn't set up (yet) stays selectable rather than vanishing.
    if (config.icp && !icps.some((choice) => sameIcp(choice.icp, config.icp ?? ""))) {
      options.push({ value: config.icp, label: config.icp });
    }
    fields.push({ name: "icp", selector: { select: { mode: "dropdown", options } } });
  }
  fields.push(
    { name: "collection_key", selector: { text: {} } },
    { name: "billing_period_start_day", selector: { number: { min: 1, max: 28, mode: "box" } } },
    { name: "compact", selector: { boolean: {} } },
    { name: "show_empty_lines", selector: { boolean: {} } },
    { name: "header_color", selector: { text: {} } },
  );
  return fields;
}

/** Return what the form shows for a config: its options, with defaults for those left out. */
export function formData(config: BillCardConfig): BillCardConfig {
  return { ...DEFAULTS, ...config };
}

/**
 * Return the config with the form's changes, leaving out options that were cleared or set back
 * to their defaults.
 */
export function editedConfig(
  config: BillCardConfig,
  changes: Partial<BillCardConfig>,
): BillCardConfig {
  const edited: Record<string, unknown> = { ...config, ...changes };
  for (const [key, value] of Object.entries(edited)) {
    const unset = value === "" || value === undefined || value === null;
    const isDefault = key in DEFAULTS && DEFAULTS[key as keyof BillCardConfig] === value;
    if (key !== "type" && (unset || isDefault)) {
      delete edited[key];
    }
  }
  return edited as unknown as BillCardConfig;
}

function sameIcp(a: string, b: string): boolean {
  return a.trim().toUpperCase() === b.trim().toUpperCase();
}

export class EcotricityBillCardEditor extends LitElement {
  static override properties = {
    hass: { attribute: false },
    _config: { state: true },
    _icps: { state: true },
  };

  declare hass?: HomeAssistant;
  declare _config?: BillCardConfig;
  declare _icps: IcpChoice[];

  private _loadingIcps = false;

  constructor() {
    super();
    this._icps = [];
  }

  setConfig(config: BillCardConfig): void {
    this._config = config;
  }

  protected override updated(): void {
    if (this.hass && !this._loadingIcps && !this._icps.length) {
      this._loadingIcps = true;
      this.hass
        .callWS<{ icps: IcpChoice[] }>({ type: "ecotricity_nz/icps" })
        .then((result) => {
          this._icps = result.icps;
        })
        // Without the list the ICP can still be typed in YAML; the form just doesn't offer it.
        .catch(() => undefined);
    }
  }

  protected override render(): TemplateResult | typeof nothing {
    if (!this.hass || !this._config) {
      return nothing;
    }
    return html`
      <ha-form
        .hass=${this.hass}
        .data=${formData(this._config)}
        .schema=${editorSchema(this._icps, this._config)}
        .computeLabel=${(field: FormField) => LABELS[field.name] ?? field.name}
        .computeHelper=${(field: FormField) => HELPERS[field.name]}
        @value-changed=${this._valueChanged}
      ></ha-form>
    `;
  }

  private _valueChanged(event: CustomEvent<{ value: Partial<BillCardConfig> }>): void {
    event.stopPropagation();
    if (!this._config) {
      return;
    }
    this._config = editedConfig(this._config, event.detail.value);
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: this._config },
        bubbles: true,
        composed: true,
      }),
    );
  }
}

if (!customElements.get(EDITOR_TYPE)) {
  customElements.define(EDITOR_TYPE, EcotricityBillCardEditor);
}
