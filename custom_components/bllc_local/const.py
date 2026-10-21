"""Constants for the locally verified BLLC BE ventilator."""

DOMAIN = "bllc_local"
CONF_PASSCODE = "passcode"
CONF_DEVICE_ID = "device_id"
CONF_MAC = "mac"
PRODUCT_KEY = "a2c216633edd4c6d9d2934aeb441508c"
UDP_PORT = 12414
TCP_PORT = 12416
UPDATE_INTERVAL_SECONDS = 15
MODE_OFF = "5"
MODE_INTERMITTENT = "3"
# The original app uses raw enum indices 0=high, 1=low, 2=intermittent,
# 4=off for this product. Our codec exposes the product's one-based values.
MANUAL_MODES = ("2", "1")  # Low, high, in speed order.
OPERATING_MODES = (*MANUAL_MODES, MODE_INTERMITTENT)
PRESET_INTERMITTENT = "intermittent"
DEFAULT_ON_MODE = "2"
