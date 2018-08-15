import unittest
from io import BytesIO

from haka_mqtt import mqtt
from binascii import a2b_hex


class TestDecodeFixedHeader(unittest.TestCase):
    def test_decode_zero_nrb(self):
        buf = bytearray(a2b_hex('c000'))
        num_bytes_consumed, h = mqtt.MqttFixedHeader.decode(buf)
        self.assertEqual(h.remaining_len, 0)
        self.assertEqual(2, num_bytes_consumed)

    def test_decode_one_nrb(self):
        buf = bytearray(a2b_hex('c001'))
        num_bytes_consumed, h = mqtt.MqttFixedHeader.decode(buf)
        self.assertEqual(h.remaining_len, 1)
        self.assertEqual(2, num_bytes_consumed)

    def test_underflow_0(self):
        buf = ''
        self.assertRaises(mqtt.UnderflowDecodeError, mqtt.MqttFixedHeader.decode, buf)


class TestCodecVarInt(unittest.TestCase):
    def assert_codec_okay(self, n, buf):
        bio = BytesIO()
        expected_buf = a2b_hex(buf)

        num_bytes_written = mqtt.encode_varint(n, bio)
        actual_buf = bio.getvalue()
        self.assertEqual(expected_buf, actual_buf)
        self.assertEqual(num_bytes_written, len(actual_buf))

        self.assertEqual((len(actual_buf), n), mqtt.decode_varint(bytearray(expected_buf)))

    def test_0(self):
        self.assert_codec_okay(0, '00')

    def test_127(self):
        self.assert_codec_okay(127, '7f')

    def test_128(self):
        self.assert_codec_okay(128, '8001')

    def test_16383(self):
        self.assert_codec_okay(16383, 'ff7f')

    def test_16384(self):
        self.assert_codec_okay(16384, '808001')

    def test_2097151(self):
        self.assert_codec_okay(2097151, 'ffff7f')

    def test_2097152(self):
        self.assert_codec_okay(2097152, '80808001')

    def test_268435455(self):
        self.assert_codec_okay(268435455, 'ffffff7f')

    def test_underflow_zero_bytes(self):
        buf = bytearray()
        self.assertRaises(mqtt.UnderflowDecodeError, mqtt.decode_varint, buf)

    def test_mid_underflow(self):
        buf = bytearray(a2b_hex('808080'))
        self.assertRaises(mqtt.UnderflowDecodeError, mqtt.decode_varint, buf)

    def test_decode_error_too_big(self):
        buf = bytearray(a2b_hex('ffffffff'))
        self.assertRaises(mqtt.DecodeError, mqtt.decode_varint, buf)


class TestUtf8Decode(unittest.TestCase):
    def test_decode_encode(self):
        buf = a2b_hex('000541f0aa9b94')
        num_bytes_consumed, s = mqtt.decode_utf8(bytearray(buf))
        self.assertEqual(u'A\U0002a6d4', s)
        self.assertEqual(len(buf), num_bytes_consumed)

        bio = BytesIO()
        num_bytes_written = mqtt.encode_utf8(s, bio)
        self.assertEqual(bytearray(buf), bytearray(bio.getvalue()))
        self.assertEqual(num_bytes_consumed, num_bytes_written)


class TestConnectCodec(unittest.TestCase):
    def test_codec(self):
        c = mqtt.MqttConnect('client_id', False, 0)
        bio = BytesIO()

        num_encoded_bytes = c.encode(bio)
        self.assertTrue(num_encoded_bytes > 1)

        buf = bytearray(bio.getvalue())
        num_decoded_bytes, actual = mqtt.MqttConnect.decode(buf)
        self.assertEqual(num_encoded_bytes, num_decoded_bytes)


class TestConnackCodec(unittest.TestCase):
    def test_decode(self):
        buf = bytearray(a2b_hex('20020000'))
        packet = mqtt.MqttConnack.decode(buf)


class TestSubscribeCodec(unittest.TestCase):
    def test_subscribe(self):
        subscribe = mqtt.MqttSubscribe(7, [
            mqtt.MqttTopic('hello', 0),
            mqtt.MqttTopic('x', 1),
            mqtt.MqttTopic('Z', 2),
        ])
        bio = BytesIO()
        subscribe.encode(bio)
        buf = bytearray(bio.getvalue())

        recovered = mqtt.MqttSubscribe.decode(buf)


class TestSubackCodec(unittest.TestCase):
    def test_subscribe(self):
        subscribe = mqtt.MqttSuback(3, [
            mqtt.SubscribeResult.qos0,
            mqtt.SubscribeResult.qos1,
            mqtt.SubscribeResult.qos2,
            mqtt.SubscribeResult.fail,
        ])
        bio = BytesIO()
        subscribe.encode(bio)
        buf = bytearray(bio.getvalue())

        recovered = mqtt.MqttSuback.decode(buf)
