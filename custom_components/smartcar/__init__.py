import asyncio
from functools import partial
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.components import cloud, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import util
from .auth import AbstractAuth
from .auth_impl import AccessTokenAuthImpl, AsyncConfigEntryAuth
from .const import API_ENDPOINTS, CONF_CLOUDHOOK, DOMAIN, PLATFORMS, Scope
from .coordinator import SmartcarVehicleCoordinator
from .errors import (
    EmptyVehicleListError,
    InvalidAuthError,
    MissingVINError,
    UnsupportedUserConfigurationError,
)
from .services import async_setup_services
from .types import SmartcarData
from .util import api_version_for_client_id
from .webhooks import handle_webhook, webhook_url_from_id

_LOGGER = logging.getLogger(__name__)


async def async_setup(  # noqa: RUF029
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001
) -> bool:
    """Set up Smartcar services.

    Returns:
        If the setup was successful.
    """
    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smartcar from a config entry.

    Returns:
        If the setup was successful.

    Raises:
        ConfigEntryError: For overlapping VIN in config entries.
    """
    implementation = await async_get_config_entry_implementation(hass, entry)
    websession = async_get_clientsession(hass)
    oauth_session = OAuth2Session(hass, entry, implementation)
    auth = AsyncConfigEntryAuth(
        websession,
        implementation,
        oauth_session,
        API_ENDPOINTS,
        user_id=entry.data.get("user_id"),
    )
    coordinators: dict[str, SmartcarVehicleCoordinator] = {}
    meta_coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_meta", config_entry=entry
    )
    meta_coordinator.async_set_updated_data({})
    entry.runtime_data = SmartcarData(
        auth=auth,
        coordinators=coordinators,
        meta_coordinator=meta_coordinator,
    )
    device_registry = dr.async_get(hass)
    other_vins = vehicle_vins_in_use(hass, entry)
    pending_vin_vehicle_ids: list[str] = []

    for vehicle_id, details in entry.data.get("vehicles", {}).items():
        vin = details["vin"]
        make = details.get("make")
        model = details.get("model")
        year = details.get("year")

        if not vin:
            _LOGGER.info(
                "VIN not yet available for vehicle %s; skipping device setup",
                vehicle_id,
            )
            pending_vin_vehicle_ids.append(vehicle_id)
            continue

        if vin in other_vins:
            msg = f"Cannot setup multiple config entries with VIN {vin}"
            raise ConfigEntryError(msg)

        # register device
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, vin)},
            manufacturer=make,
            model=f"{model} ({year})" if model and year else model,
            name=f"{make} {model}" if make and model else f"Smartcar {vin[-4:]}",
        )
        _LOGGER.info("Registered device for VIN: %s", vin)

        # create and store coordinator
        coordinator = SmartcarVehicleCoordinator(hass, auth, vehicle_id, vin, entry)
        coordinators[vin] = coordinator
        _LOGGER.debug("Coordinator created and initial data fetched for VIN: %s", vin)

    # setup platforms before doing first refresh. this gets the entity registry
    # populated with the desired entities & allows the coordinator to determine
    # what to fetch on the first refresh. (some entities, for instance, are
    # disabled by default.)
    _LOGGER.debug("Forwarding setup to platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if CONF_WEBHOOK_ID in entry.data:
        _LOGGER.info(
            "Registering webhook at url: %s",
            (await webhook_url_from_id(hass, entry.data[CONF_WEBHOOK_ID]))[0],
        )
        webhook.async_register(
            hass,
            DOMAIN,
            entry.title,
            entry.data[CONF_WEBHOOK_ID],
            partial(handle_webhook, config_entry=entry),
        )
    else:
        _LOGGER.debug("Webhooks are not enabled")

    if auth.version == "v2":
        async_create_issue(
            hass,
            DOMAIN,
            f"legacy_client_id_{entry.entry_id}",
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="legacy_client_id",
            translation_placeholders={
                "title": entry.title,
                "docs_url": "https://github.com/wbyoung/smartcar#upgrading-from-legacy-v2-api-to-v3",
            },
        )

    if pending_vin_vehicle_ids:
        async_create_issue(
            hass,
            DOMAIN,
            f"vin_pending_{entry.entry_id}",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="vin_pending",
            translation_placeholders={
                "title": entry.title,
                "docs_url": "https://github.com/wbyoung/smartcar#webhooks",
            },
        )
        entry.async_create_background_task(
            hass,
            _async_poll_pending_vins(hass, entry, auth, pending_vin_vehicle_ids),
            name=f"{DOMAIN}_pending_vin_poll",
        )
    else:
        async_delete_issue(hass, DOMAIN, f"vin_pending_{entry.entry_id}")

    await asyncio.gather(
        *[async_do_first_refresh(coordinator) for coordinator in coordinators.values()]
    )

    # log stored scopes once on successful setup
    _LOGGER.info(
        "Using token with scopes: %s", entry.data.get("token", {}).get("scopes")
    )

    entry.async_on_unload(
        entry.add_update_listener(
            partial(async_update_listener, initial_data=entry.data)
        )
    )

    return True


async def async_do_first_refresh(coordinator: SmartcarVehicleCoordinator) -> None:
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug(
        "Coordinator created and initial data fetched for VIN: %s", coordinator.vin
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Returns:
        If the unload was successful.
    """
    _LOGGER.info("Unloading Smartcar entry %s", entry.entry_id)
    if CONF_WEBHOOK_ID in entry.data:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    return bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cleanup when entry is removed."""
    if CONF_WEBHOOK_ID in entry.data and (
        cloud.async_active_subscription(hass) or entry.data.get(CONF_CLOUDHOOK, False)
    ):
        try:
            _LOGGER.debug(
                "Removing Smartcar cloudhook (%s)", entry.data[CONF_WEBHOOK_ID]
            )
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
        except cloud.CloudNotAvailable:
            pass


async def async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    initial_data: dict[str, Any],
) -> None:
    """Handle options update."""

    entry_data = {k: v for k, v in entry.data.items() if k != "token"}
    initial_data = {k: v for k, v in initial_data.items() if k != "token"}

    if entry_data != initial_data:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    # prevent rollbacks
    if config_entry.version > 2:
        return False

    if config_entry.version == 1:
        old_data = config_entry.data
        implementation = await async_get_config_entry_implementation(hass, config_entry)
        session = async_get_clientsession(hass)
        token = old_data[CONF_TOKEN]
        access_token = token[CONF_ACCESS_TOKEN]
        scopes = token["scope"].split(" ")
        auth = AccessTokenAuthImpl(
            session,
            access_token,
            API_ENDPOINTS,
            version=api_version_for_client_id(implementation.client_id),
        )

        # copy old data & remove old keys
        new_data = {**old_data}
        new_data[CONF_TOKEN] = {**old_data[CONF_TOKEN]}
        new_data[CONF_TOKEN].pop("scope", None)

        await populate_entry_data(new_data, auth, scopes)

        old_vehicle_ids = set(old_data.get("vehicles", {}).keys())
        new_vehicle_ids = set(new_data["vehicles"].keys())

        # limit the vehicles in the config entry to whatever was in the previous
        # entry even if the API is returning new items.
        if old_vehicle_ids:
            for vehicle_id in new_vehicle_ids:
                if vehicle_id not in old_vehicle_ids:
                    new_data["vehicles"].pop(vehicle_id, None)

        # ensure all previously accessible vehicles are still accessible.
        inaccessible_vehicle_ids = [
            vehicle_id
            for vehicle_id in old_vehicle_ids
            if vehicle_id not in new_vehicle_ids
        ]

        if inaccessible_vehicle_ids:
            _LOGGER.error(
                "Vehicle(s) are no longer accessible via the API: %s",
                inaccessible_vehicle_ids,
            )
            return False

        hass.config_entries.async_update_entry(
            config_entry,
            unique_id=util.unique_id_from_entry_data(new_data),
            data=new_data,
            version=2,
            minor_version=0,
        )

    _LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


def vehicle_vins_in_use(
    hass: HomeAssistant, config_entry: ConfigEntry = None
) -> set[str]:
    return {
        vin
        for other_entry in hass.config_entries.async_entries(DOMAIN)
        for vehicle in other_entry.data.get("vehicles", {}).values()
        if (vin := vehicle.get("vin"))
        and (not config_entry or other_entry.unique_id != config_entry.unique_id)
    }


async def populate_entry_data(
    data: dict,
    auth: AbstractAuth,
    scopes: list[Scope],
) -> None:
    """Populate config entry data during initial creation or migration."""
    _inject_requested_scopes_into_entry_data(data, scopes)

    await _store_all_vehicles(data, auth)


def _inject_requested_scopes_into_entry_data(data: dict, scopes: list[Scope]) -> None:
    """Inject selected scopes into stored token data."""
    data.setdefault("token", {})["scopes"] = scopes


async def _store_all_vehicles(
    data: dict,
    auth: AbstractAuth,
) -> None:
    """Fetch and store data for all vehicles in config entry data.

    Raises:
        EmptyVehicleListError: If no vehicles are found.
        UnsupportedUserConfigurationError: If there is not exactly 1 user.
        InvalidAuthError: If the request cannot be authorized.
        ClientResponseError: If there is a request error.
    """

    _LOGGER.info("Fetching Smartcar vehicle IDs...")

    data["vehicles"] = {}

    try:
        if auth.version == "v2":
            vehicle_list_resp = await auth.request_v2("get", "vehicles")
            vehicle_list_resp.raise_for_status()
            vehicle_list_data = await vehicle_list_resp.json()
            vehicle_ids = vehicle_list_data.get("vehicles", [])
        else:
            assert auth.version == "v3"
            connections_list_resp = await auth.request_v3("get", "connections")
            connections_list_resp.raise_for_status()
            connections_list_data = await connections_list_resp.json()
            vehicle_ids = [
                vehicle_id
                for connection in connections_list_data.get("data", [])
                if (
                    vehicle_id := connection.get("relationships", {})
                    .get("vehicle", {})
                    .get("data", {})
                    .get("id", None)
                )
            ]
            user_ids = {
                user_id
                for connection in connections_list_data.get("data", [])
                if (
                    user_id := connection.get("relationships", {})
                    .get("user", {})
                    .get("data", {})
                    .get("id", None)
                )
            }

            if len(user_ids) != 1:
                raise UnsupportedUserConfigurationError

            auth.user_id = data["user_id"] = next(iter(user_ids))

    except ClientResponseError as err:
        if err.status == HTTPStatus.UNAUTHORIZED:
            msg = f"Auth error fetching vehicle list: {err.status}"
            raise InvalidAuthError(msg) from err
        raise

    _LOGGER.info("Found %s vehicle IDs", len(vehicle_ids))

    if not vehicle_ids:
        raise EmptyVehicleListError

    await asyncio.gather(
        *[_store_vehicle_details(data, auth, vid) for vid in vehicle_ids]
    )


_VIN_SIGNAL_CODE = "vehicleidentification-vin"
_VIN_FETCH_MAX_RETRIES = 3
_VIN_FETCH_BASE_DELAY = 2.0
_VIN_POLL_INTERVAL = 60.0


async def _async_poll_pending_vins(
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth: AbstractAuth,
    vehicle_ids: list[str],
) -> None:
    """Poll for VINs of vehicles whose signals are not yet ingested.

    Updates the config entry as soon as a VIN becomes available; the
    entry update listener then reloads the entry so the device and
    entities get created (and polling restarts for any remaining
    vehicles).
    """
    while True:
        await asyncio.sleep(_VIN_POLL_INTERVAL)

        for vehicle_id in vehicle_ids:
            try:
                vin, vehicle_info = await _fetch_vin_and_info_v3(
                    auth, vehicle_id, max_retries=0
                )
            except ClientResponseError as err:
                _LOGGER.debug(
                    "Error %s polling VIN for vehicle %s", err.status, vehicle_id
                )
                continue

            if not vin:
                continue

            _LOGGER.info("VIN now available for vehicle %s", vehicle_id)
            vehicles = {**entry.data["vehicles"]}
            vehicles[vehicle_id] = {
                **vehicles[vehicle_id],
                "vin": vin,
                "make": vehicle_info.get("make"),
                "model": vehicle_info.get("model"),
                "year": str(vehicle_info.get("year")),
            }
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "vehicles": vehicles}
            )
            return  # the entry update listener triggers a reload


async def _fetch_vin_and_info_v3(
    auth: AbstractAuth,
    vehicle_id: str,
    *,
    max_retries: int = _VIN_FETCH_MAX_RETRIES,
) -> tuple[str | None, dict]:
    """Fetch the VIN and vehicle attributes from the v3 signals list.

    Signal values are ingested asynchronously after a connection is
    created, so the VIN may not be available immediately; retry with
    backoff before giving up.

    Returns:
        A tuple of the VIN (or None) and the vehicle attributes.

    Raises:
        AssertionError: Should never be raised; satisfies the type checker.
    """
    for attempt in range(max_retries + 1):
        signals_resp = await auth.request_v3("get", f"vehicles/{vehicle_id}/signals")
        if not signals_resp.ok:
            _LOGGER.error(
                "Error %s fetching signals for vehicle %s: %s",
                signals_resp.status,
                vehicle_id,
                await signals_resp.text(),
            )
        signals_resp.raise_for_status()
        signals_data = await signals_resp.json()
        vehicle_info = (
            signals_data.get("included", {}).get("vehicle", {}).get("attributes", {})
        )

        vin = next(
            (
                signal.get("attributes", {}).get("body", {}).get("value")
                for signal in signals_data.get("data", [])
                if signal.get("attributes", {}).get("code") == _VIN_SIGNAL_CODE
            ),
            None,
        )

        if vin or attempt == max_retries:
            return vin, vehicle_info

        delay = _VIN_FETCH_BASE_DELAY * 2**attempt
        _LOGGER.debug(
            "VIN signal not yet ingested for vehicle %s; retrying in %.1fs",
            vehicle_id,
            delay,
        )
        await asyncio.sleep(delay)

    # unreachable — the loop always returns — but satisfies the type checker
    raise AssertionError  # pragma: no cover


async def _store_vehicle_details(
    data: dict,
    auth: AbstractAuth,
    vehicle_id: str,
) -> None:
    """Fetch and store data for a single vehicle.

    For v3, the VIN may not be ingested yet (e.g. before the vehicle is
    subscribed to a webhook); the vehicle is stored without a VIN and
    the setup of the config entry will fetch it in the background.

    Raises:
        MissingVINError: If the VIN is not available (v2 only).
        InvalidAuthError: If the request cannot be authorized.
        ClientResponseError: If there is a request error.
    """

    try:
        _LOGGER.debug("Fetching VIN for vehicle ID: %s", vehicle_id)
        if auth.version == "v2":
            vin_resp = await auth.request_v2("get", f"vehicles/{vehicle_id}/vin")
            vin_resp.raise_for_status()
            vin_data = await vin_resp.json()
            vin = vin_data.get("vin")
        else:
            assert auth.version == "v3"
            vin, vehicle_info = await _fetch_vin_and_info_v3(auth, vehicle_id)

        if not vin:
            if auth.version == "v2":
                msg = f"No VIN for vehicle {vehicle_id}"
                raise MissingVINError(msg)
            vin = None
            _LOGGER.warning(
                "No VIN ingested yet for vehicle %s; it will be fetched in "
                "the background once available (make sure the vehicle is "
                "subscribed to a webhook in the Smartcar dashboard)",
                vehicle_id,
            )

        data["vehicles"][vehicle_id] = {
            "vin": vin,
        }

        if auth.version == "v2":
            _LOGGER.debug("Fetching attributes for vehicle ID: %s", vehicle_id)
            attr_resp = await auth.request_v2("get", f"vehicles/{vehicle_id}")
            attr_resp.raise_for_status()
            vehicle_info = await attr_resp.json()

        make = vehicle_info.get("make")
        model = vehicle_info.get("model")
        year = str(vehicle_info.get("year"))

        data["vehicles"][vehicle_id].update(
            {
                "make": make,
                "model": model,
                "year": year,
            }
        )
    except ClientResponseError as err:
        if err.status == HTTPStatus.UNAUTHORIZED:
            msg = f"Auth error [{err.status}] during vehicle setup"
            raise InvalidAuthError(msg) from err
        raise
