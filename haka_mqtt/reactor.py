import errno
import socket
import logging
import ssl
from collections import OrderedDict
from io import BytesIO
import os

from enum import (
    IntEnum,
    unique,
)

from haka_mqtt.clock import SystemClock
from haka_mqtt.packet_ids import PacketIdGenerator
from haka_mqtt.selector import Selector
from mqtt_codec.io import (
    UnderflowDecodeError,
    DecodeError,
    BytesReader,
)
from mqtt_codec.packet import (
    MqttControlPacketType,
    MqttFixedHeader,
    MqttConnect,
    ConnackResult,
    MqttConnack,
    MqttSuback,
    MqttPublish,
    MqttPuback,
    MqttPubrec,
    MqttPubrel,
    MqttPubcomp,
    MqttPingreq,
    MqttPingresp,
    MqttDisconnect,
    MqttWill, MqttUnsuback)
from haka_mqtt.mqtt_request import (
    MqttSubscribeTicket,
    MqttUnsubscribeTicket,
    MqttPublishTicket,
    MqttPublishStatus,
    MqttSubscribeStatus,
)
from haka_mqtt.on_str import HexOnStr, ReprOnStr


class ReactorProperties(object):
    """
    Attributes
    ----------
    socket_factory: haka_mqtt.socket_factory.SocketFactory
    client_id: str
    clock:
    endpoint: tuple
        2-tuple of (host: `str`, port: `int`).  The `port` value is
        constrainted such that 0 <= `port` <= 2**16-1.
    keepalive_period: int
        0 <= keepalive_period <= 2*16-1
    clean_session: bool
        With clean session set to True reactor will clear all message
        buffers on disconnect without regard to QoS; otherwise
        unacknowledged messages will be retransmitted after a
        re-connect.
    name_resolver: callable
        DNS resolver.
    username: str optional
    password: str optional
    selector: Selector
    """
    def __init__(self):
        self.socket_factory = None
        self.endpoint = None
        self.client_id = None
        self.clock = SystemClock()
        self.keepalive_period = 10*60
        self.scheduler = None
        self.clean_session = True
        self.name_resolver = None
        self.username = None
        self.password = None
        self.selector = Selector()


@unique
class MqttState(IntEnum):
    """
    Inactive states are those where there are no active deadlines, the
    socket is closed and there is no active I/O.  Active states are
    those where any of these characteristics is not met.

    Active States:

    * :py:const:`MqttState.connack`
    * :py:const:`MqttState.connected`
    * :py:const:`MqttState.mute`

    Inactive States:

    * :py:const:`MqttState.stopped`

    """
    connack = 0
    connected = 1
    mute = 2
    stopped = 3


ACTIVE_MQTT_STATES = (MqttState.connack, MqttState.connected, MqttState.mute)
INACTIVE_MQTT_STATES = (MqttState.stopped,)
assert set(ACTIVE_MQTT_STATES).union(INACTIVE_MQTT_STATES) == set(iter(MqttState))


@unique
class SocketState(IntEnum):
    """
    Inactive states are those where there are no active deadlines, the
    socket is closed and there is no active I/O.  Active states are
    those where any of these characteristics is not met.

    Active States:

    * :py:const:`SocketState.name_resolution`
    * :py:const:`SocketState.connecting`
    * :py:const:`SocketState.handshake`
    * :py:const:`SocketState.connected`
    * :py:const:`SocketState.deaf`
    * :py:const:`SocketState.mute`

    Inactive States:

    * :py:const:`SocketState.stopped`

    """
    name_resolution = 1
    connecting = 2
    handshake = 3
    connected = 5
    deaf = 6
    mute = 7
    stopped = 8


ACTIVE_SOCKET_STATES = (
    SocketState.name_resolution,
    SocketState.connecting,
    SocketState.handshake,
    SocketState.connected,
    SocketState.mute,
    SocketState.deaf,
)
INACTIVE_SOCK_STATES = (SocketState.stopped,)
assert set(ACTIVE_SOCKET_STATES).union(INACTIVE_SOCK_STATES) == set(iter(SocketState))


@unique
class ReactorState(IntEnum):
    """
    Inactive states are those where there are no active deadlines, the
    socket is closed and there is no active I/O.  Active states are
    those where any of these characteristics is not met.

    Active States:

    * :py:const:`ReactorState.init`
    * :py:const:`ReactorState.stopped`
    * :py:const:`ReactorState.error`

    Inactive States:

    * :py:const:`ReactorState.connecting`
    * :py:const:`ReactorState.handshake`
    * :py:const:`ReactorState.connack`
    * :py:const:`ReactorState.connected`

    """
    init = 0
    starting = 1
    started = 2
    stopping = 3
    stopped = 4
    error = 5

# States where there are no active deadlines, the socket is closed and there
# is no active I/O.
#
INACTIVE_STATES = (ReactorState.init, ReactorState.stopped, ReactorState.error)

# States with active deadlines, open sockets, or pending I/O.
#
ACTIVE_STATES = (
    ReactorState.starting,
    ReactorState.started,
    ReactorState.stopping,
)


assert set(INACTIVE_STATES).union(ACTIVE_STATES) == set(iter(ReactorState))


def get_packet_type(d, packet_id, packet_type):
    """
    Parameters
    ----------
    d: dict
    packet_id: int
    packet_type: MqttControlPacketType

    Returns
    -------
    object
        Mqtt packet with given packet id and type.
    """
    try:
        maybe_packet = d[packet_id]
    except KeyError:
        packet = None
    else:
        packet = maybe_packet if maybe_packet.packet_type is packet_type else None

    return packet


class ReactorError(object):
    def __repr__(self):
        return '{}()'.format(self.__class__.__name__)


class MutePeerReactorError(ReactorError):
    """Error that occurs when the server closes its write stream
    unexpectedly."""

    def __repr__(self):
        return '{}()'.format(self.__class__.__name__)


class ConnectReactorError(ReactorError):
    """Error that occurs when the server sends a connack fail in
    response to an initial connect packet.

    Parameters
    ----------
    result: ConnackResult
        Asserted not to be `ConnackResult.accepted`.
    """
    def __init__(self, result):
        assert result != ConnackResult.accepted
        self.__result = result

    @property
    def result(self):
        """ConnackResult: guaranteed that value is not `ConnackResult.accepted`."""
        return self.__result

    def __eq__(self, other):
        return hasattr(other, 'result') and self.result == other.result

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, self.result)


class KeepaliveTimeoutReactorError(ReactorError):
    """Server fails to respond in a timely fashion."""
    def __eq__(self, other):
        return isinstance(other, KeepaliveTimeoutReactorError)

    def __repr__(self):
        return '{}()'.format(self.__class__.__name__)


class SocketReactorError(ReactorError):
    """A socket error-code in `errno.errorcode`.

    Parameters
    ----------
    errno_val: int
        Asserted to be in `errno.errorcode`.
    """
    def __init__(self, errno_val):
        assert errno_val in errno.errorcode, errno_val

        self.__errno = errno_val

    @property
    def errno(self):
        """int: value in `errno.errorcode`."""
        return self.__errno

    def __repr__(self):
        return 'SocketError(<{}: {}>)'.format(errno.errorcode[self.errno], self.errno)

    def __eq__(self, other):
        return self.errno == other.errno


