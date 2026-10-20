import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import wave

from worlds_alarm import Feed, FeedError, Match, Watcher, WORLD_LEAGUE, parse_page, make_wave, App


def event(event_id="one", state="unstarted", league=WORLD_LEAGUE, teams=("T1", "BLG")):
    return {"id": event_id, "startTime": "2026-10-23T23:00:00Z", "state": state, "type": "match",
            "league": {"id": league}, "blockName": "Swiss", "matchTeams": [{"code": t} for t in teams]}


def payload(events, older=None, newer=None):
    return {"data": {"esports": {"events": events, "pages": {"older": older, "newer": newer}}}}


def match(event_id="one", state="unstarted", teams=("T1", "BLG")):
    return Match(event_id, "2026-10-23T23:00:00Z", state, teams)


class WatcherTests(unittest.TestCase):
    def test_scheduled_time_alone_never_triggers(self):
        old = Match("past", "2000-01-01T00:00:00Z", "unstarted", ("A", "B"))
        watcher = Watcher([old])
        self.assertEqual(watcher.update([old]), ([], [], []))
        self.assertIn("past", watcher.pending)

    def test_unstarted_to_live_triggers_exactly_once(self):
        watcher = Watcher([match()])
        self.assertEqual(watcher.update([match()])[0], [])
        self.assertEqual(watcher.update([match(state="inProgress")])[0], [match(state="inProgress")])
        self.assertEqual(watcher.update([match(state="inProgress")])[0], [])

    def test_already_live_triggers_on_first_check(self):
        live = match(state="inProgress")
        self.assertEqual(Watcher([live]).update([live])[0], [live])

    def test_completed_after_outage_is_not_false_alarm(self):
        watcher = Watcher([match()])
        rings, ended, missing = watcher.update([match(state="completed")])
        self.assertEqual(rings, [])
        self.assertEqual(len(ended), 1)
        self.assertFalse(watcher.pending)

    def test_same_teams_different_event_do_not_trigger(self):
        watcher = Watcher([match("chosen")])
        rings, ended, missing = watcher.update([match("other", "inProgress")])
        self.assertFalse(rings)
        self.assertEqual(missing, [match("chosen")])
        self.assertIn("chosen", watcher.pending)

    def test_tbd_and_reschedule_keep_identity(self):
        watcher = Watcher([match(teams=("TBD", "TBD"))])
        live = Match("one", "2026-10-24T23:00:00Z", "inProgress", ("T1", "BLG"))
        self.assertEqual(watcher.update([live])[0], [live])

    def test_multi_match_waits_for_remaining_event(self):
        watcher = Watcher([match("one"), match("two")])
        self.assertEqual(len(watcher.update([match("one", "inProgress"), match("two")])[0]), 1)
        self.assertEqual(set(watcher.pending), {"two"})
        self.assertEqual(len(watcher.update([match("two", "inProgress")])[0]), 1)


class FeedTests(unittest.TestCase):
    def test_china_time_rollover(self):
        self.assertEqual(match().time_text, "10-24 07:00")

    def test_other_leagues_are_excluded(self):
        matches, _ = parse_page(payload([event(), event("other", league="lpl")]))
        self.assertEqual([m.id for m in matches], ["one"])

    def test_unknown_state_or_broken_schema_is_an_error(self):
        for data in [payload([event(state="unknown")]), {}, {"data": None}, {"errors": [{"message": "changed"}]}]:
            with self.subTest(data=data), self.assertRaises(FeedError):
                parse_page(data)

    def test_pagination_dedup_and_cycle(self):
        feed = Feed()
        calls = []
        def page(variables):
            token = variables.get("pageToken")
            calls.append(token)
            if token is None:
                return [match("one")], {"older": "old", "newer": "new"}
            if token == "old":
                return [match("one"), match("two")], {"newer": "new"}
            return [match("three")], {"older": "old"}
        feed.page = page
        self.assertEqual(len(feed.fetch()), 3)
        self.assertEqual(calls, [None, "old", "new"])

    def test_request_uses_official_endpoint_and_public_headers(self):
        requests = []
        def open_request(request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(payload([event()])).encode())
        matches, _ = Feed(opener=open_request).page({"hl": "en_US"})
        request, timeout = requests[0]
        self.assertEqual(urllib.parse.urlparse(request.full_url).netloc, "lolesports.com")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(timeout, 15)
        self.assertEqual(matches[0].id, "one")

    def test_network_failure_and_http_auth_error_remain_errors(self):
        for error in [urllib.error.URLError("offline"), urllib.error.HTTPError("url", 401, "changed", {}, None)]:
            with self.subTest(error=error), self.assertRaises(FeedError):
                Feed(opener=Mock(side_effect=error)).page({})


class AudioTests(unittest.TestCase):
    def test_generated_wav_is_non_silent_and_well_formed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wav"
            make_wave(path)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 22050)
                self.assertEqual(wav.getnframes(), 44100)
                self.assertTrue(any(wav.readframes(wav.getnframes())))

    def test_real_alarm_cancels_preview_auto_silence(self):
        app = App.__new__(App)
        app.root = Mock()
        app.alarm_text = Mock()
        app.test_timer = "preview-timer"
        app.testing = True
        app.play = Mock()
        app.phone = Mock()
        app.alarm([match(state="inProgress")])
        app.root.after_cancel.assert_called_once_with("preview-timer")
        self.assertIsNone(app.test_timer)
        self.assertFalse(app.testing)
        app.play.assert_called_once()
        app.phone.notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
