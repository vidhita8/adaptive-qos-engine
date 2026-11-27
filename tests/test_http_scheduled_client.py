import random
import unittest

from traffic.http_scheduled_client import (
    build_parser,
    choose_chunk_bytes,
    choose_interval_s,
    validate_args,
)


class HttpScheduledClientTests(unittest.TestCase):

    def test_defaults_preserve_fixed_chunk_and_interval(self):
        parser = build_parser()
        args = parser.parse_args(["--output", "out.csv"])
        stop_s, min_c, max_c, min_i, max_i = validate_args(parser, args)

        self.assertEqual(stop_s, 120.0)
        self.assertEqual((min_c, max_c), (1_000_000, 1_000_000))
        self.assertEqual((min_i, max_i), (2.0, 2.0))

    def test_seeded_chunk_and_interval_variation_is_reproducible(self):
        rng_a = random.Random(77)
        rng_b = random.Random(77)

        chunks_a = [choose_chunk_bytes(rng_a, 100_000, 800_000) for _ in range(20)]
        chunks_b = [choose_chunk_bytes(rng_b, 100_000, 800_000) for _ in range(20)]
        self.assertEqual(chunks_a, chunks_b)
        self.assertTrue(all(100_000 <= c <= 800_000 for c in chunks_a))

        intervals_a = [choose_interval_s(rng_a, 0.2, 1.5) for _ in range(20)]
        intervals_b = [choose_interval_s(rng_b, 0.2, 1.5) for _ in range(20)]
        self.assertEqual(intervals_a, intervals_b)
        self.assertTrue(all(0.2 <= iv <= 1.5 for iv in intervals_a))

    def test_invalid_ranges_are_rejected(self):
        parser = build_parser()
        for bad_args in (
            ["--output", "out.csv", "--min-chunk-bytes", "500", "--max-chunk-bytes", "100"],
            ["--output", "out.csv", "--min-interval", "2.0", "--max-interval", "1.0"],
            ["--output", "out.csv", "--min-interval", "0"],
        ):
            with self.subTest(bad_args=bad_args):
                args = parser.parse_args(bad_args)
                with self.assertRaises(SystemExit):
                    validate_args(parser, args)


if __name__ == "__main__":
    unittest.main()
