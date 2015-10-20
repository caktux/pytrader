
import time
import hmac
import Queue
import threading
from api import BaseObject, Signal, Timer, start_thread
from api import USER_AGENT, FORCE_PROTOCOL, FORCE_NO_FULLDEPTH, FORCE_NO_DEPTH, FORCE_NO_LAG, FORCE_NO_HISTORY, FORCE_HTTP_API, FORCE_NO_HTTP_API
from urllib import urlencode
import websocket

SOCKETIO_HOST = "socketio.mtgox.com"
WEBSOCKET_HOST = "websocket.mtgox.com"
HTTP_HOST = "data.mtgox.com"

class BaseClient(BaseObject):
    """Abstract base client class for SocketIOClient and WebsocketClient"""

    _last_unique_microtime = 0
    _nonce_lock = threading.Lock()

    def __init__(self, curr_base, curr_quote, secret, config):
        BaseObject.__init__(self)

        self.signal_recv         = Signal()
        self.signal_fulldepth    = Signal()
        self.signal_fullhistory  = Signal()
        self.signal_connected    = Signal()
        self.signal_disconnected = Signal()

        self._timer = Timer(60)
        self._timer.connect(self.slot_timer)

        self._info_timer = None # used when delayed requesting private/info

        self.curr_base = curr_base
        self.curr_quote = curr_quote

        self.currency = curr_quote # deprecated, use curr_quote instead

        self.secret = secret
        self.config = config
        self.socket = None
        self.http_requests = Queue.Queue()

        self._recv_thread = None
        self._http_thread = None
        self._terminating = False
        self.connected = False
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
        if self.socket:
            self.debug("### closing socket")
            self.socket.sock.close()

    def force_reconnect(self):
        """force client to reconnect"""
        self.socket.close()

    def _try_send_raw(self, raw_data):
        """send raw data to the websocket or disconnect and close"""
        if self.connected:
            try:
                self.socket.send(raw_data)
            except Exception as exc:
                self.debug(exc)
                self.connected = False
                self.socket.close()

    def send(self, json_str):
        """there exist 2 subtly different ways to send a string over a
        websocket. Each client class will override this send method"""
        raise NotImplementedError()

    def get_unique_mirotime(self):
        """produce a unique nonce that is guaranteed to be ever increasing"""
        with self._nonce_lock:
            microtime = int(time.time() * 1E6)
            if microtime <= self._last_unique_microtime:
                microtime = self._last_unique_microtime + 1
            self._last_unique_microtime = microtime
            return microtime

    def use_http(self):
        """should we use http api? return true if yes"""
        use_http = self.config.get_bool("api", "use_http_api")
        if FORCE_HTTP_API:
            use_http = True
        if FORCE_NO_HTTP_API:
            use_http = False
        return use_http

    def use_tonce(self):
        """should we use tonce instead on nonce? tonce is current microtime
        and also works when messages come out of order (which happens at
        the mtgox server in certain siuations). They still have to be unique
        because mtgox will remember all recently used tonce values. It will
        only be accepted when the local clock is +/- 10 seconds exact."""
        return self.config.get_bool("api", "use_tonce")

    def request_fulldepth(self):
        """start the fulldepth thread"""

        def fulldepth_thread():
            """request the full market depth, initialize the order book
            and then terminate. This is called in a separate thread after
            the streaming API has been connected."""
            self.debug("### requesting initial full depth")
            use_ssl = self.config.get_bool("api", "use_ssl")
            proto = {True: "https", False: "http"}[use_ssl]
            fulldepth = http_request("%s://%s/api/2/%s%s/money/depth/full" % (
                proto,
                HTTP_HOST,
                self.curr_base,
                self.curr_quote
            ))
            self.signal_fulldepth(self, (json.loads(fulldepth)))

        start_thread(fulldepth_thread, "http request full depth")

    def request_history(self):
        """request trading history"""

        # Api() will have set this field to the timestamp of the last
        # known candle, so we only request data since this time
        since = self.history_last_candle

        def history_thread():
            """request trading history"""

            # 1308503626, 218868 <-- last small transacion ID
            # 1309108565, 1309108565842636 <-- first big transaction ID

            if since:
                querystring = "?since=%i" % (since * 1000000)
            else:
                querystring = ""

            self.debug("### requesting history")
            use_ssl = self.config.get_bool("api", "use_ssl")
            proto = {True: "https", False: "http"}[use_ssl]
            json_hist = http_request("%s://%s/api/2/%s%s/money/trades%s" % (
                proto,
                HTTP_HOST,
                self.curr_base,
                self.curr_quote,
                querystring
            ))
            history = json.loads(json_hist)
            if history["result"] == "success":
                self.signal_fullhistory(self, history["data"])

        start_thread(history_thread, "http request trade history")

    def _recv_thread_func(self):
        """this will be executed as the main receiving thread, each type of
        client (websocket or socketio) will implement its own"""
        raise NotImplementedError()

    def channel_subscribe(self, download_market_data=True):
        """subscribe to needed channnels and download initial data (orders,
        account info, depth, history, etc. Some of these might be redundant but
        at the time I wrote this code the socketio server seemed to have a bug,
        not being able to subscribe via the GET parameters, so I send all
        needed subscription requests here again, just to be on the safe side."""

        symb = "%s%s" % (self.curr_base, self.curr_quote)
        if not FORCE_NO_DEPTH:
            self.send(json.dumps({"op":"mtgox.subscribe", "channel":"depth.%s" % symb}))
        self.send(json.dumps({"op":"mtgox.subscribe", "channel":"ticker.%s" % symb}))

        # trades and lag are the same channels for all currencies
        self.send(json.dumps({"op":"mtgox.subscribe", "type":"trades"}))
        if not FORCE_NO_LAG:
            self.send(json.dumps({"op":"mtgox.subscribe", "type":"lag"}))

        self.request_idkey()
        self.request_orders()
        self.request_info()

        if download_market_data:
            if self.config.get_bool("api", "load_fulldepth"):
                if not FORCE_NO_FULLDEPTH:
                    self.request_fulldepth()

            if self.config.get_bool("api", "load_history"):
                if not FORCE_NO_HISTORY:
                    self.request_history()

        self._time_last_subscribed = time.time()

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
        if self.use_http():
            self.enqueue_http_request("money/info", {}, "info")
        else:
            self.send_signed_call("private/info", {}, "info")

    def request_idkey(self):
        """request the private/idkey object"""
        if self.use_http():
            self.enqueue_http_request("money/idkey", {}, "idkey")
        else:
            self.send_signed_call("private/idkey", {}, "idkey")

    def request_orders(self):
        """request the private/orders object"""
        if self.use_http():
            self.enqueue_http_request("money/orders", {}, "orders")
        else:
            self.send_signed_call("private/orders", {}, "orders")

    def _http_thread_func(self):
        """send queued http requests to the http API (only used when
        http api is forced, normally this is much slower)"""
        while not self._terminating:
            # pop queued request from the queue and process it
            (api_endpoint, params, reqid) = self.http_requests.get(True)
            translated = None
            try:
                answer = self.http_signed_call(api_endpoint, params)
                if answer["result"] == "success":
                    # the following will reformat the answer in such a way
                    # that we can pass it directly to signal_recv()
                    # as if it had come directly from the websocket
                    translated = {
                        "op": "result",
                        "result": answer["data"],
                        "id": reqid
                    }
                else:
                    if "error" in answer:
                        if answer["token"] == "unknown_error":
                            # enqueue it again, it will eventually succeed.
                            self.enqueue_http_request(api_endpoint, params, reqid)
                        else:

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

            except Exception as exc:
                # should this ever happen? HTTP 5xx wont trigger this,
                # something else must have gone wrong, a totally malformed
                # reply or something else.
                #
                # After some time of testing during times of heavy
                # volatility it appears that this happens mostly when
                # there is heavy load on their servers. Resubmitting
                # the API call will then eventally succeed.
                self.debug("### exception in _http_thread_func:",
                    exc, api_endpoint, params, reqid)

                # enqueue it again, it will eventually succeed.
                self.enqueue_http_request(api_endpoint, params, reqid)

            if translated:
                self.signal_recv(self, (json.dumps(translated)))

            self.http_requests.task_done()

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

        if self.use_tonce():
            params["tonce"] = self.get_unique_mirotime()
        else:
            params["nonce"] = self.get_unique_mirotime()

        post = urlencode(params)
        prefix = api_endpoint + chr(0)
        # pylint: disable=E1101
        sign = hmac.new(base64.b64decode(sec), prefix + post, hashlib.sha512).digest()

        headers = {
            'Rest-Key': key,
            'Rest-Sign': base64.b64encode(sign)
        }

        use_ssl = self.config.get_bool("api", "use_ssl")
        proto = {True: "https", False: "http"}[use_ssl]
        url = "%s://%s/api/2/%s" % (
            proto,
            HTTP_HOST,
            api_endpoint
        )
        self.debug("### (%s) calling %s" % (proto, url))
        return json.loads(http_request(url, post, headers))

    def send_signed_call(self, api_endpoint, params, reqid):
        """send a signed (authenticated) API call over the socket.io.
        This method will only succeed if the secret key is available,
        otherwise it will just log a warning and do nothing."""
        if (not self.secret) or (not self.secret.know_secret()):
            self.debug("### don't know secret, cannot call %s" % api_endpoint)
            return

        key = self.secret.key
        sec = self.secret.secret

        call = {
            "id"       : reqid,
            "call"     : api_endpoint,
            "params"   : params,
            "currency" : self.curr_quote,
            "item"     : self.curr_base
        }
        if self.use_tonce():
            call["tonce"] = self.get_unique_mirotime()
        else:
            call["nonce"] = self.get_unique_mirotime()
        call = json.dumps(call)

        # pylint: disable=E1101
        sign = hmac.new(base64.b64decode(sec), call, hashlib.sha512).digest()
        signedcall = key.replace("-", "").decode("hex") + sign + call

        self.debug("### (socket) calling %s" % api_endpoint)
        self.send(json.dumps({
            "op"      : "call",
            "call"    : base64.b64encode(signedcall),
            "id"      : reqid,
            "context" : "mtgox.com"
        }))

    def send_order_add(self, typ, price, volume):
        """send an order"""
        reqid = "order_add:%s:%d:%d" % (typ, price, volume)
        if price > 0:
            params = {"type": typ, "price_int": price, "amount_int": volume}
        else:
            params = {"type": typ, "amount_int": volume}

        if self.use_http():
            api = "%s%s/money/order/add" % (self.curr_base , self.curr_quote)
            self.enqueue_http_request(api, params, reqid)
        else:
            api = "order/add"
            self.send_signed_call(api, params, reqid)

    def send_order_cancel(self, oid):
        """cancel an order"""
        params = {"oid": oid}
        reqid = "order_cancel:%s" % oid
        if self.use_http():
            api = "money/order/cancel"
            self.enqueue_http_request(api, params, reqid)
        else:
            api = "order/cancel"
            self.send_signed_call(api, params, reqid)

    def on_idkey_received(self, data):
        """id key was received, subscribe to private channel"""
        self.send(json.dumps({"op":"mtgox.subscribe", "key":data}))

    def slot_timer(self, _sender, _data):
        """check timeout (last received, dead socket?)"""
        if self.connected:
            if time.time() - self._time_last_received > 60:
                self.debug("### did not receive anything for a long time, disconnecting.")
                self.force_reconnect()
                self.connected = False
            if time.time() - self._time_last_subscribed > 1800:
                # sometimes after running for a few hours it
                # will lose some of the subscriptons for no
                # obvious reason. I've seen it losing the trades
                # and the lag channel channel already, and maybe
                # even others. Simply subscribing again completely
                # fixes this condition. For this reason we renew
                # all channel subscriptions once every hour.
                self.debug("### refreshing channel subscriptions")
                self.channel_subscribe(False)


