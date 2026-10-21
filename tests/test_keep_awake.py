import unittest

from src.keep_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, keep_awake


class KeepAwakeTests(unittest.TestCase):
    def test_requests_and_releases_the_exemption(self):
        calls = []

        def setter(flags):
            calls.append(flags)
            return 1

        with keep_awake(setter) as accepted:
            self.assertTrue(accepted)
            self.assertEqual(calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED])
        self.assertEqual(calls[-1], ES_CONTINUOUS)

    def test_releases_even_when_the_job_fails(self):
        calls = []
        with self.assertRaises(RuntimeError):
            with keep_awake(lambda flags: calls.append(flags) or 1):
                raise RuntimeError("cycle broke")
        self.assertEqual(calls[-1], ES_CONTINUOUS)

    def test_a_refused_or_broken_request_never_stops_the_job(self):
        with keep_awake(lambda flags: 0) as accepted:
            self.assertFalse(accepted)

        def broken(flags):
            raise OSError("no power API")

        ran = []
        with keep_awake(broken) as accepted:
            ran.append(True)
        self.assertFalse(accepted)
        self.assertEqual(ran, [True])


if __name__ == "__main__":
    unittest.main()