class AddressReactorError(ReactorError):
    """Failed to lookup a valid address.

    Parameters
    ----------
    gaierror: socket.gaierror
    """

    def __init__(self, gaierror):
        assert isinstance(gaierror, socket.gaierror)
        self.__gaierror = gaierror

    @property
    def gaierror(self):
        """socket.gaierror: Addressing error."""
        return self.__gaierror

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, repr(self.gaierror))

    def __eq__(self,  other):
        return (
            hasattr(other, 'gaierror')
            and self.gaierror.errno == other.gaierror.errno
            and hasattr(other, 'gaierror')
            and self.gaierror.message == other.gaierror.message
        )


class DecodeReactorError(ReactorError):
    """Server wrote a sequence of bytes that could not be interpreted as
    an MQTT packet."""
    def __init__(self, description):
        self.description = description

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__, self.description)


class ProtocolReactorError(ReactorError):
    """Server send an inappropriate MQTT packet to the client."""
    def __init__(self, description):
        self.description = description


class _AssertSelectAdapter(object):
    def __init__(self, reactor, selector):
        self.__sock = None
        self.__reactor = reactor
        self.__selector = selector
        self.__want_read = False
        self.__want_write = False

    def assert_closed(self):
        assert self.__want_read is False
        assert self.__want_write is False

    def update(self, want_read, want_write, f):
        if self.__sock is not f:
            # Not permitted to switch to another file while signed up
            # for an existing read notification.
            self.assert_closed()
            self.__sock = f

        if self.__want_write != want_write:
            # There has been a change in want write.
            self.__want_write = want_write

            if want_write:
                self.__selector.add_write(f, self.__reactor)
            else:
                self.__selector.del_write(f, self.__reactor)

        if self.__want_read != want_read:
            # There has been a change in want read.
            self.__want_read = want_read

            if want_read:
                self.__selector.add_read(f, self.__reactor)
            else:
                self.__selector.del_read(f, self.__reactor)


