// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from "vitest";

import { CARD_TYPE } from "../src/card-type";
import { EcotricityBillCard, type BillCardConfig, type HomeAssistant } from "../src/ecotricity-bill-card";
import {
  DEFAULTS,
  EDITOR_TYPE,
  EcotricityBillCardEditor,
  editedConfig,
  editorSchema,
  formData,
  type FormField,
  type IcpChoice,
} from "../src/editor";

const CONFIG: BillCardConfig = { type: `custom:${CARD_TYPE}`, collection_key: "energy_bill" };
const HOME: IcpChoice = { icp: "0000123456AB1C2", title: "Ecotricity 0000123456AB1C2" };
const FLAT: IcpChoice = { icp: "0000654321AB1C2", title: "Ecotricity 0000654321AB1C2" };

const names = (fields: FormField[]) => fields.map((field) => field.name);

afterEach(() => {
  document.body.replaceChildren();
});

describe("editorSchema", () => {
  it("asks for an ICP only when there's more than one to choose from", () => {
    expect(names(editorSchema([HOME], CONFIG))).not.toContain("icp");
    const fields = editorSchema([HOME, FLAT], CONFIG);
    expect(names(fields).slice(0, 3)).toEqual(["title", "icp", "collection_key"]);
    expect(fields[1]?.selector).toEqual({
      select: {
        mode: "dropdown",
        options: [
          { value: HOME.icp, label: HOME.title },
          { value: FLAT.icp, label: FLAT.title },
        ],
      },
    });
  });

  it("keeps an ICP the config already names, even one that isn't set up", () => {
    expect(names(editorSchema([HOME], { ...CONFIG, icp: HOME.icp.toLowerCase() }))).toContain("icp");
    const options = editorSchema([HOME], { ...CONFIG, icp: "0000999999AB1C2" })[1]?.selector.select as {
      options: { value: string }[];
    };
    expect(options.options.map((option) => option.value)).toEqual([HOME.icp, "0000999999AB1C2"]);
  });
});

// review: TEST-21
describe("every option", () => {
  it("is in the editor", () => {
    expect(names(editorSchema([HOME, FLAT], { ...CONFIG, config_entry_id: "abc" })).sort()).toEqual(
      [
        "title",
        "icp",
        "config_entry_id",
        "collection_key",
        "billing_period_start_day",
        "compact",
        "show_empty_lines",
        "header_color",
      ].sort(),
    );
  });
});

describe("config entry ID", () => {
  it("is only offered once a config sets it, as it's an alternative to the ICP", () => {
    expect(names(editorSchema([HOME, FLAT], CONFIG))).not.toContain("config_entry_id");
    expect(names(editorSchema([HOME], { ...CONFIG, config_entry_id: "abc" }))).toContain("config_entry_id");
  });
});

describe("editedConfig", () => {
  it("leaves out options set back to their defaults", () => {
    const edited = editedConfig(CONFIG, { ...DEFAULTS, compact: true });
    expect(edited).toEqual({ ...CONFIG, compact: true });
  });

  it("shows defaults for the options left out", () => {
    expect(formData(CONFIG)).toMatchObject({ billing_period_start_day: 1, compact: false, show_empty_lines: false });
    expect(formData({ ...CONFIG, compact: true }).compact).toBe(true);
  });

  it("applies the form's changes, and drops options that were cleared", () => {
    const edited = editedConfig({ ...CONFIG, title: "Bill", icp: HOME.icp }, { title: "", icp: FLAT.icp });
    expect(edited).toEqual({ type: CONFIG.type, collection_key: "energy_bill", icp: FLAT.icp });
  });
});

describe("ecotricity-bill-card-editor", () => {
  async function mount(icps: IcpChoice[]): Promise<EcotricityBillCardEditor> {
    const hass: HomeAssistant = {
      connection: {},
      callWS: async <T>() => ({ icps }) as T,
    };
    const editor = document.createElement(EDITOR_TYPE) as EcotricityBillCardEditor;
    editor.setConfig(CONFIG);
    editor.hass = hass;
    document.body.append(editor);
    for (let i = 0; i < 5; i++) {
      await editor.updateComplete;
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return editor;
  }

  it("offers the ICPs set up, and reports each change as the card's config", async () => {
    const editor = await mount([HOME, FLAT]);
    const form = editor.shadowRoot?.querySelector("ha-form") as
      | (HTMLElement & { schema: FormField[]; data: BillCardConfig })
      | null;
    expect(form).not.toBeNull();
    expect(names(form?.schema ?? [])).toContain("icp");
    // Options left out show their defaults.
    expect(form?.data).toEqual({ ...DEFAULTS, ...CONFIG });

    const changes: BillCardConfig[] = [];
    editor.addEventListener("config-changed", (event) => {
      changes.push((event as CustomEvent<{ config: BillCardConfig }>).detail.config);
    });
    form?.dispatchEvent(new CustomEvent("value-changed", { detail: { value: { ...CONFIG, icp: FLAT.icp } } }));
    expect(changes).toEqual([{ ...CONFIG, icp: FLAT.icp }]);
  });

  it("is the card's editor", async () => {
    const element = await EcotricityBillCard.getConfigElement();
    expect(element.tagName.toLowerCase()).toBe(EDITOR_TYPE);
  });
});
