# -*- coding: utf-8 -*-
""" Kraken Client """

import json
import time
import hmac
import Queue
import base64
import hashlib
import threading
# import traceback
from api import BaseObject, Signal, Timer, start_thread, http_request
from api import FORCE_NO_FULLDEPTH, FORCE_NO_HISTORY
from urllib import urlencode

HTTP_HOST = "api.kraken.com"

class PollClient(BaseObject):
    """Polling client class"""

    _last_unique_microtime = 0
    _nonce_lock = threading.Lock()

    def __init__(self, curr_base, curr_quote, secret, config):
        BaseObject.__init__(self)

        self.signal_recv = Signal()
        self.signal_fulldepth = Signal()
        self.signal_fullhistory = Signal()
        self.signal_ticker = Signal()
        self.signal_connected = Signal()
        self.signal_disconnected = Signal()

        self._timer_lag = Timer(120)
        self._timer_info = Timer(8)
        self._timer_depth = Timer(10)
        self._timer_ticker = Timer(11)
        self._timer_orders = Timer(15)
        self._timer_volume = Timer(300)
        self._timer_history = Timer(15)

        self._timer_lag.connect(self.slot_timer_lag)
        self._timer_info.connect(self.slot_timer_info)
        self._timer_ticker.connect(self.slot_timer_ticker)
        self._timer_orders.connect(self.slot_timer_orders)
        self._timer_volume.connect(self.slot_timer_volume)
        self._timer_depth.connect(self.slot_timer_depth)
        self._timer_history.connect(self.slot_timer_history)

        self._info_timer = None  # used when delayed requesting private/info
        self._wait_for_next_info = False

        self.curr_base = curr_base
        self.curr_quote = curr_quote
        self.pair = "%s%s" % (curr_base, curr_quote)

        self.secret = secret
        self.config = config

        use_ssl = self.config.get_bool("api", "use_ssl")
        self.proto = {True: "https", False: "http"}[use_ssl]
        self.http_requests = Queue.Queue()

        self._http_thread = None
        self._terminating = False
        self.history_last_candle = None

        self.request_info()
        self.request_volume()
        self.request_fulldepth()
        self.request_history()

    def start(self):
        """Start the client"""
        self._http_thread = start_thread(self._http_thread_func, "http thread")

    def stop(self):
        """Stop the client"""
        self._terminating = True
        self._timer_lag.cancel()
        self._timer_info.cancel()
        self._timer_depth.cancel()
        self._timer_ticker.cancel()
        self._timer_orders.cancel()
        self._timer_volume.cancel()
        self._timer_history.cancel()
        self.debug("### stopping client")

    def get_unique_microtime(self):
        """Produce a unique nonce that is guaranteed to be ever increasing"""
        with self._nonce_lock:
            microtime = int(time.time() * 1e6)
            if microtime <= self._last_unique_microtime:
                microtime = self._last_unique_microtime + 1
            self._last_unique_microtime = microtime
            return microtime

    def request_fulldepth(self):
        """Start the fulldepth thread"""

        def fulldepth_thread():
            """Request the full market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            querystring = "?pair=%s" % self.pair
            # self.debug("### requesting full depth")
            json_depth = http_request("%s://%s/0/public/Depth%s" % (
                self.proto,
                HTTP_HOST,
                querystring
            ))
            if json_depth and not self._terminating:
                try:
                    fulldepth = json.loads(json_depth)
                    depth = {}
                    depth['error'] = fulldepth['error']
                    # depth['data'] = fulldepth['result']
                    depth['data'] = {'asks': [], 'bids': []}
                    for ask in fulldepth['result'][self.pair]['asks']:
                        depth['data']['asks'].append({
                            'price': float(ask[0]),
                            'amount': float(ask[1])
                        })
                    for bid in reversed(fulldepth['result'][self.pair]['bids']):
                        depth['data']['bids'].append({
                            'price': float(bid[0]),
                            'amount': float(bid[1])
                        })
                    if depth:
                        self.signal_fulldepth(self, (depth))
                except Exception as exc:
                    self.debug("### exception in fulldepth_thread:", exc)

        start_thread(fulldepth_thread, "http request full depth")

    def request_history(self):
        """Start the trading history thread"""

        # Api() will have set this field to the timestamp of the last
        # known candle, so we only request data since this time
        # since = self.history_last_candle

        def history_thread():
            """request trading history"""

            querystring = "?pair=%s" % self.pair
            if not self.history_last_candle:
                querystring += "&since=%i" % ((time.time() - 172800) * 1e9)
                # self.debug("Requesting history since: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 172800)))
            else:
                querystring += "&since=%i" % (self.history_last_candle * 1e9)
                # self.debug("Last candle: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.history_last_candle)))

            # self.debug("### requesting history")
            json_hist = http_request("%s://%s/0/public/Trades%s" % (
                self.proto,
                HTTP_HOST,
                querystring
            ))
            if json_hist and not self._terminating:
                try:
                    raw_history = json.loads(json_hist)

                    if raw_history['error']:
                        self.debug("Error in history: %s" % raw_history['error'])
                        return

                    # self.debug("History: %s" % raw_history)
                    history = []
                    for h in raw_history["result"][self.pair]:
                        history.append({
                            'price': float(h[0]),
                            'amount': float(h[1]),
                            'date': h[2]
                        })
                    if history:
                        self.signal_fullhistory(self, history)
                except Exception as exc:
                    self.debug("### exception in history_thread:", exc)

        start_thread(history_thread, "http request trade history")

    def request_ticker(self):
        """Request ticker"""
        def ticker_thread():
            querystring = "?pair=%s" % self.pair
            json_ticker = http_request("%s://%s/0/public/Ticker%s" % (
                self.proto,
                HTTP_HOST,
                querystring
            ))
            if not self._terminating:
                try:
                    answer = json.loads(json_ticker)
                    # self.debug("TICK %s" % answer)
                    if not answer["error"]:
                        bid = float(answer['result'][self.pair]['b'][0])
                        ask = float(answer['result'][self.pair]['a'][0])
                        self.signal_ticker(self, (bid, ask))
                except Exception as exc:
                    self.debug("### exception in ticker_thread:", exc)

        start_thread(ticker_thread, "http request ticker")

    def request_lag(self):
        """Request server time to calculate lag"""
        def lag_thread():
            json_time = http_request("%s://%s/0/public/Time" % (
                self.proto,
                HTTP_HOST
            ))
            if not self._terminating:
                try:
                    answer = json.loads(json_time)
                    if not answer["error"]:
                        lag = time.time() - answer['result']['unixtime']
                        result = {
                            'lag': lag * 1000,
                            'lag_text': "%0.3f s" % lag
                        }
                        translated = {
                            "op": "result",
                            "result": result,
                            "id": "order_lag"
                        }
                        self.signal_recv(self, (json.dumps(translated)))
                except Exception as exc:
                    self.debug("### exception in lag_thread:", exc)

        start_thread(lag_thread, "http request lag")

    def _slot_timer_info_later(self, _sender, _data):
        """the slot for the request_info_later() timer signal"""
        self.request_info()
        self._info_timer = None

    def request_info_later(self, delay):
        """request the private/info in delay seconds from now"""
        if self._info_timer:
            self._info_timer.cancel()
        self._info_timer = Timer(delay, True)
        self._info_timer.connect(self._slot_timer_info_later)

    def request_info(self):
        """request the private/Balance object"""
        self.enqueue_http_request("private/Balance", {}, "info")

    def request_volume(self):
        """request trade volume and fee"""
        self.enqueue_http_request("private/TradeVolume", {'pair': self.pair, 'fee-info': True}, "volume")

    def request_orders(self):
        """request the private/OpenOrders object"""
        self.enqueue_http_request("private/OpenOrders", {}, "orders")

    def _http_thread_func(self):
        """send queued http requests to the http API"""
        while not self._terminating:
            try:
                # pop queued request from the queue and process it
                (api_endpoint, params, reqid) = self.http_requests.get(True)
                translated = None

                answer = self.http_signed_call(api_endpoint, params)
                # self.debug("Result: %s" % answer)
                if "result" in answer:
                    # the following will reformat the answer in such a way
                    # that we can pass it directly to signal_recv()
                    # as if it had come directly from the websocket
                    if api_endpoint == 'private/OpenOrders':
                        result = []
                        orders = answer["result"]["open"]
                        for txid in orders:
                            tx = orders[txid]
                            result.append({
                                'oid': txid,
                                'base': "X" + tx['descr']['pair'][0:3],
                                'currency': "X" + tx['descr']['pair'][3:],
                                'status': tx['status'],
                                'type': 'bid' if tx['descr']['type'] == 'buy' else 'ask',
                                'price': float(tx['descr']['price']),
                                'amount': float(tx['vol'])
                            })
                            # self.debug("TX: %s" % result)
                    elif api_endpoint == 'private/TradeVolume':
                        result = {
                            'volume': float(answer['result']['volume']),
                            'currency': answer['result']['currency'],
                            'fee': float(answer['result']['fees_maker'][self.pair]['fee'])
                        }
                    else:
                        result = answer["result"]

                    translated = {
                        "op": "result",
                        "result": result,
                        "id": reqid
                    }
                else:
                    if "error" in answer:
                        if "token" not in answer:
                            answer["token"] = "-"
                        # if answer["token"] == "unknown_error":
                            # enqueue it again, it will eventually succeed.
                            # self.enqueue_http_request(api_endpoint, params, reqid)
                        # else:
                            # these are errors like "Order amount is too low"
                            # or "Order not found" and the like, we send them
                            # to signal_recv() as if they had come from the
                            # streaming API beause Api() can handle these errors.
                        translated = {
                            "op": "remark",
                            "success": False,
                            "message": answer["error"],
                            "token": answer["token"],
                            "id": reqid
                        }

                    else:
                        self.debug("### unexpected http result:", answer, reqid)

                if translated:
                    self.signal_recv(self, (json.dumps(translated)))

                self.http_requests.task_done()

                # Try to prevent going over API rate limiting, especially
                # when cancelling and adding orders all at once
                time.sleep(3)

            except Exception as exc:
                # should this ever happen? HTTP 5xx wont trigger this,
                # something else must have gone wrong, a totally malformed
                # reply or something else.
                #
                # After some time of testing during times of heavy
                # volatility it appears that this happens mostly when
                # there is heavy load on their servers. Resubmitting
                # the API call will then eventally succeed.
                self.debug("### exception in _http_thread_func:", exc)  # , api_endpoint, params, reqid)
                # self.debug(traceback.format_exc())

                # enqueue it again, it will eventually succeed.
                # self.enqueue_http_request(api_endpoint, params, reqid)

        self.debug("Polling terminated...")

    def enqueue_http_request(self, api_endpoint, params, reqid):
        """enqueue a request for sending to the HTTP API, returns
        immediately, behaves exactly like sending it over the websocket."""
        if self.secret and self.secret.know_secret():
            self.http_requests.put((api_endpoint, params, reqid), True, 10)

    def http_signed_call(self, api_endpoint, params):
        """send a signed request to the HTTP API V2"""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        params["nonce"] = self.get_unique_microtime()

        urlpath = "/0/" + api_endpoint
        post = urlencode(params)
        message = urlpath + hashlib.sha256(str(params["nonce"]) + post).digest()
        sign = hmac.new(base64.b64decode(sec), message, hashlib.sha512).digest()

        headers = {
            'API-Key': key,
            'API-Sign': base64.b64encode(sign)
        }

        url = "%s://%s/0/%s" % (
            self.proto,
            HTTP_HOST,
            api_endpoint
        )

        # self.debug("### (%s) calling %s" % (proto, url))
        try:
            result = json.loads(http_request(url, post, headers))
            return result
        except ValueError as exc:
            self.debug("### exception in http_signed_call:", exc)

    def send_order_add(self, typ, price, volume):
        """send an order"""
        reqid = "order_add:%s:%f:%f" % (typ, price, volume)
        self.debug("Sending %s" % reqid)
        typ = "sell" if typ == "ask" else "buy"
        if price > 0:
            params = {
                "pair": self.pair,
                "type": typ,
                "ordertype": "limit",
                "price": str(price),
                "volume": str(volume)
            }
        else:
            params = {
                "pair": self.pair,
                "type": typ,
                "ordertype": "market",
                "volume": str(volume)
            }

        api = "private/AddOrder"
        self.enqueue_http_request(api, params, reqid)

    def send_order_cancel(self, txid):
        """cancel an order"""
        params = {"txid": txid}
        reqid = "order_cancel:%s" % txid
        self.debug("Sending %s" % reqid)
        api = "private/CancelOrder"
        self.enqueue_http_request(api, params, reqid)

    def slot_timer_lag(self, _sender, _data):
        """get server time and calculate lag"""
        self.request_lag()

    def slot_timer_info(self, _sender, _data):
        """download info data"""
        self.request_info()

    def slot_timer_ticker(self, _sender, _data):
        """get ticker prices"""
        self.request_ticker()
        # reqid = "ticker"
        # api = "public/Ticker"
        # self.enqueue_http_request(api, {}, reqid)

    def slot_timer_volume(self, _sender, _data):
        """download volume and fee data"""
        self.request_volume()

    def slot_timer_orders(self, _sender, _data):
        """download orders data"""
        self.request_orders()

    def slot_timer_depth(self, _sender, _data):
        """download depth data"""
        if self.config.get_bool("api", "load_fulldepth"):
            if not FORCE_NO_FULLDEPTH:
                self.request_fulldepth()

    def slot_timer_history(self, _sender, _data):
        """download history data"""
        if self.config.get_bool("api", "load_history"):
            if not FORCE_NO_HISTORY:
                self.request_history()
