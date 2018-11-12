from __future__ import print_function

import errno
import logging
import os
import ssl
import sys
import unittest
import socket
from io import BytesIO
from time import gmtime

from mock import Mock

from haka_mqtt.clock import SettableClock
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
    MqttDisconnect, MqttWill, MqttUnsubscribe, MqttUnsuback, MqttPingresp)
from haka_mqtt.mqtt_request import MqttPublishTicket, MqttPublishStatus, MqttSubscribeStatus, MqttSubscribeTicket, \
    MqttUnsubscribeTicket
from haka_mqtt.reactor import (
    Reactor,
    ReactorProperties,
    ReactorState,
    KeepaliveTimeoutReactorError,
    ConnectReactorError, INACTIVE_STATES, SocketReactorError, AddressReactorError, DecodeReactorError,
    ProtocolReactorError, INACTIVE_SOCK_STATES, SocketState, MqttState)
from haka_mqtt.scheduler import DurationScheduler
from tests.reactor_harness import TestReactor, buffer_packet, socket_error


class TestDnsResolution(TestReactor, unittest.TestCase):
    def test_sync_getaddrinfo_fail_eai_noname(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.name_resolver_future.set_exception(e)
        self.name_resolver_future.set_result(None)
        self.name_resolver_future.set_done(True)
        self.reactor.start()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.on_connect_fail.assert_called_once()
        self.assertEqual(AddressReactorError(e), self.reactor.error)

    def test_sync_getaddrinfo_fail_nohost(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        self.name_resolver_future.set_result([])
        self.reactor.start()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.on_connect_fail.assert_called_once()

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.assertEqual(AddressReactorError(e), self.reactor.error)

    def test_async_getaddrinfo_fail_eai_noname(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.name_resolver_future.set_exception(e)
        self.name_resolver_future.set_result(None)
        self.name_resolver_future.set_done(False)
        self.reactor.start()
        self.assertEqual(SocketState.name_resolution, self.reactor.sock_state)

        self.on_connect_fail.assert_not_called()
        self.on_disconnect.assert_not_called()

        self.name_resolver_future.set_done(True)
        self.on_connect_fail.assert_called_once_with(self.reactor)
        self.on_disconnect.assert_not_called()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(AddressReactorError(e), self.reactor.error)


class TestWillProperty(TestReactor, unittest.TestCase):
    def test_will_set_reset(self):
        self.assertIsNone(self.reactor.will)
        will = MqttWill(0, 'topic', b'payload', False)
        self.reactor.will = will
        self.assertEqual(will, self.reactor.will)
        self.reactor.will = None
        self.assertIsNone(self.reactor.will)

    def test_will_invalid_type(self):
        try:
            self.reactor.will = 1
            self.fail('Expected TypeError to be raised.')
        except TypeError:
            pass


class TestConnect(TestReactor, unittest.TestCase):
    def test_immediate_connect(self):
        self.start_to_immediate_connect()

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(ReactorState.started, self.reactor.state)

        self.reactor.terminate()

    def test_immediate_connect_socket_error(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        e = errno.ECONNABORTED
        socket_error = socket.error(errno.ECONNABORTED, os.strerror(e))
        self.socket.connect.side_effect = socket_error
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.socket.connect.reset_mock()
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(SocketReactorError(e), self.reactor.error)


class TestReactorPaths(TestReactor, unittest.TestCase):
    def test_start(self):
        self.start_to_connected()

        # Subscribe to topic
        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        # Publish new message
        p = MqttPublish(1, topics[0].name, b'outgoing', False, 1, False)
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

        publish = MqttPublish(1, topics[0].name, b'incoming', False, 1, False)
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
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_packet_drip_and_write(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.starting)
        self.assertEqual(self.reactor.mqtt_state, MqttState.connack)

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(ReactorState.started, self.reactor.state)

        # Subscribe to topic
        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        p = MqttPublish(1, topics[0].name, b'outgoing', False, 1, False)
        self.set_send_packet_side_effect(p)
        self.reactor.publish(p.topic, p.payload, p.qos)
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        p = MqttPuback(p.packet_id)
        self.recv_packet_then_ewouldblock(p)

        publish = MqttPublish(1, topics[0].name, b'incoming', False, 1, False)
        puback = MqttPuback(p.packet_id)
        self.set_send_packet_side_effect(puback)
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once_with(self.reactor, publish)
        self.on_publish.reset_mock()
        self.reactor.write()
        self.socket.send.assert_called_once_with(buffer_packet(puback))

        self.reactor.terminate()

    def test_recv_connect(self):
        self.start_to_connected()

        connect = MqttConnect('client', False, 0)
        self.recv_packet_then_ewouldblock(connect)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, DecodeReactorError))


class TestPacketsBeforeConnack(TestReactor, unittest.TestCase):
    def setUp(self):
        TestReactor.setUp(self)
        self.start_to_connack()

    def tearDown(self):
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))
        TestReactor.tearDown(self)

    def test_suback(self):
        self.recv_packet_then_ewouldblock(MqttSuback(0, [SubscribeResult.qos0]))

    def test_unsuback(self):
        self.recv_packet_then_ewouldblock(MqttUnsuback(0))

    def test_puback(self):
        self.recv_packet_then_ewouldblock(MqttPuback(0))

    def test_publish(self):
        self.recv_packet_then_ewouldblock(MqttPublish(0, 'topic_str', b'payload_bytes', False, 0, False))

    def test_pingresp(self):
        self.recv_packet_then_ewouldblock(MqttPingresp())

    def test_pingreq(self):
        self.recv_packet_then_ewouldblock(MqttPingreq())

    def test_pubrel(self):
        self.recv_packet_then_ewouldblock(MqttPubrel(0))

    def test_pubcomp(self):
        self.recv_packet_then_ewouldblock(MqttPubcomp(0))

    def test_pubrec(self):
        self.recv_packet_then_ewouldblock(MqttPubrec(0))


