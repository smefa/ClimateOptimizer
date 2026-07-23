# ClimateOptimizer

A Home Assistant custom integration (HACS) that computes a **compensated outdoor
temperature** for a heat pump's weather-compensation curve, adjusted for:

- Indoor temperature vs. your target (closes the loop — your heat pump usually
  only sees outdoor temperature, not how it's actually going indoors)
- Forecast wind speed (extra heat loss)
- Forecast sun / cloud cover (passive solar gain)
- Electricity price via Nordpool (optional — let indoor temperature drift down,
  within limits you set, during expensive price periods)

ClimateOptimizer does **not** write to your heat pump directly. It publishes a
sensor, `sensor.<name>_compensated_outdoor_temperature`, with the computed value
and full attribute breakdown of *why* it's that value. You wire that sensor into
your heat pump's own external-temperature input (however your specific
integration/automation supports that).

Everything is configured from the Home Assistant UI — no YAML.

## Installation

1. Add this repository to HACS as a custom repository (category: Integration),
   or once published, install directly from HACS.
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **ClimateOptimizer**.
4. Pick your indoor temperature sensor, an outdoor temperature sensor (a real
   sensor entity — used as the current-temperature baseline, since it's
   generally more accurate than a weather service's estimate), a weather
   entity (used only for its wind/cloud forecast), an optional Nordpool price
   entity, and your target indoor temperature.
5. Tune coefficients, comfort bounds, and the price feature later via the
   integration's **Configure** (options) dialog — no reinstall needed.

## How the value is computed (Phase 1: heuristic)

```
compensated_outdoor_temp = raw_outdoor_temp
    - k_indoor * (indoor_target - indoor_actual)   # colder than target -> read lower -> more heat
    - k_wind   * wind_speed_m_s                     # windier -> read lower -> more heat
    + k_sun    * solar_effect                        # sunnier -> read higher -> less heat
    + price_adjustment                                # expensive price -> read higher -> less heat (optional, bounded)
```

All coefficients, comfort min/max bounds, and price thresholds are adjustable
in the options flow. The sensor's attributes include a per-term breakdown and a
plain-language `reason` string so the output is always explainable.

This is intentionally a simple, transparent heuristic, not a black-box model.
A future phase will add an optional physics-based (RC network) thermal model
with online parameter fitting and a proper multi-hour cost-optimizing
controller — the current heuristic is structured so that can slot in later
without breaking existing sensors/automations.

## License

MIT — see [LICENSE](LICENSE).
