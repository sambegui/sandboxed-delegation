"""provider_error: a failed model call must surface the provider's own words, not an empty "omp exited 1".
The event shapes are copied from a live omp 18.9.2 run against provider.example on 2026-10-14 (quota exhausted)."""
import unittest

from bridge.runner_batch import provider_error, summarize_events

QUOTA = "429 Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-10-15 16:07:12 (type=1310)"


def quota_stream():
    failed = {"role": "assistant", "content": [], "stopReason": "error", "errorStatus": 429, "errorMessage": QUOTA}
    return [
        {"type": "session", "id": "abc"},
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        {"type": "message_end", "message": failed},
        {"type": "turn_end", "message": failed},
        {"type": "auto_retry_end", "success": False, "attempt": 1,
         "finalError": "Provider requested 30208693ms wait, exceeds retry.maxDelayMs. Original error: " + QUOTA},
    ]


class ProviderErrorTests(unittest.TestCase):
    def test_quota_failure_is_reported_with_the_providers_words(self):
        message = provider_error(quota_stream())
        self.assertIn("1310", message)
        self.assertIn("2026-10-15 16:07:12", message)

    def test_a_good_round_reports_no_error(self):
        events = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                                      "content": [{"type": "text", "text": "OK"}]}}]
        self.assertIsNone(provider_error(events))
        self.assertEqual(summarize_events(events)[1], "OK")

    def test_the_message_is_capped(self):
        events = [{"type": "auto_retry_end", "success": False, "finalError": "x" * 5000}]
        self.assertLessEqual(len(provider_error(events)), 400)

    def test_garbage_events_are_tolerated(self):
        self.assertIsNone(provider_error([None, "text", {"type": "turn_end", "message": "oops"}, 3]))


if __name__ == "__main__":
    unittest.main()
