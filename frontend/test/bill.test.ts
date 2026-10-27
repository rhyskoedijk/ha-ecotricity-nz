import { describe, expect, it } from "vitest";

import {
  billingPeriodStart,
  buildBill,
  formatAmount,
  formatQuantity,
  formatRate,
  lineLabel,
  statisticIds,
  wholeHours,
} from "../src/bill";
import { CHANGES, LAYOUT, LOSS_FACTOR } from "./fixtures";

describe("buildBill", () => {
  const bill = buildBill(LAYOUT, CHANGES, 1);

  it("keeps the bill's sections and lines in order", () => {
    expect(bill.sections.map((section) => section.kind)).toEqual(["network", "energy", "other"]);
    expect(bill.sections[0]?.lines.map((line) => line.label)).toEqual([
      "Daily charge - Network",
      "Import - Off-peak",
      "Import - Peak",
      "Export - Peak",
    ]);
  });

  it("charges daily items per day charged, not per day in the range", () => {
    // A month's range with usage imported for 23 days: the rate is the daily charge itself.
    const month = buildBill(LAYOUT, new Map([...CHANGES, ["s:daily_charge_network", 23 * 1.035]]), 23);
    expect(month.sections[0]?.lines[0]).toMatchObject({ quantity: 23, unit: "days" });
    expect(month.sections[0]?.lines[0]?.rate).toBeCloseTo(1.035);
  });

  it("has no daily rate with no days charged", () => {
    const none = buildBill(LAYOUT, CHANGES, 0);
    expect(none.sections[0]?.lines[0]?.rate).toBeNull();
  });

  it("charges daily items per day", () => {
    const daily = bill.sections[0]?.lines[0];
    expect(daily).toMatchObject({ quantity: 1, unit: "days", losses: null });
    expect(daily?.rate).toBeCloseTo(1.5951);
    const week = buildBill(LAYOUT, new Map([...CHANGES, ["s:daily_charge_network", 7 * 1.5951]]), 7);
    expect(week.sections[0]?.lines[0]?.rate).toBeCloseTo(1.5951);
    expect(week.sections[0]?.lines[0]?.quantity).toBe(7);
  });

  it("charges Ecotricity's import on usage plus losses", () => {
    // Losses are charged on top of the usage.
    const usage = bill.sections[1]?.lines[0];
    expect(usage?.quantity).toBeCloseTo(7.3);
    expect(usage?.losses).toBeCloseTo(7.3 * (LOSS_FACTOR - 1));
    expect(usage?.rate).toBeCloseTo(0.18);
    expect(bill.sections[0]?.lines[1]?.losses).toBeNull();
    expect(bill.sections[0]?.lines[1]?.quantity).toBeCloseTo(6.64);
  });

  it("charges every line its units, plus any losses, at its unit price", () => {
    for (const line of bill.sections.flatMap((section) => section.lines)) {
      expect((line.quantity + (line.losses ?? 0)) * (line.rate ?? 0)).toBeCloseTo(line.amount);
    }
  });

  it("keeps a credit negative", () => {
    const credit = bill.sections[1]?.lines[1];
    expect(credit?.amount).toBeLessThan(0);
    expect(credit?.rate).toBeCloseTo(-0.16);
  });

  it("adds up each section, and every line", () => {
    for (const section of bill.sections) {
      const lines = section.lines.reduce((total, line) => total + line.amount, 0);
      expect(section.total).toBeCloseTo(lines);
    }
    const all = bill.sections.reduce((total, section) => total + section.total, 0);
    expect(bill.charges).toBeCloseTo(all);
    expect(bill.total).toBeCloseTo(bill.charges + bill.gst);
  });

  it("leaves out lines with nothing charged, and sections left empty", () => {
    const quiet = new Map(CHANGES);
    for (const key of ["s:line_export_supplier_charge_low", "s:line_export_supplier_charge_low_energy"]) {
      quiet.set(key, 0);
    }
    quiet.set("s:line_import_supplier_charge_24", null);
    quiet.set("s:line_import_supplier_charge_24_energy", null);
    quiet.set("s:daily_charge_metering", 0);
    const trimmed = buildBill(LAYOUT, quiet, 1);
    expect(trimmed.sections.map((section) => section.kind)).toEqual(["network", "other"]);
    expect(trimmed.sections[1]?.lines.map((line) => line.label)).toEqual([
      "EA levy",
      "Daily charge - Ecotricity",
    ]);
  });

  it("has no rate for a line charged without usage", () => {
    const odd = buildBill(LAYOUT, new Map([...CHANGES, ["s:line_import_network_charge_high_energy", 0]]), 1);
    expect(odd.sections[0]?.lines[2]?.rate).toBeNull();
  });

  it("shows each line without GST, whatever the integration's option", () => {
    // The plan's own rates, as the fixture's statistics were charged at.
    const lines = bill.sections.flatMap((section) => section.lines);
    expect(lines.find((line) => line.label === "Import - Anytime")?.rate).toBeCloseTo(0.18);
    expect(lines.find((line) => line.label === "EA levy")?.rate).toBeCloseTo(0.002);
    expect(lines.find((line) => line.label === "Daily charge - Network")?.rate).toBeCloseTo(1.5951);
  });

  it("adds GST once, on imports and daily charges but not export", () => {
    const taxed = bill.sections
      .flatMap((section) => section.lines)
      .filter((line) => !line.label.startsWith("Export"))
      .reduce((total, line) => total + line.amount, 0);
    expect(bill.gst).toBeCloseTo(taxed * 0.15);
    expect(bill.total).toBeCloseTo(bill.charges + bill.gst);
    // A bill of only export credits has no GST.
    const exportOnly = new Map([...CHANGES].map(([id, value]) => [id, id.includes("export") ? value : 0]));
    expect(buildBill(LAYOUT, exportOnly, 1).gst).toBe(0);
  });

  it("has a total of nothing with no statistics", () => {
    const empty = buildBill(LAYOUT, new Map(), 1);
    expect(empty.sections).toEqual([]);
    expect(empty.charges).toBe(0);
    expect(empty.gst).toBe(0);
    expect(empty.total).toBe(0);
  });
});

