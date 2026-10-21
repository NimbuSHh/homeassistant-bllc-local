"""Shared polling and confirmed control results for one ventilator."""

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_DEVICE_ID, CONF_PASSCODE, DOMAIN, UPDATE_INTERVAL_SECONDS
from .protocol import AuthenticationError, BllcClient, BllcError, Status

_LOGGER = logging.getLogger(__name__)


class BllcCoordinator(DataUpdateCoordinator[Status]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
            always_update=False,
        )
        self.client = BllcClient(
            entry.data[CONF_HOST],
            entry.data[CONF_DEVICE_ID],
            entry.data[CONF_PASSCODE],
        )

    async def _async_update_data(self) -> Status:
        try:
            return await self.client.read_status()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed("Local device authentication failed") from err
        except BllcError as err:
            raise UpdateFailed(str(err)) from err

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.async_close()

    async def async_set_mode(self, mode: str) -> None:
        try:
            status = await self.client.set_mode(mode)
        except BllcError as err:
            # No optimistic state: a failed or unconfirmed command cannot make
            # the dashboard display an unverified speed or power state.
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="control_failed",
            ) from err
        self.async_set_updated_data(status)