class TestPacketsBeforeConnackWhileMute(TestReactor, unittest.TestCase):
    def setUp(self):
        TestReactor.setUp(self)
        self.start_to_connack()
        self.reactor.stop()
        self.send_packet(MqttDisconnect())
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

    def tearDown(self):
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))
        TestReactor.tearDown(self)

    def test_suback(self):
        self.recv_packet_then_ewouldblock(MqttSuback(0, [SubscribeResult.qos0]))

    def test_unsuback(self):
        self.recv_packet_then_ewouldblock(MqttUnsuback(0))

    def test_puback(self):
        self.recv_packet_then_ewouldblock(MqttPuback(0))

    def test_publish(self):
        self.recv_packet_then_ewouldblock(MqttPublish(0, 'topic_str', b'payload_bytes', False, 0, False))

    def test_pingresp(self):
        self.recv_packet_then_ewouldblock(MqttPingresp())

    def test_pingreq(self):
        self.recv_packet_then_ewouldblock(MqttPingreq())

    def test_pubrel(self):
        self.recv_packet_then_ewouldblock(MqttPubrel(0))

    def test_pubcomp(self):
        self.recv_packet_then_ewouldblock(MqttPubcomp(0))

    def test_pubrec(self):
        self.recv_packet_then_ewouldblock(MqttPubrec(0))


