# -*- coding: utf-8 -*-
""" Poloniex Client """

import json
import time
import hmac
import Queue
import base64
import hashlib
import threading
import traceback
from api import BaseObject, Signal, Timer, start_thread, http_request
from urllib import urlencode
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from autobahn.twisted.wamp import ApplicationSession, ApplicationRunner
import HTMLParser
html_parser = HTMLParser.HTMLParser()

WEBSOCKET_HOST = "api.poloniex.com"
HTTP_HOST = "poloniex.com"

class PoloniexComponent(ApplicationSession):

    def onLeave(self, details):
        self.disconnect()

    def onDisconnect(self):
        client = self.config.extra['client']
        if client.reconnect:
            client.reconnect = False
            client.run()
        else:
            reactor.stop()

    def onConnect(self):
        client = self.config.extra['client']
        client.debug("### connected, subscribing needed channels")
        client.connected = True
        client.leave = self.leave

        client.signal_connected(self, None)

        client.request_fulldepth()
        client.request_history()

        client._time_last_subscribed = time.time()

        self.join(self.config.realm)

    @inlineCallbacks
    def onJoin(self, details):
        client = self.config.extra['client']

        def onTicker(*args):
            try:
                if not client._terminating and args[0] == client.pair:
                    client._time_last_received = time.time()
                    # print("Ticker event received:", args)

                    translated = {
                        "op": "ticker",
                        "ticker": {
                            'bid': float(args[3]),
                            'ask': float(args[2])
                        }
                    }
                    client.signal_recv(client, translated)
            except Exception as exc:
                client.debug("onTicker exception:", exc)
                client.debug(traceback.format_exc())

        def onBookUpdate(*args):
            try:
                if not client._terminating:
                    data = args[0]
                    # print("BookUpdate event:", data)
                    if data['type'] in ('orderBookRemove', 'orderBookModify'):
                        timestamp = time.time()
                        translated = {
                            'op': 'depth',
                            'depth': {
                                'type': data['data']['type'],
                                'price': float(data['data']['rate']),
                                'volume': float(data['data']['amount']) if data['type'] == 'orderBookModify' else 0,
                                'timestamp': timestamp
                            },
                            'id': "depth"
                        }
                        client.signal_recv(client, translated)

                    elif data['type'] == 'newTrade':
                        # {
                        #     data: {
                        #         tradeID: '364476',
                        #         rate: '0.00300888',
                        #         amount: '0.03580906',
                        #         date: '2014-10-07 21:51:20',
                        #         total: '0.00010775',
                        #         type: 'sell'
                        #     },
                        #     type: 'newTrade'
                        # }
                        data = data['data']
                        client.debug("newTrade:", data)
                        translated = {
                            'op': 'trade',
                            'trade': {
                                'id': data['tradeID'],
                                'type': 'ask' if data['type'] == 'buy' else 'bid',
                                'price': data['rate'],
                                'amount': data['amount'],
                                'timestamp': time.mktime(time.strptime(data['date'], "%Y-%m-%d %H:%M:%S"))
                            }
                        }
                        client.signal_recv(client, translated)
                    else:
                        client.debug("Unknown trade event:", args)

            except Exception as exc:
                client.debug("onBookUpdate exception:", exc)
                client.debug(traceback.format_exc())

        def onTrollbox(*args):
            try:
                if not client._terminating:
                    # print("troll:", args)
                    # msg = args[0]
                    if len(args) == 5:
                        translated = {
                            "op": "chat",
                            "msg": {
                                'type': args[0],
                                'user': args[2],
                                'msg': html_parser.unescape(args[3]),
                                'rep': args[4]
                            }
                        }
                    else:
                        translated = {
                            "op": "chat",
                            "msg": {
                                "type": args[0],
                                "user": args[1],
                                "msg": "-",
                                "rep": "-"
                            }
                        }
                    client.signal_recv(client, translated)
            except Exception as exc:
                client.debug("onTrollbox exception:", exc)
                client.debug(traceback.format_exc())

        try:
            yield self.subscribe(onBookUpdate, client.pair)
            yield self.subscribe(onTicker, 'ticker')
            yield self.subscribe(onTrollbox, 'trollbox')
        except Exception as exc:
            client.debug("Could not subscribe to topic:", exc)
            client.connected = False
            client.signal_disconnected(client, None)

            if not client._terminating:
                client.debug("### ", exc.__class__.__name__, exc,
                             "reconnecting in %i seconds..." % 1)
                client.force_reconnect()