describe("lineLabel", () => {
  const all = LAYOUT.sections.flatMap((section) => section.lines);

  it("names every line the same way, whatever the network calls it", () => {
    expect(all.map(lineLabel)).toEqual([
      "Daily charge - Network",
      "Import - Off-peak",
      "Import - Peak",
      "Export - Peak",
      "Import - Anytime",
      "Export - Off-peak",
      "EA levy",
      "Daily charge - Metering",
      "Daily charge - Ecotricity",
    ]);
    // None of the plan's own wording shows.
    for (const line of all) {
      expect(lineLabel(line)).not.toContain(line.name);
    }
  });

  it("names periods the way the integration's period sensors do", () => {
    const usage = all[1]!;
    expect(lineLabel({ ...usage, period: "super_off_peak" })).toBe("Import - Super off-peak");
    expect(lineLabel({ ...usage, period: "shoulder_peak" })).toBe("Import - Shoulder peak");
  });

  it("keeps the integration's or plan's name for what it has no name for", () => {
    const [daily, usage] = all;
    expect(lineLabel({ ...usage!, period: "charge_9", name: "Charge 9" })).toBe("Import - Charge 9");
    expect(lineLabel({ ...daily!, part: "connection_fee", name: "Connection Fee" })).toBe(
      "Daily charge - Connection Fee",
    );
    const levy = all.find((line) => line.kind === "levy")!;
    expect(lineLabel({ ...levy, part: "carbon_levy", name: "Carbon Levy" })).toBe("Carbon Levy");
  });
});

