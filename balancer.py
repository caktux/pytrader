"""
The portfolio rebalancing bot will buy and sell to maintain a
constant asset allocation ratio of exactly 50/50
"""

import glob
import math
import time
import strategy
import simplejson as json

# Load user.conf
conf = json.load(open("user.conf"))

# Set defaults
conf.setdefault('balancer_simulate', True)
conf.setdefault('balancer_distance', 5)
conf.setdefault('balancer_distance_sell', 5)
conf.setdefault('balancer_quote_cold', 0)
conf.setdefault('balancer_coin_cold', 0)
conf.setdefault('balancer_marker', 9)
conf.setdefault('balancer_compensate_fees', True)
conf.setdefault('balancer_target_margin', 1)

# Simulate
SIMULATE = int(conf['balancer_simulate'])

# Compensate fees
COMPENSATE_FEES = int(conf['balancer_compensate_fees'])

# Live or simulation notice
SIMULATE_OR_LIVE = 'SIMULATION - ' if SIMULATE else 'LIVE - '

DISTANCE = float(conf['balancer_distance'])  # percent price distance of next rebalancing orders
DISTANCE_SELL = float(conf['balancer_distance_sell'])  # percent price distance of next rebalancing orders
QUOTE_COLD = float(conf['balancer_quote_cold'])  # Amount of Quote stored at home but included in calculations
BASE_COLD = float(conf['balancer_coin_cold'])  # Amount of Coin stored at home but included in calculations

MARKER = int(conf['balancer_marker'])  # lowest digit of price to identify bot's own orders
BASE = 1E8  # number of satoshi per coin, this is a constant.

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

def write_log(txt):
    """write line to a separate logfile"""
    with open("balancer.log" if not SIMULATE else "simulation.log", "a") as logfile:
        logfile.write(txt + "\n")


