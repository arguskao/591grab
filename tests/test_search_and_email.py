import base64
import json
import logging
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import search_and_email as app


def base_search(**overrides):
    search = {
        "id": "tainan_east_5plus",
        "name": "台南東區",
        "enabled": True,
        "region_id": 15,
        "city": "台南市",
        "section_id": 206,
        "district": "東區",
        "price_min_wan": 600,
        "price_max_wan": 1200,
        "rooms_min": 5,
        "posted_since": "2026-07-01",
        "max_pages": 20,
    }
    search.update(overrides)
    return search


def validated_search(**overrides):
    return app.validate_searches({"searches": [base_search(**overrides)]})[0]


def raw_listing(listing_id, *, timestamp=None, **overrides):
    if timestamp is None:
        timestamp = int(
            datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        )
    item = {
        "houseid": listing_id,
        "type": "2",
        "title": f"物件 {listing_id}",
        "region_name": "台南市",
        "section_name": "東區",
        "section_id": 206,
        "address": "東門路一段",
        "price": "800",
        "unitprice": "20",
        "area": "40坪",
        "mainarea": "30坪",
        "room": "5房2廳2衛",
        "floor": "3F/10F",
        "posttime": timestamp,
        "photo_url": "https://img.example.invalid/a.jpg",
    }
    item.update(overrides)
    return item


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.params = []

    def get_json(self, _base, _path, *, params, referer, origin):
        self.params.append(params)
        if not self.responses:
            raise AssertionError("unexpected extra API call")
        return self.responses.pop(0)


class ConfigurationTests(unittest.TestCase):
    def test_validates_and_normalizes_search(self):
        search = validated_search()
        self.assertEqual(search["region_id"], 15)
        self.assertEqual(search["price_min_wan"], 600.0)
        self.assertEqual(search["posted_since"], "2026-07-01")

    def test_rejects_unknown_key_and_path_traversal_id(self):
        with self.assertRaisesRegex(ValueError, "未知欄位"):
            app.validate_searches(
                {"searches": [base_search(price_minimum_wan=600)]}
            )
        with self.assertRaisesRegex(ValueError, "id 只能"):
            app.validate_searches({"searches": [base_search(id="../result")]})

    def test_rejects_duplicate_invalid_range_and_date_conflict(self):
        with self.assertRaisesRegex(ValueError, "id 重複"):
            app.validate_searches(
                {"searches": [base_search(), base_search()]}
            )
        with self.assertRaisesRegex(ValueError, "最低總價"):
            app.validate_searches(
                {"searches": [base_search(price_min_wan=1300)]}
            )
        with self.assertRaisesRegex(ValueError, "只能擇一"):
            app.validate_searches(
                {"searches": [base_search(posted_within_days=7)]}
            )

    def test_location_mapping_must_match_pipeline_config(self):
        search = validated_search(city="高雄市")
        with self.assertRaisesRegex(ValueError, "不是 高雄市"):
            app.validate_location_mapping(
                [search], {"target_cities": {15: "台南市"}}
            )

    def test_output_dir_cannot_escape_project(self):
        with self.assertRaisesRegex(ValueError, "不可離開"):
            app.resolve_output_dir({"output_dir": "../elsewhere"})