describe("statisticIds", () => {
  it("lists every statistic once", () => {
    const ids = statisticIds(LAYOUT);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual(expect.arrayContaining([...CHANGES.keys()]));
    expect(ids).toHaveLength(CHANGES.size);
  });
});

describe("bill options", () => {
  it("lists lines that charged nothing when asked to", () => {
    const quiet = new Map([...CHANGES].map(([id, value]) => [id, id.includes("supplier") ? 0 : value]));
    const hidden = buildBill(LAYOUT, quiet, 1).sections.flatMap((section) => section.lines);
    const shown = buildBill(LAYOUT, quiet, 1, { showEmptyLines: true }).sections.flatMap(
      (section) => section.lines,
    );
    expect(shown.length).toBeGreaterThan(hidden.length);
    const empty = shown.find((line) => line.label === "Daily charge - Ecotricity");
    expect(empty).toMatchObject({ amount: 0, rate: null });
  });
});

describe("billingPeriodStart", () => {
  it("starts on the billing period start day this month, or last month's before it comes round", () => {
    expect(billingPeriodStart(new Date(2026, 9, 27, 9, 30), 15)).toEqual(new Date(2026, 9, 15));
    expect(billingPeriodStart(new Date(2026, 9, 10, 9, 30), 15)).toEqual(new Date(2026, 8, 15));
    expect(billingPeriodStart(new Date(2026, 0, 3), 15)).toEqual(new Date(2025, 11, 15));
    // Day 1 is the calendar month, starting at midnight on the day itself.
    expect(billingPeriodStart(new Date(2026, 9, 1, 0, 0), 1)).toEqual(new Date(2026, 9, 1));
  });
});

describe("wholeHours", () => {
  it("takes in the last hour of a day the date picker ends at 23:59:59.999", () => {
    // 23 October 2026 in New Zealand (UTC+13).
    const day = wholeHours(new Date("2026-10-22T11:00:00Z"), new Date("2026-10-23T10:59:59.999Z"));
    expect(day.start.toISOString()).toBe("2026-10-22T11:00:00.000Z");
    expect(day.end.toISOString()).toBe("2026-10-23T11:00:00.000Z");
  });

  it("leaves whole hours as they are, and widens a range ending now to the hour", () => {
    const whole = wholeHours(new Date("2026-10-22T11:00:00Z"), new Date("2026-10-23T11:00:00Z"));
    expect(whole.end.toISOString()).toBe("2026-10-23T11:00:00.000Z");
    const now = wholeHours(new Date("2026-10-22T11:20:00Z"), new Date("2026-10-26T09:42:13Z"));
    expect(now.start.toISOString()).toBe("2026-10-22T11:00:00.000Z");
    expect(now.end.toISOString()).toBe("2026-10-26T10:00:00.000Z");
  });
});

describe("formatting", () => {
  it("shows amounts like the bill, with credits in brackets", () => {
    expect(formatAmount(1.3874)).toBe("$1.39");
    expect(formatAmount(-0.9136)).toBe("($0.91)");
    expect(formatAmount(0)).toBe("$0.00");
    expect(formatAmount(-0.00001)).toBe("$0.00");
  });

  it("shows rates to four places at most and two at least", () => {
    expect(formatRate(0.18)).toBe("0.18");
    expect(formatRate(0.0251)).toBe("0.0251");
    expect(formatRate(1.5951)).toBe("1.5951");
    expect(formatRate(0.3)).toBe("0.30");
    expect(formatRate(-0.02)).toBe("(0.02)");
    expect(formatRate(0.123456)).toBe("0.1235");
  });

  it("shows kWh to two places, and days whole unless one was partly charged", () => {
    expect(formatQuantity(7.3, "kWh")).toBe("7.30");
    expect(formatQuantity(3, "days")).toBe("3");
    expect(formatQuantity(22.5, "days")).toBe("22.5");
    expect(formatQuantity(1 / 3, "days")).toBe("0.33");
  });
});
