"""
trading robot - buy BTC

save this file in the same folder as 'pytrader.py' as 'buy.py'
to load this strategy execute 'pytrader.py' with the --strategy option:

$ ./pytrader.py --strategy buy.py

You can make changes to this file whilst 'pytrader.py' is running.
Dynamically reload() buy pressing the 'l' key in the pytrader terminal
Other keypresses are defined in the 'slot_keypress' function below.

Activate this strategy's BUY functionality by switching 'simulate' to False
Test first before enabling the BUY function!

Note: the pytrader.py application swallows most Python exceptions
and outputs them to the status window and pytrader.log (in app folder).
This complicates tracing of runtime errors somewhat, but
to keep an eye on such it is recommended that the developer runs
an additional terminal with 'tail -f ./pytrader.log' to see
continuous logfile output.

coded by tarzan (c) April 2013, modified by caktux
copying & distribution allowed - attribution appreciated
"""

import strategy
import simplejson as json

# Load buy.conf
conf = {}
try:
    conf = json.load(open("buy.conf"))
except:
    print "File buy.conf not found, saving default config file."

# Set defaults
conf.setdefault('buy_simulate', True)
conf.setdefault('buy_level', 1)
conf.setdefault('buy_volume', 1)
conf.setdefault('buy_alert', 100)

# Simulate
simulate = bool(conf['buy_simulate'])

# Live or simulation notice
simulate_or_live = ('SIMULATION - ' if simulate else 'LIVE - ')

# variables
global bidbuf, askbuf, buy_amount  # comparators to avoid redundant bid/ask output
bidbuf = 0
askbuf = 0
buy_level = float(conf['buy_level'])  # price at which you want to buy BTC
threshold = float(conf['buy_alert'])  # alert price distance from buy_level
buy_alert = float(buy_level + threshold)  # alert level for user info
volume = float(conf['buy_volume'])  # user specified fiat amount as volume, set to 0 to use full fiat balance

class Strategy(strategy.Strategy):

    def __init__(self, api):
        strategy.Strategy.__init__(self, api)
        self.signal_debug.connect(api.signal_debug)
        api.signal_keypress.connect(self.slot_keypress)
        # api.signal_strategy_unload.connect(self.slot_before_unload)
        api.signal_ticker.connect(self.slot_tick)
        api.signal_depth.connect(self.slot_depth)
        api.signal_trade.connect(self.slot_trade)
        api.signal_userorder.connect(self.slot_userorder)
        api.orderbook.signal_owns_changed.connect(self.slot_owns_changed)
        api.signal_wallet.connect(self.slot_wallet_changed)
        self.api = api
        self.name = "%s.%s" % (__name__, self.__class__.__name__)
        self.debug("[s]%s%s loaded" % (simulate_or_live, self.name))
        self.debug("[s]Press 'b' to see Buy objective")
        # get existing orders for later decision making
        self.existingorders = []
        for order in self.api.orderbook.owns:
            self.existingorders.append(order.oid)

    def __del__(self):
        try:
            self.debug("[s]%s unloaded" % self.name)
        except Exception, e:
            self.debug("[s]%s exception: %s" % (self.name, e))

    # def slot_before_unload(self, _sender, _data):
    #     self.debug("[s]%s before unload" % self.name)

    def slot_keypress(self, api, (key)):
        # some custom keypresses are caught here:
        # 'b' outputs the strategy objective to the status window & log
        # 'o' displays own orders
        # self.debug("someone pressed the %s key" % chr(key))
        if key == ord('b'):
            self.debug("[s]%sObjective: BUY Bitcoins for %f %s when price reaches %f" % (simulate_or_live, buy_amount, self.api.curr_quote, buy_level))
            # self.debug("[s]Python wallet object: %s" % str(self.api.wallet))
            # check if the user changed volume
            # also ensure the buy_amount does not exceed wallet balance
            # if it does, set buy_amount to wallet full fiat balance
            walletbalance = api.quote2float(self.api.wallet[self.api.curr_quote])
            if volume == 0:
                buy_amount = walletbalance
            else:
                buy_amount = volume
            # if volume != 0 and volume <= walletbalance:
            #     if buy_amount != volume:
            #         buy_amount = volume
            #     else:
            #         buy_amount = walletbalance
            # else:
            #     buy_amount = walletbalance
            self.debug("[s] %sstrategy will spend %f of %f %s on next BUY" % (simulate_or_live, buy_amount, walletbalance, str(self.api.curr_quote)))

    def slot_tick(self, api, (bid, ask)):
        # if api receives a no-change tick update, don't output anything
        if bid != bidbuf or ask != askbuf:
            seen = 0   # var seen is a flag for default output below (=0)
            self.ask = api.quote2float(ask)
            if self.ask > buy_level and self.ask < buy_alert:
                self.debug("[s] !!! buy ALERT @ %s; ask currently at %s" % (str(buy_alert), str(self.ask)))
                self.debug("[s] !!! BUY for %f %s will trigger @ %f" % (buy_amount, str(self.api.curr_quote), buy_level))
                seen = 1
            elif self.ask <= buy_level:
                # this is the condition to action api.buy()
                if simulate == False:
                    self.api.buy(self.ask, buy_amount)
                self.debug("[s] >>> %sBUY BTC @ %s; ask currently at %s" % (simulate_or_live, str(buy_level), str(self.ask)))
                seen = 1
            if seen == 0:
                # no conditions met above, so give the user default info
                self.debug("Buy level @ %s (alert: %s); ask @ %s" % (buy_level, buy_alert, self.ask))
            # is the updated tick different from previous?
            if bid != bidbuf:
                bidbuf = bid
            elif ask != askbuf:
                askbuf = ask

    def slot_depth(self, api, (typ, price, volume, total_volume)):
        pass

    def slot_trade(self, api, (date, price, volume, typ, own)):
        """a trade message has been received. Note that this might come
        before the orderbook.owns list has been updated, don't rely on the
        own orders and wallet already having been updated when this fires."""
        # trade messages include trades by other traders
        # if own == True then it is your own
        if str(own) == 'True':
            self.debug("own trade message received: date %s price %s volume %s typ %s own %s" % (date, price, volume, typ, own))

    def slot_userorder(self, api, (price, volume, typ, oid, status)):
        """this comes directly from the API and owns list might not yet be
        updated, if you need the new owns list then use slot_owns_changed"""
        # the coder assumes that if an order id is received via
        # this signal then it was not instantaneously actioned, so cancel
        # at once
        self.debug("userorder message received: price %f volume %s typ %s oid %s status %s" % (price, volume, typ, oid, status))
        # cancel by oid
        if status not in ['pending', 'executing', 'post-pending', 'removed'] and oid not in self.existingorders:
            if api.quote2float(price) == buy_level:
                self.api.cancel(oid)

    def slot_owns_changed(self, orderbook, _dummy):
        """this comes *after* userorder and orderbook.owns is updated already"""
        pass

    def slot_wallet_changed(self, api, _dummy):
        """this comes after the wallet has been updated"""
        # buy_amount can either be manually specified or
        # this strategy will query the user wallet and buy BTC using the
        # FULL fiat (e.g. USD) balance
        # changes to wallet balance should be picked up here - press 'w'
        # to confirm. Else, restart pytrader to reload wallet
        # also ensure the buy_amount does not exceed wallet balance
        # if it does, set buy_amount to wallet full fiat balance
        walletbalance = api.quote2float(self.api.wallet[self.api.curr_quote])
        if volume != 0 and volume <= walletbalance:
            buy_amount = volume
        else:
            buy_amount = walletbalance
