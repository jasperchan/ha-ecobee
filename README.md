# ha-ecobee — program control fork

A fork of Home Assistant's core `ecobee` integration (HA 2026.11.0) that adds control over the
thermostat **program**: comfort-setting temperatures, participating sensors, the weekly schedule, and
copying all of it from one thermostat to the others. It exists because setting schedules and comfort
settings across several ecobee thermostats in the ecobee app is painful.

Everything core does is unchanged. The fork adds four files
(`program.py`, `program_writer.py`, `program_services.py`, `program_numbers.py`), registers them with
two one-line hooks in `services.py` and `number.py`, and documents them in `services.yaml`,
`strings.json`, `translations/en.json` and `icons.json`.

## Install

1. Home Assistant → Marketplace (HACS) → three-dot menu → **Custom repositories** → add
   `https://github.com/jasperchan/ha-ecobee` as type *Integration*.
2. Install **ecobee (program control fork)**.
3. **Restart Home Assistant.** The fork uses the same domain `ecobee` as core, so it overrides the
   built-in integration; your existing ecobee config entry, entities and automations keep working.
   Nothing to reconfigure.

To go back to core, uninstall the repository and restart.

## What it adds

### Services

All services take the **climate** entity of a thermostat (`climate.kitchen`, …). Temperatures are in
your Home Assistant unit (°F or °C); the fork converts to ecobee's tenths-of-°F internally. Every
write sends the complete program (all climates + the full 7×48 schedule), which is what the ecobee
API requires.

#### `ecobee.set_comfort_setting`

Change one comfort setting (climate) on one thermostat: heat and/or cool temperature and/or the
participating sensors.

```yaml
action: ecobee.set_comfort_setting
data:
  entity_id: climate.atlas_room
  climate_ref: home          # climateRef or display name: home / away / sleep / smart1 / "Home"
  heat_temp: 70
  cool_temp: 78
  sensors:                   # optional; replaces the list; names or ids (ei:0, rs2:100)
    - Atlas Room
    - Jasper Room
```

#### `ecobee.set_schedule`

Replace selected days of the weekly schedule using a compact spec. Days you do not mention are left
exactly as they are.

```yaml
action: ecobee.set_schedule
data:
  entity_id:
    - climate.kitchen
    - climate.hallway
  schedule:
    weekdays: "06:30 home, 22:00 sleep"
    weekend: "08:00 home, 23:00 sleep"
    # also: mon tue wed thu fri sat sun, all (a concrete day overrides a group)
```

Rules:

- Each entry is `HH:MM climate`, comma-separated, strictly increasing times. `climate` is a climateRef
  or name.
- Times snap to the nearest 30 minutes (ecobee's resolution): `06:40` → `06:30`, `06:50` → `07:00`;
  exact quarter hours round up (`06:45` → `07:00`, `07:15` → `07:30`).
- **Gap before the first entry:** if a day's spec does not start at `00:00`, the slots from midnight
  up to the first entry continue whatever climate was running at the end of the previous day (Sunday
  for Monday), after that day's own changes. So `weekdays: "06:30 home, 22:00 sleep"` keeps `sleep`
  running from 22:00 through 06:30 the next morning, which is how the ecobee app shows it. Start a
  spec with `00:00 …` to set the night explicitly.

#### `ecobee.copy_program`

Copy comfort settings and/or the schedule from one thermostat to others.

```yaml
action: ecobee.copy_program
data:
  entity_id: climate.atlas_room        # source
  targets:
    - climate.hallway
    - climate.guest_room
    - climate.master_bedroom
    - climate.kitchen
  comfort_settings: true               # default true
  schedule: true                       # default true
  preserve_target_temperatures: false  # default false
```

Semantics:

- The standard climates (`home`, `away`, `sleep`; also `wakeup`, `occupied`, `unoccupied`) are matched
  by `climateRef`. Custom comfort settings get a server-generated ref (`smart1`, `smart2`, …) that is
  assigned per thermostat, so the same ref on two thermostats is usually a different climate; they
  are matched by **name** (case-insensitive) and the target keeps its own ref. A target climate with
  the same `smartN` ref but a different name is left alone.
- Matched climates take every field from the source **except** `climateRef` (kept) and `sensors`
  (sensors are thermostat-specific; each target keeps its own). With
  `preserve_target_temperatures: true` the target also keeps its own heat/cool.
- Source climates that do not exist on a target are created without a `climateRef` (the server
  assigns one) and get the target's *Home* sensors (or those of the first target climate that has
  any). The copied schedule is remapped to the target's refs. A brand-new climate cannot be referenced
  by the schedule in the same write, so for such targets the service writes twice: climates first,
  then — after re-reading the ref the server assigned — the schedule. One call does the whole job.
- Target-only climates are kept.
- The source itself is skipped if it is also listed in `targets`.
- `set_schedule` and `copy_program` validate every target before writing any of them; a validation
  error on one target means nothing is written.

#### `ecobee.get_program`

Response-only; use it in a script with `response_variable` or from Developer tools → Actions.