class BaseClient(BaseObject):
    """Abstract base client class for WebsocketClient"""

    _last_unique_microtime = 0
    _nonce_lock = threading.Lock()

    def __init__(self, curr_base, curr_quote, secret, config):
        # PoloniexComponent.__init__(self, curr_base, curr_quote)

        self.signal_recv = Signal()
        self.signal_ticker = Signal()
        self.signal_connected = Signal()
        self.signal_disconnected = Signal()
        self.signal_fulldepth = Signal()
        self.signal_fullhistory = Signal()

        self._timer = Timer(60)
        self._timer_history = Timer(30)

        self._timer.connect(self.slot_timer)
        self._timer_history.connect(self.slot_history)

        self._info_timer = None  # used when delayed requesting private/info

        self.curr_base = curr_base
        self.curr_quote = curr_quote
        self.pair = "%s_%s" % (curr_quote, curr_base)

        self.currency = curr_quote  # deprecated, use curr_quote instead

        self.secret = secret
        self.config = config
        self.socket = None

        use_ssl = self.config.get_bool("api", "use_ssl")
        self.proto = {True: "https", False: "http"}[use_ssl]
        self.http_requests = Queue.Queue()

        self._recv_thread = None
        self._http_thread = None
        self._terminating = False
        self.reconnect = False
        self.connected = False
        self.leave = None
        self._time_last_received = 0
        self._time_last_subscribed = 0
        self.history_last_candle = None

    def start(self):
        """start the client"""
        self._recv_thread = start_thread(self._recv_thread_func, "socket receive thread")
        self._http_thread = start_thread(self._http_thread_func, "http thread")

    def stop(self):
        """stop the client"""
        self._terminating = True
        self._timer.cancel()
        self._timer_history.cancel()
        self.debug("### stopping reactor")
        try:
            self.leave()
        except Exception as exc:
            self.debug("Reactor exception:", exc)

    def force_reconnect(self):
        """force client to reconnect"""
        try:
            self.reconnect = True
            self.leave()
        except Exception as exc:
            self.debug("Reactor exception:", exc)
            self.debug(traceback.format_exc())

    def _try_send_raw(self, raw_data):
        """send raw data to the websocket or disconnect and close"""
        if self.connected:
            try:
                self.debug("TODO - Would send: %s" % raw_data)
                # self.socket.send(raw_data)
            except Exception as exc:
                self.debug(exc)
                # self.connected = False

    def send(self, json_str):
        """there exist 2 subtly different ways to send a string over a
        websocket. Each client class will override this send method"""
        raise NotImplementedError()

    def get_unique_mirotime(self):
        """produce a unique nonce that is guaranteed to be ever increasing"""
        with self._nonce_lock:
            microtime = int(time.time() * 1e6)
            if microtime <= self._last_unique_microtime:
                microtime = self._last_unique_microtime + 1
            self._last_unique_microtime = microtime
            return microtime

    def request_fulldepth(self):
        """start the fulldepth thread"""

        def fulldepth_thread():
            """request the full market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            # self.debug("### requesting full depth")
            json_depth = http_request("%s://%s/public?command=returnOrderBook&currencyPair=%s&depth=500" % (
                self.proto,
                HTTP_HOST,
                self.pair
            ))
            if json_depth and not self._terminating:
                try:
                    fulldepth = json.loads(json_depth)

                    # self.debug("Depth: %s" % fulldepth)

                    depth = {}
                    depth['error'] = {}

                    if 'error' in fulldepth:
                        depth['error'] = fulldepth['error']

                    depth['data'] = {'asks': [], 'bids': []}

                    for ask in fulldepth['asks']:
                        depth['data']['asks'].append({
                            'price': float(ask[0]),
                            'amount': float(ask[1])
                        })
                    for bid in reversed(fulldepth['bids']):
                        depth['data']['bids'].append({
                            'price': float(bid[0]),
                            'amount': float(bid[1])
                        })

                    self.signal_fulldepth(self, depth)
                except Exception as exc:
                    self.debug("### exception in fulldepth_thread:", exc)

        start_thread(fulldepth_thread, "http request full depth")

    def request_history(self):
        """request trading history"""
        # Api() will have set this field to the timestamp of the last
        # known candle, so we only request data since this time
        # since = self.history_last_candle

        def history_thread():
            """request trading history"""

            if not self.history_last_candle:
                querystring = "&start=%i&end=%i" % ((time.time() - 172800), (time.time() - 86400))
                # self.debug("### requesting 2d history since %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 172800)))
            else:
                querystring = "&start=%i" % (self.history_last_candle - 14400)
                # self.debug("Last candle: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.history_last_candle - 14400)))

            json_hist = http_request("%s://%s/public?command=returnTradeHistory&currencyPair=%s%s" % (
                self.proto,
                HTTP_HOST,
                self.pair,
                querystring
            ))
            if json_hist and not self._terminating:
                try:
                    raw_history = json.loads(json_hist)

                    # self.debug("History: %s" % raw_history)

                    history = []
                    for h in reversed(raw_history):
                        history.append({
                            'price': float(h['rate']),
                            'amount': float(h['amount']),
                            'date': time.mktime(time.strptime(h['date'], "%Y-%m-%d %H:%M:%S")) - 480
                        })

                    # self.debug("History: %s" % history)

                    if history and not self._terminating:
                        self.signal_fullhistory(self, history)
                except Exception as exc:
                    self.debug("### exception in history_thread:", exc)

        start_thread(history_thread, "http request trade history")

    def _recv_thread_func(self):
        """this will be executed as the main receiving thread, each type of
        client (websocket or socketio) will implement its own"""
        raise NotImplementedError()

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
        """request the private/info object"""
        self.enqueue_http_request("tradingApi", {'command': 'returnBalances'}, "info")

    def request_orders(self):
        """request the private/orders object"""
        self.enqueue_http_request("tradingApi", {'command': 'returnOpenOrders'}, "orders")

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
                # time.sleep(3)

            except Exception as exc:
                # should this ever happen? HTTP 5xx wont trigger this,
                # something else must have gone wrong, a totally malformed
                # reply or something else.
                #
                # After some time of testing during times of heavy
                # volatility it appears that this happens mostly when
                # there is heavy load on their servers. Resubmitting
                # the API call will then eventally succeed.
                self.debug("### exception in _http_thread_func:", exc)
                # self.debug(traceback.format_exc())

                # enqueue it again, it will eventually succeed.
                # self.enqueue_http_request(api_endpoint, params, reqid)

        self.debug("Polling terminated...")

    def enqueue_http_request(self, api_endpoint, params, reqid):
        """enqueue a request for sending to the HTTP API, returns
        immediately, behaves exactly like sending it over the websocket."""
        if self.secret and self.secret.know_secret():
            self.http_requests.put((api_endpoint, params, reqid))

    def http_signed_call(self, api_endpoint, params):
        """send a signed request to the HTTP API V2"""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        params["nonce"] = self.get_unique_mirotime()

        post = urlencode(params)
        # prefix = api_endpoint
        # sign = hmac.new(base64.b64decode(sec), prefix + post, hashlib.sha512).digest()
        sign = hmac.new(sec, post, hashlib.sha512).hexdigest()

        headers = {
            'Key': key,
            'Sign': base64.b64encode(sign)
        }

        url = "%s://%s/%s" % (
            self.proto,
            HTTP_HOST,
            api_endpoint
        )
        # self.debug("### (%s) calling %s" % (self.proto, url))
        try:
            result = json.loads(http_request(url, post, headers))
            return result
        except ValueError as exc:
            self.debug("### exception in http_signed_call:", exc)

    def send_order_add(self, typ, price, volume):
        """send an order"""
        reqid = "order_add:%s:%f:%f" % (typ, price, volume)
        api = 'tradingApi'
        params = {
            'currencyPair': self.pair,
            'rate': price,
            'amount': volume
        }
        if typ == 'bid':
            params['command'] = 'buy'
        else:
            params['command'] = 'sell'

        self.enqueue_http_request(api, params, reqid)

    def send_order_cancel(self, oid):
        """cancel an order"""
        reqid = "order_cancel:%s" % oid
        api = "tradingApi"
        params = {
            "command": "cancelOrder",
            "orderNumber": oid
        }
        self.enqueue_http_request(api, params, reqid)

    def slot_timer(self, _sender, _data):
        """check timeout (last received, dead socket?)"""
        if self.connected:
            if time.time() - self._time_last_received > 60:
                self.debug("### did not receive anything for a long time, disconnecting.")
                self.force_reconnect()
                self.connected = False
            # if time.time() - self._time_last_subscribed > 1800:
            # sometimes after running for a few hours it
            # will lose some of the subscriptions for no
            # obvious reason. I've seen it losing the trades
            # and the lag channel already, and maybe
            # even others. Simply subscribing again completely
            # fixes this condition. For this reason we renew
            # all channel subscriptions once every half hour.
            # self.channel_subscribe(True)
        # self.debug("### refreshing depth chart")
        self.request_fulldepth()
        # self.debug("### refreshing depth chart")
        self.request_history()

    def slot_history(self, _sender, _data):
        """request history"""
        self.request_history()


class WebsocketClient(BaseClient):
    """this implements a connection through the websocket protocol."""
    def __init__(self, curr_base, curr_quote, secret, config):
        # runner_config = {'url': u"wss://api.poloniex.com", 'realm': u"realm1"}
        BaseClient.__init__(self, curr_base, curr_quote, secret, config)
        self.hostname = WEBSOCKET_HOST
        self.signal_debug = Signal()

    def run(self):
        self.runner = ApplicationRunner(url=u"wss://api.poloniex.com", realm=u"realm1", extra={'client': self})
        self.runner.run(PoloniexComponent, start_reactor=False)

    def _recv_thread_func(self):
        """connect to the websocket and start receiving in an infinite loop.
        Try to reconnect whenever connection is lost. Each received json
        string will be dispatched with a signal_recv signal"""

        try:
            self.run()
            reactor.run(installSignalHandlers=0)
        except Exception as exc:
            self.debug("Reactor exception:", exc)
            self.debug(traceback.format_exc())

    def send(self, json_str):
        """send the json encoded string over the websocket"""
        self._try_send_raw(json_str)
