import asyncio
import json
import os
import time
import threading
import statistics
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

from electrum.plugin import BasePlugin, hook
from electrum.logging import Logger
from electrum.simple_config import SimpleConfig
from electrum.util import to_string

# Price feeds
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

try:
    from cachetools import cached, TTLCache
    _CACHE = TTLCache(maxsize=1024, ttl=60)
except Exception:
    _CACHE = None
    def cached(cache=None):
        def deco(fn): return fn
        return deco

MSAT_PER_BTC = 1000 * 100_000_000  # 1e11

@dataclass
class StableChannelState:
    name: str
    channel_id: str               # Electrum channel id string (funding outpoint preferred)
    counterparty: str             # node pubkey hex
    stable_usd: float             # target USD for the Stable Receiver side
    native_msat: int              # msat excluded from stabilization
    is_receiver: bool             # True if this wallet is the SR
    our_balance_msat: int = 0
    their_balance_msat: int = 0
    risk_score: int = 0
    stable_receiver_usd: float = 0.0
    timestamp: int = 0
    payment_made: bool = False

class StableChannelsPlugin(BasePlugin, Logger):
    _TASK_PERIOD_SEC = 300  # 5 minutes

    def __init__(self, *args):
        BasePlugin.__init__(self, *args)
        Logger.__init__(self)
        self.wallet = None
        self._loop_task = None
        self._stop_event = threading.Event()
        self._states: Dict[str, StableChannelState] = {}
        self._log_dir: Optional[str] = None

        # simple multi-source price config
        self._sources = [
            ("bitstamp",      "https://www.bitstamp.net/api/v2/ticker/btc{cc}/", ("last",)),
            ("coingecko",     "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies={cc}", ("bitcoin", "{cc}")),
            ("coinbase",      "https://api.coinbase.com/v2/prices/spot?currency={C}", ("data","amount")),
            ("blockchain.info","https://blockchain.info/ticker", ("{C}","last")),
            ("bitfinex",       "https://api.bitfinex.com/v1/pubticker/btcusd", ("last_price",)),
        ]

    def fullname(self):
        return "Stable Channels"

    def description(self):
        return "Keep a Lightning channel’s USD value stable by periodic rebalancing."

    def is_available(self) -> bool:
        # Requires a Lightning-enabled wallet
        return True

    def start_plugin(self, wallet):
        # Called from cmdline/qt on wallet ready
        if self.wallet:
            return
        self.wallet = wallet
        # derive a directory under the wallet storage path
        storage_path = getattr(wallet.storage, 'path', None)
        base = os.path.dirname(storage_path) if storage_path else os.path.expanduser("~/.electrum")
        self._log_dir = os.path.join(base, "stablechannels")
        os.makedirs(self._log_dir, exist_ok=True)

        # Start background scheduler
        self._stop_event.clear()
        self._loop_task = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._loop_task.start()
        self.logger.info("stablechannels: started background loop")

    def stop(self):
        self._stop_event.set()
        self.logger.info("stablechannels: stopped")

    # ---------- Public commands (invoked via __init__.py) ----------
    def add_stable_channel(self, name: str, channel_id: str, counterparty: str,
                           stable_usd: float, native_sats: int, is_receiver: bool):
        st = StableChannelState(
            name=name,
            channel_id=channel_id,
            counterparty=counterparty,
            stable_usd=float(stable_usd),
            native_msat=int(native_sats) * 1000 if native_sats > 0 else 0,
            is_receiver=bool(is_receiver),
        )
        self._states[name] = st
        self.logger.info(f"stablechannels: added {st}")

    def remove_stable_channel(self, name: str):
        self._states.pop(name, None)

    def list_stable_channels(self) -> Dict[str, dict]:
        return {k: asdict(v) for k, v in self._states.items()}

    async def dev_tick(self, name: str) -> dict:
        st = self._states[name]
        return await self._check_and_rebalance(st)

    # ---------- Core loop ----------
    def _scheduler_loop(self):
        # simple loop; respects stop event
        while not self._stop_event.is_set():
            try:
                # drive checks sequentially to simplify wallet access
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                tasks = [self._check_and_rebalance(st) for st in list(self._states.values())]
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.close()
            except Exception as e:
                self.logger.exception(f"stablechannels: loop error: {e}")
            # sleep
            self._stop_event.wait(self._TASK_PERIOD_SEC)

    # ---------- Price feeds ----------
    def _requests_retry_session(self, retries=5, backoff=0.3, status=(500, 502, 504, 404)):
        s = requests.Session()
        r = Retry(total=retries, read=retries, connect=retries, backoff_factor=backoff, status_forcelist=status)
        a = HTTPAdapter(max_retries=r)
        s.mount('http://', a); s.mount('https://', a)
        return s

    @cached(cache=_CACHE)
    def _get_rates_msat_per_usd(self) -> Dict[str, int]:
        # Return msat per USD from multiple sources; for USD the BTCUSD is used.
        out: Dict[str, int] = {}
        for name, urlf, members in self._sources:
            try:
                url = urlf.format(cc='usd', C='USD')
                r = self._requests_retry_session().get(url, timeout=5)
                if r.status_code != 200:
                    continue
                j = r.json()
                for m in members:
                    m = m.format(cc='usd', C='USD')
                    j = j[m]
                price = float(j)  # USD per BTC
                msat_per_usd = int(MSAT_PER_BTC / price)
                out[name] = msat_per_usd
            except Exception:
                continue
        if not out:
            raise RuntimeError("No price sources available")
        return out

    def _median_msat_per_usd(self) -> Tuple[int, float]:
        rates = self._get_rates_msat_per_usd()
        vals = [v for v in rates.values()]
        med = statistics.median(vals)
        # also return human-friendly BTCUSD estimate
        est_price = MSAT_PER_BTC / med
        return int(round(med)), float(est_price)

    # ---------- Electrum LN helpers ----------
    def _resolve_channel(self, channel_id: str):
        """
        Attempt to find an Electrum Channel object given a user-supplied id.
        Prefer funding outpoint id (txid:idx) if available.
        """
        lnw = getattr(self.wallet, 'lnworker', None)
        if lnw is None:
            raise RuntimeError("Wallet has no LN worker")
        for chan_id, chan in lnw.channels.items():
            # chan.funding_outpoint.to_str() often looks like "<txid>:<idx>"
            fid = getattr(chan, 'funding_outpoint', None)
            fid_s = getattr(fid, 'to_str', lambda: None)()
            short_id = getattr(chan, 'short_id', None)
            if channel_id in (fid_s, short_id, chan_id):
                return chan
        raise RuntimeError(f"Channel '{channel_id}' not found")

    def _channel_balances_msat(self, chan) -> Tuple[int, int, int]:
        """
        Returns (capacity_msat, our_msat, their_msat)
        Uses Electrum channel methods; we avoid guessing private attrs.
        """
        cap = int(getattr(chan, "capacity_msat", lambda: 0)() or 0)
        our = int(getattr(chan, "available_to_spend", lambda: 0)() or 0) * 1000  # available_to_spend returns sats
        # Conservative: estimate counterparty by capacity - our - reserves (approx).
        # NOTE: Electrum’s API doesn’t expose a trivial 'their_msat'; this is a coarse proxy.
        their = max(cap - our, 0)
        return cap, our, their

    async def _keysend_or_fail(self, node_pubkey_hex: str, amount_msat: int) -> str:
        """
        Try to send a keysend (spontaneous) payment if supported.
        If not supported by this Electrum build, raise with a clear message.
        """
        lnw = self.wallet.lnworker
        # Some Electrum builds expose a method like 'pay_to_node'/'pay_to_node_keysend'; this is not guaranteed.
        func = getattr(lnw, 'pay_to_node_keysend', None) or getattr(lnw, 'pay_to_node', None)
        if not callable(func):
            raise RuntimeError("Electrum build does not support keysend; provide an invoice instead.")
        # amount_msat integer required
        pay_r = await func(node_pubkey_hex, amount_msat)
        return to_string(pay_r)

    # ---------- Core check/rebalance ----------
    async def _check_and_rebalance(self, st: StableChannelState) -> dict:
        try:
            chan = self._resolve_channel(st.channel_id)
        except Exception as e:
            self._log_state(st, extra={"error": str(e)})
            return {"ok": False, "error": str(e)}

        msat_per_usd, est_price = self._median_msat_per_usd()
        target_msat = int(round(st.stable_usd * msat_per_usd))

        cap_msat, our_msat, their_msat = self._channel_balances_msat(chan)
        st.our_balance_msat = our_msat
        st.their_balance_msat = their_msat
        st.timestamp = int(time.time())
        st.payment_made = False

        # compute SR (stable-receiver) USD based on SR-side “stable bucket”
        if st.is_receiver:
            sr_msat = max(our_msat - st.native_msat, 0)
        else:
            sr_msat = max(their_msat - st.native_msat, 0)

        st.stable_receiver_usd = round((sr_msat / msat_per_usd), 3)
        delta_usd = st.stable_usd - st.stable_receiver_usd

        # Small diff: ignore under 1 cent
        if abs(delta_usd) < 0.01:
            self._log_state(st, est_price=est_price)
            return {"ok": True, "action": "noop", "price": est_price}

        # Compute msat to move toward target (bounded by channel)
        # If SR has less than target → SR should receive (counterparty pays)
        # If SR has more than target → SR should pay (SR pays)
        want_msat = int(round(abs(delta_usd) * msat_per_usd))

        # Decide direction relative to this wallet
        should_this_wallet_pay = (st.is_receiver and delta_usd < 0) or ((not st.is_receiver) and delta_usd > 0)

        try:
            if should_this_wallet_pay:
                # This wallet pays counterparty
                await self._keysend_or_fail(st.counterparty, want_msat)
                st.payment_made = True
            else:
                # Expect counterparty to pay; give them ~30s before penalizing.
                await asyncio.sleep(30)
                # Re-read balances
                _, our2, their2 = self._channel_balances_msat(chan)
                if st.is_receiver:
                    sr_msat2 = max(our2 - st.native_msat, 0)
                else:
                    sr_msat2 = max(their2 - st.native_msat, 0)
                sr_usd2 = round((sr_msat2 / msat_per_usd), 3)
                if abs(st.stable_usd - sr_usd2) < 0.01:
                    st.payment_made = True
                else:
                    st.risk_score += 1
        except Exception as e:
            # Any payment error: raise risk and log
            st.risk_score += 1
            self._log_state(st, est_price=est_price, extra={"error": str(e)})
            return {"ok": False, "error": str(e)}

        self._log_state(st, est_price=est_price)
        return {"ok": True, "payment_made": st.payment_made, "price": est_price}

    # ---------- Logging & coin movement ----------
    def _log_state(self, st: StableChannelState, est_price: Optional[float] = None, extra: Optional[dict] = None):
        line = {
            "time_utc": time.strftime("%H:%M %d %b %Y", time.gmtime()),
            "estimated_btcusd": est_price,
            "expected_usd": st.stable_usd,
            "sr_usd": st.stable_receiver_usd,
            "payment_made": st.payment_made,
            "risk_score": st.risk_score,
            "name": st.name,
        }
        if extra:
            line.update(extra)
        path = os.path.join(self._log_dir or ".", f"{'stablelog1' if st.is_receiver else 'stablelog2'}.json")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(line) + ",\n")
        except Exception:
            pass
        self.logger.debug(f"stablechannels: {line}")

    @hook
    def on_event_channel_db(self, *args, **kwargs):
        # Placeholder: Electrum doesn’t surface CLN’s 'coin_movement' event.
        # If you want on-the-fly target changes based on outbound invoices/payments,
        # use CLI to adjust `stable_usd` periodically (or add a wallet hook here).
        return
