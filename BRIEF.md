# ha-ecobee — program control fork of the core ecobee integration

Why: setting comfort settings (Home/Away/Sleep temperatures) and schedules across five ecobee thermostats is
painful in the ecobee app. The core Home Assistant integration (copied here verbatim from HA 2026.11.0 into
`custom_components/ecobee/`) can only create holds; it cannot write the program. This fork overrides core
(same domain `ecobee`, installed through HA's Marketplace from github.com/jasperchan/ha-ecobee) and adds
program control. Keep the diff against core SMALL and ISOLATED (new files + minimal registration edits) so the
fork can be rebased on future core versions.

## Facts established (do not re-derive)
- Library: python-ecobee-api 0.4.2 (`_reference/pyecobee/` is a copy). It has no program writer. It exposes
  `Ecobee._request_with_refresh(method, endpoint, log_msg_action, params=None, body=None)` which handles token
  refresh; existing setters POST to `ECOBEE_ENDPOINT_THERMOSTAT` with a body of
  `{"selection": {"selectionType": "thermostats", "selectionMatch": <identifier>}, "thermostat": {...}}`.
  Use the same mechanism for `{"thermostat": {"program": {...}}}`. Thermostat identifiers come from
  `ecobee.thermostats[index]["identifier"]`; HA entities carry `thermostat_index`.
- HA side: `EcobeeData` (in `__init__.py`) wraps the library; `data.ecobee.thermostats[i]["program"]` holds
  the current program; writes must run in the executor (`hass.async_add_executor_job`); after a write, call
  `await data.update()` so entities refresh. Services are registered in `services.py` via
  `hass.services.async_register` / `async_register_platform_entity_service`; the climate entities are
  `Thermostat` objects in `hass.data[DOMAIN]` style lookups (see `_async_get_thermostats`). Number entities
  follow the pattern in `number.py` (`EcobeeNumberEntityDescription` + `EcobeeBaseEntity`).
- Program object (verified against a live thermostat, fixture in `tests/fixtures/thermostats_program.json`):
  `program.schedule` is 7 lists of 48 climateRef strings (half-hour slots, 00:00 first). Day order per ecobee
  docs: index 0 = Sunday … 6 = Saturday (the implementer MUST confirm this from
  https://developer.ecobee.com docs and note the source in code). `program.climates` is a list of Climate
  objects with keys climateRef, name, isOccupied, isOptimized, coolFan, heatFan, vent, ventilatorMinOnTime,
  owner, type, colour, coolTemp, heatTemp, sensors (list of {"id": "ei:0:1", "name": "Atlas Room"}).
  Temperatures are integers in TENTHS of a degree Fahrenheit (700 = 70.0 °F) regardless of the user's unit.
  `program.currentClimateRef` is read-only. Climate doc: climateRef is server-generated and must be supplied
  for existing climates, otherwise the climate is treated as new; names must stay unique; for sensors only
  `id` is mandatory. Always send the COMPLETE program (all climates + full schedule) on every write.
- The house: five thermostats named Atlas Room, Hallway, Guest Room, Master Bedroom, Kitchen; climates home,
  away, sleep on each (Atlas Room's Home is 70/78, the others 69/78; Away 64/82; Sleep 65/82). HA is in °F.

## Deliverables (first slice)
1. `custom_components/ecobee/program.py` — pure, unit-tested helpers: temperature <-> tenths °F (and °C when
   HA uses it); compact schedule spec parsing (`"07:00 home, 21:00 sleep"` per day; day keys sun..sat plus
   `weekdays`, `weekend`, `all`; times snap to 30 min, validate) and formatting back to compact form; building
   a full program payload from the current program plus requested changes; validation (unknown climateRef,
   duplicate names, temps within 45–92 °F, cool ≥ heat + deadband 2 °F unless hvac is heat-only/cool-only).
2. Program writer on `EcobeeData` (or a small helper module): `async_set_program(index, program)` that POSTs
   the complete program through `_request_with_refresh`, raises a clear HomeAssistantError on failure, then
   refreshes data.
3. Services (register in `services.py`, document in `services.yaml` + `strings.json`/translations):
   - `ecobee.set_comfort_setting` {entity_id (climate entity), climate_ref, heat_temp?, cool_temp?, sensors? (list
     of sensor names or ids)} — update one climate's temps/sensors on one thermostat, full-program write.
   - `ecobee.set_schedule` {entity_id(s), schedule: {day_key: "HH:MM ref, HH:MM ref, …"}} — unspecified days
     untouched; a day spec must start at 00:00 or inherit the previous day's last climate for the gap before
     the first entry (document the chosen rule).
   - `ecobee.copy_program` {entity_id (source), targets (entity_ids), comfort_settings: bool=true,
     schedule: bool=true, preserve_target_temperatures: bool=false} — copies climates (matched by climateRef;
     climates missing on the target are created WITHOUT climateRef; sensors are NOT copied — they are
     thermostat-specific — the target keeps its own sensors per climateRef) and/or the schedule.
   - `ecobee.get_program` {entity_id} with `supports_response=SupportsResponse.ONLY` returning
     {climates: [{ref, name, heat_temp, cool_temp, sensors:[names]}], schedule: {sun: "00:00 sleep, 06:00 home, 23:30 sleep", …}} in the user's unit.
4. Number entities (new file `program_numbers.py` or extend `number.py`; prefer a new file and add to the
   number platform's setup): per thermostat and per climate in (home, away, sleep): `<Thermostat> <Climate> heat`
   and `… cool` — device_class temperature, native unit °F (HA converts), min 45 max 92 step 0.5,
   entity_category CONFIG, unique_id `<thermostat identifier>-program-<ref>-heat|cool`, icon mdi:thermometer-chevron-up/down.
   Setting one writes the full program; read values from `program.climates`.
5. Tests: `tests/test_program.py` with plain pytest covering parsing, formatting, payload building, validation,
   copy semantics (use the fixture). Must pass with `python3 -m pytest` in a venv that has only pytest (no HA).
   The HA-coupled parts (services, numbers) must import cleanly; keep logic in `program.py` so it is testable.
6. README.md: what it adds, service examples (YAML), the day-order and temperature-unit facts, install via
   Marketplace custom repository, the fact that it overrides core and needs an HA restart.

## Rules
- Never change behaviour of existing core code paths beyond registering the additions.
- Every write sends the complete program; never send partial climates.
- No network calls in tests. No secrets in the repo.
- Python 3.13+, HA 2026.11 APIs (ServiceCall, SupportsResponse, HomeAssistantError, async_add_executor_job).
