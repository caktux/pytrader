"""
trading robot - sell BTC

save this file in the same folder as 'goxtool.py' as 'sell.py'
to load this strategy execute 'goxtool.py' with the --strategy option:

$ ./goxtool.py --strategy sell.py

You can make changes to this file whilst 'goxtool.py' is running.
Dynamically reload() buy pressing the 'l' key in the goxtool terminal
Other keypresses are defined in the 'slot_keypress' function below.

Activate this strategy's SELL functionality by switching 'simulate' to False
Test first before enabling the SELL function!

Note: the goxtool.py application swallows most Python exceptions
and outputs them to the status window and goxtool.log (in app folder).
This complicates tracing of runtime errors somewhat, but
to keep an eye on such it is recommended that the developer runs
an additional terminal with 'tail -f ./goxtool.log' to see
continuous logfile output.

coded by tarzan (c) April 2013, modified by caktux
copying & distribution allowed - attribution appreciated
"""

import goxapi
import simplejson as json

# Load user.conf
conf = json.load(open("user.conf"))

# Set defaults
conf.setdefault('sell_simulate', True)
conf.setdefault('sell_level', 10000000)
conf.setdefault('sell_volume', 0.1)
conf.setdefault('sell_alert', 100000)

# Simulate
simulate = int(conf['sell_simulate'])

# Live or simulation notice
simulate_or_live = ('SIMULATION - ' if simulate else 'LIVE - ')

# variables
global bidbuf, askbuf # comparators to avoid redundant bid/ask output
bidbuf = 0
askbuf = 0
sell_level = float(conf['sell_level']) # price at which you want to sell BTC
threshold = float(conf['sell_alert']) # alert price distance from sell_level
sell_alert = float(sell_level - threshold) # alert level for user info
volume = float(conf['sell_volume']) # user specified BTC volume, set 0 to sell all BTC