class WebsocketClient(BaseClient):
    """this implements a connection to MtGox through the websocket protocol."""
    def __init__(self, curr_base, curr_quote, secret, config):
        BaseClient.__init__(self, curr_base, curr_quote, secret, config)
        self.hostname = WEBSOCKET_HOST

    def _recv_thread_func(self):
        """connect to the websocket and start receiving in an infinite loop.
        Try to reconnect whenever connection is lost. Each received json
        string will be dispatched with a signal_recv signal"""
        reconnect_time = 1
        use_ssl = self.config.get_bool("api", "use_ssl")
        wsp = {True: "wss://", False: "ws://"}[use_ssl]
        port = {True: 443, False: 80}[use_ssl]
        ws_origin = "%s:%d" % (self.hostname, port)
        ws_headers = ["User-Agent: %s" % USER_AGENT]
        while not self._terminating:  #loop 0 (connect, reconnect)
            try:
                # channels separated by "/", wildcards allowed. Available
                # channels see here: https://mtgox.com/api/2/stream/list_public
                # example: ws://websocket.mtgox.com/?Channel=depth.LTCEUR/ticker.LTCEUR
                # the trades and lag channel will be subscribed after connect
                sym = "%s%s" % (self.curr_base, self.curr_quote)
                if not FORCE_NO_DEPTH:
                    ws_url = "%s%s?Channel=depth.%s/ticker.%s" % \
                    (wsp, self.hostname, sym, sym)
                else:
                    ws_url = "%s%s?Channel=ticker.%s" % \
                    (wsp, self.hostname, sym)
                self.debug("### trying plain old Websocket: %s ... " % ws_url)

                self.socket = websocket.WebSocket()
                # The server is somewhat picky when it comes to the exact
                # host:port syntax of the origin header, so I am supplying
                # my own origin header instead of the auto-generated one
                self.socket.connect(ws_url, origin=ws_origin, header=ws_headers)
                self._time_last_received = time.time()
                self.connected = True
                self.debug("### connected, subscribing needed channels")
                self.channel_subscribe()
                self.debug("### waiting for data...")
                self.signal_connected(self, None)
                while not self._terminating: #loop1 (read messages)
                    str_json = self.socket.recv()
                    self._time_last_received = time.time()
                    if str_json[0] == "{":
                        self.signal_recv(self, (str_json))

            except Exception as exc:
                self.connected = False
                self.signal_disconnected(self, None)
                if not self._terminating:
                    self.debug("### ", exc.__class__.__name__, exc,
                        "reconnecting in %i seconds..." % reconnect_time)
                    if self.socket:
                        self.socket.close()
                    time.sleep(reconnect_time)

    def send(self, json_str):
        """send the json encoded string over the websocket"""
        self._try_send_raw(json_str)


