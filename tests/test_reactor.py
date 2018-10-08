"""Testing for the reactor.py.


CHECKED-KC0: tests that are at a production level of readiness.

"""
from __future__ import print_function

import errno
import logging
import os
import sys
import unittest
import socket
from io import BytesIO

from mock import Mock

from mqtt_codec.packet import (
    MqttConnect,
    ConnackResult,
    MqttConnack,
    MqttTopic,
    MqttSubscribe,
    SubscribeResult,
    MqttSuback,
    MqttPublish,
    MqttPuback,
    MqttPubrec,
    MqttPubrel,
    MqttPubcomp,
    MqttPingreq,
    MqttDisconnect)
from haka_mqtt.mqtt_request import MqttPublishTicket, MqttPublishStatus, MqttSubscribeStatus, MqttSubscribeTicket
from haka_mqtt.reactor import (
    Reactor,
    ReactorProperties,
    ReactorState,
    KeepaliveTimeoutReactorError,
    MqttConnectFail, INACTIVE_STATES, SocketError, AddressReactorError, DecodeReactorError)
from haka_mqtt.scheduler import Scheduler


def buffer_packet(packet):
    """Creates a str for packet.encode and returns it.

    Parameters
    ----------
    packet: MqttPacketBody

    Returns
    -------
    str
    """
    bio = BytesIO()
    packet.encode(bio)
    return bio.getvalue()


def socket_error(errno):
    return socket.error(errno, os.strerror(errno))


