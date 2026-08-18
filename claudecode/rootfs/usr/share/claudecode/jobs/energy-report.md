---
description: Daily energy report with a comparison to the past week.
model: sonnet
timeout: 600
max_cost_usd: 1.00
max_turns: 50
stale_after: 93600
tools:
  - mcp__homeassistant__list_entities
  - mcp__homeassistant__get_entity
  - mcp__homeassistant__get_history
  - mcp__homeassistant__domain_summary_tool
notify:
  info: [persistent]
  ok: [persistent]
input:
  date:
    type: string
    pattern: '^\d{4}-\d{2}-\d{2}$'
    description: Which day to report on; defaults to yesterday.
---
# Daily energy report

Produce a short energy report for one day and compare it with the average of the seven
days before it. You only read data; you change nothing.

## Which day

If a `<job-input>` block above gives `date` (YYYY-MM-DD), report on that day. Otherwise
report on **yesterday** in the house's local time zone (your `TZ` environment is already
set to it; "yesterday" means the full local calendar day before today). State the day you
chose in the headline.

## Finding the energy entities (works on any install)

1. Use `mcp__homeassistant__domain_summary_tool` for the `sensor` domain and
   `mcp__homeassistant__list_entities` (domain `sensor`, search terms such as `energy`,
   `kwh`, `consumption`, `production`, `solar`, `grid`, `meter`) to collect candidates.
2. Keep sensors whose attributes (via `mcp__homeassistant__get_entity`) show
   `device_class: energy` with `state_class: total` or `total_increasing` and a unit of
   `kWh`/`Wh`/`MWh`. These are cumulative meters: consumption for a period is
   `last reading − first reading` within the period (handle a meter reset — a drop to near
   zero — by summing the segments). Convert everything to kWh.
3. Classify by name/attributes: grid import (consumption), grid export / solar production,
   battery in/out, and individual device meters (plugs, appliances, EV charger, heat pump).
   Utility-meter helpers (`sensor.*_daily`, `*_monthly`) are fine to use directly if present.
   If the install has an obvious whole-house meter, prefer it for the total; otherwise sum
   the device meters and say the total is partial.
4. If you find no energy sensors at all, report `info` with a headline saying so and list in
   `detail` what you searched for. Do not guess.

## Computing

- `mcp__homeassistant__get_history(entity_id, hours)` returns the last `hours` hours ending
  NOW — it cannot start at an arbitrary date. Ask for a window large enough to cover the report
  day plus the seven days before it (about 200 hours when reporting on yesterday; more for an
  older `date`), then slice the days out of what comes back. If the window cannot reach far
  enough back, compute what you can and say so in `detail`. Query at most 5 entities.
- Report-day totals: consumption kWh, production/solar kWh (if any), net.
- Seven-day daily average for the same quantities, and the report day as a percentage of it.
- Top consumers: the three device meters with the highest kWh on the report day.
- Note any energy meter that was `unavailable`/`unknown` for a meaningful part of the day.

## How to report

- `status`: `info` normally (this is a report worth reading). `warning` if consumption was
  more than 50 % above the seven-day average, or if a main meter was unavailable for over an
  hour. `ok` only if there was genuinely nothing to say (e.g. no data for the day yet).
- `headline`: day + total + comparison, e.g. "2026-08-17: 14.2 kWh used (+12 % vs 7-day avg),
  9.8 kWh solar" (percent change: +12 means 12 % above the average, -8 means 8 % below).
- `detail`: a small Markdown table (quantity, report day, 7-day average, change), the top
  consumers list, any data-quality notes, and one line naming the entities you used so the
  numbers can be checked.
- `metrics`: `total_kwh`, `solar_kwh` (omit if no production), `top_consumer_kwh`,
  `vs_avg_pct` (the same percent change as the headline: +12 = 12 % above the 7-day average,
  0 = equal), `meters_unavailable`.

Other Home Assistant MCP tools may be listed but only the four above are granted; calling
anything else is denied — do not probe.