class SocketIO(websocket.WebSocket):
    """This is the WebSocket() class with added Super Cow Powers. It has a
    different connect method so that it can connect to socket.io. It will do
    the initial HTTP request with keep-alive and then use that same socket
    to upgrade to websocket"""
    def __init__(self, get_mask_key = None):
        websocket.WebSocket.__init__(self, get_mask_key)

    def connect(self, url, **options):
        """connect to socketio and then upgrade to websocket transport. Example:
        connect('wss://websocket.mtgox.com/socket.io/1', query='Currency=EUR')"""

        def read_block(sock):
            """read from the socket until empty line, return list of lines"""
            lines = []
            line = ""
            while True:
                res = sock.recv(1)
                line += res
                if res == "":
                    return None
                if res == "\n":
                    line = line.strip()
                    if line == "":
                        return lines
                    lines.append(line)
                    line = ""

        # pylint: disable=W0212
        hostname, port, resource, is_secure = websocket._parse_url(url)
        self.sock.connect((hostname, port))
        if is_secure:
            self.io_sock = websocket._SSLSocketWrapper(self.sock)

        path_a = resource
        if "query" in options:
            path_a += "?" + options["query"]
        self.io_sock.send("GET %s HTTP/1.1\r\n" % path_a)
        self.io_sock.send("Host: %s:%d\r\n" % (hostname, port))
        self.io_sock.send("User-Agent: %s\r\n" % USER_AGENT)
        self.io_sock.send("Accept: text/plain\r\n")
        self.io_sock.send("Connection: keep-alive\r\n")
        self.io_sock.send("\r\n")

        headers = read_block(self.io_sock)
        if not headers:
            raise IOError("disconnected while reading headers")
        if not "200" in headers[0]:
            raise IOError("wrong answer: %s" % headers[0])
        result = read_block(self.io_sock)
        if not result:
            raise IOError("disconnected while reading socketio session ID")
        if len(result) != 3:
            raise IOError("invalid response from socket.io server")

        ws_id = result[1].split(":")[0]
        resource = "%s/websocket/%s" % (resource, ws_id)
        if "query" in options:
            resource = "%s?%s" % (resource, options["query"])

        # now continue with the normal websocket GET and upgrade request
        self._handshake(hostname, port, resource, **options)