class TestConnackFail(TestReactor, unittest.TestCase):
    def test_connack_unexpected_session_present(self):
        self.start_to_connack()

        connack = MqttConnack(True, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.on_connack.assert_not_called()

    def test_connack_fail_codes(self):
        fail_codes = [r for r in ConnackResult if r != ConnackResult.accepted]
        fail_codes.sort()

        for fail_code in fail_codes:
            self.start_to_connack()

            connack = MqttConnack(False, fail_code)
            self.recv_packet_then_ewouldblock(connack)
            self.assertEqual(self.reactor.state, ReactorState.error)
            self.assertEqual(self.reactor.error, ConnectReactorError(fail_code))


class TestSubscribePath(TestReactor, unittest.TestCase):
    def start_to_subscribe(self):
        self.start_to_connected()

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
        self.assertEqual(ReactorState.started, self.reactor.state)

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


class TestUnsubscribePath(TestReactor, unittest.TestCase):
    def start_to_unsubscribe(self):
        self.start_to_connected()

        # Create subscribe
        topics = [
            'topic1',
            'topic2',
        ]
        eut = MqttUnsubscribeTicket(0, topics)
        unsubscribe_ticket = self.reactor.unsubscribe(topics)
        self.assertEqual(eut, unsubscribe_ticket)

        self.send_packet(MqttUnsubscribe(eut.packet_id, eut.topics))

    def test_unsubscribe(self):
        self.start_to_unsubscribe()
        self.on_unsuback.assert_not_called()

        up = MqttUnsuback(0)
        self.recv_packet_then_ewouldblock(up)
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.on_unsuback.assert_called_once()

        self.reactor.terminate()

    def test_unsubscribe_bad_suback_packet_id(self):
        self.start_to_unsubscribe()
        self.on_unsuback.assert_not_called()

        up = MqttUnsuback(1)
        self.recv_packet_then_ewouldblock(up)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.on_disconnect.assert_called_once()

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
        self.start_to_connected()

        # Create publish
        ept = MqttPublishTicket(0, 'topic', b'outgoing', 0)
        publish_ticket = self.reactor.publish(ept.topic,
                                              ept.payload,
                                              ept.qos,
                                              ept.retain)
        self.assertEqual(ept, publish_ticket)
        self.assertTrue(self.reactor.want_write())
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(1, len(self.reactor.send_packet_ids()))
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
        self.assertEqual(set(), self.reactor.send_packet_ids())

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
        self.start_to_connected()

        # Create publish
        ept = MqttPublishTicket(0, 'topic', b'outgoing', 1)
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
        self.assertEqual({0}, self.reactor.send_packet_ids())

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
        self.assertEqual({0}, self.reactor.send_packet_ids())

        return publish_ticket

    def test_publish_qos1(self):
        # CHECKED-KC0 (2018-09-17)
        publish_ticket = self.start_and_publish_qos1()

        # Server sends puback back to reactor.
        puback = MqttPuback(publish_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_called_once_with(self.reactor, puback)
        self.assertEqual(publish_ticket.status, MqttPublishStatus.done)
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.send_packet_ids())

        self.reactor.terminate()

    def test_publish_qos1_puback_after_mute(self):
        # CHECKED-KC0 (2018-09-17)
        publish_ticket = self.start_and_publish_qos1()

        # Stop
        self.reactor.stop()
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        # Muted
        self.send_packet(MqttDisconnect())
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        # Receive puback after muted.
        self.assertEqual(publish_ticket.status, MqttPublishStatus.puback)
        puback = MqttPuback(publish_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_called_once_with(self.reactor, puback)
        self.assertEqual(publish_ticket.status, MqttPublishStatus.done)
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.send_packet_ids())

        self.reactor.terminate()

    def test_publish_qos1_out_of_order_puback(self):
        # CHECKED-KC0 (2018-09-17)
        pt0 = self.start_and_publish_qos1()

        # Publish a new message.
        ept1 = MqttPublishTicket(1, 'topic1', b'outgoing1', 1)
        pt1 = self.reactor.publish(ept1.topic,
                                   ept1.payload,
                                   ept1.qos,
                                   ept1.retain)
        self.assertTrue(self.reactor.want_write())
        self.socket.send.assert_not_called()
        self.assertEqual(ept1, pt1)
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(1, len(self.reactor.in_flight_packets()))
        self.assertEqual({0, 1}, self.reactor.send_packet_ids())

        # Allow reactor to put pt1 in-flight.
        publish = MqttPublish(1, pt1.topic, pt1.payload, pt1.dupe, pt1.qos, pt1.retain)
        self.set_send_packet_side_effect(publish)

        self.reactor.write()

        self.socket.send.assert_called_once_with(buffer_packet(publish))

        self.assertEqual(1, pt1.packet_id)
        self.assertEqual(MqttPublishStatus.puback, pt1.status)
        self.socket.send.reset_mock()
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(2, len(self.reactor.in_flight_packets()))
        self.assertEqual({0, 1}, self.reactor.send_packet_ids())

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

        self.start_to_connected()

        puback = MqttPuback(0)
        self.recv_packet_then_ewouldblock(puback)
        self.on_puback.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)
        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual(set(), self.reactor.send_packet_ids())

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
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())
        self.assertTrue(publish_ticket.dupe)  # dupe flag set on publish ticket.
        self.assertEqual(1, len(self.reactor.preflight_packets()))
        self.assertEqual(0, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.send_packet_ids())

        # Socket connects and reactor sends a MqttConnect packet.
        publish = publish_ticket.packet()

        self.socket.getsockopt.return_value = 0
        self.send_packets([MqttConnect(self.client_id, self.properties.clean_session, self.keepalive_period), publish])
        self.assertEqual(self.reactor.state, ReactorState.starting)
        self.assertEqual(self.reactor.mqtt_state, MqttState.connack)

        # Receives connack.
        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)

        self.assertEqual(0, len(self.reactor.preflight_packets()))
        self.assertEqual(1, len(self.reactor.in_flight_packets()))
        self.assertEqual({0}, self.reactor.send_packet_ids())
        self.assertTrue(publish_ticket.dupe)
        self.assertEqual(self.reactor.state, ReactorState.started)
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
        self.start_to_connected()

        publish = MqttPublish(0, 'topic', b'outgoing', False, 2, False)
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

        publish = MqttPublish(1, 'topic', b'outgoing', False, 2, False)
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
        publish1 = MqttPublish(1, 'topic', b'outgoing', False, 2, False)
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
        self.assertEqual(self.reactor.state, ReactorState.started)

        pubrec1 = MqttPubrec(publish1.packet_id)
        pubrel1 = MqttPubrel(publish1.packet_id)
        self.set_send_packet_side_effect([pubrel1])
        self.recv_packet_then_ewouldblock(pubrec1)
        self.reactor.write()
        self.on_pubrec.assert_called_once(); self.on_pubrec.reset_mock()
        self.socket.send.assert_called_once_with(buffer_packet(pubrel1))
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.started)

        pubrec = MqttPubcomp(publish1.packet_id)
        self.recv_packet_then_ewouldblock(pubrec)
        self.on_pubrec.assert_not_called()
        self.socket.send.assert_not_called()
        self.socket.send.reset_mock()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_pubcomp_not_in_flightpublish_qos2_out_of_order_pubcomp(self):
        self.start_to_connected()

        pubcomp = MqttPubcomp(0)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_not_called()
        self.socket.send.assert_not_called()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_pubrec_not_in_flight_packet_id(self):
        self.start_to_connected()

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
        self.start_to_connected()

        publish = MqttPublish(0, 'topic', b'incoming', False, 2, False)
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
        self.assertEqual(self.reactor.error, SocketReactorError(se.errno))
        self.socket.send.reset_mock()

        self.start_to_connack(preflight_queue=[publish])

        connack = MqttConnack(False, ConnackResult.accepted)
        self.set_send_packet_side_effect(publish)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.started)

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
        self.start_to_connected()

        publish = MqttPublish(0, 'topic', b'incoming', False, 2, False)
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
        self.assertEqual(self.reactor.error, SocketReactorError(se.errno))

        self.start_to_connack(preflight_queue=[pubrel])

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(self.reactor.state, ReactorState.started)

        pubcomp = MqttPubcomp(publish.packet_id)
        self.recv_packet_then_ewouldblock(pubcomp)
        self.on_pubcomp.assert_called_once()
        self.socket.send.assert_not_called()

        self.assertEqual(self.reactor.state, ReactorState.started)

        self.reactor.terminate()