class TestReactor(unittest.TestCase):
    def reactor_properties(self):
        p = ReactorProperties()
        p.socket_factory = lambda: self.socket
        p.endpoint = self.endpoint
        p.client_id = self.client_id
        p.keepalive_period = self.keepalive_period
        p.scheduler = self.scheduler
        p.clean_session = True
        p.name_resolver = self.name_resolver

        return p

    def setup_logging(self):
        log_env_var = 'LOGGING'
        formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s  %(message)s')
        if log_env_var in os.environ:
            self.handler = logging.StreamHandler(sys.stdout)
        else:
            self.handler = logging.NullHandler()
        self.handler.setFormatter(formatter)

        self.log = logging.getLogger(self.__class__.__name__)
        self.logs = [
            logging.getLogger('haka'),
            self.log
        ]
        for log in self.logs:
            log.setLevel(logging.DEBUG)
            log.addHandler(self.handler)

    def teardown_logging(self):
        for log in self.logs:
            log.removeHandler(self.handler)

    def af_inet_name_resolution(self):
        """
        ********************
        family AF_INET
        socktype 1
        proto 6
        canonname
        sockaddr ('93.184.216.34', 80)

        ********************
        family AF_INET6
        socktype 1
        proto 6
        canonname
        sockaddr ('2606:2800:220:1:248:1893:25c8:1946', 80, 0, 0)
        """
        return socket.AF_INET, 1, 6, '', ('93.184.216.34', 80)

    def af_inet6_name_resolution(self):
        return socket.AF_INET6, 1, 6, '', ('2606:2800:220:1:248:1893:25c8:1946', 80, 0, 0)

    def setUp(self):
        self.setup_logging()

        self.socket = Mock()
        self.endpoint = ('test.mosquitto.org', 1883)
        self.name_resolver = Mock()
        self.name_resolver.return_value = [
            self.af_inet_name_resolution(),
            self.af_inet6_name_resolution()
        ]
        self.client_id = 'client'
        self.keepalive_period = 10*60
        self.scheduler = Scheduler()

        self.on_connect_fail = Mock()
        self.on_disconnect = Mock()
        self.on_publish = Mock()
        self.on_pubrel = Mock()
        self.on_pubrec = Mock()
        self.on_pubcomp = Mock()
        self.on_connack = Mock()
        self.on_puback = Mock()
        self.on_suback = Mock()

        self.properties = self.reactor_properties()
        self.reactor = Reactor(self.properties)
        self.reactor.on_publish = self.on_publish
        self.reactor.on_connack = self.on_connack
        self.reactor.on_pubrel = self.on_pubrel
        self.reactor.on_pubcomp = self.on_pubcomp
        self.reactor.on_pubrec = self.on_pubrec
        self.reactor.on_puback = self.on_puback
        self.reactor.on_suback = self.on_suback
        self.reactor.on_connect_fail = self.on_connect_fail
        self.reactor.on_disconnect = self.on_disconnect

        self.log.info('%s setUp()', self._testMethodName)

    def tearDown(self):
        try:
            self.log.info('%s tearDown()', self._testMethodName)
            self.assertEqual(0, len(self.scheduler))
            self.assertTrue(self.reactor.state in INACTIVE_STATES)
            self.assertFalse(self.reactor.want_read())
            self.assertFalse(self.reactor.want_write())
        finally:
            self.teardown_logging()

    def set_recv_side_effect(self, rv_iterable):
        self.socket.recv.side_effect = rv_iterable

    def recv_packet_then_ewouldblock(self, p):
        self.set_recv_side_effect([buffer_packet(p), socket.error(errno.EWOULDBLOCK)])
        self.reactor.read()
        self.socket.recv.assert_called_once()
        self.socket.recv.reset_mock()
        self.socket.recv.side_effect = None
        self.socket.recv.return_value = None

    def recv_mute(self):
        self.set_recv_side_effect([''])
        self.reactor.read()
        self.socket.recv.assert_called_once()
        self.socket.recv.reset_mock()
        self.socket.recv.side_effect = None
        self.socket.recv.return_value = None

    def set_send_side_effect(self, rv_iterable):
        self.socket.send.side_effect = rv_iterable

    def set_send_packet_side_effect(self, p):
        try:
            packet_it = iter(p)
        except TypeError:
            packet_it = [p]

        self.set_send_side_effect([sum(len(buffer_packet(p)) for p in packet_it)])

    def send_packet(self, packet_on_wire, extra_packets=[]):
        """Calls write and expects to find a serialized version of p in
        the socket send buffer.

        Parameters
        -----------
        Mqtt"""
        buf = buffer_packet(packet_on_wire)
        num_bytes_sent = len(buf)
        for p in extra_packets:
            buf += buffer_packet(p)
        self.set_send_side_effect([num_bytes_sent])

        self.reactor.write()
        self.socket.send.assert_called_once_with(buf)
        self.socket.send.reset_mock()

    def send_packets(self, packets_on_wire, extra_packets=[]):
        """Calls write and expects to find a serialized version of p in
        the socket send buffer.

        Parameters
        -----------
        Mqtt"""
        buf = ''
        for p in packets_on_wire:
            buf += buffer_packet(p)

        num_bytes_sent = len(buf)
        for p in extra_packets:
            buf += buffer_packet(p)

        self.set_send_side_effect([num_bytes_sent])

        self.reactor.write()
        self.socket.send.assert_called_once_with(buf)
        self.socket.send.reset_mock()

    def recv_disconnect(self, exception):
        """Calls write and expects to find a serialized version of p in
        the socket send buffer.

        Parameters
        -----------
        Mqtt"""
        self.set_send_side_effect([exception])

        self.reactor.read()
        self.socket.recv.assert_called_once()
        self.socket.recv.reset_mock()

    def set_send_packet_drip_and_write(self, p):
        buf = buffer_packet(p)
        for b in buf[0:-1]:
            ec = errno.EWOULDBLOCK
            self.socket.send.side_effect = [1, socket.error(ec, os.strerror(ec))]
            self.reactor.write()
            self.socket.send.assert_called_once()
            self.socket.send.reset_mock()
            self.assertTrue(self.reactor.want_write())
            # TODO: Reactor wants read all the time it is writing!
            # Is this okay?
            # self.assertFalse(self.reactor.want_read())

        self.socket.send.side_effect = None
        self.socket.send.return_value = 1
        self.reactor.write()

        self.socket.send.assert_called_once()
        self.socket.send.reset_mock()
        self.assertFalse(self.reactor.want_write())

    def start_to_connect(self, preflight_queue=[]):
        """
        1. Call start but socket does not immediately connect (connect returns EINPROGRESS).
        2. Socket becomes connected.
        3. MqttConnect is sent to server.
        """
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Set up start call.
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')

        self.reactor.start()

        self.name_resolver.assert_called_once()
        self.name_resolver.reset_mock()
        self.socket.connect.assert_called_once_with(self.af_inet_name_resolution()[4])
        self.socket.connect.reset_mock()
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        connect = MqttConnect(self.client_id, self.properties.clean_session, self.keepalive_period)
        buf = buffer_packet(connect)
        for p in preflight_queue:
            buf += buffer_packet(p)
        self.set_send_side_effect([len(buf)])

        self.reactor.write()
        self.socket.send.assert_called_once_with(buf)
        self.socket.send.reset_mock()

        self.assertEqual(self.reactor.state, ReactorState.connack)
        self.assertFalse(self.reactor.want_write())
        self.assertTrue(self.reactor.want_read())

    def start_to_immediate_connect(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        self.socket.connect.return_value = None
        connect = MqttConnect(self.client_id, self.properties.clean_session, self.keepalive_period)
        self.set_send_packet_side_effect(connect)
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.socket.connect.reset_mock()
        self.assertEqual(ReactorState.connack, self.reactor.state)
        self.assertTrue(self.reactor.want_read())
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_called_once_with(buffer_packet(connect))
        self.assertEqual(self.reactor.state, ReactorState.connack)
        self.socket.send.reset_mock()

    def start_to_connack(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        """
        self.start_to_connect()

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)
        self.assertFalse(self.reactor.want_write())
        self.assertTrue(self.reactor.want_read())

    def subscribe_and_suback(self, topics):
        """

        Parameters
        ----------
        topics: iterable of MqttTopic
        """

        # Push subscribe onto the wire.
        self.assertEqual(set(), self.reactor.active_send_packet_ids())
        subscribe = MqttSubscribe(0, topics)
        subscribe_ticket = self.reactor.subscribe(subscribe.topics)
        self.assertTrue(self.reactor.want_write())
        self.socket.send.assert_not_called()
        self.assertEqual(subscribe_ticket.status, MqttSubscribeStatus.preflight)
        self.assertEqual({subscribe.packet_id}, self.reactor.active_send_packet_ids())

        # Allow reactor to push subscribe onto the wire.
        self.set_send_packet_side_effect(subscribe)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(subscribe))
        self.socket.send.reset_mock()
        self.on_suback.assert_not_called()
        self.assertEqual(subscribe_ticket.status, MqttSubscribeStatus.ack)

        # Feed reactor a suback
        suback = MqttSuback(subscribe.packet_id, [SubscribeResult(t.max_qos) for t in topics])
        self.recv_packet_then_ewouldblock(suback)
        self.assertEqual(self.reactor.state, ReactorState.connected)
        self.assertEqual(subscribe_ticket.status, MqttSubscribeStatus.done)
        self.on_suback.assert_called_once_with(self.reactor, suback)
        self.on_suback.reset_mock()
        self.assertEqual(set(), self.reactor.active_send_packet_ids())


class TestDnsResolution(TestReactor, unittest.TestCase):
    def test_sync_getaddrinfo_fail_enohost(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.name_resolver.side_effect = e
        self.reactor.start()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.on_connect_fail.assert_called_once()
        self.assertEqual(AddressReactorError(e), self.reactor.error)

    def test_sync_getaddrinfo_fail_nohost(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        self.name_resolver.return_value = []
        self.reactor.start()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.on_connect_fail.assert_called_once()

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.assertEqual(AddressReactorError(e), self.reactor.error)

    def test_async_getaddrinfo_fail_enohost(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.name_resolver.return_value = None
        self.reactor.start()
        self.assertEqual(ReactorState.name_resolution, self.reactor.state)

        self.assertEqual(ReactorState.name_resolution, self.reactor.state)
        self.on_connect_fail.assert_not_called()
        self.on_disconnect.assert_not_called()

        kwargs = self.name_resolver.call_args[1]
        callable = kwargs['callback']
        callable(e)
        self.on_connect_fail.assert_called_once_with(self.reactor)
        self.on_disconnect.assert_not_called()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(AddressReactorError(e), self.reactor.error)


class TestConnect(TestReactor, unittest.TestCase):
    def test_immediate_connect(self):
        self.start_to_immediate_connect()

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        self.reactor.terminate()

    def test_immediate_connect_socket_error(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = errno.ECONNABORTED
        socket_error = socket.error(errno.ECONNABORTED, os.strerror(e))
        self.socket.connect.side_effect = socket_error
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.socket.connect.reset_mock()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(SocketError(e), self.reactor.error)


class TestReactorPaths(TestReactor, unittest.TestCase):
    def test_start(self):
        self.start_to_connack()

        # Subscribe to topic
        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        # Publish new message
        p = MqttPublish(1, topics[0].name, 'outgoing', False, 1, False)
        self.set_send_packet_side_effect(p)
        self.reactor.publish(p.topic, p.payload, p.qos)
        self.socket.send.assert_not_called()
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        # Acknowledge new message
        p = MqttPuback(p.packet_id)
        self.recv_packet_then_ewouldblock(p)
        self.socket.send.assert_not_called()

        publish = MqttPublish(1, topics[0].name, 'incoming', False, 1, False)
        puback = MqttPuback(p.packet_id)
        self.set_send_packet_side_effect(puback)
        self.recv_packet_then_ewouldblock(publish)
        self.reactor.write()
        self.on_publish.assert_called_once_with(self.reactor, publish)
        self.on_publish.reset_mock()
        self.socket.send.assert_called_once_with(buffer_packet(puback))

        self.reactor.terminate()

    def test_conn_and_connack_dripfeed(self):
        self.assertEqual(ReactorState.init, self.reactor.state)

        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_packet_drip_and_write(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        # Subscribe to topic
        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        p = MqttPublish(1, topics[0].name, 'outgoing', False, 1, False)
        self.set_send_packet_side_effect(p)
        self.reactor.publish(p.topic, p.payload, p.qos)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        p = MqttPuback(p.packet_id)
        self.recv_packet_then_ewouldblock(p)

        publish = MqttPublish(1, topics[0].name, 'incoming', False, 1, False)
        puback = MqttPuback(p.packet_id)
        self.set_send_packet_side_effect(puback)
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once_with(self.reactor, publish)
        self.on_publish.reset_mock()
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(puback))

        self.reactor.terminate()

    def test_recv_connect(self):
        self.start_to_connack()

        connect = MqttConnect('client', False, 0)
        self.recv_packet_then_ewouldblock(connect)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, DecodeReactorError))


class TestConnackFail(TestReactor, unittest.TestCase):
    def test_connack_keepalive_timeout(self):
        self.start_to_connack()
        p = MqttPingreq()
        self.set_send_packet_side_effect(p)
        self.assertFalse(self.reactor.want_write())
        self.scheduler.poll(self.keepalive_period)
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        self.scheduler.poll(self.keepalive_period * 0.5)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(self.reactor.error, KeepaliveTimeoutReactorError())

    def test_connack_unexpected_session_present(self):
        self.start_to_connect()

        connack = MqttConnack(True, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.on_connack.assert_not_called()

    def test_connack_fail_codes(self):
        fail_codes = [r for r in ConnackResult if r != ConnackResult.accepted]
        fail_codes.sort()

        for fail_code in fail_codes:
            self.start_to_connect()

            connack = MqttConnack(False, fail_code)
            self.recv_packet_then_ewouldblock(connack)
            self.assertEqual(self.reactor.state, ReactorState.error)
            self.assertEqual(self.reactor.error, MqttConnectFail(fail_code))


class TestSubscribePath(TestReactor, unittest.TestCase):
    def start_to_subscribe(self):
        self.start_to_connack()

        # Create subscribe
        topics = [
            MqttTopic('topic1', 1),
            MqttTopic('topic2', 2),
        ]
        est = MqttSubscribeTicket(0, topics)
        subscribe_ticket = self.reactor.subscribe(topics)
        self.assertEqual(est, subscribe_ticket)

        self.send_packet(MqttSubscribe(est.packet_id, est.topics))

    def test_subscribe(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        5. Creates subscribe
        6. Socket transmits subscribe.
        7. Socket receives suback.
        """
        self.start_to_subscribe()

        sp = MqttSuback(0, [SubscribeResult.qos1, SubscribeResult.qos2])
        self.recv_packet_then_ewouldblock(sp)
        self.assertEqual(ReactorState.connected, self.reactor.state)

        self.reactor.terminate()

    def test_subscribe_bad_suback_packet_id(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        5. Creates subscribe
        6. Socket transmits subscribe.
        7. Socket receives suback.
        """
        self.start_to_subscribe()

        sp = MqttSuback(1, [SubscribeResult.qos1, SubscribeResult.qos2])
        self.recv_packet_then_ewouldblock(sp)
        self.assertEqual(ReactorState.error, self.reactor.state)

        self.reactor.terminate()

    def test_subscribe_bad_num_suback_results(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        5. Creates subscribe
        6. Socket transmits subscribe.
        7. Socket receives suback.
        """
        self.start_to_subscribe()

        sp = MqttSuback(0, [SubscribeResult.qos1])
        self.recv_packet_then_ewouldblock(sp)
        self.assertEqual(ReactorState.error, self.reactor.state)

        self.reactor.terminate()


class TestSendPathQos0(TestReactor, unittest.TestCase):
    def test_start_and_publish_qos0(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        5. Publishes a QoS=0 packet;
        6. Calls write and places publish packet in-flight.

        Returns
        -------
        MqttPublishTicket
            The returned MqttPublishTicket will have its status set to
            :py:const:`MqttPublishStatus.done`.
        """
        # CHECKED-KC0 (2018-09-19)
        self.start_to_connack()

        # Create publish
        ept = MqttPublishTicket(0, 'topic', 'outgoing', 0)
        publish_ticket = self.reactor.publish(ept.topic,
                                              ept.payload,
                                              ept.qos,
                                              ept.retain)
        self.assertEqual(ept, publish_ticket)
        self.assertTrue(self.reactor.want_write())
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(1, len(self.reactor.active_send_packet_ids()))
        self.socket.send.assert_not_called()
        self.assertEqual(publish_ticket.status, MqttPublishStatus.preflight)

        # Write is called; reactor attempts to push packet to socket
        # send buffer.
        #
        publish = MqttPublish(0, ept.topic, ept.payload, ept.dupe, ept.qos, ept.retain)
        self.set_send_packet_side_effect(publish)
        self.reactor.write()
        self.assertEqual(publish_ticket.status, MqttPublishStatus.done)
        self.assertEqual(publish_ticket.packet_id, publish.packet_id)
        self.socket.send.assert_called_once_with(buffer_packet(publish))
        self.assertFalse(self.reactor.want_write())
        self.socket.send.reset_mock()
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.active_send_packet_ids())

        self.reactor.terminate()


class TestSendPathQos1(TestReactor, unittest.TestCase):
    def reactor_properties(self):
        p = super(type(self), self).reactor_properties()
        p.clean_session = False
        return p

    def start_and_publish_qos1(self):
        """
        1. Call start
        2. Socket does not immediately connect (EINPROGRESS)
        3. Socket transmits connect
        4. Socket receives connack
        5. Publishes a QoS=1 packet;
        6. Calls write and places publish packet in-flight.

        Returns
        -------
        MqttPublishTicket
            The returned MqttPublishTicket will have its status set to
            :py:const:`MqttPublishStatus.puback`.
        """
        self.start_to_connack()

        # Create publish
        ept = MqttPublishTicket(0, 'topic', 'outgoing', 1)
        publish_ticket = self.reactor.publish(ept.topic,
                                              ept.payload,
                                              ept.qos,
                                              ept.retain)
        self.assertEqual(ept, publish_ticket)
        self.assertTrue(self.reactor.want_write())
        self.socket.send.assert_not_called()
        self.assertEqual(publish_ticket.status, MqttPublishStatus.preflight)
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.active_send_packet_ids())

        # Write is called; reactor attempts to push packet to socket
        # send buffer.
        #
        publish = MqttPublish(0, ept.topic, ept.payload, ept.dupe, ept.qos, ept.retain)
        self.set_send_packet_side_effect(publish)
        self.reactor.write()
        self.assertEqual(publish_ticket.status, MqttPublishStatus.puback)
        self.assertEqual(publish_ticket.packet_id, publish.packet_id)
        self.socket.send.assert_called_once_with(buffer_packet(publish))
        self.assertFalse(self.reactor.want_write())
        self.socket.send.reset_mock()
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(1, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.active_send_packet_ids())

        return publish_ticket

    def test_publish_qos1(self):
        # CHECKED-KC0 (2018-09-17)
        publish_ticket = self.start_and_publish_qos1()

        # Server sends puback back to reactor.
        puback = MqttPuback(publish_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_called_once_with(self.reactor, puback)
        self.assertEqual(publish_ticket.status, MqttPublishStatus.done)
        self.assertEqual(ReactorState.connected, self.reactor.state)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.active_send_packet_ids())

        self.reactor.terminate()

    def test_publish_qos1_out_of_order_puback(self):
        # CHECKED-KC0 (2018-09-17)
        pt0 = self.start_and_publish_qos1()

        # Publish a new message.
        ept1 = MqttPublishTicket(1, 'topic1', 'outgoing1', 1)
        pt1 = self.reactor.publish(ept1.topic,
                                   ept1.payload,
                                   ept1.qos,
                                   ept1.retain)
        self.assertTrue(self.reactor.want_write())
        self.socket.send.assert_not_called()
        self.assertEqual(ept1, pt1)
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(1, len(self.reactor.in_flight_packets()))
        self.assertEqual({0, 1}, self.reactor.active_send_packet_ids())

        # Allow reactor to put pt1 in-flight.
        publish = MqttPublish(1, pt1.topic, pt1.payload, pt1.dupe, pt1.qos, pt1.retain)
        self.set_send_packet_side_effect(publish)

        self.reactor.write()

        self.socket.send.assert_called_once_with(buffer_packet(publish))

        self.assertEqual(1, pt1.packet_id)
        self.assertEqual(MqttPublishStatus.puback, pt1.status)
        self.socket.send.reset_mock()
        self.assertEqual(ReactorState.connected, self.reactor.state)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(2, len(self.reactor.in_flight_packets()))
        self.assertEqual({0, 1}, self.reactor.active_send_packet_ids())

        # Send a puback for pt1 to the reactor before a puback for pt0
        # is sent.  This is a violation of [MQTT-4.6.0-2].
        puback = MqttPuback(pt1.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)

        self.reactor.terminate()

    def test_publish_qos1_pubrec(self):
        # CHECKED-KC0 (2018-09-17)
        publish_ticket = self.start_and_publish_qos1()

        puback = MqttPubrec(publish_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.assertEqual(publish_ticket.status, MqttPublishStatus.puback)

        self.reactor.terminate()

    def test_puback_not_in_flight(self):
        # CHECKED-KC0 (2018-09-17)

        self.start_to_connack()

        puback = MqttPuback(0)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.active_send_packet_ids())

    def test_publish_disconnect_connect_republish_qos1(self):
        # CHECKED-KC0 (2018-09-17)
        publish_ticket = self.start_and_publish_qos1()

        # Terminate
        self.reactor.terminate()
        self.assertEqual(ReactorState.stopped, self.reactor.state)
        self.assertFalse(publish_ticket.dupe)

        # Start a new connection which does not immediately connect.
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())
        self.assertTrue(publish_ticket.dupe)  # dupe flag set on publish ticket.
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.active_send_packet_ids())

        # Socket connects and reactor sends a MqttConnect packet.
        publish = publish_ticket.packet()

        self.socket.getsockopt.return_value = 0
        self.send_packets([MqttConnect(self.client_id, self.properties.clean_session, self.keepalive_period), publish])
        self.assertEqual(self.reactor.state, ReactorState.connack)

        # Receives connack.
        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)

        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(1, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.active_send_packet_ids())
        self.assertTrue(publish_ticket.dupe)
        self.assertEqual(self.reactor.state, ReactorState.connected)
        self.socket.send.assert_not_called()
        self.assertFalse(self.reactor.want_write())
        self.assertEqual(publish_ticket.status, MqttPublishStatus.puback)

        self.reactor.terminate()


class TestSendPathQos2(TestReactor, unittest.TestCase):
    def reactor_properties(self):
        p = super(type(self), self).reactor_properties()
        p.clean_session = False
        return p

    def start_and_publish_qos2(self):
        self.start_to_connack()

        publish = MqttPublish(0, 'topic', 'outgoing', False, 2, False)
        pub_status = MqttPublishTicket(0, publish.topic, publish.payload, publish.qos, publish.retain)
        self.set_send_packet_side_effect(publish)
        actual_publish = self.reactor.publish(publish.topic,
                                       publish.payload,
                                       publish.qos,
                                       publish.retain)
        self.assertTrue(self.reactor.want_write())
        self.assertEqual(pub_status, actual_publish)
        self.reactor.write()
        self.socket.send.reset_mock()

        return publish

    def test_publish_qos2(self):
        publish = self.start_and_publish_qos2()

        pubrec = MqttPubrec(publish.packet_id)
        pubrel = MqttPubrel(publish.packet_id)
        self.set_send_packet_side_effect(pubrel)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_called_once()
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel))
        self.socket.send.reset_mock()

        pubcomp = MqttPubcomp(publish.packet_id)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_called_once()
        self.socket.send.assert_not_called()

        self.reactor.terminate()

    def test_publish_qos2_out_of_order_pubrec(self):
        self.start_and_publish_qos2()

        publish = MqttPublish(1, 'topic', 'outgoing', False, 2, False)
        self.set_send_packet_side_effect(publish)
        actual_publish = self.reactor.publish(publish.topic,
                                       publish.payload,
                                       publish.qos,
                                       publish.retain)
        self.socket.send.reset_mock()
        self.assertTrue(self.reactor.want_write())

        pubrec = MqttPubrec(publish.packet_id)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_not_called()
        self.socket.send.assert_not_called()
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_publish_qos2_out_of_order_pubcomp(self):
        publish0 = self.start_and_publish_qos2()

        # on-the-wire
        #
        # C --MqttPublish(packet_id=0, topic='topic', payload=0x6f7574676f696e67, dupe=False, qos=2, retain=False)--> S
        #
        publish1 = MqttPublish(1, 'topic', 'outgoing', False, 2, False)
        pub_status = MqttPublishTicket(1, publish1.topic, publish1.payload, publish1.qos, publish1.retain)
        actual_publish = self.reactor.publish(publish1.topic,
                                       publish1.payload,
                                       publish1.qos,
                                       publish1.retain)
        # publish is queued
        self.set_send_packet_side_effect(publish1)
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()

        # on-the-wire
        #
        # C --MqttPublish(packet_id=1, topic='topic', payload=0x6f7574676f696e67, dupe=False, qos=2, retain=False)--> S
        #
        self.socket.send.reset_mock()
        self.assertFalse(self.reactor.want_write())
        pub_status._set_status(MqttPublishStatus.pubrec)
        self.assertEqual(pub_status, actual_publish)

        pubrec0 = MqttPubrec(publish0.packet_id)
        pubrel0 = MqttPubrel(publish0.packet_id)
        self.set_send_packet_side_effect([pubrel0])
        #
        # on-the-wire
        #
        # C --MqttPublish(packet_id=1, topic='topic', payload=0x6f7574676f696e67, dupe=False, qos=2, retain=False)--> S
        #
        self.recv_packet_then_ewouldblock(pubrec0)
        self.on_pubrec.assert_called_once(); self.on_pubrec.reset_mock()
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel0))
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.connected)

        pubrec1 = MqttPubrec(publish1.packet_id)
        pubrel1 = MqttPubrel(publish1.packet_id)
        self.set_send_packet_side_effect([pubrel1])
        self.recv_packet_then_ewouldblock(pubrec1)
        self.reactor.write()
        self.on_pubrec.assert_called_once(); self.on_pubrec.reset_mock()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel1))
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.connected)

        pubrec = MqttPubcomp(publish1.packet_id)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_not_called()
        self.socket.send.assert_not_called()
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_pubcomp_not_in_flightpublish_qos2_out_of_order_pubcomp(self):
        self.start_to_connack()

        pubcomp = MqttPubcomp(0)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_not_called()
        self.socket.send.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_pubrec_not_in_flight_packet_id(self):
        self.start_to_connack()

        pubrec = MqttPubrec(0)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_not_called()
        self.socket.send.assert_not_called()
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_publish_qos2_puback_error(self):
        publish = self.start_and_publish_qos2()

        puback = MqttPuback(publish.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.socket.send.assert_not_called()
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_publish_disconnect_qos2(self):
        self.start_to_connack()

        publish = MqttPublish(0, 'topic', 'incoming', False, 2, False)
        pub_status = MqttPublishTicket(0, publish.topic, publish.payload, publish.qos, publish.retain)
        se = socket_error(errno.ECONNABORTED)
        self.set_send_side_effect([se])
        self.assertFalse(self.reactor.want_write())
        actual_publish = self.reactor.publish(publish.topic,
                                       publish.payload,
                                       publish.qos,
                                       publish.retain)
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()
        self.assertEqual(pub_status, actual_publish)
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.assertEqual(self.reactor.error, SocketError(se.errno))
        self.socket.send.reset_mock()

        self.start_to_connect(preflight_queue=[publish])

        connack = MqttConnack(False, ConnackResult.accepted)
        self.set_send_packet_side_effect(publish)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        pubrec = MqttPubrec(publish.packet_id)
        pubrel = MqttPubrel(publish.packet_id)
        self.set_send_packet_side_effect(pubrel)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_called_once()
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel))
        self.socket.send.reset_mock()

        pubcomp = MqttPubcomp(publish.packet_id)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_called_once()
        self.socket.send.assert_not_called()

        self.reactor.terminate()

    def test_pubrel_disconnect_qos2(self):
        self.start_to_connack()

        publish = MqttPublish(0, 'topic', 'incoming', False, 2, False)
        self.set_send_packet_side_effect(publish)
        actual_publish = self.reactor.publish(publish.topic,
                                       publish.payload,
                                       publish.qos,
                                       publish.retain)
        self.assertTrue(self.reactor.want_write())
        self.reactor.write()
        self.socket.send.reset_mock()

        pubrec = MqttPubrec(publish.packet_id)
        pubrel = MqttPubrel(publish.packet_id)
        se = socket_error(errno.ECONNABORTED)
        self.set_send_side_effect([se])
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_called_once()
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel))
        self.socket.send.reset_mock()

        self.assertEqual(self.reactor.state, ReactorState.error)
        self.assertEqual(self.reactor.error, SocketError(se.errno))

        self.start_to_connect(preflight_queue=[pubrel])

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        pubcomp = MqttPubcomp(publish.packet_id)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_called_once()
        self.socket.send.assert_not_called()

        self.assertEqual(self.reactor.state, ReactorState.connected)

        self.reactor.terminate()


class TestReceivePathQos0(TestReactor, unittest.TestCase):
    def test_recv_publish(self):
        self.start_to_connack()

        topics = [MqttTopic('bear_topic', 0)]
        self.subscribe_and_suback(topics)

        # Receive QoS=0 publish
        publish = MqttPublish(1, topics[0].name, 'incoming', False, topics[0].max_qos, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once()
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_not_called()

        # Immediate shut-down.
        self.reactor.terminate()


class TestReceivePathQos1(TestReactor, unittest.TestCase):
    def test_recv_publish(self):
        self.start_to_connack()

        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        self.on_publish.assert_not_called()
        publish = MqttPublish(1, topics[0].name, 'incoming', False, topics[0].max_qos, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once()

        puback = MqttPuback(publish.packet_id)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(puback))
        self.socket.send.reset_mock()

        self.reactor.terminate()


class TestReceivePathQos2(TestReactor, unittest.TestCase):
    def test_recv_publish(self):
        self.start_to_connack()

        topics = [MqttTopic('bear_topic', 2)]
        self.subscribe_and_suback(topics)

        self.on_publish.assert_not_called()
        publish = MqttPublish(1, topics[0].name, 'outgoing', False, topics[0].max_qos, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once()

        pubrec = MqttPubrec(publish.packet_id)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubrec))
        self.socket.send.reset_mock()

        self.on_pubrel.assert_not_called()
        pubrel = MqttPubrel(publish.packet_id)
        pubcomp = MqttPubcomp(publish.packet_id)
        self.set_send_side_effect([len(buffer_packet(pubcomp))])
        self.recv_packet_then_ewouldblock(pubrel)
        self.assertEqual(ReactorState.connected, self.reactor.state)
        self.on_pubrel.assert_called_once()

        pubcomp = MqttPubcomp(publish.packet_id)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(pubcomp))
        self.socket.send.reset_mock()

        self.reactor.terminate()


class TestReactorPeerDisconnect(TestReactor, unittest.TestCase):
    def test_connect(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_side_effect([socket.error(errno.EPIPE, os.strerror(errno.EPIPE))])
        self.reactor.write()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_connack(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.send_packet(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)

        self.set_recv_side_effect([''])
        self.reactor.read()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_connected(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.name_resolver.return_value[0][4])
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.send_packet(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)

        self.recv_packet_then_ewouldblock(MqttConnack(False, ConnackResult.accepted))
        self.assertEqual(self.reactor.state, ReactorState.connected)

        self.set_recv_side_effect([''])
        self.reactor.read()


class TestReactorStop(TestReactor, unittest.TestCase):
    def test_init(self):
        self.assertEqual(ReactorState.init, self.reactor.state)
        self.assertFalse(self.reactor.enable)
        self.reactor.stop()
        self.assertFalse(self.reactor.enable)
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_name_resolution_error_and_stop_on_error(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start called
        self.name_resolver.return_value = None
        self.reactor.start()
        self.assertEqual(ReactorState.name_resolution, self.reactor.state)

        # DNS resolved
        kwargs = self.name_resolver.call_args[1]
        callable = kwargs['callback']
        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        callable(e)
        self.on_connect_fail.assert_called_once_with(self.reactor)
        self.on_disconnect.assert_not_called()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(AddressReactorError(e), self.reactor.error)

        # Verify that stop has no effect.
        self.reactor.stop()
        self.assertEqual(ReactorState.error, self.reactor.state)

    def test_name_resolution_stop(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start called
        self.name_resolver.return_value = None
        self.reactor.start()
        self.assertEqual(ReactorState.name_resolution, self.reactor.state)

        # Stop request
        self.reactor.stop()
        # TODO: Immediate stop should be possible here if the async DNS
        #       query can be cancelled.

        # DNS resolved
        kwargs = self.name_resolver.call_args[1]
        callable = kwargs['callback']
        callable([self.af_inet_name_resolution()])
        self.on_connect_fail.assert_called_once_with(self.reactor)
        self.on_disconnect.assert_not_called()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connecting(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start with async connect
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.assertEqual(ReactorState.connecting, self.reactor.state)

        # Stop should have immediate effect at this point.
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connack_empty_preflight(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start with async connect
        self.start_to_connect()

        # Verify that stop has no effect.
        self.reactor.stop()
        self.assertEqual(ReactorState.connack, self.reactor.state)

        # Disconnect packet should be launched.
        disconnect = MqttDisconnect()
        self.send_packet(disconnect)
        self.assertEqual(ReactorState.mute, self.reactor.state)

        # Receives connack;
        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(ReactorState.mute, self.reactor.state)

        # Server processes disconnect and sends mute of its own.
        self.recv_mute()

    def test_connack_with_preflight(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        pub_ticket = self.reactor.publish('topic', 'payload', 1)

        # Start with async connect
        self.start_to_connect(preflight_queue=[pub_ticket.packet()])

        # Verify that stop has no effect.
        self.reactor.stop()
        self.assertEqual(ReactorState.connack, self.reactor.state)

        # Send disconnect packet
        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(ReactorState.mute, self.reactor.state)

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(ReactorState.mute, self.reactor.state)

        puback = MqttPuback(pub_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.assertEqual(ReactorState.mute, self.reactor.state)

        self.recv_mute()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connected_with_empty_preflight(self):
        self.start_to_connack()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(ReactorState.mute, self.reactor.state)

        self.recv_mute()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_stopping(self):
        pass

    def test_mute(self):
        pass

    def test_stopped(self):
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_error(self):
        self.socket.connect.side_effect = socket_error(errno.ECONNREFUSED)
        self.reactor.start()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.reactor.stop()
        self.assertEqual(ReactorState.error, self.reactor.state)
