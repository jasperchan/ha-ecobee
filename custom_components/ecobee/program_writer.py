"""Write a complete program to an ecobee thermostat.

python-ecobee-api 0.4.2 has no program writer, so this posts the same body shape its
own setters use::

    {"selection": {"selectionType": "thermostats", "selectionMatch": <identifier>},
     "thermostat": {"program": {"climates": [...], "schedule": [[...] x 7]}}}

The POST is made with ``requests`` directly (same URL, headers and timeout as
``pyecobee.Ecobee._request``) instead of through ``_request_with_refresh``: pyecobee
swallows an HTTP 500 that is not a token error, logs ecobee's ``status.message`` and
returns ``None``, which left the user with "no response" for every server-side
rejection. Here the JSON body is parsed whatever the HTTP status so the message
(``5: Invalid request. ...``) reaches the HomeAssistantError.

ecobee program updates are atomic: the ENTIRE program is sent and replaces the stored
one. Two consequences are handled here rather than by every caller:

* The cached ``data.ecobee.thermostats`` may be up to 180 s stale, so a program built
  from it would silently revert changes made in the ecobee app meanwhile. Writes
  therefore refresh (``data.update(no_throttle=True)``), build the program from the
  FRESH thermostat dict via the caller's ``build`` callable, POST, then refresh again
  to pick up server-assigned climateRefs.
* Concurrent writes to one thermostat (two number entities set in one service call)
  would each read-modify-write the same program and the last POST would win. A per
  thermostat ``asyncio.Lock`` serialises refresh-build-POST-refresh.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from pyecobee import EcobeeError, ExpiredTokenError, InvalidTokenError
from pyecobee.const import (
    ECOBEE_API_VERSION,
    ECOBEE_BASE_URL,
    ECOBEE_DEFAULT_TIMEOUT,
    ECOBEE_ENDPOINT_THERMOSTAT,
)
import requests

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from . import EcobeeData

#: ``build(thermostat) -> program``: given the freshly fetched thermostat dict, return
#: the complete program to write (validated; raise to abort before the POST).
ProgramBuilder = Callable[[Mapping[str, Any]], dict[str, Any]]

#: ecobee status codes that mean the access token must be refreshed / is unusable.
_CODE_EXPIRED_TOKEN = 14
_CODES_INVALID_TOKEN = (1, 16)

#: One lock per (config entry, thermostat identifier); module-level so services and
#: number entities share it.
_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


class ProgramWriteError(EcobeeError):
    """ecobee rejected the program; ``str(err)`` is ``"<code>: <message>"``."""


def _lock_for(data: EcobeeData, identifier: str) -> asyncio.Lock:
    key = (data.entry.entry_id, identifier)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


def _post_program(ecobee: Any, identifier: str, program: dict[str, Any]) -> dict[str, Any]:
    """Blocking POST of the program; runs in the executor.

    Returns the parsed JSON on ``status.code == 0``. Raises ExpiredTokenError /
    InvalidTokenError for the token status codes (same mapping as pyecobee) and
    ProgramWriteError with ecobee's own message for any other non-zero status.
    """
    url = f"{ECOBEE_BASE_URL}/{ECOBEE_API_VERSION}/{ECOBEE_ENDPOINT_THERMOSTAT}"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": f"Bearer {ecobee.access_token}",
    }
    body = {
        "selection": {"selectionType": "thermostats", "selectionMatch": identifier},
        "thermostat": {"program": program},
    }
    try:
        response = requests.post(
            url, headers=headers, json=body, timeout=ECOBEE_DEFAULT_TIMEOUT
        )
    except requests.RequestException as err:
        raise ProgramWriteError(f"connection error: {err}") from err

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    LOGGER.debug("Program write response %s: %s", response.status_code, payload)

    status = payload.get("status", {}) if isinstance(payload, dict) else {}
    code = status.get("code")
    if code == 0:
        return payload
    if code == _CODE_EXPIRED_TOKEN:
        raise ExpiredTokenError("ecobee access token expired; token refresh required")
    if code in _CODES_INVALID_TOKEN:
        raise InvalidTokenError("ecobee tokens invalid; re-authentication required")
    message = status.get("message") or response.text or "no response body"
    if code is None:
        raise ProgramWriteError(f"HTTP {response.status_code}: {message}")
    raise ProgramWriteError(f"{code}: {message}")


def _failed(name: str, error: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="program_write_failed",
        translation_placeholders={"name": name, "error": error},
    )


async def async_set_program(
    hass: HomeAssistant, data: EcobeeData, index: int, build: ProgramBuilder
) -> dict[str, Any]:
    """Refresh, build the program from fresh data, POST it, refresh again.

    ``build`` receives the freshly fetched thermostat dict for ``index`` and returns the
    complete program (it should validate and may raise ServiceValidationError). The
    whole sequence runs under the thermostat's lock. Returns the program written.
    Raises HomeAssistantError carrying ecobee's status message on failure.
    """
    identifier = data.ecobee.get_thermostat(index)["identifier"]
    async with _lock_for(data, identifier):
        await data.update(no_throttle=True)
        thermostat = data.ecobee.get_thermostat(index)
        if thermostat["identifier"] != identifier:
            raise _failed(identifier, "thermostat list changed during the write")
        name = thermostat.get("name", identifier)
        program = build(thermostat)
        if "currentClimateRef" in program:
            program = {k: v for k, v in program.items() if k != "currentClimateRef"}

        LOGGER.debug("Writing program to ecobee %s (%s)", name, identifier)
        for attempt in range(2):
            try:
                await hass.async_add_executor_job(
                    _post_program, data.ecobee, identifier, program
                )
                break
            except ExpiredTokenError as err:
                # Refresh through EcobeeData so the rotated refresh token is stored in
                # the config entry, then retry once.
                if attempt == 0 and await data.refresh():
                    continue
                raise _failed(name, str(err)) from err
            except EcobeeError as err:  # InvalidTokenError, ProgramWriteError
                raise _failed(name, str(err)) from err

        # Pull the server's view (new climateRefs, normalised fields) so entities refresh.
        await data.update(no_throttle=True)
        return program