class TestReceivePathQos0(TestReactor, unittest.TestCase):
    def test_recv_publish(self):
        self.start_to_connected()

        topics = [MqttTopic('bear_topic', 0)]
        self.subscribe_and_suback(topics)

        # Receive QoS=0 publish
        publish = MqttPublish(1, topics[0].name, b'incoming', False, topics[0].max_qos, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once()
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_not_called()

        # Immediate shut-down.
        self.reactor.terminate()

    def test_mute_recv_publish(self):
        self.start_to_connected()
        self.reactor.stop()
        self.send_packet(MqttDisconnect())
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        # Receive QoS=0 publish
        publish = MqttPublish(1, 'topic', b'payload', False, 1, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.on_publish.assert_called_once()
        self.assertFalse(self.reactor.want_write())
        self.socket.send.assert_not_called()

        # Immediate shut-down.
        self.reactor.terminate()

    def test_publish_before_connack(self):
        self.start_to_connack()

        # Receive QoS=0 publish
        publish = MqttPublish(1, 'topic', b'payload', False, 1, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))

    def test_mute_publish_before_connack(self):
        self.start_to_connack()
        self.reactor.stop()
        self.send_packet(MqttDisconnect())
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        # Receive QoS=0 publish
        publish = MqttPublish(1, 'topic', b'payload', False, 1, False)
        self.set_send_side_effect([len(buffer_packet(publish))])
        self.recv_packet_then_ewouldblock(publish)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))


