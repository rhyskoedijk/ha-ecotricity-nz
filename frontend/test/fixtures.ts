/** A bill layout and one day's statistics for it, shared by the tests. */
import type { BillLayout, LayoutLine } from "../src/bill";

export const LOSS_FACTOR = 1.0548;

/** A plan with a daily charge, peak and off-peak lines on both sides, a levy and export. */
export const LAYOUT: BillLayout = {
  icp: "0000123456AB1C2",
  network_name: "Example Lines",
  loss_factor: LOSS_FACTOR,
  // Export has had no losses on the ICPs seen so far.
  export_loss_factor: 1,
  // Each line's name is what a network might call it, which the card mustn't show.
  sections: [
    {
      section: "network",
      lines: [
        daily("daily_charge_network", "network", "Daily Network Charges"),
        usage("line_import_network_charge_low", "import", "off_peak", "Uncontrolled - Off Peak"),
        usage("line_import_network_charge_high", "import", "peak", "Uncontrolled - On Peak"),
        usage("line_export_network_charge_high", "export", "peak", "On Peak Export"),
      ],
    },
    {
      section: "energy",
      lines: [
        usage("line_import_supplier_charge_24", "import", "anytime", "Anytime usage", true),
        usage("line_export_supplier_charge_low", "export", "off_peak", "Off Peak Export", true),
      ],
    },
    {
      section: "other",
      lines: [
        { ...line("line_import_ea_levy", "energy_imported"), kind: "levy", part: "ea_levy", period: null, name: "EA Levy" },
        daily("daily_charge_metering", "metering", "Metering Fee"),
        daily("daily_charge_supplier", "supplier", "Admin & climate positive certification"),
      ],
    },
  ],
  gst_rate: 0.15,
};

function line(key: string, usageKey: string | null, losses = false) {
  const usage_statistic_id = usageKey && `s:${usageKey}`;
  return { key, statistic_id: `s:${key}`, usage_statistic_id, losses };
}

function daily(key: string, part: string, name: string): LayoutLine {
  return { ...line(key, null), kind: "daily", part, period: null, name };
}

function usage(key: string, side: string, period: string, name: string, losses = false): LayoutLine {
  return { ...line(key, `${key}_energy`, losses), kind: "usage", part: side, period, name };
}

/** One day's statistics: 7.30 kWh imported (6.64 off-peak, 0.66 peak), 2.12 + 5.71 exported. */
export const CHANGES = new Map<string, number | null>([
  ["s:daily_charge_network", 1.5951],
  ["s:line_import_network_charge_low", 6.64 * 0.0251],
  ["s:line_import_network_charge_low_energy", 6.64],
  ["s:line_import_network_charge_high", 0.66 * 0.1451],
  ["s:line_import_network_charge_high_energy", 0.66],
  ["s:line_export_network_charge_high", 2.12 * -0.02],
  ["s:line_export_network_charge_high_energy", 2.12],
  ["s:line_import_supplier_charge_24", 7.3 * LOSS_FACTOR * 0.18],
  ["s:line_import_supplier_charge_24_energy", 7.3],
  ["s:line_export_supplier_charge_low", 5.71 * -0.16],
  ["s:line_export_supplier_charge_low_energy", 5.71],
  ["s:line_import_ea_levy", 7.3 * 0.002],
  ["s:energy_imported", 7.3],
  ["s:daily_charge_metering", 0.35],
  ["s:daily_charge_supplier", 0.3],
]);
