/**
 * Works out a bill from the integration's statistics, laid out like Ecotricity's own.
 *
 * Nothing here touches the DOM or Home Assistant, so it can be tested on its own. Amounts are in
 * dollars, and keep their sign: a credit is negative. Like Ecotricity's bill, lines exclude GST,
 * and GST is added once at the end, on the total: export credits included.
 */

/** A section of the bill, in the order it lists them. */
export type SectionKind = "network" | "energy" | "other";

/** What a line charges for. */
type LineKind = "daily" | "usage" | "levy";

/** One line of the bill, as the `ecotricity_nz/bill` websocket command describes it. */
export interface LayoutLine {
  key: string;
  kind: LineKind;
  /** For a daily charge or levy, which one; for usage, the side: "import" or "export". */
  part: string;
  /** For usage, the period's key, as the integration's period sensors have it. */
  period: string | null;
  /** The period's name for usage; otherwise the plan's description, for what has no name here. */
  name: string;
  statistic_id: string;
  /** The kWh it's charged on, or null for a daily charge. */
  usage_statistic_id: string | null;
  /** Whether it's charged on the kWh plus line losses, as Ecotricity's import and export are. */
  losses: boolean;
}

/** The bill's layout, from the `ecotricity_nz/bill` websocket command. */
export interface BillLayout {
  icp: string;
  network_name: string;
  /** The consumption loss factor, for import. */
  loss_factor: number;
  /** The generation loss factor, for export. */
  export_loss_factor: number;
  sections: { section: SectionKind; lines: LayoutLine[] }[];
  /** The GST rate charged on the total, such as 0.15. */
  gst_rate: number;
}

/** Each statistic's change over the bill's range, null where it has no data. */
export type Changes = ReadonlyMap<string, number | null>;

interface BillLine {
  label: string;
  /** What it's charged on: kWh used, or days. Any losses are charged on top. */
  quantity: number;
  unit: "kWh" | "days";
  /** kWh of line losses charged on top of the quantity, or null where there are none. */
  losses: number | null;
  /** The average rate over the range, in dollars excluding GST, or null with nothing to charge on. */
  rate: number | null;
  amount: number;
}

export interface BillSection {
  kind: SectionKind;
  lines: BillLine[];
  total: number;
}

export interface Bill {
  sections: BillSection[];
  /** Every line added up, excluding GST. */
  charges: number;
  /** GST on the total, export credits included, so a credit has negative GST. */
  gst: number;
  /** What the bill comes to, including GST. */
  total: number;
}

/** Period names, the same as the integration's period sensors show. */
const PERIOD_NAMES: Record<string, string> = {
  anytime: "Anytime",
  peak: "Peak",
  off_peak: "Off-peak",
  shoulder: "Shoulder",
  shoulder_peak: "Shoulder peak",
  super_off_peak: "Super off-peak",
  weekend: "Weekend",
};

const SIDE_NAMES: Record<string, string> = { import: "Import", export: "Export" };

/**
 * Daily charge items by who charges them, as the integration splits the daily charge. The
 * network's is the only daily charge in its section, so it needs no name.
 */
const DAILY_NAMES: Record<string, string> = {
  metering: "Metering",
  supplier: "Ecotricity",
};
const NETWORK_DAILY = "network";

const LEVY_NAMES: Record<string, string> = { ea_levy: "Electricity Authority (EA) Levy" };

/**
 * Name a line the same way whatever the network calls its rates: "Import - Off-peak Usage",
 * "Daily Charges - Ecotricity", "Electricity Authority (EA) Levy". Anything without a name of its
 * own here keeps the integration's (for a period) or the plan's.
 */
export function lineLabel(line: LayoutLine): string {
  switch (line.kind) {
    case "daily":
      if (line.part === NETWORK_DAILY) {
        return "Daily Charges";
      }
      return `Daily Charges - ${DAILY_NAMES[line.part] ?? line.name}`;
    case "levy":
      return LEVY_NAMES[line.part] ?? line.name;
    case "usage": {
      const period = (line.period && PERIOD_NAMES[line.period]) || line.name;
      return `${SIDE_NAMES[line.part] ?? line.part} - ${period} Usage`;
    }
  }
}

const HOUR_MS = 3_600_000;