class TestReceivePathQos1(TestReactor, unittest.TestCase):
    def test_recv_publish(self):
        self.start_to_connected()

        topics = [MqttTopic('bear_topic', 1)]
        self.subscribe_and_suback(topics)

        self.on_publish.assert_not_called()
        publish = MqttPublish(1, topics[0].name, b'incoming', False, topics[0].max_qos, False)
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
        self.start_to_connected()

        topics = [MqttTopic('bear_topic', 2)]
        self.subscribe_and_suback(topics)

        self.on_publish.assert_not_called()
        publish = MqttPublish(1, topics[0].name, b'outgoing', False, topics[0].max_qos, False)
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
        self.assertEqual(ReactorState.started, self.reactor.state)
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
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
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
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.send_packet(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.starting)
        self.assertEqual(self.reactor.mqtt_state, MqttState.connack)

        self.set_recv_side_effect([''])
        self.reactor.read()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_connected(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.expected_sockaddr)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.send_packet(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.starting)
        self.assertEqual(self.reactor.mqtt_state, MqttState.connack)

        self.recv_packet_then_ewouldblock(MqttConnack(False, ConnackResult.accepted))
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.assertEqual(SocketState.connected, self.reactor.sock_state)

        self.set_recv_side_effect([''])
        self.reactor.read()


class TestReactorStart(TestReactor, unittest.TestCase):
    def test_name_resolution(self):
        self.start_to_name_resolution()
        self.reactor.start()
        self.assertEqual(SocketState.name_resolution, self.reactor.sock_state)
        self.reactor.terminate()

    def test_connecting(self):
        self.start_to_connecting()
        self.reactor.start()
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)
        self.reactor.terminate()

    def test_handshake(self):
        self.start_to_handshake()
        self.reactor.start()
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.handshake, self.reactor.sock_state)
        self.reactor.terminate()

    def test_connack(self):
        self.start_to_connack()
        self.reactor.start()
        self.assertEqual(MqttState.connack, self.reactor.mqtt_state)
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.reactor.terminate()

    def test_connected(self):
        self.start_to_connected()
        self.reactor.start()
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.reactor.terminate()

    def test_mute(self):
        self.start_to_connected()
        self.reactor.stop()
        #self.reactor.read()
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.connected, self.reactor.sock_state)
        self.send_packet(MqttDisconnect())
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.reactor.start()
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.reactor.terminate()


