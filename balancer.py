"""
The portfolio rebalancing bot will buy and sell to maintain a
constant asset allocation ratio of exactly 50/50 = fiat/BTC
"""

import goxapi
import strategy

# Simulate
simulate = True

# Live or simulation notice
simulate_or_live = ('SIMULATION - ' if simulate else '')

DISTANCE    = 7     # percent price distance of next rebalancing orders
FIAT_COLD   = 0     # Amount of Fiat stored at home but included in calculations
COIN_COLD   = 0     # Amount of Coin stored at home but included in calculations

MARKER      = 7     # lowest digit of price to identify bot's own orders
COIN        = 1E8   # number of satoshi per coin, this is a constant.

def add_marker(price, marker):
    """encode a marker in the price value to find bot's own orders"""
    return price / 10 * 10 + marker

def has_marker(price, marker):
    """return true if the price value has the marker"""
    return (price % 10) == marker

def mark_own(price):
    """return the price with our own marker embedded"""
    return add_marker(price, MARKER)

def is_own(price):
    """return true if this price has our own marker"""
    return has_marker(price, MARKER)


class Strategy(strategy.Strategy):
    """a portfolio rebalancing bot"""
    def __init__(self, gox):
        strategy.Strategy.__init__(self, gox)
        self.ask = 0
        self.simulate_or_live = simulate_or_live
        self.distance = DISTANCE
        self.init_distance = float(DISTANCE)
        self.temp_halt = False
        self.name = "%s.%s" % (__name__, self.__class__.__name__)
        self.debug("[s]%s loaded" % self.name)
        self.debug("[s]Press 'i' for information (how much currently out of balance)\n WARNING Rebalancing will buy or sell up to half your fiat or BTC balance\n Press 'r' to rebalance with market order at current price (required before rebalancing)\n Press 'p' to add initial rebalancing orders and start trading\n Press 'c' to cancel all rebalancing orders and suspend trading\n Press 'u' to update account information, order list and wallet")

    def __del__(self):
        try:
            self.debug("[s]%s unloaded" % self.name)
        except Exception, e:
            self.debug("[s]%s exception: %s" % (self.name, e))

    def slot_keypress(self, gox, (key)):
        """a key has been pressed"""

        if key == ord("c"):
            # cancel existing rebalancing orders and suspend trading
            self.debug("[s]canceling all rebalancing orders")
            self.temp_halt = True
            self.cancel_orders()

        if key == ord("p"):
            # create the initial two rebalancing orders and start trading.
            # Before you do this the portfolio should already be balanced.
            # use "i" to show current status and "b" to rebalance with a
            # market order at current price.
            self.debug("[s]adding new initial rebalancing orders")
            self.temp_halt = False
            self.place_orders()

        if key == ord("u"):
            # update the own order list and wallet by forcing what
            # normally happens only after reconnect
            gox.client.channel_subscribe(False)

        if key == ord("i"):
            # print some information into the log file about
            # current status (how much currently out of balance)
            price = (gox.orderbook.bid + gox.orderbook.ask) / 2
            vol_buy = self.get_buy_at_price(price)
            price_balanced = self.get_price_where_it_was_balanced()
            self.debug("[s]BTC difference at current price:",
                gox.base2float(vol_buy))
            self.debug("[s]Price where it would be balanced:",
                gox.quote2float(price_balanced))

        if key == ord("r"):
            # manually rebalance with market order at current price
            price = (gox.orderbook.bid + gox.orderbook.ask) / 2
            vol_buy = self.get_buy_at_price(price)
            if abs(vol_buy) > 0.01 * COIN:
                self.temp_halt = True
                self.cancel_orders()
                if vol_buy > 0:
                    self.debug("[s]%sbuying %f at market price of %f" % (
                        self.simulate_or_live,
                        gox.base2float(vol_buy),
                        gox.quote2float(price)))
                    if simulate == False:
                        gox.buy(0, vol_buy)
                else:
                    self.debug("[s]%sselling %f at market price of %f" % (
                        self.simulate_or_live,
                        gox.base2float(-vol_buy),
                        gox.quote2float(price)))
                    if simulate == False:
                        gox.sell(0, -vol_buy)

    def cancel_orders(self):
        """cancel all rebalancing orders, we identify
        them through the marker in the price value"""
        must_cancel = []
        for order in self.gox.orderbook.owns:
            if is_own(order.price):
                must_cancel.append(order)

        for order in must_cancel:
            self.gox.cancel(order.oid)

    def get_price_where_it_was_balanced(self):
        """get the price at which it was perfectly balanced, given the current
        BTC and Fiat account balances. Immediately after a rebalancing order was
        filled this should be pretty much excactly the price where the order was
        filled (because by definition it should be quite exactly balanced then),
        so even after missing the trade message due to disconnect it should be
        possible to place the next 2 orders precisely around the new center"""
        gox = self.gox
        fiat_have = gox.quote2float(gox.wallet[gox.curr_quote]) + FIAT_COLD
        btc_have  = gox.base2float(gox.wallet[gox.curr_base]) + COIN_COLD
        return gox.quote2int(fiat_have / btc_have)

    def get_buy_at_price(self, price_int):
        """calculate amount of BTC needed to buy at price to achieve
        rebalancing. price and return value are in mtgox integer format"""
        gox = self.gox
        fiat_have = gox.quote2float(gox.wallet[gox.curr_quote]) + FIAT_COLD
        btc_have  = gox.base2float(gox.wallet[gox.curr_base]) + COIN_COLD
        price_then = gox.quote2float(price_int)

        btc_value_then = btc_have * price_then
        diff = fiat_have - btc_value_then
        diff_btc = diff / price_then
        must_buy = diff_btc / 2
        return self.gox.base2int(must_buy)

    def place_orders(self):
        """place two new rebalancing orders above and below center price"""
        center = self.get_price_where_it_was_balanced()
        self.debug("[s]center is %f" % self.gox.quote2float(center))

        step = int(center * self.distance / 100.0)
        next_sell = mark_own(center + step)
        next_buy  = mark_own(center - step)
        status_prefix = self.simulate_or_live

        # Protect against selling below current ask price + step
        if self.ask != 0 and self.gox.quote2float(next_sell) < self.ask:
            next_sell = mark_own(self.gox.quote2int(self.ask) + step)
            self.debug("[s]next_sell at %f, self.ask at %f" % (self.gox.quote2float(next_sell), self.ask))
        elif self.ask == 0:
            status_prefix = 'Waiting for price, skipped ' + self.simulate_or_live

        sell_amount = -self.get_buy_at_price(next_sell)
        buy_amount = self.get_buy_at_price(next_buy)

        if sell_amount < 0.01 * COIN:
            sell_amount = int(0.01 * COIN)
            self.debug("WARNING! minimal sell amount adjusted to 0.01")

        if buy_amount < 0.01 * COIN:
            buy_amount = int(0.011 * COIN)
            self.debug("WARNING! minimal buy amount adjusted to 0.011")

        self.debug("[s]%snew buy order %f at %f" % (
            status_prefix,
            self.gox.base2float(buy_amount),
            self.gox.quote2float(next_buy)
        ))
        if simulate == False and self.ask != 0:
            self.gox.buy(next_buy, buy_amount)

        self.debug("[s]%snew sell order %f at %f" % (
            status_prefix,
            self.gox.base2float(sell_amount),
            self.gox.quote2float(next_sell)
        ))
        if simulate == False and self.ask != 0:
            self.gox.sell(next_sell, sell_amount)

    def slot_tick(self, gox, (bid, ask)):
        # Set last ask price
        self.ask = goxapi.int2float(ask, self.gox.orderbook.gox.currency)
        if self.gox.orderbook.total_ask and self.ask != False and self.distance:
            ratio = (self.gox.orderbook.total_bid / self.gox.orderbook.total_ask) / 1000
            self.debug("ratio: %f bid/ask with %f percent target distance" % (ratio, self.distance))

    def slot_trade(self, gox, (date, price, volume, typ, own)):
        """a trade message has been receivd"""
        # not interested in other people's trades
        if not own:
            return

        # not interested in manually entered (not bot) trades
        if not is_own(price):
            return

        text = {"bid": "sold", "ask": "bought"}[typ]
        self.debug("[s]*** %s %f at %f" % (
            text,
            gox.base2float(volume),
            gox.quote2float(price)
        ))
        self.check_trades()

    def slot_owns_changed(self, orderbook, _dummy):
        """status or amount of own open orders has changed"""
        self.check_trades()

    def check_trades(self):
        """find out if we need to place new orders and do it if neccesary"""

        # bot temporarily disabled
        if self.temp_halt:
            return

        # still waiting for submitted orders,
        # can wait for next signal
        if self.gox.count_submitted:
            return

        # we count the open and pending orders
        count = 0
        count_pending = 0
        book = self.gox.orderbook
        for order in book.owns:
            if is_own(order.price):
                if order.status == "open":
                    count += 1
                else:
                    count_pending += 1

        # as long as there are ANY pending orders around we
        # just do nothing and wait for the next signal
        if count_pending:
            return

        # if count is exacty 1 then one of the orders must have been filled,
        # now we cancel the other one and place two fresh orders in the
        # distance of DISTANCE around center price.
        if count == 1:
            self.cancel_orders()
            self.place_orders()
