import unittest
import cv2
import numpy as np
from app.workers.captcha_rotation import estimate_rotation


class RotationTests(unittest.TestCase):
    def test_finds_rotation_in_synthetic_concentric_image(self):
        rng = np.random.default_rng(4)
        image = cv2.GaussianBlur(rng.integers(0, 256, (347, 347, 3), dtype=np.uint8), (0, 0), 5)
        inner = image[68:279, 68:279]
        rotated = cv2.warpAffine(inner, cv2.getRotationMatrix2D((105, 105), 55, 1), (211, 211))
        angle, _ = estimate_rotation(image, rotated)
        self.assertAlmostEqual(angle, 55, delta=2)

    def test_rejects_uninformative_or_nonconcentric_images(self):
        self.assertIsNone(estimate_rotation(np.zeros((347, 347, 3), dtype=np.uint8),
                                            np.zeros((211, 211, 3), dtype=np.uint8)))
        self.assertIsNone(estimate_rotation(np.zeros((200, 300, 3), dtype=np.uint8),
                                            np.zeros((100, 100, 3), dtype=np.uint8)))

    def test_different_sizes_and_angles(self):
        for size, inner_size in ((210, 128), (347, 211), (180, 105)):
            rng = np.random.default_rng(size)
            image = cv2.GaussianBlur(rng.integers(0, 256, (size, size, 3), dtype=np.uint8), (0, 0), 4)
            offset = (size - inner_size) // 2
            inner = image[offset:offset + inner_size, offset:offset + inner_size]
            center = ((inner_size - 1) / 2, (inner_size - 1) / 2)
            for degrees in range(0, 360, 30):
                with self.subTest(size=size, degrees=degrees):
                    rotated = cv2.warpAffine(inner, cv2.getRotationMatrix2D(center, degrees, 1), (inner_size, inner_size))
                    result = estimate_rotation(image, rotated)
                    self.assertIsNotNone(result)
                    self.assertLessEqual(abs((result[0] - degrees + 180) % 360 - 180), 5)

    def test_unrelated_images_are_not_guessed(self):
        rng = np.random.default_rng(11)
        for _ in range(30):
            outer = cv2.GaussianBlur(rng.integers(0, 256, (210, 210, 3), dtype=np.uint8), (0, 0), 4)
            inner = cv2.GaussianBlur(rng.integers(0, 256, (128, 128, 3), dtype=np.uint8), (0, 0), 4)
            self.assertIsNone(estimate_rotation(outer, inner))