class TestReactorStop(TestReactor, unittest.TestCase):
    def test_init(self):
        self.assertEqual(ReactorState.init, self.reactor.state)
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_name_resolution_error_and_stop_on_error(self):
        self.start_to_name_resolution()

        # DNS resolved
        e = socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
        self.name_resolver_future.set_exception(e)
        self.name_resolver_future.set_result(None)
        self.name_resolver_future.set_done(True)
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
        self.name_resolver_future.set_done(False)
        self.reactor.start()
        self.assertEqual(SocketState.name_resolution, self.reactor.sock_state)

        # Stop request
        self.reactor.stop()
        self.on_connect_fail.assert_called_once_with(self.reactor)
        self.on_disconnect.assert_not_called()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connecting(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start with async connect
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(SocketState.connecting, self.reactor.sock_state)

        # Stop should have immediate effect at this point.
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_handshake(self):
        self.start_to_handshake()

        # Stop should have immediate effect at this point.
        self.reactor.stop()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connack_empty_preflight(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        # Start with async connect
        self.start_to_connack()

        # Verify that stop has no effect.
        self.reactor.stop()
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(MqttState.connack, self.reactor.mqtt_state)

        # Disconnect packet should be launched.
        disconnect = MqttDisconnect()
        self.send_packet(disconnect)
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        # Receives connack;
        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        # Server processes disconnect and sends mute of its own.
        self.recv_eof()

    def test_connack_with_preflight(self):
        self.assertTrue(self.reactor.state in INACTIVE_STATES)

        pub_ticket = self.reactor.publish('topic', b'payload', 1)

        # Start with async connect
        self.start_to_connack(preflight_queue=[pub_ticket.packet()])

        # Verify that stop has no effect.
        self.reactor.stop()
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(MqttState.connack, self.reactor.mqtt_state)

        # Send disconnect packet
        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        connack = MqttConnack(False, ConnackResult.accepted)
        self.recv_packet_then_ewouldblock(connack)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        puback = MqttPuback(pub_ticket.packet_id)
        self.recv_packet_then_ewouldblock(puback)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        self.recv_eof()
        self.assertEqual(SocketState.stopped, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_recv_connack_while_mute(self):
        self.start_to_connack()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        self.recv_packet_then_ewouldblock(MqttConnack(False, ConnackResult.accepted))
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        self.recv_eof()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_recv_double_connack_while_mute(self):
        self.start_to_connack()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        self.recv_packet_then_ewouldblock(MqttConnack(False, ConnackResult.accepted))
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        self.recv_packet_then_ewouldblock(MqttConnack(False, ConnackResult.accepted))
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))

    def test_connected_with_empty_preflight(self):
        self.start_to_connected()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        self.recv_eof()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_connected_double_stop(self):
        self.start_to_connected()
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.reactor.stop()
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.reactor.stop()
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        self.recv_eof()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

    def test_mute(self):
        self.start_to_connected()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.reactor.stop()

        self.recv_eof()
        self.assertEqual(ReactorState.stopped, self.reactor.state)

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


class TestKeepalive(TestReactor, unittest.TestCase):
    def test_connack(self):
        # Start with async connect
        self.start_to_connack()

        # Verify waiting for connack and no writes are needed
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(MqttState.connack, self.reactor.mqtt_state)
        self.assertFalse(self.reactor.want_write())

        # Before a connack is received no pingreq should be sent.
        self.scheduler.poll(self.reactor.keepalive_period - 1)
        self.assertFalse(self.reactor.want_write())
        self.scheduler.poll(1)
        self.assertTrue(self.reactor.want_write())

        # A pingresp is an error at this point.
        self.recv_packet_then_ewouldblock(MqttPingresp())
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, ProtocolReactorError))

    def test_connected(self):
        self.start_to_connected()

        # Send pingreq
        self.scheduler.poll(self.reactor.keepalive_period)
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.send_packet(MqttPingreq())

        # Feed pingresp
        self.recv_packet_then_ewouldblock(MqttPingresp())
        self.assertEqual(ReactorState.started, self.reactor.state)

        # There have been `2 x keepalive_periods` that have passed in
        # the test.  Since `keepalive_timeout_period = 1.5 x keepalive_period`
        # then a keepalive error would have resulted if the pingresp
        # were not processed accurately.
        self.scheduler.poll(self.reactor.keepalive_period)
        self.assertEqual(ReactorState.started, self.reactor.state)

        self.reactor.terminate()

    def test_connected_unsolicited_pingresp(self):
        self.start_to_connected()

        # Feed unsolicited pingresp; should be ignored.
        self.recv_packet_then_ewouldblock(MqttPingresp())
        self.assertEqual(ReactorState.started, self.reactor.state)

        self.reactor.terminate()

    def test_mute(self):
        self.start_to_connected()

        # Send pingreq
        self.scheduler.poll(self.reactor.keepalive_period)
        self.assertEqual(ReactorState.started, self.reactor.state)
        self.send_packet(MqttPingreq())

        # Reactor is stopped before receiving the pingresp.
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        # Feed pingresp
        self.recv_packet_then_ewouldblock(MqttPingresp())
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        # There have been `2 x keepalive_periods` that have passed in
        # the test.  Since `keepalive_timeout_period = 1.5 x keepalive_period`
        # then a keepalive error would have resulted if the pingresp
        # were not processed accurately.
        self.scheduler.poll(self.reactor.keepalive_period)
        self.assertEqual(ReactorState.stopping, self.reactor.state)
        self.assertEqual(SocketState.mute, self.reactor.sock_state)

        self.recv_eof()
        self.assertEqual(ReactorState.stopped, self.reactor.state, self.reactor.error)


