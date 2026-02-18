import unittest

from responses import no_store, ok_response
from utils import parse_photo_refs


class ParsePhotoRefsTests(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(parse_photo_refs(None), [])

    def test_list_normalization(self):
        value = [
            " https://example.com/a.jpg ",
            "",
            {"url": "https://example.com/b.jpg"},
            {"href": " https://example.com/c.jpg "},
            {"unknown": "skip"},
        ]
        self.assertEqual(
            parse_photo_refs(value),
            [
                "https://example.com/a.jpg",
                "https://example.com/b.jpg",
                "https://example.com/c.jpg",
            ],
        )

    def test_json_string_list(self):
        value = '["https://example.com/a.jpg", {"href": "https://example.com/b.jpg"}]'
        self.assertEqual(
            parse_photo_refs(value),
            ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        )

    def test_plain_string_passthrough(self):
        self.assertEqual(parse_photo_refs("photo-key"), ["photo-key"])


class NoStoreResponseTests(unittest.TestCase):
    def test_no_store_adds_cache_header(self):
        response = no_store(ok_response(None, {"ok": True}))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()