class SocketIOClient(BaseClient):
    """this implements a connection to MtGox using the socketIO protocol."""

    def __init__(self, curr_base, curr_quote, secret, config):
        BaseClient.__init__(self, curr_base, curr_quote, secret, config)
        self.hostname = SOCKETIO_HOST
        self._timer.connect(self.slot_keepalive_timer)

    def _recv_thread_func(self):
        """this is the main thread that is running all the time. It will
        connect and then read (blocking) on the socket in an infinite
        loop. SocketIO messages ('2::', etc.) are handled here immediately
        and all received json strings are dispathed with signal_recv."""
        use_ssl = self.config.get_bool("api", "use_ssl")
        wsp = {True: "wss://", False: "ws://"}[use_ssl]
        while not self._terminating: #loop 0 (connect, reconnect)
            try:
                url = "%s%s/socket.io/1" % (wsp, self.hostname)

                # subscribing depth and ticker through the querystring,
                # the trade and lag will be subscribed later after connect
                sym = "%s%s" % (self.curr_base, self.curr_quote)
                if not FORCE_NO_DEPTH:
                    querystring = "Channel=depth.%s/ticker.%s" % (sym, sym)
                else:
                    querystring = "Channel=ticker.%s" % (sym)
                self.debug("### trying Socket.IO: %s?%s ..." % (url, querystring))
                self.socket = SocketIO()
                self.socket.connect(url, query=querystring)

                self._time_last_received = time.time()
                self.connected = True
                self.debug("### connected")
                self.socket.send("1::/mtgox")

                self.debug(self.socket.recv())
                self.debug(self.socket.recv())

                self.debug("### subscribing to channels")
                self.channel_subscribe()

                self.debug("### waiting for data...")
                self.signal_connected(self, None)
                while not self._terminating: #loop1 (read messages)
                    msg = self.socket.recv()
                    self._time_last_received = time.time()
                    if msg == "2::":
                        #self.debug("### ping -> pong")
                        self.socket.send("2::")
                        continue
                    prefix = msg[:10]
                    if prefix == "4::/mtgox:":
                        str_json = msg[10:]
                        if str_json[0] == "{":
                            self.signal_recv(self, (str_json))

            except Exception as exc:
                self.connected = False
                self.signal_disconnected(self, None)
                if not self._terminating:
                    self.debug("### ", exc.__class__.__name__, exc, \
                        "reconnecting in 1 seconds...")
                    self.socket.close()
                    time.sleep(1)

    def send(self, json_str):
        """send a string to the websocket. This method will prepend it
        with the 1::/mtgox: that is needed for the socket.io protocol
        (as opposed to plain websockts) and the underlying websocket
        will then do the needed framing on top of that."""
        self._try_send_raw("4::/mtgox:" + json_str)

    def slot_keepalive_timer(self, _sender, _data):
        """send a keepalive, just to make sure our socket is not dead"""
        if self.connected:
            #self.debug("### sending keepalive")
            self._try_send_raw("2::")