class TestKeepaliveX(TestReactor, unittest.TestCase):
    def reactor_properties(self):
        rp = super(type(self), self).reactor_properties()
        kp = 30
        rp.keepalive_period = kp
        rp.recv_idle_ping_period = kp
        rp.recv_idle_abort_period = int(1.5*kp)

        self.keepalive_period = rp.keepalive_period
        self.recv_idle_ping_period = rp.recv_idle_ping_period
        self.recv_idle_abort_period = rp.recv_idle_abort_period
        return rp

    def test_1(self):
        """
/home/kcallin/src/haka-mqtt/venv/bin/python /home/kcallin/src/haka-mqtt/examples/mqtt-sub.py --keepalive=30 --topic0=Advantech/# --topic0=/test/# --topic0=/temp/# test.mosquitto.org 8883
2018-11-12 08:35:00,333 Starting.
2018-11-12 08:35:00,333 Looking up host test.mosquitto.org:8883.
2018-11-12 08:35:00,426 Found family=inet sock=sock_stream proto=tcp addr=37.187.106.16:8883 (chosen)
2018-11-12 08:35:00,426 Found family=inet6 sock=sock_stream proto=tcp addr=2001:41d0:a:3a10::1:8883
2018-11-12 08:35:00,428 Connecting.
2018-11-12 08:35:00,563 Connected.
2018-11-12 08:35:00,853 Launching message MqttConnect(client_id='client-2018-11-12T08:35:00-grizzly-15509', clean_session=True, keep_alive=30s, username=***, password=***, will=None).
2018-11-12 08:35:00,990 Received MqttConnack(session_present=False, return_code=<ConnackResult.accepted: 0>).
2018-11-12 08:35:00,990 Launching message MqttSubscribe(packet_id=0, topics=[Topic('Advantech/#', max_qos=0), Topic('/test/#', max_qos=0), Topic('/temp/#', max_qos=0)]).
2018-11-12 08:35:01,127 Received MqttSuback(packet_id=0, results=[<SubscribeResult.qos0: 0>, <SubscribeResult.qos0: 0>, <SubscribeResult.qos0: 0>]).
2018-11-12 08:35:01,302 Received MqttPublish(packet_id=31522, topic=u'Advantech/00D0C9FAC3EA/data', payload=0x73223a312c2274223a302c2271223a3139322c2263223a312c22646931223a747275652c22646932223a66616c73652c22646933223a66616c73652c22646934223a66616c73652c22646f31223a66616c73652c22646f32223a66616c73652c22646f33223a66616c73652c22646f34223a66616c73657d, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,302 Received MqttPublish(packet_id=31522, topic=u'Advantech/00D0C9FAC3EA/Device_Status', payload=0x737461747573223a22636f6e6e656374222c226e616d65223a224144414d36323636222c226d61636964223a22303044304339464143334541222c22697061646472223a2231302e312e312e3133227d, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,303 Received MqttPublish(packet_id=17513, topic=u'Advantech/Device_Status', payload=0x73636f6e6e656374696f6e5f4144414d363236365f303044304339464541304533, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,303 Received MqttPublish(packet_id=31522, topic=u'Advantech/00D0C9F8CE88/Device_Status', payload=0x737461747573223a22636f6e6e656374222c226e616d65223a22574953452d343031302f4c414e222c226d61636964223a22303044304339463843453838222c22697061646472223a223139322e3136382e302e313331227d, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,303 Received MqttPublish(packet_id=12336, topic=u'/test/tfl/bddjpv', payload=0x30303030, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,303 Received MqttPublish(packet_id=29797, topic=u'/test/publish', payload=0x73742031, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,304 Received MqttPublish(packet_id=31522, topic=u'/test/iot/srdat', payload=0x747a6f6e65223a20302c202273756e72697365223a2022373a3133222c202273756e736574223a2022343a3135222c202274656d70223a2031322c202268696768223a2031332c20226c6f77223a2031307d, dupe=False, qos=0, retain=True).
2018-11-12 08:35:01,304 Received MqttPublish(packet_id=12851, topic=u'/temp/random', payload=0x, dupe=False, qos=0, retain=True).
2018-11-12 08:35:02,233 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234362d50413a343138, dupe=False, qos=0, retain=False).
2018-11-12 08:35:12,274 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234342d50413a343137, dupe=False, qos=0, retain=False).
2018-11-12 08:35:22,308 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234362d50413a343138, dupe=False, qos=0, retain=False).
2018-11-12 08:35:30,991 Launching message MqttPingreq().
2018-11-12 08:35:31,128 Received MqttPingresp().
2018-11-12 08:35:32,223 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234372d50413a343139, dupe=False, qos=0, retain=False).
2018-11-12 08:35:42,253 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234372d50413a343139, dupe=False, qos=0, retain=False).
2018-11-12 08:35:52,340 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234372d50413a343139, dupe=False, qos=0, retain=False).
2018-11-12 08:36:02,234 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234362d50413a343138, dupe=False, qos=0, retain=False).
2018-11-12 08:36:12,241 Received MqttPublish(packet_id=18511, topic=u'/test/MT2007/konyha', payload=0x3a3234342d50413a343137, dupe=False, qos=0, retain=False).
2018-11-12 08:36:16,346 Remote has unexpectedly closed remote->local writes; Aborting.
"""
        self.start_to_connected()
        topics = [MqttTopic('Advantech/#', max_qos=0), MqttTopic('/test/#', max_qos=0), MqttTopic('/temp/#', max_qos=0)]
        self.subscribe_and_suback(topics)

        time_of_next_send_ping = self.clock.time() + self.keepalive_period
        time_of_next_recv_ping = self.clock.time() + self.recv_idle_ping_period
        self.poll(15)
        self.recv_packet_then_ewouldblock(MqttPublish(18511, u'/test/MT2007/konyha', '', dupe=False, qos=0, retain=False))
        time_of_next_recv_ping = self.clock.time() + self.recv_idle_ping_period
        self.poll(time_of_next_send_ping - self.clock.time() - 1)
        self.assertFalse(self.reactor.want_write())

        # First ping (send path)
        self.poll(1)
        self.assertTrue(self.reactor.want_write())
        self.send_packet(MqttPingreq())
        time_of_next_send_ping = self.clock.time() + self.keepalive_period
        self.poll(15)
        self.recv_packet_then_ewouldblock(MqttPingresp())
        time_of_next_recv_ping = self.clock.time() + self.recv_idle_ping_period
        self.poll(time_of_next_send_ping - self.clock.time() - 1)
        self.assertFalse(self.reactor.want_write())

        # Second ping (send path)
        self.poll(1)
        self.assertTrue(self.reactor.want_write())
        self.send_packet(MqttPingreq())
        time_of_next_send_ping = self.clock.time() + self.keepalive_period
        self.poll(5)
        self.recv_packet_then_ewouldblock(MqttPingresp())
        time_of_next_recv_ping = self.clock.time() + self.recv_idle_ping_period
        self.poll(time_of_next_send_ping - self.clock.time() - 1)
        self.assertFalse(self.reactor.want_write())

        # Third ping (send path)
        self.poll(1)
        self.assertTrue(self.reactor.want_write())
        self.send_packet(MqttPingreq())
        time_of_next_send_ping = self.clock.time() + self.keepalive_period
        self.poll(5)
        self.recv_packet_then_ewouldblock(MqttPingresp())
        time_of_next_recv_ping = self.clock.time() + self.recv_idle_ping_period
        self.poll(time_of_next_send_ping - self.clock.time() - 1)
        self.assertFalse(self.reactor.want_write())

        pub = MqttPublish(1, 'topic', 'payload', False, 0, False)
        self.reactor.publish(pub.topic, pub.payload, pub.qos, pub.retain)
        self.assertTrue(self.reactor.want_write())
        self.send_packet(pub)
        time_of_next_send_ping = self.clock.time() + self.keepalive_period
        self.poll(time_of_next_recv_ping - self.clock.time() - 1)
        self.assertFalse(self.reactor.want_write())

        # Fourth ping (recv path)
        self.poll(1)
        self.assertTrue(self.reactor.want_write())
        self.send_packet(MqttPingreq())
        time_of_next_send_ping = self.clock.time() + self.keepalive_period

        self.reactor.terminate()


