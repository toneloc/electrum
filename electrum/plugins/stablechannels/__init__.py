from typing import TYPE_CHECKING
from electrum.commands import plugin_command

if TYPE_CHECKING:
    from .stablechannels import StableChannelsPlugin
    from electrum.commands import Commands

PLUGIN_NAME = "stablechannels"

# Default primary price relay (Bitstamp OK; can be overridden in config)

@plugin_command('', plugin_name=PLUGIN_NAME)
async def sc_add(
    self: 'Commands',
    name: str,
    channel_id: str,
    counterparty: str,
    stable_usd: float,
    native_sats: int = 0,
    is_receiver: bool = True,
    plugin: 'StableChannelsPlugin' = None
) -> str:
    """
    Register a Stable Channel.
    arg:str:name: local nickname for this SC
    arg:str:channel_id: funding outpoint or short_id known to Electrum
    arg:str:counterparty: node pubkey (hex) of your peer
    arg:float:stable_usd: target stable USD value to maintain for the receiver side
    arg:int:native_sats: sats excluded from stabilization (optional)
    arg:bool:is_receiver: True if this wallet is the Stable Receiver
    """
    plugin.add_stable_channel(name=name,
                              channel_id=channel_id,
                              counterparty=counterparty,
                              stable_usd=stable_usd,
                              native_sats=native_sats,
                              is_receiver=is_receiver)
    return f"stablechannels: added '{name}'"

@plugin_command('', plugin_name=PLUGIN_NAME)
async def sc_list(self: "Commands", plugin: "StableChannelsPlugin" = None) -> dict:
    """List configured Stable Channels."""
    return plugin.list_stable_channels()

@plugin_command('', plugin_name=PLUGIN_NAME)
async def sc_remove(self: "Commands", name: str, plugin: "StableChannelsPlugin" = None) -> str:
    """
    Remove a configured Stable Channel.
    arg:str:name: SC nickname
    """
    plugin.remove_stable_channel(name)
    return f"stablechannels: removed '{name}'"

@plugin_command('', plugin_name=PLUGIN_NAME)
async def sc_tick(self: "Commands", name: str, plugin: "StableChannelsPlugin" = None) -> dict:
    """
    Force a one-shot check/rebalance.
    arg:str:name: SC nickname
    """
    return await plugin.dev_tick(name)