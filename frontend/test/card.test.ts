// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";

import { CARD_TYPE, EcotricityBillCard, type EnergyCollection, type HomeAssistant } from "../src/ecotricity-bill-card";
import type { BillLayout } from "../src/bill";
import { CHANGES, LAYOUT } from "./fixtures";

interface StatisticRequest {
  type: string;
  statistic_id: string;
  fixed_period: { start_time: string; end_time: string };
}

/** A collection like the energy date picker's, whose range the test can change. */
class FakeCollection implements EnergyCollection {
  start = new Date("2026-09-29T11:00:00Z");
  end?: Date = new Date("2026-09-30T10:59:59.999Z");
  private callbacks: ((data: unknown) => void)[] = [];

  subscribe(callback: (data: unknown) => void): () => void {
    this.callbacks.push(callback);
    return () => {
      this.callbacks = this.callbacks.filter((item) => item !== callback);
    };
  }

  setPeriod(start: Date, end?: Date): void {
    this.start = start;
    this.end = end;
    for (const callback of this.callbacks) {
      callback({});
    }
  }

  get subscribers(): number {
    return this.callbacks.length;
  }
}

/** Days of daily charges the integration reports for any range; tests can change it. */
const charged = { days: 1 };

function fakeHass(connection: Record<string, unknown> = {}, layout: BillLayout = LAYOUT) {
  const requests: StatisticRequest[] = [];
  const commands: Record<string, unknown>[] = [];
  const hass: HomeAssistant = {
    connection,
    async callWS<T>(message: Record<string, unknown>): Promise<T> {
      if (String(message.type).startsWith("ecotricity_nz/")) {
        commands.push(message);
      }
      if (message.type === "ecotricity_nz/bill") {
        return layout as T;
      }
      if (message.type === "ecotricity_nz/bill_days") {
        return { days: charged.days } as T;
      }
      const request = message as unknown as StatisticRequest;
      requests.push(request);
      return { change: CHANGES.get(request.statistic_id) ?? null } as T;
    },
  };
  return { hass, requests, commands };
}

async function mount(config: Record<string, unknown>, hass: HomeAssistant): Promise<EcotricityBillCard> {
  const card = document.createElement(CARD_TYPE) as EcotricityBillCard;
  card.setConfig({ type: `custom:${CARD_TYPE}`, ...config });
  card.hass = hass;
  document.body.append(card);
  await settle(card);
  return card;
}

async function settle(card: EcotricityBillCard): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await card.updateComplete;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function text(card: EcotricityBillCard): string {
  return card.shadowRoot?.textContent?.replace(/\s+/g, " ") ?? "";
}