class TestKeepaliveTimeout(TestReactor, unittest.TestCase):
    def test_handshake(self):
        # Start with async connect
        self.start_to_handshake()

        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError))

    def test_connack(self):
        # Start with async connect
        self.start_to_connack()

        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError))

    def test_connected(self):
        self.start_to_connected()

        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError))

    def test_mute(self):
        self.start_to_connected()
        self.reactor.stop()

        disconnect = MqttDisconnect()
        self.send_packet(disconnect)

        self.assertEqual(SocketState.mute, self.reactor.sock_state)
        self.assertEqual(ReactorState.stopping, self.reactor.state)

        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError))


class TestKeepaliveDisabled(TestReactor, unittest.TestCase):
    def reactor_properties(self):
        self.keepalive_period = 0
        return TestReactor.reactor_properties(self)

    def test_connack(self):
        # Start with async connect
        self.assertEqual(0, self.reactor.keepalive_period)
        self.start_to_connack()

        self.assertEqual(1, len(self.scheduler))
        self.assertEqual(ReactorState.starting, self.reactor.state)
        self.assertEqual(MqttState.connack, self.reactor.mqtt_state)
        self.assertFalse(self.reactor.want_write())

        # Before a connack is received no pingreq should be sent.
        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError), self.reactor.error)
        self.socket.send.assert_not_called()

    def test_connected(self):
        self.start_to_connected()

        self.assertEqual(1, len(self.scheduler))
        self.scheduler.poll(self.reactor.recv_idle_abort_period)
        self.assertTrue(isinstance(self.reactor.error, KeepaliveTimeoutReactorError), self.reactor.error)
        self.socket.send.assert_not_called()