/**
 * Return a range widened to whole hours, for asking the recorder about it.
 *
 * The integration's statistics are hourly, and the recorder reads any part of an hour at either
 * end of a range from 5-minute statistics, which they don't have, so that part would count as
 * nothing. The energy date picker ends a day at 23:59:59.999, which would lose its last hour.
 */
export function wholeHours(start: Date, end: Date): { start: Date; end: Date } {
  return {
    start: new Date(Math.floor(start.getTime() / HOUR_MS) * HOUR_MS),
    end: new Date(Math.ceil(end.getTime() / HOUR_MS) * HOUR_MS),
  };
}

/** Return every statistic a bill needs, each once. */
export function statisticIds(layout: BillLayout): string[] {
  const ids = new Set<string>();
  for (const section of layout.sections) {
    for (const line of section.lines) {
      ids.add(line.statistic_id);
      if (line.usage_statistic_id) {
        ids.add(line.usage_statistic_id);
      }
    }
  }
  return [...ids];
}

/** Which parts of the bill to show. */
interface BillOptions {
  /** Whether to list the plan's lines that charged nothing in the range, too. */
  showEmptyLines?: boolean;
}

/**
 * Work out the bill from each statistic's change over the range.
 *
 * `days` is how many days of daily charges the range holds (`ecotricity_nz/bill_days`): only
 * days whose usage has been imported are charged, so it can be fewer than the range spans.
 */
export function buildBill(layout: BillLayout, changes: Changes, days: number, options: BillOptions = {}): Bill {
  const value = (id: string | null): number => (id ? changes.get(id) ?? 0 : 0);
  const sections: BillSection[] = [];
  for (const section of layout.sections) {
    const lines: BillLine[] = [];
    for (const line of section.lines) {
      const amount = value(line.statistic_id);
      if (line.usage_statistic_id === null) {
        if (amount || options.showEmptyLines) {
          const rate = days && amount ? amount / days : null;
          lines.push({ label: lineLabel(line), quantity: days, unit: "days", losses: null, rate, amount });
        }
        continue;
      }
      const usage = value(line.usage_statistic_id);
      if (!amount && !usage && !options.showEmptyLines) {
        continue;
      }
      const factor = line.part === "export" ? layout.export_loss_factor : layout.loss_factor;
      const losses = line.losses && factor !== 1 ? usage * (factor - 1) : null;
      const charged = usage + (losses ?? 0);
      lines.push({
        label: lineLabel(line),
        quantity: usage,
        unit: "kWh",
        losses,
        rate: charged ? amount / charged : null,
        amount,
      });
    }
    if (lines.length) {
      sections.push({ kind: section.section, lines, total: sum(lines.map((line) => line.amount)) });
    }
  }
  const charges = sum(sections.map((section) => section.total));
  const gst = charges * layout.gst_rate;
  return { sections, charges, gst, total: charges + gst };
}

/**
 * Return when the billing period containing `now` started, for a bill that starts on `day` of
 * each month (1 to 28, so every month has it), at local midnight.
 */
export function billingPeriodStart(now: Date, day: number): Date {
  const start = new Date(now.getFullYear(), now.getMonth(), day);
  if (start.getTime() > now.getTime()) {
    start.setMonth(start.getMonth() - 1);
  }
  return start;
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

/** Show an amount the way the bill does: "$1.39", or "($0.45)" for a credit. */
export function formatAmount(amount: number): string {
  const text = `$${Math.abs(amount).toFixed(2)}`;
  return isCredit(amount) ? `(${text})` : text;
}

/** Show a rate in dollars the way the bill does: up to four places, "(0.21)" for a credit. */
export function formatRate(rate: number): string {
  const text = trimZeros(Math.abs(rate).toFixed(4));
  return isCredit(rate) ? `(${text})` : text;
}

/** Show a quantity: kWh to two places, days whole unless a day was only partly charged. */
export function formatQuantity(quantity: number, unit: "kWh" | "days"): string {
  if (unit === "days") {
    return String(Math.round(quantity * 100) / 100);
  }
  return quantity.toFixed(2);
}

/** Whether an amount shows as a credit, once rounded to what's shown. */
function isCredit(amount: number): boolean {
  return amount < 0 && Math.abs(amount) >= 0.00005;
}

/** Drop trailing zeros past two decimal places: 0.1800 is 0.18, 0.0251 stays. */
function trimZeros(text: string): string {
  return text.replace(/(\.\d\d\d*?)0+$/, "$1");
}
