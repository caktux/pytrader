"""
The portfolio rebalancing bot will buy and sell to maintain a
constant asset allocation ratio of exactly 50/50
"""

import glob
import math
import time
import strategy
import simplejson as json

# Load balancer.conf
conf = {}
try:
    conf = json.load(open("balancer.conf"))
except:
    print "File balancer.conf not found, saving default config file."

# Set defaults
conf.setdefault('simulate', True)
conf.setdefault('distance', 5)
conf.setdefault('distance_sell', 5)
conf.setdefault('quote_cold', 0)
conf.setdefault('base_cold', 0)
conf.setdefault('quote_limit', 0)
conf.setdefault('base_limit', 0)
conf.setdefault('marker', 9)
conf.setdefault('compensate_fees', True)
conf.setdefault('correction_margin', 1)
conf.setdefault('simulate_quote', 15)
conf.setdefault('simulate_base', 5000)
conf.setdefault('simulate_fee', 0.05)
with open('balancer.conf', 'w') as configfile:
    json.dump(conf, configfile, indent=2)

# Compensate fees
COMPENSATE_FEES = bool(conf['compensate_fees'])

DISTANCE = float(conf['distance'])  # percent price distance of next rebalancing orders
DISTANCE_SELL = float(conf['distance_sell'])  # percent price distance of next rebalancing orders
QUOTE_COLD = float(conf['quote_cold'])  # Amount of Quote stored at home but included in calculations
BASE_COLD = float(conf['base_cold'])  # Amount of Coin stored at home but included in calculations
QUOTE_LIMIT = float(conf['quote_limit'])  # Minimum amount to keep
BASE_LIMIT = float(conf['base_limit'])  # Minimum amount to keep
SIMULATE_QUOTE = float(conf['simulate_quote'])  # Quote balance to simulate
SIMULATE_BASE = float(conf['simulate_base'])  # Base balance to simulate
SIMULATE_FEE = float(conf['simulate_fee'])  # Fee to simulate
MARKER = int(conf['marker'])  # lowest digit of price to identify bot's own orders
BASE = 1E8  # number of satoshi per coin, this is a constant.

ALERT = False
try:
    from pygame import mixer
    mixer.init()
    buy_alert = mixer.Sound('./sounds/bought.wav')
    sell_alert = mixer.Sound('./sounds/sold.wav')
    ALERT = True
except:
    pass

# FIXME Replace those with a registry of our own orders
def add_marker(price, marker):
    """encode a marker in the price value to find bot's own orders"""
    return ((math.floor(price * 1e5) / 1e5) * 1e6 + marker) / 1e6

def has_marker(price, marker):
    """return true if the price value has the marker"""
    return ((price * 1e6) % 10) == marker

def mark_own(price):
    """return the price with our own marker embedded"""
    return add_marker(price, MARKER)

def is_own(price):
    """return true if this price has our own marker"""
    return has_marker(price, MARKER)