class QueryAndFilterTests(unittest.TestCase):
    def setUp(self):
        self.search = validated_search()
        self.taipei = ZoneInfo("Asia/Taipei")
        self.run_date = date(2026, 8, 8)

    def test_current_591_query_syntax(self):
        params = app.build_query_params(self.search, 30, 0, 0)
        self.assertEqual(params["section"], 206)
        self.assertEqual(params["price"], "600$_1200$")
        self.assertEqual(params["pattern"], "5")
        self.assertEqual(params["order"], "postTime_desc")

        relative = validated_search(posted_since=None, posted_within_days=7)
        params = app.build_query_params(relative, 30, 0, 0)
        self.assertEqual(params["publish_day"], 7)

    def test_room_buckets(self):
        self.assertEqual(app.build_room_query(4), "4,5")
        self.assertEqual(app.build_room_query(5), "5")
        self.assertEqual(app.build_room_query(8), "5")

    def test_timestamp_uses_taipei_timezone_even_when_numeric_string(self):
        timestamp = str(
            int(datetime(2026, 6, 30, 16, 30, tzinfo=timezone.utc).timestamp())
        )
        parsed = app.parse_item_posted_date(
            timestamp, self.taipei, self.run_date
        )
        self.assertEqual(parsed, date(2026, 7, 1))

    def test_exact_boundaries_are_inclusive(self):
        row = app.normalize_scheduled_item(
            raw_listing(123, price="600", room="5房1廳1衛"),
            self.search,
            self.taipei,
            self.run_date,
        )
        self.assertTrue(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )

        row["total_price_wan"] = 1200
        self.assertTrue(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )
        row["total_price_wan"] = 1200.1
        self.assertFalse(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )

    def test_wrong_district_room_or_old_date_is_excluded(self):
        row = app.normalize_scheduled_item(
            raw_listing(123), self.search, self.taipei, self.run_date
        )
        row["district"] = "永康區"
        self.assertFalse(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )

        row["district"] = "東區"
        row["rooms"] = 4
        self.assertFalse(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )

        row["rooms"] = 5
        row["posted_date"] = "2026-06-30"
        self.assertFalse(
            app.row_matches_search(row, self.search, date(2026, 7, 1))
        )


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.search = validated_search(max_pages=5)
        self.pipeline = {
            "sale": {
                "api_base": "https://example.invalid",
                "api_path": "/sale/list",
                "referer": "https://example.invalid/",
                "origin": "https://example.invalid",
                "items_per_page": 30,
                "max_pages": 60,
            }
        }
        self.logger = logging.getLogger("pagination-test")

    def test_uses_total_not_raw_ad_count_and_deduplicates(self):
        first_page = [raw_listing(index) for index in range(1, 31)]
        first_page.extend(
            [
                raw_listing(9001, type="8", is_newhouse=1),
                raw_listing(9002, type="8", is_newhouse=1),
            ]
        )
        alias = dict(first_page[28])
        alias["houseid"] = 24770029
        second_page = [raw_listing(31), raw_listing(1), alias]
        client = FakeClient(
            [
                {"status": 1, "data": {"total": "31", "house_list": first_page}},
                {"status": 1, "data": {"total": "31", "house_list": second_page}},
            ]
        )

        rows = app.search_listings(
            client,
            self.pipeline,
            self.search,
            ZoneInfo("Asia/Taipei"),
            date(2026, 8, 8),
            self.logger,
        )

        self.assertEqual(len(rows), 31)
        self.assertIn(29, {row["listing_id"] for row in rows})
        self.assertNotIn(24770029, {row["listing_id"] for row in rows})
        self.assertEqual([call["firstRow"] for call in client.params], [0, 30])
        self.assertEqual(client.params[1]["totalRows"], 31)

    def test_max_pages_truncation_is_an_error(self):
        search = validated_search(max_pages=1)
        client = FakeClient(
            [
                {
                    "status": 1,
                    "data": {
                        "total": "100",
                        "house_list": [raw_listing(index) for index in range(1, 31)],
                    },
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "未寄成完整結果"):
            app.search_listings(
                client,
                self.pipeline,
                search,
                ZoneInfo("Asia/Taipei"),
                date(2026, 8, 8),
                self.logger,
            )


class OutputAndEmailTests(unittest.TestCase):
    def setUp(self):
        self.search = validated_search()
        self.row = app.normalize_scheduled_item(
            raw_listing(20695210),
            self.search,
            ZoneInfo("Asia/Taipei"),
            date(2026, 8, 8),
        )

    def test_csv_has_bom_fixed_columns_and_one_clean_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.csv"
            app.save_csv([self.row], path)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            frame = pd.read_csv(path)
            self.assertEqual(list(frame.columns), app.CSV_COLUMNS)
            self.assertNotIn("photo_url", frame.columns)
            self.assertEqual(
                frame.loc[0, "listing_url"],
                "https://sale.591.com.tw/home/house/detail/2/20695210.html",
            )

    def test_email_escapes_title_and_uses_only_listing_url(self):
        self.row["title"] = "<危險 & 標題>"
        subject, html_body, _text = app.build_email_content(
            [{"search": self.search, "rows": [self.row]}],
            [],
            date(2026, 8, 8),
            "591\r\nInjected",
            10,
        )
        self.assertNotIn("\n", subject)
        self.assertIn("&lt;危險 &amp; 標題&gt;", html_body)
        self.assertIn(
            'href="https://sale.591.com.tw/home/house/detail/2/20695210.html"',
            html_body,
        )
        self.assertNotIn("img.example.invalid", html_body)

    def test_attachment_round_trip_and_resend_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.csv"
            path.write_bytes(b"hello,csv\n")
            encoded = app.build_attachments([path])[0]["content"]
            self.assertEqual(base64.b64decode(encoded), path.read_bytes())

            response = Mock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"id": "email_123"}
            with patch.object(app.requests, "post", return_value=response) as post:
                message_id = app.send_with_resend(
                    api_key="re_secret",
                    sender="sender@example.com",
                    recipients=["recipient@gmail.com"],
                    subject="subject",
                    html_body="<p>body</p>",
                    text_body="body",
                    attachments=[path],
                    idempotency_key="test-key",
                )

            self.assertEqual(message_id, "email_123")
            request_body = json.loads(post.call_args.kwargs["data"].decode("utf-8"))
            self.assertEqual(request_body["to"], ["recipient@gmail.com"])
            self.assertEqual(request_body["attachments"][0]["filename"], "result.csv")
            self.assertEqual(
                post.call_args.kwargs["headers"]["Idempotency-Key"], "test-key"
            )


if __name__ == "__main__":
    unittest.main()