class Strategy(strategy.Strategy):
    """a portfolio rebalancing bot"""
    def __init__(self, instance):
        strategy.Strategy.__init__(self, instance)
        self.bid = 0
        self.ask = 0
        self.simulate_or_live = SIMULATE_OR_LIVE
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
        if (SIMULATE and not self.instance.wallet) or (SIMULATE and self.wallet):
            self.wallet = True
            self.simulate = {'next_sell': 0, 'sell_amount': 0, 'next_buy': 0, 'buy_amount': 0}
            self.instance.wallet = {}
            self.instance.wallet[self.instance.curr_quote] = 28
            self.instance.wallet[self.instance.curr_base] = 15000
            self.instance.trade_fee = 0.1

    def __del__(self):
        try:
            if SIMULATE and self.wallet:
                self.instance.wallet = {}
            self.debug("[s]%s unloaded" % self.name)
        except Exception, e:
            self.debug("[s]%s exception: %s" % (self.name, e))

    def slot_keypress(self, api, (key)):
        """a key has been pressed"""

        if key == ord("h"):
            self.help()

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
            profit_base = 0
            profit_quote = 0
            for order in self.instance.orderbook.owns:
                base_diff = (order.volume - order.volume * api.trade_fee / 100) if str(order.typ) == 'bid' else order.volume
                profit_base = base_diff if not profit_base else profit_base - base_diff
                quote_diff = order.price * order.volume
                profit_quote = -quote_diff if not profit_quote else profit_quote + (quote_diff if str(order.typ) == 'ask' else -quote_diff)

                self.debug("[s]  %s: %s: %s @ %s %s order id: %s" % (
                    order.status,
                    order.typ,
                    order.volume,
                    order.price,
                    order.price * order.volume,
                    order.oid))
            self.debug("[s]  Profit would be: %.8f %s / %.8f %s" % (
                profit_base,
                api.curr_base,
                profit_quote,
                api.curr_quote))

            if SIMULATE and self.wallet and self.simulate['next_sell'] and self.simulate['next_buy']:
                self.debug("[s]SIMULATION orders:")
                self.debug("[s]  %s: %s @ %s %s" % (
                    'ask',
                    self.simulate['sell_amount'],
                    self.simulate['next_sell'],
                    self.simulate['next_sell'] * self.simulate['sell_amount']))
                self.debug("[s]  %s: %s @ %s %s" % (
                    'bid',
                    self.simulate['buy_amount'],
                    self.simulate['next_buy'],
                    self.simulate['next_buy'] * self.simulate['buy_amount']))

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
                if not SIMULATE:
                    api.buy(0, vol_buy)
            else:
                price = api.orderbook.bid
                vol_buy = self.get_buy_at_price(price)
                self.debug("[s]%sselling %.8f at market price of %.6f" % (
                    self.simulate_or_live,
                    -vol_buy,
                    price))
                if not SIMULATE:
                    api.sell(0, -vol_buy)

    def help(self):
        self.debug("[s]Press 'h' to see this help")
        self.debug("[s]Press 'i' for information")
        self.debug("[s]Press 'o' to see order book")
        self.debug("[s]WARNING Rebalancing will buy or sell up to half your %s or %s balance" % (self.quote, self.base))
        self.debug("[s]Press 'r' to rebalance with market order at current price (recommended before rebalancing)")
        self.debug("[s]Press 'p' to add initial rebalancing orders and start trading")
        self.debug("[s]Press 'c' to cancel all rebalancing orders and suspend trading")
        self.debug("[s]Press 'u' to update account information, order list and wallet")

    def cancel_orders(self):
        """cancel all rebalancing orders, we identify
        them through the marker in the price value"""
        must_cancel = []
        for order in self.instance.orderbook.owns:
            # self.debug("[s]is_own: %.6f, oid: %s, price: %.6f" % ((order.price * 1e6) % 10, order.oid, order.price))
            # if is_own(order.price):
            must_cancel.append(order)

        for order in must_cancel:
            if not SIMULATE:
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
        quote_have = self.instance.wallet[self.instance.curr_quote] + QUOTE_COLD
        base_value_then = self.get_base_value(price)
        diff = quote_have - base_value_then
        diff_base = diff / price
        must_buy = diff_base / 2

        return must_buy

    def get_base_value(self, price):
        """get total base value in quote at current price"""
        base_have = self.instance.wallet[self.instance.curr_base] + BASE_COLD
        base_value = base_have * price
        return base_value

    def place_orders(self):
        """place two new rebalancing orders above and below center price"""
        center = self.get_price_where_it_was_balanced()
        if center:
            self.debug("[s]center is %.8f" % center)
        else:
            return

        next_sell = self.get_next_sell_price(center, self.step_factor_sell)
        next_buy = self.get_next_buy_price(center, self.step_factor)

        status_prefix = self.simulate_or_live

        target_margin = float(conf['balancer_target_margin'])

        # Protect against selling below current ask price
        # self.debug("ask: %s, next_sell: %s" % (self.ask, next_sell))
        if self.ask != 0 and next_sell < self.ask:
            bad_next_sell = next_sell

            # step = int(center * self.distance / 100.0)

            # Apply target margin to corrected sell price
            if target_margin:
                # next_sell = mark_own(math.ceil(self.ask * (1 + target_margin / 100) * 1e8) / 1e8)
                next_sell = math.ceil(self.ask * (1 + target_margin / 100) * 1e8) / 1e8
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
            if target_margin:
                # next_buy = mark_own(math.ceil(self.bid * 2 - (self.bid * (1 + target_margin / 100)) * 1e8) / 1e8)
                next_buy = math.ceil(self.bid * 2 - (self.bid * (1 + target_margin / 100)) * 1e8) / 1e8
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
            self.instance.curr_quote
        ))
        if not SIMULATE and self.ask != 0:
            self.instance.buy(next_buy, buy_amount)
        elif SIMULATE and self.wallet and self.ask != 0:
            self.simulate.update({"next_buy": next_buy, "buy_amount": buy_amount})

        self.debug("[s]%snew sell order %.8f at %.8f for %.8f %s" % (
            status_prefix,
            sell_amount,
            next_sell,
            next_sell * sell_amount,
            self.instance.curr_quote
        ))
        if not SIMULATE and self.bid != 0:
            self.instance.sell(next_sell, sell_amount)
        elif SIMULATE and self.wallet and self.bid != 0:
            self.simulate.update({"next_sell": next_sell, "sell_amount": sell_amount})

    def slot_tick(self, api, (bid, ask)):
        # Set last bid/ask price
        # self.debug("TICK %s - %s, %s" % (bid, ask, api))
        self.bid = bid
        self.ask = ask

        # Simulation wallet
        if SIMULATE and self.wallet:
            if ask >= self.simulate['next_sell']:
                self.instance.wallet[self.instance.curr_quote] += self.simulate['sell_amount'] * self.simulate['next_sell']
                self.instance.wallet[self.instance.curr_base] -= self.simulate['sell_amount']
                # Trigger slot_trade for simulation.log
                self.slot_trade(self.instance, (time, self.simulate['next_sell'], self.simulate['sell_amount'], 'bid', True))
                self.place_orders()

            if bid <= self.simulate['next_buy']:
                self.instance.wallet[self.instance.curr_base] += self.simulate['buy_amount'] - (self.simulate['buy_amount'] * self.instance.trade_fee / 100)
                self.instance.wallet[self.instance.curr_quote] -= self.simulate['buy_amount'] * self.simulate['next_buy']
                # Trigger slot_trade for simulation.log
                self.slot_trade(self.instance, (time, self.simulate['next_buy'], self.simulate['buy_amount'], 'ask', True))
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
                'SIMULATION - ' if SIMULATE else '',
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
            write_log('"%s", "%s", %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f, %.8f' % (
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
            if not self.instance.client._info_ready:
                self.debug("[s]Waiting for balances...")
                return
            self.debug("[s]Got balances...")

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
            self.instance.curr_base,
            price,
            self.instance.curr_quote,
            fees_at_price,
            self.instance.curr_base,
            price_with_fees,
            self.instance.curr_quote))

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
            if COMPENSATE_FEES and center:
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