```yaml
action: ecobee.get_program
data:
  entity_id: climate.kitchen
response_variable: kitchen_program
```

Returns, in your unit:

```yaml
climates:
  - ref: home
    name: Home
    heat_temp: 69.0
    cool_temp: 78.0
    sensors: [Kitchen]
  - ref: away
    ...
schedule:
  mon: "00:00 sleep, 06:30 home, 23:30 sleep"
  tue: "00:00 sleep, 06:30 home, 23:30 sleep"
  ...
  sun: "00:00 sleep, 06:30 home, 23:30 sleep"
current_climate_ref: home
```

The `schedule` strings are exactly the format `set_schedule` accepts, so you can read, tweak, write.

### Number entities

For each thermostat and each of the default comfort settings present (home, away, sleep) the fork
adds two configuration numbers: **`<Thermostat> Home heat`**, **`<Thermostat> Home cool`**, and so
on (six per thermostat). Range = the thermostat's own heat range (heat numbers) or cool range (cool
numbers) from its settings (ecobee defaults 45–79 °F / 65–92 °F), 0.5 steps, shown in your HA unit,
icons `mdi:thermometer-chevron-up` / `-down`, unique id `<identifier>-program-<ref>-heat|cool`.
Changing one rewrites the full program; a change that would violate the rules below is rejected with
a clear error.

### Validation (applied before every write)

- Every schedule slot must reference an existing climateRef; schedule must be 7 days × 48 slots.
- Climate names unique (case-insensitive), climateRefs unique; every climate has at least one sensor.
- Heat set points within the thermostat's heat range and cool set points within its cool range
  (`settings.heatRangeLow/High`, `coolRangeLow/High`; 45–92 °F if unknown).
- Cool ≥ heat + the thermostat's `heatCoolMinDelta` (ecobee factory default 5 °F; 2 °F if unknown)
  unless its `hvacMode` is `heat`, `cool`, `auxHeatOnly` or `off` — the same rule the server applies.

Failures raise a Home Assistant error (`Invalid ecobee program change: …`); nothing is written. If
ecobee rejects the write, the error carries ecobee's status code and message.

Every write re-reads the thermostat from ecobee first (the integration's cache can be up to three
minutes old and a program update replaces the whole program, so building it from a stale cache would
undo changes just made in the ecobee app), applies the change, posts, and re-reads again. Writes to
one thermostat are serialised, so setting several numbers at once cannot lose a change.

## Facts worth knowing

- **Temperatures** in the ecobee API are integers in tenths of a degree Fahrenheit (`700` = 70.0 °F)
  regardless of the account's display unit. The fork converts from/to your HA unit; °C values are
  rounded to 0.1 °C on read.
- **Schedule layout:** `program.schedule` is 7 lists of 48 climateRef strings; slot 0 is 00:00–00:30,
  slot 47 is 23:30–24:00.
- **Day order — index 0 is Monday, index 6 is Sunday.** Source: ecobee API example 11, "Changing your
  thermostat's program" (<https://www.ecobee.com/home/developer/api/examples/ex11.shtml>): *"The
  schedule is an array of 7 elements. Each element represents a day within the week starting with
  Monday. Each day is split into 48 thirty minutes slots. Each slot is a string reference to a
  climate."* The newer ecobee SmartBuildings schedule schema
  (<https://docs.sb.ecobee.com/reference/getthermostatschedule>) also lists monday…sunday.
  **Heads-up:** the Core Concepts page shows an illustrative table with columns Sun…Sat and the
  original design brief assumed Sunday first; the explicit statement in example 11 was taken as
  authoritative. To verify on your own thermostats, call `ecobee.get_program` after making a
  single-day change in the ecobee app, e.g. a different Saturday wake-up time, and check which key
  moved. If it is off, change `SCHEDULE_DAYS` in `program.py`; nothing else hard-codes the order.
- `program.currentClimateRef` is read-only and is stripped before every write.
- `climateRef` is server-generated; it must be supplied for existing climates and omitted for new
  ones. Climate sensor entries need only `id` (the temperature capability id, e.g. `ei:0:1`,
  `rs2:100:1` — the remote sensor id plus `:1`), which is also what pyecobee sends.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests
```

The tests load `custom_components/ecobee/program.py` directly (no Home Assistant or pyecobee needed)
and exercise parsing, formatting, payload building, validation and copy semantics against
`tests/fixtures/thermostats_program.json`, a five-thermostat program captured from a live account.

Nothing in the tests talks to the network. The HA-coupled modules (`program_writer.py`,
`program_services.py`, `program_numbers.py`) hold no program logic of their own and can only be
exercised inside Home Assistant.

## Keeping the fork current

Core files were copied verbatim from HA 2026.11.0. To rebase on a newer core: replace everything
except the four `program*.py` files, then re-apply the two hook lines (`services.py`:
`async_setup_program_services(hass)`; `number.py`: `entities.extend(build_program_number_entities(data))`),
and re-add the fork's entries in `services.yaml`, `strings.json`, `translations/en.json`, `icons.json`
and `manifest.json` (name, version, codeowners, documentation, issue_tracker).
