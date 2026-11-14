import unittest

import cv2
import numpy as np

from app.workers.captcha_slide import find_gap


class SlideTests(unittest.TestCase):
    def setUp(self):
        self.piece = np.zeros((50, 50, 4), dtype=np.uint8)
        cv2.circle(self.piece, (25, 25), 20, (120, 120, 120, 255), -1)

    def test_different_gap_positions(self):
        for x in (60, 100, 150, 200, 240):
            for y in (20, 55, 90):
                with self.subTest(x=x, y=y):
                    background = np.full((160, 320, 3), 180, dtype=np.uint8)
                    cv2.circle(background, (x + 25, y + 25), 20, (30, 30, 30), -1)
                    location = find_gap(background, self.piece, y)
                    self.assertIsNotNone(location)
                    self.assertAlmostEqual(location * 320, x, delta=2)

    def test_unrelated_textures_do_not_match(self):
        rng = np.random.default_rng(20)
        for _ in range(30):
            background = cv2.GaussianBlur(rng.integers(0, 256, (160, 320, 3), dtype=np.uint8), (3, 3), 0)
            self.assertIsNone(find_gap(background, self.piece, 55))

    def test_invalid_piece_rejected(self):
        background = np.full((160, 320, 3), 180, dtype=np.uint8)
        self.assertIsNone(find_gap(background, np.zeros((50, 50, 3), dtype=np.uint8), 55))
