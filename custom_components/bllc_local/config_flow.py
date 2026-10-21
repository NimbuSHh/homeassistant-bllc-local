"""Configure by LAN address, using the device's existing local passcode."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import CONF_DEVICE_ID, CONF_MAC, CONF_PASSCODE, DOMAIN
from .protocol import AuthenticationError, BllcClient, BllcError, IdentityError


def schema(defaults: dict[str, Any] | None = None, *, include_name: bool = True) -> vol.Schema:
    defaults = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
    }
    if include_name:
        fields[vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, ""))] = str
    fields[vol.Optional(CONF_PASSCODE)] = TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD)
    )
    return vol.Schema(fields)


class BllcConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, user_input: dict[str, Any], expected_id: str = "") -> BllcClient:
        client = BllcClient(
            user_input[CONF_HOST], expected_id, user_input.get(CONF_PASSCODE, "")
        )
        try:
            await client.read_status()
        finally:
            await client.async_close()
        return client

    @staticmethod
    def _data(client: BllcClient) -> dict[str, str]:
        assert client.identity is not None
        return {
            CONF_HOST: client.host,
            CONF_DEVICE_ID: client.device_id,
            CONF_MAC: client.identity.mac,
            CONF_PASSCODE: client.passcode,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                client = await self._validate(user_input)
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except IdentityError:
                errors["base"] = "unsupported_device"
            except BllcError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(client.device_id)
                self._abort_if_unique_id_configured()
                name = user_input.get(CONF_NAME, "").strip() or f"BLLC {client.host.rsplit('.', 1)[-1]}"
                return self.async_create_entry(title=name, data=self._data(client))
        return self.async_show_form(step_id="user", data_schema=schema(user_input), errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            supplied = dict(user_input)
            if not supplied.get(CONF_PASSCODE):
                supplied[CONF_PASSCODE] = entry.data[CONF_PASSCODE]
            try:
                client = await self._validate(supplied, entry.unique_id or "")
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except IdentityError:
                errors["base"] = "wrong_device"
            except BllcError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=self._data(client))
        defaults = user_input or {CONF_HOST: entry.data[CONF_HOST]}
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema(defaults, include_name=False), errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            supplied = {CONF_HOST: entry.data[CONF_HOST], **user_input}
            try:
                client = await self._validate(supplied, entry.unique_id or "")
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except IdentityError:
                errors["base"] = "wrong_device"
            except BllcError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=self._data(client))
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PASSCODE): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )}),
            errors=errors,
        )
