from typing import TYPE_CHECKING
from electrum.plugin import hook
from .stablechannels import StableChannelsPlugin

if TYPE_CHECKING:
    from electrum.daemon import Daemon
    from electrum.wallet import Abstract_Wallet

class Plugin(StableChannelsPlugin):
    def __init__(self, *args):
        super().__init__(*args)

    @hook
    def daemon_wallet_loaded(self, daemon: 'Daemon', wallet: 'Abstract_Wallet'):
        # Start background scheduler once a wallet is ready
        self.start_plugin(wallet)