afterEach(() => {
  charged.days = 1;
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("ecotricity-bill-card", () => {
  it("shows the bill for the date picker's range", async () => {
    const collection = new FakeCollection();
    const { hass, requests } = fakeHass({ _energy_bill: collection });
    const card = await mount({ collection_key: "energy_bill", title: "Bill" }, hass);

    const shown = text(card);
    expect(shown).toContain("Network Charges [Example Lines]");
    expect(shown).toContain("Energy Charges [Ecotricity]");
    expect(shown).toContain("Import - Off-peak");
    expect(shown).toContain("Daily charge - Ecotricity");
    expect(shown).not.toContain("Uncontrolled");
    expect(shown).toContain("Other Charges");
    expect(shown).toContain("1 days");
    expect(shown).toContain("1.5951 $ per day");
    expect(shown).toContain("7.30 kWh");
    expect(shown).toContain("plus 0.40 kWh losses");
    // "@" has a column of its own, on each line with a unit price.
    const cells = (label: string) => {
      const row = [...(card.shadowRoot?.querySelectorAll("tbody tr") ?? [])].find((tr) =>
        tr.textContent?.includes(label),
      );
      return [...(row?.querySelectorAll("td") ?? [])].map((cell) => cell.textContent?.trim());
    };
    expect(cells("Import - Anytime")).toEqual([
      "Import - Anytime",
      "7.30 kWh",
      "plus 0.40 kWh losses",
      "@",
      "0.18 $ per kWh",
      "$1.39",
    ]);
    expect(cells("Daily charge - Network")).toEqual([
      "Daily charge - Network",
      "1 days",
      "",
      "@",
      "1.5951 $ per day",
      "$1.60",
    ]);
    expect(shown).toContain("0.18 $ per kWh");
    expect(shown).toContain("($0.91)");
    // Lines exclude GST, which is added once at the end.
    const footer = [...(card.shadowRoot?.querySelectorAll(".totals tr") ?? [])].map((row) =>
      row.textContent?.replace(/\s+/g, " ").trim(),
    );
    expect(footer.map((row) => row?.replace(/ \(?\$.*$/, ""))).toEqual([
      "Total Charges",
      "Charges",
      "GST",
      "Total",
    ]);
    expect(requests[0]?.fixed_period.start_time).toBe(collection.start.toISOString());
    // The day's last hour is asked for whole, not up to 23:59:59.999.
    expect(requests[0]?.fixed_period.end_time).toBe("2026-09-30T11:00:00.000Z");
  });

  it("asks for the ICP the card names, when more than one is set up", async () => {
    const { hass, commands } = fakeHass({ _energy_bill: new FakeCollection() });
    await mount({ collection_key: "energy_bill", icp: "0000654321ab1c2" }, hass);

    expect(commands.map((command) => command.type)).toEqual([
      "ecotricity_nz/bill",
      "ecotricity_nz/bill_days",
    ]);
    for (const command of commands) {
      expect(command.icp).toBe("0000654321ab1c2");
      expect(command).not.toHaveProperty("config_entry_id");
    }
  });

  it("asks for no ICP in particular when the card names none", async () => {
    const { hass, commands } = fakeHass({ _energy_bill: new FakeCollection() });
    await mount({ collection_key: "energy_bill" }, hass);

    expect(commands.length).toBeGreaterThan(0);
    for (const command of commands) {
      expect(command).not.toHaveProperty("icp");
      expect(command).not.toHaveProperty("config_entry_id");
    }
  });

  it("reads the statistics again when the range changes", async () => {
    const collection = new FakeCollection();
    const { hass, requests } = fakeHass({ _energy_bill: collection });
    const card = await mount({ collection_key: "energy_bill" }, hass);
    const first = requests.length;

    // A month, with usage imported for 23 days of it.
    charged.days = 23;
    collection.setPeriod(new Date("2026-08-31T12:00:00Z"), new Date("2026-09-30T10:59:59.999Z"));
    await settle(card);
    expect(requests.length).toBe(2 * first);
    expect(requests.at(-1)?.fixed_period.start_time).toBe("2026-08-31T12:00:00.000Z");
    // Daily charges are for the days charged, not the days in the range.
    expect(text(card)).toContain("23 days");
  });

  it("waits for the date picker to create its collection", async () => {
    const connection: Record<string, unknown> = {};
    const { hass, requests } = fakeHass(connection);
    const card = await mount({ collection_key: "energy_bill" }, hass);
    expect(requests).toHaveLength(0);

    connection._energy_bill = new FakeCollection();
    await new Promise((resolve) => setTimeout(resolve, 300));
    await settle(card);
    expect(requests.length).toBeGreaterThan(0);
  });

  it("says so when no date picker shares its collection key", async () => {
    vi.useFakeTimers();
    const { hass } = fakeHass();
    const card = document.createElement(CARD_TYPE) as EcotricityBillCard;
    card.setConfig({ type: `custom:${CARD_TYPE}`, collection_key: "energy_missing" });
    card.hass = hass;
    document.body.append(card);
    await vi.advanceTimersByTimeAsync(11_000);
    await card.updateComplete;
    expect(text(card)).toContain('No energy date picker with collection_key "energy_missing"');
  });

  it("shows this month so far without a collection key", async () => {
    const { hass, requests } = fakeHass();
    await mount({}, hass);
    const start = new Date(requests[0]?.fixed_period.start_time ?? "");
    expect(start.getDate()).toBe(1);
    expect(start.getHours()).toBe(0);
  });

  it("stops following the range once removed", async () => {
    const collection = new FakeCollection();
    const { hass } = fakeHass({ _energy_bill: collection });
    const card = await mount({ collection_key: "energy_bill" }, hass);
    expect(collection.subscribers).toBe(1);
    card.remove();
    expect(collection.subscribers).toBe(0);
  });

  it("shows the integration's error", async () => {
    const hass: HomeAssistant = {
      connection: {},
      callWS: () => Promise.reject({ code: "not_ready", message: "Pricing hasn't been fetched yet" }),
    };
    const card = await mount({}, hass);
    expect(text(card)).toContain("Pricing hasn't been fetched yet");
  });

  it("leaves out the losses and unit price columns, and who charges each section, when compact", async () => {
    const { hass } = fakeHass({ _energy_bill: new FakeCollection() });
    const card = await mount({ collection_key: "energy_bill", compact: true }, hass);
    expect(card.shadowRoot?.querySelector("table")?.classList.contains("compact")).toBe(true);
    const headings = [...(card.shadowRoot?.querySelectorAll("tr.header th") ?? [])].map((cell) =>
      cell.textContent?.trim(),
    );
    expect(headings).toEqual(["Network Charges", "Energy Charges", "Other Charges", "Total Charges"]);
  });

  it("keeps every row's columns lined up when compact", async () => {
    const { hass } = fakeHass({ _energy_bill: new FakeCollection() });
    const card = await mount({ collection_key: "energy_bill", compact: true }, hass);

    // Every row fills the same columns, counting spans, so the amounts all line up.
    const widths = [...(card.shadowRoot?.querySelectorAll("tr") ?? [])].map((row) =>
      [...row.children].reduce((total, cell) => total + Number(cell.getAttribute("colspan") ?? 1), 0),
    );
    expect(new Set(widths)).toEqual(new Set([6]));
    // Hidden columns keep their cells: only what's in them is hidden, never a cell itself.
    const css = (EcotricityBillCard.styles as { cssText: string }).cssText;
    for (const rule of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      if (/display:\s*none/.test(rule[2] ?? "")) {
        for (const selector of (rule[1] ?? "").split(",")) {
          expect(selector.trim()).not.toMatch(/\btd\.[a-z]+$/);
        }
      }
    }
  });

  it("colours the section headings", async () => {
    const { hass } = fakeHass({ _energy_bill: new FakeCollection() });
    const card = await mount({ collection_key: "energy_bill", header_color: "#336699" }, hass);
    expect(card.style.getPropertyValue("--ecotricity-bill-header-color")).toBe("#336699");
    card.setConfig({ type: `custom:${CARD_TYPE}`, collection_key: "energy_bill" });
    await settle(card);
    expect(card.style.getPropertyValue("--ecotricity-bill-header-color")).toBe("");
  });

  it("starts on the billing period start day without a date picker", async () => {
    const { hass, requests } = fakeHass();
    await mount({ billing_period_start_day: 15 }, hass);
    const start = new Date(requests[0]?.fixed_period.start_time ?? "");
    expect(start.getDate()).toBe(15);
  });

  it("changes how the bill's shown without reading the statistics again", async () => {
    const { hass, requests, commands } = fakeHass({ _energy_bill: new FakeCollection() });
    const card = await mount({ collection_key: "energy_bill" }, hass);
    const [reads, asked] = [requests.length, commands.length];

    card.setConfig({ type: `custom:${CARD_TYPE}`, collection_key: "energy_bill", compact: true });
    await settle(card);
    expect([requests.length, commands.length]).toEqual([reads, asked]);
    expect(text(card)).not.toContain("Example Lines");

    // A different ICP is a different bill, so it's read again.
    card.setConfig({ type: `custom:${CARD_TYPE}`, collection_key: "energy_bill", icp: "0000654321AB1C2" });
    await settle(card);
    expect(commands.length).toBeGreaterThan(asked);
  });

  it.each([
    [{ billing_period_start_day: 0 }, "billing_period_start_day must be a whole number from 1 to 28"],
    [{ billing_period_start_day: 31 }, "billing_period_start_day must be a whole number from 1 to 28"],
    [{ billing_period_start_day: 1.5 }, "billing_period_start_day must be a whole number from 1 to 28"],
  ])("rejects an option it can't use: %o", (option, message) => {
    const card = document.createElement(CARD_TYPE) as EcotricityBillCard;
    expect(() => card.setConfig({ type: `custom:${CARD_TYPE}`, ...(option as object) })).toThrow(message);
  });

  it("rejects a collection key the energy date picker can't have", () => {
    const card = document.createElement(CARD_TYPE) as EcotricityBillCard;
    expect(() => card.setConfig({ type: `custom:${CARD_TYPE}`, collection_key: "bill" })).toThrow(
      'collection_key must start with "energy_"',
    );
  });

  it("is listed in the card picker", () => {
    expect(window.customCards?.some((card) => card.type === CARD_TYPE)).toBe(true);
    expect(EcotricityBillCard.getStubConfig()).toEqual({ type: `custom:${CARD_TYPE}`, collection_key: "energy_bill" });
  });
});