class Strategy(strategy.Strategy):
    """a portfolio rebalancing bot"""
    def __init__(self, instance):
        strategy.Strategy.__init__(self, instance)
        self._waiting = False
        self.bid = 0
        self.ask = 0
        self.simulate = bool(conf['simulate'])
        self.simulate_or_live = 'SIMULATION - ' if self.simulate else 'LIVE - '
        self.base = self.instance.curr_base
        self.quote = self.instance.curr_quote
        self.wallet = False
        self.step_factor = 1 + DISTANCE / 100.0
        self.step_factor_sell = 1 + DISTANCE_SELL / 100.0
        self.temp_halt = False
        self.name = "%s.%s" % (__name__, self.__class__.__name__)
        self.debug("[s]%s%s loaded" % (self.simulate_or_live, self.name))
        self.help()

        # Simulation wallet
        if (self.simulate and not self.instance.wallet) or (self.simulate and self.wallet):
            self.init_simulation_wallet()

    def __del__(self):
        try:
            if self.simulate and self.wallet:
                self.instance.wallet = {}
            self.debug("[s]%s unloaded" % self.name)
        except Exception, e:
            self.debug("[s]%s exception: %s" % (self.name, e))

    def write_log(self, txt):
        """write line to a separate logfile"""
        with open("balancer.log" if not self.simulate else "simulation.log", "a") as logfile:
            logfile.write(txt + "\n")

    def init_simulation_wallet(self):
        self.wallet = True
        self.simulated = {'next_sell': 0, 'sell_amount': 0, 'next_buy': 0, 'buy_amount': 0}
        self.instance.wallet = {}
        self.instance.wallet[self.quote] = SIMULATE_QUOTE
        self.instance.wallet[self.base] = SIMULATE_BASE
        self.instance.trade_fee = SIMULATE_FEE

    def slot_keypress(self, api, (key)):
        """a key has been pressed"""

        if key == ord("h"):
            self.help()

        if key == ord("s"):
            if self.simulate:
                self.simulate = False
            else:
                self.simulate = True
                self.init_simulation_wallet()
            self.simulate_or_live = 'SIMULATION - ' if self.simulate else 'LIVE - '
            self.debug("[s]%s" % self.simulate_or_live)

        if key == ord("c"):
            # cancel existing rebalancing orders and suspend trading
            self.debug("[s]%scanceling all rebalancing orders" % self.simulate_or_live)
            self.temp_halt = True
            self.cancel_orders()

        if key == ord("p"):
            # create the initial two rebalancing orders and start trading.
            # Before you do this the portfolio should already be balanced.
            # use "i" to show current status and "b" to rebalance with a
            # market order at current price.
            self.debug("[s]%sadding new initial rebalancing orders" % self.simulate_or_live)
            self.temp_halt = False
            self.cancel_orders()
            self.place_orders()

        if key == ord("u"):
            # update the own order list and wallet by forcing what
            # normally happens only after reconnect
            api.client.request_info()
            api.client.request_orders()

        if key == ord("i"):
            # print some information into the log file about
            # current status (how much currently out of balance)
            price = (api.orderbook.bid + api.orderbook.ask) / 2
            vol_buy = self.get_buy_at_price(price)

            price_balanced = self.get_price_where_it_was_balanced()
            if not price_balanced:
                return
            self.debug("[s]center is %.8f" % price_balanced)
            price_sell = self.get_next_sell_price(price_balanced, self.step_factor_sell)
            price_buy = self.get_next_buy_price(price_balanced, self.step_factor)
            sell_amount = -self.get_buy_at_price(price_sell)
            buy_amount = self.get_buy_at_price(price_buy)

            self.debug("[s]%s difference at current price:" % self.base, vol_buy)
            self.debug("[s]Next two orders would be at:")
            self.debug("[s]  ask: %.4f %s @ %.6f = %.6f %s" % (
                sell_amount,
                api.curr_base,
                price_sell,
                price_sell * sell_amount,
                api.curr_quote))
            self.debug("[s]  bid: %.4f %s @ %.6f = %.6f %s" % (
                buy_amount,
                api.curr_base,
                price_buy,
                price_buy * buy_amount,
                api.curr_quote))

            self.debug("[s]Monthly volume: %g %s / trade fee: %g%%" % (api.monthly_volume, api.currency, api.trade_fee))

        if key == ord('o'):
            self.debug("[s] %i own orders in orderbook" % len(self.instance.orderbook.owns))

            if self.instance.orderbook.owns:
                for order in self.instance.orderbook.owns:
                    volume = order.volume - (order.volume * api.trade_fee / 100)
                    total = order.volume * order.price
                    total = total - (total * api.trade_fee / 100)
                    if order.typ == 'bid':
                        bid_volume = volume
                        bid_total = total
                    if order.typ == 'ask':
                        ask_volume = volume
                        ask_total = total

                    self.debug("[s]  %s: %s: %s @ %s %s order id: %s" % (
                        order.status,
                        order.typ,
                        order.volume,
                        order.price,
                        order.price * order.volume,
                        order.oid))

                base_profit = (bid_volume - ask_volume) / 2
                quote_profit = (ask_total - bid_total) / 2

                self.debug("[s]  Profit would be: %.8f %s / %.8f %s" % (
                    base_profit,
                    api.curr_base,
                    quote_profit,
                    api.curr_quote))

            if self.simulate and self.wallet and self.simulated['next_sell'] and self.simulated['next_buy']:
                sell_total = self.simulated['next_sell'] * self.simulated['sell_amount']
                buy_total = self.simulated['next_buy'] * self.simulated['buy_amount']
                self.debug("[s]SIMULATION orders:")
                self.debug("[s]  %s: %s @ %s %s" % (
                    'ask',
                    self.simulated['sell_amount'],
                    self.simulated['next_sell'],
                    sell_total))
                self.debug("[s]  %s: %s @ %s %s" % (
                    'bid',
                    self.simulated['buy_amount'],
                    self.simulated['next_buy'],
                    buy_total))

                base_profit = (self.simulated['buy_amount'] - self.simulated['sell_amount']) / 2
                quote_profit = (sell_total - buy_total) / 2

                self.debug("[s]  Profit would be: %.8f %s / %.8f %s" % (
                    base_profit,
                    api.curr_base,
                    quote_profit,
                    api.curr_quote))

        if key == ord("r"):
            # manually rebalance with market order at current price
            price = (api.orderbook.bid + api.orderbook.ask) / 2
            vol_buy = self.get_buy_at_price(price)
            self.temp_halt = True
            self.cancel_orders()
            if vol_buy > 0:
                price = api.orderbook.ask
                vol_buy = self.get_buy_at_price(price)
                self.debug("[s]%sbuying %.8f at market price of %.6f" % (
                    self.simulate_or_live,
                    vol_buy,
                    price))
                if not self.simulate:
                    api.buy(0, vol_buy)
            else:
                price = api.orderbook.bid
                vol_buy = self.get_buy_at_price(price)
                self.debug("[s]%sselling %.8f at market price of %.6f" % (
                    self.simulate_or_live,
                    -vol_buy,
                    price))
                if not self.simulate:
                    api.sell(0, -vol_buy)

        if key == ord("f"):
            self.instance.client.force_reconnect()

    def help(self):
        self.debug("[s]Press 'h' to see this help")
        self.debug("[s]Press 'i' for information")
        self.debug("[s]Press 'o' to see order book")
        self.debug("[s]WARNING Rebalancing will buy or sell up to half your %s or %s balance" % (self.quote, self.base))
        self.debug("[s]Press 'r' to rebalance with market order at current price (recommended before rebalancing)")
        self.debug("[s]Press 'p' to add initial rebalancing orders and start trading")
        self.debug("[s]Press 'c' to cancel all rebalancing orders and suspend trading")
        self.debug("[s]Press 'u' to update account information, order list and wallet")
        self.debug("[s]Press 's' to switch between Live and Simulation modes")

    def cancel_orders(self):
        """cancel all rebalancing orders, we identify
        them through the marker in the price value"""
        must_cancel = []
        for order in self.instance.orderbook.owns:
            # self.debug("[s]is_own: %.6f, oid: %s, price: %.6f" % ((order.price * 1e6) % 10, order.oid, order.price))
            # if is_own(order.price):
            must_cancel.append(order)

        for order in must_cancel:
            if not self.simulate:
                self.instance.cancel(order.oid)

    def get_price_where_it_was_balanced(self):
        """get the price at which it was perfectly balanced, given the current
        Base and Quote account balances. Immediately after a rebalancing order was
        filled this should be pretty much excactly the price where the order was
        filled (because by definition it should be quite exactly balanced then),
        so even after missing the trade message due to disconnect it should be
        possible to place the next 2 orders precisely around the new center"""
        api = self.instance
        if (api.wallet) and self.bid and self.ask:
            quote_have = api.wallet[api.curr_quote] + QUOTE_COLD
            base_have = api.wallet[api.curr_base] + BASE_COLD
            if quote_have == 0 and base_have and self.ask:
                return ((api.wallet[api.curr_base] / 2) * self.ask) / 2
            elif base_have == 0 and quote_have and self.bid:
                return ((api.wallet[api.curr_quote] / 2) / self.bid) / 2
        else:
            self.debug('[s]Waiting for price...')
            return False
        return quote_have / base_have

    def get_buy_at_price(self, price):
        """calculate amount of BASE needed to buy at price to achieve rebalancing.
        Negative return value means we need to sell. price and return value is a
        float"""
        if price:
            quote_have = self.instance.wallet[self.quote] + QUOTE_COLD
            base_value_then = self.get_base_value(price)
            diff = quote_have - base_value_then
            diff_base = diff / price
            must_buy = diff_base / 2

            return must_buy
        return 0

    def get_base_value(self, price):
        """get total base value in quote at current price"""
        base_have = self.instance.wallet[self.base] + BASE_COLD
        base_value = base_have * price
        return base_value

    def place_orders(self):
        """place two new rebalancing orders above and below center price"""
        center = self.get_price_where_it_was_balanced()
        if center:
            self.debug("[s][%s] center is %.8f" % (time.strftime("%H:%M:%S"), center))
        else:
            return

        next_sell = self.get_next_sell_price(center, self.step_factor_sell)
        next_buy = self.get_next_buy_price(center, self.step_factor)

        status_prefix = self.simulate_or_live

        correction_margin = float(conf['correction_margin'])

        # Protect against selling below current ask price
        # self.debug("ask: %s, next_sell: %s" % (self.ask, next_sell))
        if self.ask != 0 and next_sell < self.ask:
            bad_next_sell = next_sell

            # step = int(center * self.distance / 100.0)

            # Apply target margin to corrected sell price
            if correction_margin:
                # next_sell = mark_own(math.ceil(self.ask * (1 + correction_margin / 100) * 1e8) / 1e8)
                next_sell = self.ask * (1 + correction_margin / 100)
            else:
                # next_sell = mark_own(self.ask)
                next_sell = self.ask

            self.debug("[s]corrected next sell at %.8f instead of %.8f, ask price at %.8f" %
                       (next_sell, bad_next_sell, self.ask))
        elif self.ask == 0:
            status_prefix = 'Waiting for price, skipping ' + self.simulate_or_live

        # Protect against buying above current bid price
        if self.bid != 0 and next_buy > self.bid:
            bad_next_buy = next_buy

            # step = int(center * self.distance / 100.0)

            # Apply target margin to corrected buy price
            if correction_margin:
                # next_buy = mark_own(math.ceil(self.bid * 2 - (self.bid * (1 + correction_margin / 100)) * 1e8) / 1e8)
                next_buy = self.bid * 2 - (self.bid * (1 + correction_margin / 100))
            else:
                # next_buy = mark_own(self.bid)
                next_buy = self.bid

            self.debug("[s]corrected next buy at %.8f instead of %.8f, bid price at %.8f" % (next_buy, bad_next_buy, self.bid))
        elif self.bid == 0:
            status_prefix = 'Waiting for price, skipping ' + self.simulate_or_live

        sell_amount = -self.get_buy_at_price(next_sell)
        buy_amount = self.get_buy_at_price(next_buy)

        if sell_amount < 0.1:
            sell_amount = 0.1
            self.debug("[s]WARNING! minimal sell amount adjusted to 0.1")

        if buy_amount < 0.1:
            buy_amount = 0.1
            self.debug("[s]WARNING! minimal buy amount adjusted to 0.1")

        self.debug("[s]%snew buy order %.8f at %.8f for %.8f %s" % (
            status_prefix,
            buy_amount,
            next_buy,
            next_buy * buy_amount,
            self.quote
        ))
        if not self.simulate and self.ask != 0:
            self.instance.buy(next_buy, buy_amount)
        elif self.simulate and self.wallet and self.ask != 0:
            self.simulated.update({"next_buy": next_buy, "buy_amount": buy_amount})

        self.debug("[s]%snew sell order %.8f at %.8f for %.8f %s" % (
            status_prefix,
            sell_amount,
            next_sell,
            next_sell * sell_amount,
            self.quote
        ))
        if not self.simulate and self.bid != 0:
            self.instance.sell(next_sell, sell_amount)
        elif self.simulate and self.wallet and self.bid != 0:
            self.simulated.update({"next_sell": next_sell, "sell_amount": sell_amount})

    def slot_tick(self, api, (bid, ask)):
        # Set last bid/ask price
        # self.debug("TICK %s - %s, %s" % (bid, ask, api))
        self.bid = bid
        self.ask = ask

        # Simulation wallet
        if self.simulate and self.wallet:
            if ask >= self.simulated['next_sell']:
                self.instance.wallet[self.quote] += self.simulated['sell_amount'] * self.simulated['next_sell']
                self.instance.wallet[self.base] -= self.simulated['sell_amount']
                # Trigger slot_trade for simulation.log
                self.slot_trade(self.instance, (time, self.simulated['next_sell'], self.simulated['sell_amount'], 'bid', True))
                self.place_orders()

            if bid <= self.simulated['next_buy']:
                self.instance.wallet[self.base] += self.simulated['buy_amount'] - (self.simulated['buy_amount'] * self.instance.trade_fee / 100)
                self.instance.wallet[self.quote] -= self.simulated['buy_amount'] * self.simulated['next_buy']
                # Trigger slot_trade for simulation.log
                self.slot_trade(self.instance, (time, self.simulated['next_buy'], self.simulated['buy_amount'], 'ask', True))
                self.place_orders()

    def slot_trade(self, api, (date, price, volume, typ, own)):
        """a trade message has been received"""
        self.debug("[s]slot_trade triggered")

        # not interested in other people's trades
        if not own:
            return

        # not interested in manually entered (not bot) trades
        # if not is_own(price):
        #     return

        text = {"bid": "sold", "ask": "bought"}[typ]

        if price and volume:
            self.debug("[s]*** %s%s %.8f at %.8f" % (
                self.simulate_or_live,
                text,
                volume,
                price
            ))

        # write some account information to a separate log file
        if len(api.wallet):
            total_base = 0
            total_quote = 0
            for c, own_currency in enumerate(api.wallet):
                if own_currency == api.curr_base and api.orderbook.ask:
                    total_base += api.wallet[own_currency]
                    total_quote += api.wallet[own_currency] * api.orderbook.bid
                elif own_currency == api.curr_quote and api.orderbook.bid:
                    total_quote += api.wallet[own_currency]
                    total_base += api.wallet[own_currency] / api.orderbook.ask

            total_quote = total_quote
            quote_ratio = (total_quote / api.orderbook.bid) / total_base
            base_ratio = (total_base / api.orderbook.ask) * 100

            datetime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self.write_log('"%s", "%s", %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f' % (
                datetime,
                text,
                volume,
                price,
                api.trade_fee,
                self.get_price_where_it_was_balanced(),
                api.wallet[api.curr_quote],
                total_quote,
                QUOTE_COLD,
                quote_ratio,
                api.wallet[api.curr_base],
                total_base,
                BASE_COLD,
                base_ratio
            ))

        self.check_trades()

    def slot_owns_changed(self, orderbook, _dummy):
        """status or amount of own open orders has changed"""
        # self.debug("[s]slot_owns_changed triggered")

        # Fix leftover satoshi
        for order in orderbook.owns:
            if order.volume == 0.00000001:
                self.debug("[s]Satoshi!  %s: %s: %s @ %s order id: %s" % (order.status, order.typ, order.volume, order.price, order.oid))
                self.instance.cancel(order.oid)

        self.check_trades()

    def check_trades(self):
        """find out if we need to place new orders and do it if neccesary"""

        # bot temporarily disabled
        if self.temp_halt:
            return

        # right after initial connection we have no
        # wallet yet, we cannot trade anyways without that,
        # must wait until private/info is received.
        if self.instance.wallet == {}:
            return

        # still waiting for submitted orders,
        # can wait for next signal
        if self.instance.count_submitted:
            return

        # we count the open and pending orders
        count = 0
        count_pending = 0
        for order in self.instance.orderbook.owns:
            # if is_own(order.price):
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
            # wait for polled balances
            if not self.instance.client._wait_for_next_info and not self._waiting:
                self.instance.client._wait_for_next_info = True
                self._waiting = True
            if self.instance.client._wait_for_next_info:
                self.debug("[s]Waiting for balances...")
                return

            # Check minimum limits
            if self.instance.wallet[self.quote] <= QUOTE_LIMIT:
                self.debug("[s]%s %s is below minimum of %s, aborting..." % (
                           self.instance.wallet[self.quote],
                           self.quote,
                           QUOTE_LIMIT))
                self.cancel_orders()
                return
            if self.instance.wallet[self.base] <= BASE_LIMIT:
                self.debug("[s]%s %s is below minimum of %s, aborting..." % (
                           self.instance.wallet[self.base],
                           self.base,
                           BASE_LIMIT))
                self.cancel_orders()
                return

            self._waiting = False
            self.debug("[s]Got balances...")
            if ALERT:
                try:
                    if self.instance.orderbook.owns[0].typ == 'bid':
                        sell_alert.play()
                    else:
                        buy_alert.play()
                except:
                    pass
            self.cancel_orders()
            self.place_orders()

    def price_with_fees(self, price):
        # Get our volume at price
        volume_at_price = self.get_buy_at_price(price)

        if volume_at_price > 0:
            bid_or_ask = 'bid'
            price_with_fees = price / ((1 - self.instance.trade_fee / 100) * (1 - self.instance.trade_fee / 100))
            price_with_fees = price - (price_with_fees - price)
        else:
            bid_or_ask = 'ask'
            volume_at_price = -volume_at_price
            price_with_fees = price / ((1 - self.instance.trade_fee / 100) * (1 - self.instance.trade_fee / 100))

        # Calculate fees
        fees_at_price = volume_at_price * self.instance.trade_fee / 100

        self.debug("[s]next %s: %.8f %s @ %.8f %s - fees: %.8f %s - new: %.8f %s" % (
            bid_or_ask,
            volume_at_price,
            self.base,
            price,
            self.quote,
            fees_at_price,
            self.base,
            price_with_fees,
            self.quote))

        # Return the price with fees
        return math.ceil(price_with_fees * 1e8) / 1e8

    def get_next_buy_price(self, center, step_factor):
        """get the next buy price. If there is a forced price level
        then it will return that, otherwise return center - step"""
        price = self.get_forced_price(center, False)
        if not price:
            price = math.ceil((center / step_factor) * 1e8) / 1e8

            if not center:
                self.debug("[s]Waiting for price...")
            elif COMPENSATE_FEES:
                # Decrease our next buy price
                price = self.price_with_fees(price)

        # return mark_own(price)
        return price

    def get_next_sell_price(self, center, step_factor):
        """get the next sell price. If there is a forced price level
        then it will return that, otherwise return center + step"""
        price = self.get_forced_price(center, True)
        if not price:
            price = math.ceil((center * step_factor) * 1e8) / 1e8

            # Compensate the fees on sell price
            if not center:
                self.debug("[s]Waiting for price...")
            elif COMPENSATE_FEES:
                # Increase our next sell price
                price = self.price_with_fees(price)

        # return mark_own(price)
        return price

    def get_forced_price(self, center, need_ask):
        """get externally forced price level for order"""
        prices = []
        found = glob.glob("_balancer_force_*")
        if len(found):
            for name in found:
                try:
                    price = float(name.split("_")[3])
                    prices.append(price)
                except:
                    pass
            prices.sort()
            if need_ask:
                for price in prices:
                    if price > center * self.step_factor_sell:
                        # return mark_own(price)
                        return price
            else:
                for price in reversed(prices):
                    if price < center / self.step_factor:
                        # return mark_own(price)
                        return price

        return None
