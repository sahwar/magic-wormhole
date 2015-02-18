import os, threading, socket, SocketServer
from binascii import hexlify
from ..util import ipaddrs
from ..util.hkdf import HKDF

class TransitError(Exception):
    pass

class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass

# The beginning of each TCP connection consists of the following handshake
# messages. The sender transmits the same text regardless of whether it is on
# the initiating/connecting end of the TCP connection, or on the
# listening/accepting side. Same for the receiver.
#
#  sender -> receiver: transit sender TXID_HEX ready\n\n
#  receiver -> sender: transit receiver RXID_HEX ready\n\n
#
# Any deviations from this result in the socket being closed. The handshake
# messages are designed to provoke an invalid response from other sorts of
# servers (HTTP, SMTP, echo).
#
# If the sender is satisfied with the handshake, and this is the first socket
# to complete negotiation, the sender does:
#
#  sender -> receiver: go\n
#
# and the next byte on the wire will be from the application.
#
# If this is not the first socket, the sender does:
#
#  sender -> receiver: nevermind\n
#
# and closes the socket.

# So the receiver looks for "transit sender TXID_HEX ready\n\ngo\n" and hangs
# up upon the first wrong byte. The sender lookgs for "transit receiver
# RXID_HEX ready\n\n" and then makes a first/not-first decision about sending
# "go\n" or "nevermind\n"+close().

def build_receiver_handshake(key):
    return "rx\n\n"
    hexid = HKDF(key, 32, CTXinfo=b"transit_receiver")
    return "transit receiver %s ready\n\n" % hexlify(hexid)

def build_sender_handshake(key):
    return "tx\n\n"
    hexid = HKDF(key, 32, CTXinfo=b"transit_sender")
    return "transit sender %s ready\n\n" % hexlify(hexid)

# 1: sender only transmits, receiver only accepts, both wait forever
# 2: sender also accepts, receiver also transmits
# 3: timeouts / stop when no more progress can be made
# 4: add relay
# 5: accelerate shutdown of losing sockets

class TransitSender:
    def __init__(self):
        self.key = os.urandom(32)
        self.winning = threading.Event()
        self._negotiation_check_lock = threading.Lock()
    def get_transit_key(self):
        return self.key
    def get_direct_hints(self):
        return []
    def get_relay_hints(self):
        return []
    def add_receiver_hints(self, hints):
        self.receiver_hints = hints

    def establish_connection(self):
        sender_handshake = build_sender_handshake(self.key)
        receiver_handshake = build_receiver_handshake(self.key)
        self.listener = None
        self.connectors = []
        self.winning_skt = None
        for hint in self.receiver_hints:
            t = threading.Thread(target=connector,
                                 args=(self, hint,
                                       sender_handshake, receiver_handshake))
            t.start()

        # we sit here until one of our inbound or outbound sockets succeeds
        timeout = 10.0
        flag = self.winning.wait(timeout)

        if not flag:
            # timeout: self.winning_skt will not be set. ish. race.
            pass
        if self.listener:
            self.listener.shutdown() # does this wait? if so, push to thread
        if self.winning_skt:
            return self.winning_skt
        raise TransitError

    def _negotiation_finished(self, skt):
        # inbound/outbound sockets call this when they finish negotiation.
        # The first one wins and gets a "go". Any subsequent ones lose and
        # get a "nevermind" before being closed.

        with self._negotiation_check_lock:
            if self.winning_skt:
                winner = False
            else:
                winner = True
                self.winning_skt = skt

        if winner:
            winner.send("go\n")
            self.winning.set()
        else:
            winner.send("nevermind\n")
            winner.close()

class BadHandshake(Exception):
    pass

def connector(owner, hint, send_handshake, expected_handshake):
    addr,port = hint.split(",")
    skt = socket.create_connection((addr,port)) # timeout here
    skt.settimeout(5.0)
    print "socket(%s) connected" % hint
    try:
        skt.send(send_handshake)
        got = b""
        while len(got) < len(expected_handshake):
            got += skt.recv(1)
            if expected_handshake[:len(got)] != got:
                raise BadHandshake("got '%r' want '%r' on %s" %
                                   (got, expected_handshake, hint))
        print "connector ready", hint
    except:
        try:
            skt.shutdown(socket.SHUT_WR)
        except socket.error:
            pass
        skt.close()
        raise
    # owner is now responsible for the socket
    owner._negotiation_finished(skt) # note thread



def handle(skt, client_address, owner, send_handshake, expected_handshake):
    try:
        print "handle", skt
        skt.settimeout(5.0)
        skt.send(send_handshake)
        got = b""
        # for the receiver, this includes the "ok\n"
        while len(got) < len(expected_handshake):
            got += skt.recv(1)
            if expected_handshake[:len(got)] != got:
                raise BadHandshake("got '%r' want '%r'" %
                                   (got, expected_handshake))
        print "handler negotiation finished", client_address
    except:
        try:
            skt.shutdown(socket.SHUT_WR)
        except socket.error:
            pass
        skt.close()
        raise
    # owner is now responsible for the socket
    owner._negotiation_finished(skt) # note thread

class MyTCPServer(SocketServer.TCPServer):
    allow_reuse_address = True
    def process_request(self, request, client_address):
        if not self.owner.key:
            raise BadHandshake("connection received before set_key()")
        t = threading.Thread(target=handle,
                             args=(request, client_address,
                                   self.owner,
                                   self.owner.handler_send_handshake,
                                   self.owner.handler_expected_handshake))
        t.daemon = False
        t.start()

class TransitReceiver:
    def __init__(self):
        server = MyTCPServer(("",9999), None)
        _, port = server.server_address
        self.my_direct_hints = ["%s,%d" % (addr, port)
                                for addr in ipaddrs.find_addresses()]
        server.owner = self
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        self.listener = server

    def get_direct_hints(self):
        return self.my_direct_hints
    def set_transit_key(self, key):
        # TODO consider race: sender knows the hints and the key, connects to
        # transit before receiver gets relay message (with key)
        self.key = key
        self.handler_send_handshake = build_receiver_handshake(key)
        self.handler_expected_handshake = build_sender_handshake(key) + "ok\n"

    def add_sender_direct_hints(self, hints):
        self.sender_direct_hints = hints # TODO ignored
    def add_sender_relay_hints(self, hints):
        self.sender_relay_hints = hints # TODO ignored

    def establish_connection(self):
        self.winning_skt = None

        # we sit here until one of our inbound or outbound sockets succeeds
        timeout = 10.0
        flag = self.winning.wait(timeout)

        if not flag:
            # timeout: self.winning_skt will not be set. ish. race.
            pass
        if self.listener:
            self.listener.shutdown() # TODO: waits up to 0.5s. push to thread
        if self.winning_skt:
            return self.winning_skt
        raise TransitError

    def _negotiation_finished(self, skt):
        with self._negotiation_check_lock:
            if self.winning_skt:
                winner = False
            else:
                winner = True
                self.winning_skt = skt

        if winner:
            self.winning.set()
        else:
            winner.close()
            raise BadHandshake("weird, receiver was given duplicate winner")

