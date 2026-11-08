"""hyperliquid-desk engine: market state, metrics and snapshots on Hyperliquid public data.

Importing this package starts nothing: no thread, no socket, no file write.
The live collector starts only through hl_desk.runtime.start(), which the
/hl start command and the dashboard's Start button call.
"""

PLUGIN_NAME = "hyperliquid-desk"
PLUGIN_VERSION = "1.0.0"
