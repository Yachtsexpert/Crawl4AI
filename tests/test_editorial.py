"""Regression tests for conservative OK-Boats editorial screening."""
import json
import tempfile
import unittest
from pathlib import Path

from monitor.editorial import (
    article_path, backfill_existing, classify, extract_article_links, render_report,
)

SOURCE = {
    "id": "axopar-news",
    "url": "https://www.axopar.com/media/newsroom/",
    "article_path_prefixes": ["/media/newsroom/press-releases/"],
}


class EditorialTests(unittest.TestCase):
    def test_index_not_article(self):
        self.assertFalse(article_path(SOURCE["url"], SOURCE))
        info = classify("Newsroom – Axopar", SOURCE["url"], SOURCE)
        self.assertEqual(info["category"], "SECTION_INDEX")

    def test_us_premiere_keeps_regional_scope(self):
        title = "Axopar Brings the Complete 38 Range to FLIBS with Two U.S. Premieres"
        info = classify(
            title, "https://www.axopar.com/media/newsroom/press-releases/flibs", SOURCE
        )
        self.assertEqual(info["category"], "PREMIERE")
        self.assertEqual(info["priority"], "P1")
        self.assertEqual(info["premiere_scope"], "REGIONAL_US_CLAIM_IN_TITLE")
        self.assertEqual(info["verification_status"], "NEEDS_EDITOR_REVIEW")

    def test_dealer_not_premiere(self):
        source = {"id": "fjord", "url": "https://fjordboats.com/us/news/"}
        info = classify(
            "HanseYachts AG Appoints New FJORD Dealer in Turkey",
            "https://fjordboats.com/us/news/dealer-turkey",
            source,
        )
        self.assertEqual(info["category"], "CORPORATE")
        self.assertEqual(info["priority"], "P3")

    def test_propulsion_vs_technical(self):
        url = "https://www.axopar.com/media/newsroom/press-releases/new-engines"
        self.assertEqual(
            classify("Axopar 38 adds Mercury V10 engines", url, SOURCE)["category"],
            "PROPULSION",
        )
        self.assertEqual(
            classify("Axopar unveils upgraded navigation system", url, SOURCE)["category"],
            "TECHNICAL_UPDATE",
        )

    def test_award_and_unverified_publication_date(self):
        source = {"id": "pressmare", "url": "https://www.pressmare.it/en/media/press-mare/"}
        info = classify(
            "Southern Wind SW108 Kiboko 4 wins at the ISS Awards",
            "https://www.pressmare.it/en/media/press-mare/2026-11-02/kiboko-4-wins",
            source,
        )
        self.assertEqual(info["category"], "CORPORATE")
        self.assertEqual(info["date_hint_from_url"], "2026-11-02")

    def test_links_do_not_leave_domain_or_duplicate(self):
        sample = (
            "[Axopar unveils new model](/media/newsroom/press-releases/new-model)\n"
            "[Axopar unveils new model](https://www.axopar.com/media/newsroom/press-releases/new-model?utm_x=1)\n"
            "[Elsewhere](https://evil.invalid/media/newsroom/press-releases/test)\n"
            "[Cookies](/cookie-policy)\n"
            "[Read more](/media/newsroom/press-releases/unnamed)\n"
        )
        links = extract_article_links(sample, SOURCE["url"], SOURCE)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["discovery_status"], "LINK_ONLY_UNFETCHED")

    def test_offline_backfill_ignores_index_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            entries = [
                {"source_id": SOURCE["id"], "page_url": SOURCE["url"],
                 "title": "Newsroom", "event": "NEW_PAGE", "content_sha256": "foo"},
                {"source_id": SOURCE["id"],
                 "page_url": "https://www.axopar.com/media/newsroom/press-releases/us-debut",
                 "title": "Axopar U.S. Premiere", "event": "NEW_PAGE", "content_sha256": "bar"},
            ]
            (folder / "changes.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
            )
            self.assertEqual(backfill_existing(folder, [SOURCE]), 1)
            first = (folder / "editorial_candidates.jsonl").read_text()
            self.assertEqual(backfill_existing(folder, [SOURCE]), 1)
            self.assertEqual(first, (folder / "editorial_candidates.jsonl").read_text())
            self.assertIn("OFFLINE_HISTORICAL_BACKFILL",
                          (folder / "editorial_latest.md").read_text())

    def test_empty_results_do_not_claim_no_news(self):
        report = render_report([], [{"status": "SUCCEEDED"}], mode="LIVE")
        self.assertIn("не доказывает отсутствие новостей", report)


if __name__ == "__main__":
    unittest.main()
