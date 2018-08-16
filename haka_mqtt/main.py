import logging
import sys
import socket
from select import select

from haka_mqtt.mqtt import MqttTopic
from haka_mqtt.reactor import ReactorProperties, SystemClock, Reactor


TOPIC = 'bubbles'
count = 0


def on_connack(reactor, p):
    """

    Parameters
    ----------
    reactor: Reactor
    p: MqttConnack
    """
    reactor.subscribe([
        MqttTopic(TOPIC, 1),
    ])


def on_suback(reactor, p):
    """

    Parameters
    ----------
    reactor: Reactor
    p: MqttSuback
    """
    global count

    count += 1
    reactor.publish(TOPIC, str(count), 1)


def on_puback(reactor, p):
    """

    Parameters
    ----------
    reactor: Reactor
    p: MqttPuback
    """
    global count
    count += 1
    reactor.publish(TOPIC, str(count), 1)

def on_publish(reactor, p):
    """

    Parameters
    ----------
    reactor: Reactor
    p: MqttPuback
    """
    pass


def main():
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(0)
    endpoint = ('test.mosquitto.org', 1883)
    clock = SystemClock()

    p = ReactorProperties()
    p.socket = sock
    p.endpoint = endpoint
    p.clock = clock
    p.client_id = 'bobby'

    reactor = Reactor(p)
    reactor.on_suback = on_suback
    reactor.on_puback = on_puback
    reactor.on_connack = on_connack
    reactor.on_publish = on_publish
    reactor.start()

    while True:
        if reactor.want_read():
            rlist = [reactor.socket]
        else:
            rlist = []

        if reactor.want_write():
            wlist = [reactor.socket]
        else:
            wlist = []

        timeout = reactor.remaining()
        rlist, wlist, xlist = select(rlist, wlist, [], timeout)

        for r in rlist:
            assert r == reactor.socket
            reactor.read()

        for w in wlist:
            assert w == reactor.socket
            reactor.write()

main()