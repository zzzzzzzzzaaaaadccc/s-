import io
import json
import unittest
from unittest.mock import Mock
import urllib.error

from android_push import PushError, PhonePush, new_topic, send_notification


class PushTests(unittest.TestCase):
    def test_random_channel_is_unique_and_long(self):
        one, two = new_topic(), new_topic()
        self.assertNotEqual(one, two)
        self.assertRegex(one, r'^worlds-[0-9a-f]{32}$')

    def test_utf8_message_and_urgent_priority(self):
        topic = new_topic()
        captured = []
        def opener(request, timeout):
            captured.append(request)
            self.assertEqual(timeout, 12)
            return io.BytesIO(json.dumps({'id': 'accepted-id', 'event': 'message', 'topic': topic}).encode())
        self.assertEqual(send_notification(topic, 'S 赛开打了', 'T1 vs BLG，准备看比赛', opener), 'accepted-id')
        request = captured[0]
        self.assertEqual(request.full_url, 'https://ntfy.sh/')
        self.assertEqual(request.get_method(), 'POST')
        body = json.loads(request.data)
        self.assertEqual(body['priority'], 5)
        self.assertEqual(body['topic'], topic)
        self.assertEqual(body['title'], 'S 赛开打了')
        self.assertEqual(body['tags'], ['alarm_clock'])

    def test_invalid_topic_never_contacts_service(self):
        opener = Mock()
        for topic in ['', 'worlds', 'https://ntfy.sh/something', 'a'*20+'\n']:
            with self.subTest(topic=topic), self.assertRaises(PushError):
                send_notification(topic, 'title', 'message', opener)
        opener.assert_not_called()

    def test_http_rate_limit_is_clear(self):
        error = urllib.error.HTTPError('https://ntfy.sh/', 429, 'rate limit', {}, None)
        with self.assertRaisesRegex(PushError, '限流'):
            send_notification(new_topic(), 'title', 'message', Mock(side_effect=error))

    def test_network_failure_does_not_report_delivery(self):
        with self.assertRaisesRegex(PushError, '无法连接'):
            send_notification(new_topic(), 'title', 'message', Mock(side_effect=TimeoutError()))

    def test_invalid_ack_is_rejected(self):
        for payload in [b'not json', b'{}', b'null', b'{"event":"message","id":"a","topic":"wrong"}']:
            with self.subTest(payload=payload), self.assertRaises(PushError):
                send_notification(new_topic(), 'title', 'message', lambda *a, **k: io.BytesIO(payload))

    def test_disabled_push_never_sends(self):
        controller = PhonePush.__new__(PhonePush)
        controller.config = {'enabled': False}
        controller.send = Mock()
        controller.notify([Mock()])
        controller.send.assert_not_called()

    def test_live_notification_contains_match_and_detection_time(self):
        controller = PhonePush.__new__(PhonePush)
        controller.config = {'enabled': True}
        controller.send = Mock()
        controller.notify([Mock(title='T1 vs BLG')])
        title, message = controller.send.call_args.args
        self.assertIn('T1 vs BLG', title)
        self.assertIn('北京时间', message)


if __name__ == '__main__':
    unittest.main()