class Reactor(object):
    """
    Parameters
    ----------
    properties: ReactorProperties
    log: str or logging.Logger
        If `str` then calls logging.getLogger(log) to acquire a logger;
        otherwise asserts that `log` has `debug`, `info`, etc. methods
        and uses log as if it were a `logging.getLogger` object.

    Attributes
    -----------
    on_connect_fail: callable(r: Reactor)
    on_disconnect: callable(r: Reactor)
    on_connack: callable(r: Reactor, p: MqttConnack)
        Called immediately upon receiving a `MqttConnack` packet from
        the remote.  The `reactor.state` will be `ReactorState.started`
        or `ReactorState.stopping` if the reactor is shutting down.
    on_suback: callable(r: Reactor, p: MqttSuback)
        Called immediately upon receiving a `MqttSuback` packet from the
        remote.
    on_unsuback: callable(r: Reactor, p: MqttUnsuback)
        Called immediately upon receiving a `MqttUnsuback` packet from
        the remote.
    on_pubrec: callable(r: Reactor, p: MqttPubrec)
        Called immediately upon receiving a `MqttPubrec` packet from the
        remote.  This is part of the QoS=2 message send path.
    on_pubcomp: callable(r: Reactor, p: MqttPubcomp)
        Called immediately upon receiving a `MqttPubcomp` packet from the
        remote.  This is part of the QoS=2 message send path.
    on_publish: callable(r: Reactor, p: MqttPublish)
        Called immediately upon receiving a `MqttSuback` packet from the
        remote.  This is part of the QoS=0, 1, and 2 message receive
        paths.
    on_puback: callable(r: Reactor, p: MqttPuback)
        Called immediately upon receiving a `MqttPuback` packet from the
        remote.  This method is part of the QoS=1 message send path.
    on_pubrel: callable(r: Reactor, p: MqttPubrel)
        Called immediately upon receiving a `MqttPubrel` packet from the
        remote.  This is part of the QoS=2 message receive path.
    """
    def __init__(self, properties, log='haka'):
        assert properties.client_id is not None
        assert properties.socket_factory is not None
        assert properties.endpoint is not None
        assert properties.scheduler is not None
        assert 0 <= properties.keepalive_period <= 2**16-1
        assert isinstance(properties.keepalive_period, int)
        assert isinstance(properties.clean_session, bool)
        assert callable(properties.name_resolver)
        host, port = properties.endpoint
        assert isinstance(host, str)
        assert 0 <= port <= 2**16-1
        assert isinstance(port, int)
        assert properties.selector is not None

        if isinstance(log, (str, unicode)):
            self.__log = logging.getLogger(log)
        else:
            assert hasattr(log, 'debug')
            assert hasattr(log, 'info')
            assert hasattr(log, 'warning')
            assert hasattr(log, 'error')
            self.__log = log

        self.__wbuf = bytearray()  #
        self.__rbuf = bytearray()  #

        self.__ssl_want_read = False  #
        self.__ssl_want_write = False  #

        self.__client_id = properties.client_id
        self.__username = properties.username
        self.__password = properties.password

        self.__clock = properties.clock
        self.__keepalive_period = properties.keepalive_period
        self.__keepalive_due_deadline = None
        self.__keepalive_abort_deadline = None
        self.__clean_session = properties.clean_session
        self.__name_resolver = properties.name_resolver

        self.__socket_factory = properties.socket_factory
        self.socket = None
        self.__host, self.__port = properties.endpoint
        self.__recveived_connack = False
        self.__state = ReactorState.init
        self.__mqtt_state = MqttState.stopped
        self.__sock_state = SocketState.stopped
        self.__error = None

        self.__send_packet_ids = set()
        self.__send_path_packet_ids = PacketIdGenerator()

        self.__preflight_queue = []
        self.__inflight_queue = OrderedDict()

        # Publish packets must be ack'd in order of publishing
        # [MQTT-4.6.0-2], [MQTT-4.6.0-3]
        #self.__in_flight_publish = []

        # No specific requirement exists for subscribe suback ordering.
        # self.__in_flight_subscribe = {}

        # It MUST send PUBREL packets in the order in which the corresponding PUBREC packets were
        # received (QoS 2 messages) [MQTT-4.6.0-4]
        #self.__in_flight_pubrel = []

        self.__scheduler = properties.scheduler

        self.__will = None

        self.__name_resolution_future = None
        self.__pingreq_active = False

        # Want read
        self.__selector = _AssertSelectAdapter(self, properties.selector)

        # Connection Callbacks
        self.on_connect_fail = None
        self.on_disconnect = None
        self.on_connack = None

        # Send path
        self.on_pubrec = None
        self.on_pubcomp = None

        # Subscribe path
        self.on_suback = None
        self.on_unsuback = None

        # Receive path
        self.on_publish = None
        self.on_puback = None
        self.on_pubrel = None

    @property
    def clean_session(self):
        """bool: Clean session flag is true/false."""
        return self.__clean_session

    @property
    def client_id(self):
        """str: Client id."""
        return self.__client_id

    @property
    def keepalive_period(self):
        """float: If this period elapses without the client sending a
        control packet to the server then it will generate a pingreq
        packet and send it to the server."""
        return self.__keepalive_period

    @property
    def keepalive_timeout_period(self):
        """float: If the Keep Alive value is non-zero and the Server
        does not receive a Control Packet from the Client within one and
        a half times the Keep Alive time period, it MUST disconnect the
        Network Connection to the Client as if the network had failed.
        [MQTT-3.1.2-24]"""
        return int(self.keepalive_period * 1.5)

    @property
    def error(self):
        """ReactorError or None: When `self.state` is
        `ReactorState.error` returns a subclass of `ReactorError`
        otherwise returns `None`."""
        return self.__error

    @property
    def state(self):
        """ReactorState: Current reactor state."""
        return self.__state

    @property
    def mqtt_state(self):
        """MqttState: Current state of mqtt protocol handshake."""
        return self.__mqtt_state

    @property
    def sock_state(self):
        """MqttState: Current state of mqtt protocol handshake."""
        return self.__sock_state

    @property
    def will(self):
        """mqtt_codec.packet.MqttWill or None: Last will and testament."""
        return self.__will

    @will.setter
    def will(self, will):
        """

        Parameters
        ----------
        will: mqtt_codec.packet.MqttWill or None
            Last will and testament or None is no last will and
            testament is desired.
        """
        if will is None or isinstance(will, MqttWill):
            self.__will = will
        else:
            raise TypeError()

    def __update_io_notification(self):
        if self.socket is not None:
            self.__selector.update(self.want_read(), self.want_write(), self.socket)

    def __assert_state_rules(self):
        if self.mqtt_state in INACTIVE_MQTT_STATES or self.sock_state in INACTIVE_SOCK_STATES or self.state in INACTIVE_STATES:
            assert self.mqtt_state in INACTIVE_MQTT_STATES
            assert self.sock_state in INACTIVE_SOCK_STATES
            assert self.state in INACTIVE_STATES

        if self.sock_state in (SocketState.name_resolution, SocketState.connecting, SocketState.handshake):
            assert self.state is ReactorState.starting
            assert self.mqtt_state is MqttState.connack

        if self.want_write() or self.want_read():
            assert self.socket is not None

        if self.sock_state in (SocketState.name_resolution,
                               SocketState.connecting,
                               SocketState.handshake,
                               SocketState.stopped):
            assert self.__pingreq_active is False

        if self.sock_state not in (SocketState.connected, SocketState.deaf, SocketState.mute):
            assert self.__pingreq_active is False

        if self.sock_state in (SocketState.handshake, SocketState.connected, SocketState.mute, SocketState.deaf):
            assert self.__keepalive_abort_deadline is not None
        elif self.sock_state in (SocketState.name_resolution, SocketState.connecting, SocketState.stopped):
            assert self.__keepalive_abort_deadline is None
        else:
            raise NotImplementedError(self.sock_state)

        if self.sock_state not in (SocketState.connected, SocketState.deaf):
            assert self.__keepalive_due_deadline is None

        if self.sock_state in INACTIVE_SOCK_STATES:
            self.__selector.assert_closed()

        if self.state is ReactorState.error:
            assert self.error is not None

    def send_packet_ids(self):
        """

        Returns
        -------
        set[int]
            A set of active send-path packet ids.
        """
        return set(self.__send_path_packet_ids)

    def in_flight_packets(self):
        return list(self.__inflight_queue.values())

    def preflight_packets(self):
        return list(self.__preflight_queue)

    def subscribe(self, topics):
        """

        Parameters
        ----------
        topics: iterable of MqttTopic

        Raises
        ------
        haka_mqtt.exception.PacketIdReactorException
            Raised when there are no free packet ids to create a
            `MqttSubscribe` packet with.

        Returns
        --------
        MqttSubscribeTicket
        """
        self.__assert_state_rules()

        req = MqttSubscribeTicket(self.__send_path_packet_ids.acquire(), topics)
        self.__preflight_queue.append(req)

        self.__assert_state_rules()
        self.__update_io_notification()
        return req

    def unsubscribe(self, topics):
        """

        Parameters
        ----------
        topics: iterable of str

        Raises
        ------
        haka_mqtt.exception.PacketIdReactorException
            Raised when there are no free packet ids to create a
            `MqttUnsubscribe` packet with.

        Returns
        --------
        MqttUnsubscribeTicket
        """
        self.__assert_state_rules()

        req = MqttUnsubscribeTicket(self.__send_path_packet_ids.acquire(), topics)
        self.__preflight_queue.append(req)

        self.__assert_state_rules()
        self.__update_io_notification()
        return req

    def publish(self, topic, payload, qos, retain=False):
        """Publish may be called in any state.  It will place a packet
        onto the preflight queue but no packet_id will be assigned
        until the packet is placed in-flight.

        Parameters
        -----------
        topic: str
        payload: bytes
        qos: int
            0 <= qos <= 2
        retain: bool

        Raises
        ------
        haka_mqtt.exception.PacketIdReactorException
            Raised when there are no free packet ids to create a
            `MqttPublish` packet with.

        Return
        -------
        MqttPublishTicket
        """
        self.__assert_state_rules()
        assert 0 <= qos <= 2

        req = MqttPublishTicket(self.__send_path_packet_ids.acquire(), topic, payload, qos, retain)
        self.__preflight_queue.append(req)
        self.__assert_state_rules()
        self.__update_io_notification()
        return req

    def __start(self):
        assert self.sock_state in INACTIVE_SOCK_STATES
        assert self.mqtt_state in INACTIVE_MQTT_STATES
        assert self.state in INACTIVE_STATES

        self.__log.info('Starting.')

        self.__error = None

        self.__ssl_want_write = False
        self.__ssl_want_read = False

        self.__pingreq_active = False
        self.__recveived_connack = False

        self.__name_resolution_future = None

        preflight_queue = []
        for p in self.__inflight_queue.values():
            if p.packet_type is MqttControlPacketType.publish:
                # Publish packets in self.__inflight_queue will be
                # re-transmitted and the dupe flag must be set on the
                # re-transmitted packet.
                #
                # [MQTT-3.3.1.-1]
                #
                if p.qos == 1:
                    assert p.status is MqttPublishStatus.puback, p.status
                    p._set_dupe()
                elif p.qos == 2:
                    if p.status is MqttPublishStatus.pubrec:
                        p._set_dupe()
                else:
                    raise NotImplementedError(p.qos)
                preflight_queue.append(p)

        for p in self.__preflight_queue:
            if p.packet_type in (MqttControlPacketType.publish, MqttControlPacketType.pubrel):
                preflight_queue.append(p)

        self.socket = None
        self.__inflight_queue = OrderedDict()
        self.__preflight_queue = preflight_queue

        self.__wbuf = bytearray()
        self.__rbuf = bytearray()

        self.__state = ReactorState.starting
        self.__sock_state = SocketState.name_resolution
        self.__mqtt_state = MqttState.connack

        self.__log.info('Looking up host %s:%d.', self.__host, self.__port)
        self.__name_resolution_future = self.__name_resolver(self.__host,
                                      self.__port,
                                      socktype=socket.SOCK_STREAM,
                                      proto=socket.IPPROTO_TCP)
        self.__name_resolution_future.add_done_callback(self.__on_name_resolution)

    def __on_name_resolution(self, future):
        """Called when hostname resolution is complete.

        Parameters
        ----------
        Future
            The future.results will be a 5-tuple
            of (family, socktype, proto, canonname, sockaddr) or None if
            there was no result.

        """
        assert self.sock_state is SocketState.name_resolution
        assert future.done()

        if not future.cancelled():
            results = future.result()
            if results is None:
                e = future.exception()
                self.__log.error('%s (errno=%d).  Aborting.', e.strerror, e.errno)
                self.__abort(AddressReactorError(e))
            else:
                if len(results) == 0:
                    self.__log.error('No hostname entries found.  Aborting.')
                    self.__abort(AddressReactorError(socket.gaierror(socket.EAI_NONAME, 'Name or service not known')))
                elif len(results) > 0:
                    self.__log_name_resolution(results[0], chosen=True)
                    for result in results[1:]:
                        self.__log_name_resolution(result)
                    self.__connect(results[0])
                else:
                    raise NotImplementedError(len(results))

    def __log_name_resolution(self, resolution, chosen=False):
        """Called when hostname resolution is complete.

        Parameters
        ----------
        resolution: 5-tuple
            A 5-tuple of (family, socktype, proto, canonname, sockaddr).
        chosen: bool
            True if this log entry is to be marked as a chosen sockaddr
            to connect to.
        """
        family, socktype, proto, canonname, sockaddr = resolution

        socktype_str = {
            socket.SOCK_DGRAM: 'sock_dgram',
            socket.SOCK_STREAM: 'sock_stream',
            socket.SOCK_RAW: 'sock_raw',
        }.get(socktype, str(socktype))

        proto_str = {
            socket.IPPROTO_TCP: 'tcp',
            socket.IPPROTO_IP: 'ip',
            socket.IPPROTO_UDP: 'udp',
        }.get(proto, str(proto))

        if chosen:
            chosen_postfix = ' (chosen)'
        else:
            chosen_postfix = ' '

        if family == socket.AF_INET:
            ip, port = sockaddr
            self.__log.info("Found family=inet sock=%s proto=%s addr=%s:%d%s",
                            socktype_str,
                            proto_str,
                            ip,
                            port,
                            chosen_postfix)
        elif family == socket.AF_INET6:
            ip6, port, flow_info, scope_id = sockaddr
            self.__log.info("Found family=inet6 sock=%s proto=%s addr=%s:%d%s",
                            socktype_str,
                            proto_str,
                            ip6,
                            port,
                            chosen_postfix)
        else:
            raise NotImplementedError(family)

    def __connect(self, resolution):
        """Connect to the given resolved address.

        Parameters
        ----------
        resolution: 5-tuple
            A 5-tuple of (family, socktype, proto, canonname, sockaddr)
            as returned by `socket.getaddrinfo`."""
        assert self.sock_state is SocketState.name_resolution
        assert self.state is ReactorState.starting

        family, socktype, proto, canonname, sockaddr = resolution
        try:
            self.__sock_state = SocketState.connecting
            self.socket = self.__socket_factory()
            self.socket.connect(sockaddr)
        except socket.error as e:
            if e.errno == errno.EINPROGRESS:
                # Connection in progress.
                self.__update_io_notification()
                self.__log.info("Connecting.")
            else:
                self.__abort_socket_error(SocketReactorError(e.errno))
        else:
            self.__on_connect()

    def start(self):
        """Attempts to connect with remote if in one of the inactive
        states :py:const:`ReactorState.init`,
        :py:const:`ReactorState.stopped`,
        :py:const:`ReactorState.error`.  The method has no effect if
        already in an active state.
        """
        self.__assert_state_rules()

        if self.state in INACTIVE_STATES:
            self.__start()
        elif self.state is ReactorState.starting:
            self.__log.warning("Start while already starting; taking no additional action.")
        elif self.state is ReactorState.started:
            self.__log.warning("Start while already started; taking no action.")
        elif self.state is ReactorState.stopping:
            self.__log.warning("Start while already stopping; ignoring start and continuing to stop.")
        else:
            raise NotImplementedError(self.state)

        self.__assert_state_rules()
        self.__update_io_notification()

    def stop(self):
        self.__assert_state_rules()

        if self.state is ReactorState.init:
            self.__log.info('Stopped.')
            self.__state = ReactorState.stopped
        elif self.state in (ReactorState.starting, ReactorState.started):
            self.__log.info('Stopping.')
            if self.sock_state is SocketState.name_resolution:
                self.__terminate(ReactorState.stopped, None)
            elif self.sock_state is SocketState.connecting:
                self.__terminate(ReactorState.stopped, None)
            elif self.sock_state is SocketState.handshake:
                self.__terminate(ReactorState.stopped, None)
            else:
                self.__state = ReactorState.stopping
                self.__preflight_queue.append(MqttDisconnect())
        elif self.state is ReactorState.stopping:
            self.__log.warning('Stop while already stopping.')
        elif self.state is ReactorState.stopped:
            self.__log.warning('Stop while already stopped.')
        elif self.state is ReactorState.error:
            self.__log.warning('Stop while reactor in error.')
        else:
            raise NotImplementedError(self.state)

        self.__update_io_notification()
        self.__assert_state_rules()

    def terminate(self):
        """When in an active state immediately shuts down any socket
        reading and writing, closes the socket, cancels all outstanding
        scheduler deadlines, puts the reactor into state
        ReactorState.stopped, then calls self.on_connect_fail
        (if in a connect/connack state) or alternatively
        self.on_disconnect if in some other active state.  When reactor
        is not in an inactive state this method has no effect.
        """
        self.__assert_state_rules()

        self.__log.info('Terminating.')

        if self.state in ACTIVE_STATES:
            self.__terminate(ReactorState.stopped)
        elif self.state in INACTIVE_STATES:
            pass
        else:
            raise NotImplementedError(self.state)

        self.__update_io_notification()
        self.__assert_state_rules()

    def want_read(self):
        """True if the reactor is ready to process incoming socket data;
        False otherwise.

        Returns
        -------
        bool
        """
        if self.sock_state is SocketState.handshake:
            rv = self.__ssl_want_read
        elif self.sock_state in (SocketState.connected, SocketState.mute):
            rv = True
        else:
            rv = False

        return rv

    def want_write(self):
        """True if the reactor is ready write data to the socket; False
        otherwise.

        Returns
        -------
        bool
        """
        if self.sock_state in (SocketState.stopped, SocketState.name_resolution, SocketState.mute):
            rv = False
        elif self.sock_state is SocketState.connecting:
            rv = True
        elif self.sock_state is SocketState.handshake:
            rv = self.__ssl_want_write
        elif self.sock_state is SocketState.connected:
            rv = bool(self.__wbuf) or bool(self.__preflight_queue)
        else:
            raise NotImplementedError(self.sock_state)

        return rv

    def __decode_packet_body(self, header, num_header_bytes, packet_class):
        num_packet_bytes = num_header_bytes + header.remaining_len

        body = self.__rbuf[num_header_bytes:num_packet_bytes]
        num_body_bytes_consumed, packet = packet_class.decode_body(header, BytesReader(body))
        assert num_packet_bytes == num_header_bytes + num_body_bytes_consumed
        self.__rbuf = bytearray(self.__rbuf[num_packet_bytes:])
        return packet

    def __on_recv_bytes(self, new_bytes):
        assert self.sock_state in (SocketState.connected, SocketState.mute)
        assert len(new_bytes) > 0

        if self.sock_state is not SocketState.mute:
            if self.__keepalive_due_deadline is not None:
                self.__keepalive_due_deadline.cancel()
                self.__keepalive_due_deadline = None
            self.__keepalive_due_deadline = self.__scheduler.add(self.keepalive_period, self.__keepalive_due_timeout)

        self.__keepalive_abort_deadline.cancel()
        self.__keepalive_abort_deadline = self.__scheduler.add(1.5*self.keepalive_period,
                                                               self.__keepalive_abort_timeout)

        self.__log.debug('recv %d bytes 0x%s', len(new_bytes), HexOnStr(new_bytes))
        self.__rbuf.extend(new_bytes)

        while True:
            num_header_bytes, header = MqttFixedHeader.decode(BytesReader(bytes(self.__rbuf)))
            num_packet_bytes = num_header_bytes + header.remaining_len
            if len(self.__rbuf) >= num_packet_bytes:
                if header.packet_type == MqttControlPacketType.connack:
                    self.__on_connack(self.__decode_packet_body(header, num_header_bytes, MqttConnack))
                elif header.packet_type == MqttControlPacketType.suback:
                    self.__on_suback(self.__decode_packet_body(header, num_header_bytes, MqttSuback))
                elif header.packet_type == MqttControlPacketType.unsuback:
                    self.__on_unsuback(self.__decode_packet_body(header, num_header_bytes, MqttUnsuback))
                elif header.packet_type == MqttControlPacketType.puback:
                    self.__on_puback(self.__decode_packet_body(header, num_header_bytes, MqttPuback))
                elif header.packet_type == MqttControlPacketType.publish:
                    self.__on_publish(self.__decode_packet_body(header, num_header_bytes, MqttPublish))
                elif header.packet_type == MqttControlPacketType.pingresp:
                    self.__on_pingresp(self.__decode_packet_body(header, num_header_bytes, MqttPingresp))
                elif header.packet_type == MqttControlPacketType.pingreq:
                    self.__on_pingreq(self.__decode_packet_body(header, num_header_bytes, MqttPingreq))
                elif header.packet_type == MqttControlPacketType.pubrel:
                    self.__on_pubrel(self.__decode_packet_body(header, num_header_bytes, MqttPubrel))
                elif header.packet_type == MqttControlPacketType.pubcomp:
                    self.__on_pubcomp(self.__decode_packet_body(header, num_header_bytes, MqttPubcomp))
                elif header.packet_type == MqttControlPacketType.pubrec:
                    self.__on_pubrec(self.__decode_packet_body(header, num_header_bytes, MqttPubrec))
                else:
                    m = 'Received unsupported message type {}.'.format(header.packet_type)
                    self.__log.error(m)
                    self.__abort(DecodeReactorError(m))

    def read(self):
        """Calls recv on underlying socket exactly once and returns the
        number of bytes read.  If the underlying socket does not return
        any bytes due to an error or exception then zero is returned and
        the reactor state is set to error.  A write call may be made to
        the underlying socket to flush any bytes queued as a result of
        servicing the read request.

        Returns
        -------
        int
            number of bytes read from socket.
        """
        self.__assert_state_rules()

        self.__ssl_want_write = False
        self.__ssl_want_read = False

        num_bytes_read = 0
        if self.sock_state in INACTIVE_SOCK_STATES:
            pass
        elif self.sock_state in (SocketState.name_resolution, SocketState.connecting, SocketState.deaf):
            pass
        elif self.sock_state is SocketState.handshake:
            self.__set_handshake()
        elif self.sock_state in (SocketState.connected, SocketState.mute):
            try:
                new_bytes = self.socket.recv(4096)
                num_bytes_read = len(new_bytes)
                if new_bytes:
                    self.__on_recv_bytes(new_bytes)
                else:
                    self.__on_muted_remote()

            except UnderflowDecodeError:
                # Not enough header bytes.
                pass
            except DecodeError as e:
                self.__log.error('Error decoding message (%s)', str(e))
                self.__abort(DecodeReactorError(str(e)))
            except ssl.SSLWantWriteError:
                self.__ssl_want_write = True
            except ssl.SSLWantReadError:
                self.__ssl_want_read = True
            except socket.error as e:
                if e.errno == errno.EWOULDBLOCK:
                    # No write space ready.
                    pass
                else:
                    self.__abort_socket_error(SocketReactorError(e.errno))
        else:
            raise NotImplementedError(self.sock_state)

        self.__update_io_notification()
        self.__assert_state_rules()
        return num_bytes_read

    def __on_connack_accepted(self, connack):
        assert self.mqtt_state is MqttState.connack, self.mqtt_state

        if connack.session_present and self.clean_session:
            self.__abort_protocol_violation('Server indicates a session is present when none was requested. [MQTT-3.2.2-1]')
        else:
            if self.state is ReactorState.starting:
                self.__state = ReactorState.started
            elif self.state is ReactorState.stopping:
                pass
            else:
                raise NotImplementedError(self.state)
            self.__mqtt_state = MqttState.connected

            if self.on_connack is not None:
                self.on_connack(self, connack)

            self.__feed_wbuf()

    def __on_connack(self, connack):
        """Called once when a connack packet is received.

        Parameters
        ----------
        connack: MqttConnack
        """
        if self.mqtt_state is MqttState.connack:
            self.__recveived_connack = True
            self.__log.info('Received %s.', repr(connack))

            # TODO: should not close incoming socket at this time;
            # give server opportunity to close socket of its own
            # accord then give timeout.
            #
            if connack.return_code == ConnackResult.accepted:
                # The first packet sent from the Server to the Client MUST
                # be a CONNACK Packet [MQTT-3.2.0-1].
                self.__on_connack_accepted(connack)
            elif connack.return_code == ConnackResult.fail_bad_protocol_version:
                self.__log.error('Connect failed: bad protocol version.')
                self.__abort(ConnectReactorError(connack.return_code))
            elif connack.return_code == ConnackResult.fail_bad_client_id:
                self.__log.error('Connect failed: bad client ID.')
                self.__abort(ConnectReactorError(connack.return_code))
            elif connack.return_code == ConnackResult.fail_server_unavailable:
                self.__log.error('Connect failed: server unavailable.')
                self.__abort(ConnectReactorError(connack.return_code))
            elif connack.return_code == ConnackResult.fail_bad_username_or_password:
                self.__log.error('Connect failed: bad username or password.')
                self.__abort(ConnectReactorError(connack.return_code))
            elif connack.return_code == ConnackResult.fail_not_authorized:
                self.__log.error('Connect failed: not authorized.')
                self.__abort(ConnectReactorError(connack.return_code))
            else:
                raise NotImplementedError(connack.return_code)
        elif self.mqtt_state is MqttState.connected:
            self.__abort_protocol_violation('Received connack at an inappropriate time.  [MQTT-3.2.0-1]')
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_publish(self, publish):
        """Called when a publish packet is received from the remote.

        Parameters
        ----------
        publish: MqttPublish
        """
        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(publish)
        elif self.mqtt_state is MqttState.connected:
            self.__log.info('Received %s.', repr(publish))
            if self.on_publish is not None:
                self.on_publish(self, publish)

            if self.sock_state in (SocketState.connected, SocketState.deaf):
                if publish.qos == 0:
                    pass
                elif publish.qos == 1:
                    self.__preflight_queue.append(MqttPuback(publish.packet_id))
                elif publish.qos == 2:
                    self.__preflight_queue.append(MqttPubrec(publish.packet_id))
                else:
                    raise NotImplementedError(publish.qos)
            elif self.sock_state is SocketState.mute:
                if publish.qos == 0:
                    pass
                elif publish.qos == 1:
                    self.__log.info('No puback will be published because reactor is stopping.')
                elif publish.qos == 2:
                    self.__log.info('No pubrec will be published because reactor is stopping.')
                else:
                    raise NotImplementedError(publish.qos)
            else:
                raise NotImplementedError(self.sock_state)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_suback(self, suback):
        """Called when a suback packet is received from remote.

        Parameters
        ----------
        suback: MqttSuback
        """
        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(suback)
        elif self.mqtt_state is MqttState.connected:
            subscribe = get_packet_type(self.__inflight_queue, suback.packet_id, MqttControlPacketType.subscribe)

            if subscribe is None:
                self.__abort_protocol_violation('Received %s for a mid that is not in-flight; aborting.',
                                                repr(suback))
            else:
                if len(suback.results) == len(subscribe.topics):
                    self.__log.info('Received %s.', repr(suback))
                    subscribe._set_status(MqttSubscribeStatus.done)

                    self.__send_path_packet_ids.release(subscribe.packet_id)
                    del self.__inflight_queue[suback.packet_id]

                    if self.on_suback is not None:
                        self.on_suback(self, suback)
                else:
                    m = 'Received %s as a response to %s, but the number of subscription' \
                        ' results does not equal the number of subscription requests; aborting.'
                    self.__abort_protocol_violation(m,
                                                    repr(suback),
                                                    repr(subscribe.packet()))
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_unsuback(self, unsuback):
        """Called when a suback packet is received from remote.

        Parameters
        ----------
        unsuback: mqtt_codec.packet.MqttUnsuback
        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(unsuback)
        elif self.mqtt_state is MqttState.connected:
            unsubscribe = get_packet_type(self.__inflight_queue, unsuback.packet_id, MqttControlPacketType.unsubscribe)

            if unsubscribe is None:
                self.__abort_protocol_violation('Received %s for a mid that is not in-flight; aborting.',
                                                repr(unsuback))
            else:
                self.__log.info('Received %s.', repr(unsuback))
                unsubscribe._set_status(MqttSubscribeStatus.done)

                self.__send_path_packet_ids.release(unsubscribe.packet_id)
                del self.__inflight_queue[unsuback.packet_id]

                if self.on_unsuback is not None:
                    self.on_unsuback(self, unsuback)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_puback(self, puback):
        """Called when a puback packet is received from the remote.

        Parameters
        ----------
        puback: MqttPuback

        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(puback)
        elif self.mqtt_state is MqttState.connected:
            in_flight_packet_ids = [p.packet_id for p in self.__inflight_queue.values() if p.packet_type is MqttControlPacketType.publish]
            publish = self.__inflight_queue[in_flight_packet_ids[0]] if in_flight_packet_ids else None
            if publish and publish.packet_id == puback.packet_id:
                if publish.qos == 1:
                    del self.__inflight_queue[puback.packet_id]
                    self.__send_path_packet_ids.release(publish.packet_id)
                    self.__log.info('Received %s.', repr(puback))
                    publish._set_status(MqttPublishStatus.done)

                    if self.on_puback is not None:
                        self.on_puback(self, puback)
                else:
                    self.__abort_protocol_violation('Received %s, an inappropriate response to qos=%d %s; aborting.',
                                                    ReprOnStr(puback),
                                                    publish.qos,
                                                    ReprOnStr(publish))
            elif publish and puback.packet_id in in_flight_packet_ids:
                m = 'Received %s instead of puback for next-in-flight packet_id=%d; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(puback),
                                                publish.packet_id)
            else:
                m = 'Received %s when packet_id=%d was not in-flight; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(puback),
                                                puback.packet_id)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_pubrec(self, pubrec):
        """

        Parameters
        ----------
        pubrec: MqttPubrec
        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(pubrec)
        elif self.mqtt_state is MqttState.connected:
            in_flight_packet_ids = [p.packet_id for p in self.__inflight_queue.values() if p.packet_type is MqttControlPacketType.publish]
            publish_ticket = self.__inflight_queue[in_flight_packet_ids[0]] if in_flight_packet_ids else None
            if publish_ticket and publish_ticket.packet_id == pubrec.packet_id:
                if publish_ticket.qos == 2:
                    del self.__inflight_queue[pubrec.packet_id]
                    self.__log.info('Received %s.', repr(pubrec))

                    insert_idx = len(self.__preflight_queue)
                    if self.on_pubrec is not None:
                        self.on_pubrec(self, pubrec)

                    self.__preflight_queue.insert(insert_idx, MqttPubrel(pubrec.packet_id))
                else:
                    publish = MqttPublish(publish_ticket.packet_id,
                                          publish_ticket.topic,
                                          publish_ticket.payload,
                                          publish_ticket.dupe,
                                          publish_ticket.qos,
                                          publish_ticket.retain)
                    self.__abort_protocol_violation('Received unexpected %s in response to qos=%d publish %s; aborting.',
                                                    ReprOnStr(pubrec),
                                                    publish_ticket.qos,
                                                    ReprOnStr(publish))
            elif publish_ticket and pubrec.packet_id in in_flight_packet_ids:
                m = 'Received unexpected %s when packet_id=%d was next-in-flight; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(pubrec),
                                                publish_ticket.packet_id)
            else:
                m = 'Received unexpected %s when packet_id=%d was not in-flight; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(pubrec),
                                                pubrec.packet_id)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_pubcomp(self, pubcomp):
        """

        Parameters
        ----------
        pubcomp: MqttPubcomp

        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(pubcomp)
        elif self.mqtt_state is MqttState.connected:
            in_flight_pubrel_packet_ids = [p.packet_id for p in self.__inflight_queue.values() if p.packet_type is MqttControlPacketType.pubrel]
            pubrel = self.__inflight_queue[in_flight_pubrel_packet_ids[0]] if in_flight_pubrel_packet_ids else None
            if pubrel and pubrel.packet_id == pubcomp.packet_id:
                del self.__inflight_queue[pubcomp.packet_id]
                self.__log.info('Received %s.', repr(pubcomp))

                if self.on_pubcomp is not None:
                    self.on_pubcomp(self, pubcomp)
            elif pubrel and pubcomp.packet_id in in_flight_pubrel_packet_ids:
                m = 'Received %s when packet_id=%d was the next pubrel in flight; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(pubcomp),
                                                pubrel.packet_id)
            else:
                m = 'Received %s when no pubrel for packet_id=%d was in-flight; aborting.'
                self.__abort_protocol_violation(m,
                                                ReprOnStr(pubcomp),
                                                pubcomp.packet_id)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_pubrel(self, pubrel):
        """

        Part of QoS=2 receive path.

        Parameters
        ----------
        pubrel: MqttPubrel

        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(pubrel)
        elif self.mqtt_state is MqttState.connected:
            self.__log.info('Received %s.', repr(pubrel))
            if self.on_pubrel is not None:
                self.on_pubrel(self, pubrel)
            self.__preflight_queue.append(MqttPubcomp(pubrel.packet_id))
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_pingreq(self, pingreq):
        """

        Parameters
        ----------
        pingreq: MqttPingreq
        """
        assert self.sock_state in (SocketState.connected, SocketState.mute)

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(pingreq)
        elif self.mqtt_state is MqttState.connected:
            if self.sock_state is SocketState.connected:
                self.__log.info('Received %s.', repr(pingreq))
                self.__preflight_queue.append(MqttPingresp())
            elif self.sock_state is SocketState.mute:
                self.__log.info('Received %s; ignoring because muted.', repr(pingreq))
            else:
                raise NotImplementedError(self.state)
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_pingresp(self, pingresp):
        """

        Parameters
        ----------
        pingresp: MqttPingresp
        """

        if self.mqtt_state is MqttState.connack:
            self.__abort_early_packet(pingresp)
        elif self.mqtt_state is MqttState.connected:
            self.__log.info('Received %s.', repr(pingresp))
            self.__pingreq_active = False
        else:
            raise NotImplementedError(self.mqtt_state)

    def __on_muted_remote(self):
        assert self.sock_state in (SocketState.handshake, SocketState.connected, SocketState.mute)

        if self.sock_state in (SocketState.handshake, SocketState.connected):
            self.__log.warning('Remote has closed remote->local writes; Aborting.')
            self.__abort(MutePeerReactorError())
        elif self.sock_state is SocketState.mute:
            # Socket sending already closed (socket is mute).
            # If socket receive is also closed (socket is deaf), then
            # it is time for the socket to be closed.
            self.__log.warning('Remote has gracefully closed remote->local writes; Stopped.')
            self.__terminate(ReactorState.stopped, None)
        else:
            raise NotImplementedError(self.sock_state)

    def __launch_packets(self):
        """Places packets from the preflight queue on the inflight
        queue as required to provide bytes to the write buffer.

        Returns
        -------
        int
            Returns number of bytes flushed to output buffers.
        """

        # Prepare bytes for launch
        # TODO: must be larger than largest MQTT packet size
        max_buf_size = 2**16
        wbuf_size = len(self.__wbuf)

        #**************************************
        #
        #           0 1 2 3 4 5 6 7 8
        #  -----------x|----x|----x|
        #
        # packet_end_offset = [1, 4, 7]
        #
        packet_end_offsets = [wbuf_size]
        bio = BytesIO()
        num_new_bytes = 0
        for packet_record in self.__preflight_queue:
            num_bytes_encoded = packet_record.encode(bio)
            if wbuf_size + num_bytes_encoded <= max_buf_size:
                wbuf_size += num_bytes_encoded
                num_new_bytes += num_bytes_encoded
                packet_end_offsets.append(wbuf_size)
                if packet_record.packet_type is MqttControlPacketType.disconnect:
                    break
            else:
                break

        # Write as many bytes as possible.
        self.__wbuf.extend(bio.getvalue()[0:num_new_bytes])
        num_bytes_flushed = self.__flush()

        # Mark launched messages as in-flight.
        num_messages_launched = 0
        for packet_end_offset in packet_end_offsets:
            if num_bytes_flushed > packet_end_offset:
                num_messages_launched += 1
            else:
                break

        launched_packets = self.__preflight_queue[0:num_messages_launched]
        del self.__preflight_queue[0:num_messages_launched]

        for packet_record in launched_packets:
            packet = packet_record

            if packet.packet_type is MqttControlPacketType.connect:
                self.__log.info('Launching message %s.', packet.packet())
            else:
                self.__log.info('Launching message %s.', ReprOnStr(packet.packet()))

            # if packet.packet_type is MqttControlPacketType.connect:
            #     pass
            # elif packet.packet_type is MqttControlPacketType.connack:
            #     pass
            if packet_record.packet_type is MqttControlPacketType.publish:
                if packet_record.qos == 0:
                    self.__send_path_packet_ids.release(packet_record.packet_id)
                    packet_record._set_status(MqttPublishStatus.done)
                elif packet_record.qos == 1:
                    packet_record._set_status(MqttPublishStatus.puback)
                    assert packet_record.packet_id not in self.__inflight_queue
                    self.__inflight_queue[packet_record.packet_id] = packet_record
                elif packet_record.qos == 2:
                    packet_record._set_status(MqttPublishStatus.pubrec)
                    assert packet_record.packet_id not in self.__inflight_queue
                    self.__inflight_queue[packet_record.packet_id] = packet_record
                else:
                    raise NotImplementedError(packet_record.qos)
            # elif packet.packet_type is MqttControlPacketType.puback:
            #     pass
            # elif packet.packet_type is MqttControlPacketType.pubrec:
            #     pass
            elif packet_record.packet_type is MqttControlPacketType.pubrel:
                assert packet_record.packet_id not in self.__inflight_queue
                self.__inflight_queue[packet_record.packet_id] = packet_record
            # elif packet.packet_type is MqttControlPacketType.pubcomp:
            #     pass
            elif packet_record.packet_type is MqttControlPacketType.subscribe:
                packet_record._set_status(MqttSubscribeStatus.ack)
                assert packet_record.packet_id not in self.__inflight_queue
                self.__inflight_queue[packet_record.packet_id] = packet_record
            # elif packet.packet_type is MqttControlPacketType.suback:
            #     pass
            elif packet_record.packet_type is MqttControlPacketType.unsubscribe:
                assert packet_record.packet_id not in self.__inflight_queue
                self.__inflight_queue[packet_record.packet_id] = packet_record
            # elif packet.packet_type is MqttControlPacketType.unsuback:
            #     pass
            # elif packet.packet_type is MqttControlPacketType.pingreq:
            #     pass
            # elif packet.packet_type is MqttControlPacketType.pingresp:
            #     pass
            elif packet.packet_type is MqttControlPacketType.disconnect:
                assert self.state is ReactorState.stopping
                self.__log.info('Shutting down outgoing stream.')
                self.socket.shutdown(socket.SHUT_WR)
                self.__sock_state = SocketState.mute

                if self.__keepalive_due_deadline is not None:
                    self.__keepalive_due_deadline.cancel()
                    self.__keepalive_due_deadline = None

                assert self.__keepalive_abort_deadline is not None

        if num_bytes_flushed:
            self.__log.debug('send %d bytes 0x%s.', num_bytes_flushed, HexOnStr(self.__wbuf[0:num_bytes_flushed]))
            self.__wbuf = self.__wbuf[num_bytes_flushed:]

        return num_bytes_flushed

    def __feed_wbuf(self):
        """Feeds the socket write buffer if the socket is in a state
        where they can be sent.  Packets from the preflight queue are
        placed on the inflight queue as necessary to acquire the
        necessary bytes.

        Returns
        -------
        int
            Returns number of bytes flushed to output buffers.
        """

        if self.sock_state in (SocketState.connected, SocketState.deaf):
            num_bytes_flushed = self.__launch_packets()
        elif self.sock_state in (SocketState.stopped,
                                 SocketState.mute,
                                 SocketState.name_resolution,
                                 SocketState.connecting):
            num_bytes_flushed = 0
        else:
            raise NotImplementedError(self.sock_state)

        return num_bytes_flushed

    def __set_connack(self):
        assert self.sock_state in (SocketState.connecting, SocketState.handshake)
        assert self.mqtt_state is MqttState.connack
        assert self.state is ReactorState.starting
        assert not self.__inflight_queue
        assert not self.__wbuf

        self.__sock_state = SocketState.connected

        connect = MqttConnect(self.client_id,
                              self.clean_session,
                              self.keepalive_period,
                              will=self.will,
                              username=self.__username,
                              password=self.__password)
        self.__preflight_queue.insert(0, connect)
        self.__feed_wbuf()
        self.__update_io_notification()

    def __set_handshake(self):
        assert self.sock_state in (SocketState.connecting, SocketState.handshake)
        assert self.state is ReactorState.starting
        self.__sock_state = SocketState.handshake
        self.__ssl_want_read = False
        self.__ssl_want_write = False

        try:
            self.socket.do_handshake()
        except ssl.SSLWantReadError:
            self.__ssl_want_read = True
            self.__update_io_notification()
        except ssl.SSLWantWriteError:
            self.__ssl_want_write = True
            self.__update_io_notification()
        except socket.error as e:
            self.__abort(e)
        else:
            self.__set_connack()

    def __on_connect(self):
        """Called when a socket becomes connected; reactor must be in
        the `ReactorState.connecting` state."""
        assert self.sock_state is SocketState.connecting
        assert self.state is ReactorState.starting

        self.__log.info('Connected.')
        self.__keepalive_abort_deadline = self.__scheduler.add(1.5*self.keepalive_period,
                                                               self.__keepalive_abort_timeout)

        if hasattr(self.socket, 'do_handshake'):
            self.__set_handshake()
        else:
            self.__set_connack()

    def __flush(self):
        """Calls send exactly once; returning the number of bytes written.

        Returns
        -------
        int
            Number of bytes written.
        """

        self.__ssl_want_read = False
        self.__ssl_want_write = False

        num_bytes_written = 0
        if self.__wbuf:
            try:
                num_bytes_written = self.socket.send(self.__wbuf)
            except ssl.SSLWantReadError:
                self.__ssl_want_read = True
            except ssl.SSLWantWriteError:
                self.__ssl_want_write = True
            except socket.error as e:
                if e.errno == errno.EWOULDBLOCK:
                    # No write space ready.
                    pass
                elif e.errno == errno.EPIPE:
                    self.__log.error("Remote unexpectedly closed the connection (<%s: %d>); Aborting.",
                                     errno.errorcode[e.errno],
                                     e.errno)
                    self.__abort(SocketReactorError(e.errno))
                else:
                    self.__abort_socket_error(SocketReactorError(e.errno))

        return num_bytes_written

    def __terminate_socket(self):
        """Cleans up all socket-related resources.
        * Closes any name resolution future and sets to None.
        * Removes any socket from the selector.
        * Shuts down reading and writing on socket.
        * Calls close on socket.
        * Ensures want_read and want_write are False.
        * Sets sock_state to SocketState.stopped.
        """
        if self.__name_resolution_future is not None:
            self.__name_resolution_future.cancel()
            self.__name_resolution_future = None

        if self.socket is not None:
            self.__selector.update(False, False, self.socket)
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except socket.error as e:
                if e.errno == errno.ENOTCONN:
                    pass
                else:
                    raise NotImplementedError(e)
            self.socket.close()
            self.socket = None
            self.__update_io_notification()

        self.__sock_state = SocketState.stopped

    def __terminate(self, state, error=None):
        """

        Parameters
        ----------
        state: ReactorState
        error: ReactorError
        """
        assert state in INACTIVE_STATES

        # Clean up all socket-related resources.
        self.__terminate_socket()

        # Clean up all MQTT protocol related items.
        if self.mqtt_state is MqttState.connack:
            on_disconnect_cb = self.on_connect_fail
        elif self.mqtt_state in (MqttState.connected, MqttState.mute):
            on_disconnect_cb = self.on_disconnect
        elif self.mqtt_state in INACTIVE_MQTT_STATES:
            pass
        else:
            raise NotImplementedError(self.state)

        self.__pingreq_active = False

        self.__wbuf = bytearray()
        self.__rbuf = bytearray()

        if self.__keepalive_abort_deadline is not None:
            self.__keepalive_abort_deadline.cancel()
            self.__keepalive_abort_deadline = None

        if self.__keepalive_due_deadline is not None:
            self.__keepalive_due_deadline.cancel()
            self.__keepalive_due_deadline = None

        self.__state = state
        self.__error = error

        self.__mqtt_state = MqttState.stopped

        if callable(on_disconnect_cb):
            on_disconnect_cb(self)

    def __abort_socket_error(self, se):
        """

        Parameters
        ----------
        se: SocketReactorError
        """
        self.__log.error('%s (<%s: %d>).  Aborting.',
                         os.strerror(se.errno),
                         errno.errorcode[se.errno],
                         se.errno)
        self.__abort(se)

    def __abort_early_packet(self, p):
        self.__abort_protocol_violation('Received %s before connack. [MQTT-3.2.0-1]', repr(p))

    def __abort_protocol_violation(self, m, *params):
        self.__log.error(m, *params)
        self.__abort(ProtocolReactorError(m % params))

    def __abort(self, e):
        """Immediately terminates all active resources and sets
        `self.state` to the final state `ReactorState.error`.

        Parameters
        ----------
        e: ReactorError
        """
        self.__terminate(ReactorState.error, e)

    def __keepalive_due_timeout(self):
        self.__assert_state_rules()
        assert self.__keepalive_due_deadline is not None

        assert self.sock_state is SocketState.connected
        assert self.mqtt_state is MqttState.connected
        assert self.__pingreq_active is False

        self.__pingreq_active = True
        self.__preflight_queue.append(MqttPingreq())

        self.__keepalive_due_deadline.cancel()
        self.__keepalive_due_deadline = None

        self.__assert_state_rules()
        self.__update_io_notification()

    def __keepalive_abort_timeout(self):
        self.__assert_state_rules()

        assert self.__keepalive_due_deadline is None
        assert self.__keepalive_abort_deadline is not None

        if self.sock_state in (SocketState.handshake, SocketState.connected, SocketState.mute, SocketState.deaf):
            msg = "More than abort period (%.01fs) has passed since last bytes received.  Aborting."
            self.__log.warning(msg, self.keepalive_timeout_period)
        else:
            raise NotImplementedError(self.sock_state)

        self.__keepalive_abort_deadline = None

        self.__abort(KeepaliveTimeoutReactorError())

        self.__update_io_notification()
        self.__assert_state_rules()

    def write(self):
        """If there is any data queued to be written to the underlying
        socket then a single call to socket send will be made to try
        and flush it to the socket write buffer.

        If self.state is an inactive state then no action is taken."""
        self.__assert_state_rules()

        if self.sock_state is SocketState.connecting:
            e = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if e == 0:
                self.__on_connect()
            elif errno.EINPROGRESS:
                pass
            else:
                self.__abort_socket_error(SocketReactorError(e.errno))
        elif self.sock_state is SocketState.handshake:
            self.__set_handshake()
        elif self.sock_state in (SocketState.connected, SocketState.deaf):
            self.__feed_wbuf()
        elif self.sock_state in (SocketState.name_resolution, SocketState.stopped, SocketState.mute):
            pass
        else:
            raise NotImplementedError(self.sock_state)

        self.__update_io_notification()
        self.__assert_state_rules()