class Strategy(goxapi.BaseObject):
    # pylint: disable=C0111,W0613,R0201

    def __init__(self, gox):
        goxapi.BaseObject.__init__(self)
        self.signal_debug.connect(gox.signal_debug)
        gox.signal_keypress.connect(self.slot_keypress)
        # gox.signal_strategy_unload.connect(self.slot_before_unload)
        gox.signal_ticker.connect(self.slot_tick)
        gox.signal_depth.connect(self.slot_depth)
        gox.signal_trade.connect(self.slot_trade)
        gox.signal_userorder.connect(self.slot_userorder)
        gox.orderbook.signal_owns_changed.connect(self.slot_owns_changed)
        gox.signal_wallet.connect(self.slot_wallet_changed)
        self.gox = gox
        self.name = "%s.%s" % (__name__, self.__class__.__name__)
        self.debug("[s]%s%s loaded" % (simulate_or_live, self.name))
        self.debug("[s]Press 's' to see Sell objective")
        #get existing orders for later decision making
        self.existingorders = []
        for order in self.gox.orderbook.owns:
            self.existingorders.append(order.oid)

    def __del__(self):
        try:
            self.debug("[s]%s unloaded" % self.name)
        except Exception, e:
            self.debug("[s]%s exception: %s" % (self.name, e))

    # def slot_before_unload(self, _sender, _data):
    #     self.debug("[s]%s before unload" % self.name)

    def slot_keypress(self, gox, (key)):
        # some custom keypresses are caught here:
        # 's' outputs the strategy objective to the status window & log
        # 'k' displays own orders
        # self.debug("someone pressed the %s key" % chr(key))
        global sell_amount
        if key == ord('s'):
            self.debug("[s]%sObjective: SELL %f BTC when price reaches %f" % (simulate_or_live, sell_amount, sell_level ))
            # self.debug("[s]Python wallet object: %s" % str(self.gox.wallet))
            # check if the user changed volume
            # also ensure the buy_amount does not exceed wallet balance
            # if it does, set sell_amount to wallet full BTC balance
            walletbalance = gox.base2float(self.gox.wallet['BTC'])
            if volume == 0:
                sell_amount = walletbalance
            else:
                sell_amount = volume
            # if volume != 0 and volume <= walletbalance:
            #     if sell_amount != volume:
            #         sell_amount = volume
            #     else:
            #         sell_amount = walletbalance
            # else:
            #     sell_amount = walletbalance
            self.debug("[s] %sstrategy will sell %f of %f BTC on next SELL" % (simulate_or_live, sell_amount, walletbalance))

    def slot_tick(self, gox, (bid, ask)):
        global bidbuf, askbuf, sell_amount
        # if goxapi receives a no-change tick update, don't output anything
        if bid != bidbuf or ask != askbuf:
            seen = 0   # var seen is a flag for default output below (=0)
            self.bid = gox.quote2float(bid)
            if self.bid < sell_level and self.bid > sell_alert:
                self.debug("[s] !!! SELL ALERT @ %s; bid currently at %s" % (str(sell_alert), str(self.bid)))
                self.debug("[s] !!! SELL for %f BTC will trigger @ %f" % (sell_amount, sell_level))
                seen = 1
            elif self.bid >= sell_level:
                # this is the condition to action gox.sell()
                if simulate == False:
                    self.gox.sell(self.bid, sell_amount)
                self.debug("[s] >>> %sSELL BTC @ %s; bid currently at %s" % (simulate_or_live, str(sell_level), str(self.bid)))
                seen = 1
            # if seen == 0:
                # no conditions met above, so give the user default info
                # self.debug("Sell level @ %s (alert: %s); bid @ %s" % (str(sell_level), str(sell_alert), str(self.bid)))
            # is the updated tick different from previous?
            if bid != bidbuf:
                bidbuf = bid
            elif ask != askbuf:
                askbuf = ask

    def slot_depth(self, gox, (typ, price, volume, total_volume)):
        pass

    def slot_trade(self, gox, (date, price, volume, typ, own)):
        """a trade message has been received. Note that this might come
        before the orderbook.owns list has been updated, don't rely on the
        own orders and wallet already having been updated when this fires."""
        # trade messages include trades by other traders
        # if own == True then it is your own
        if str(own) == 'True':
            self.debug("own trade message received: date %s price %s volume %s typ %s own %s" % (str(date), str(price), str(volume), str(typ), str(own)))

    def slot_userorder(self, gox, (price, volume, typ, oid, status)):
        """this comes directly from the API and owns list might not yet be
        updated, if you need the new owns list then use slot_owns_changed"""
        # the coder assumes that if an order id is received via 
        # this signal then it was not instantaneously actioned, so cancel
        # at once
        self.debug("userorder message received: price %f volume %s typ %s oid %s status %s" % (gox.quote2float(price), str(volume), str(typ), str(oid), str(status)))
        # cancel by oid
        if status not in ['pending', 'executing', 'post-pending', 'removed'] and oid not in self.existingorders:
            if gox.quote2float(price) == sell_level:
                self.gox.cancel(oid)

    def slot_owns_changed(self, orderbook, _dummy):
        """this comes *after* userorder and orderbook.owns is updated already"""
        pass

    def slot_wallet_changed(self, gox, _dummy):
        """this comes after the wallet has been updated"""
        # sell_amount can either be manually specified or
        # this strategy will query the user wallet and sell ALL Bitcoins
        # changes to wallet balance should be picked up here - press 'w'
        # to confirm. Else, restart goxtool to reload wallet
            # also ensure the buy_amount does not exceed wallet balance
            # if it does, set sell_amount to wallet full BTC balance
        global sell_amount
        walletbalance = gox.base2float(self.gox.wallet['BTC'])
        if volume != 0 and volume <= walletbalance:
            sell_amount = volume
        else:
            sell_amount = walletbalance

#end
