from .stablechannels import StableChannelsPlugin
from electrum.plugin import hook
from electrum.i18n import _
from electrum.gui.qt.util import WindowModalDialog, Buttons, OkButton, CloseButton
from PyQt6.QtWidgets import QLabel, QVBoxLayout

class Plugin(StableChannelsPlugin):
    def __init__(self, *args):
        super().__init__(*args)

    @hook
    def load_wallet(self, wallet, window):
        # Simple “about” dialog entry under Tools → Plugins → Stable Channels → “About”
        self.start_plugin(wallet)
