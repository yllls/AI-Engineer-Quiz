"""Browser-level verification of filtering, pagination, export, and downloads."""
from __future__ import annotations

import codecs
import csv
import io
import json
import os
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("KEEPSTACK_E2E") != "1",
        reason="Set KEEPSTACK_E2E=1 to run browser tests",
    ),
]


def _card_ids(page) -> list[str]:
    return page.locator("#grid .card").evaluate_all(
        "cards => cards.map(card => card.dataset.uuid)"
    )


def _add_tag(page, tag: str) -> None:
    page.click("#tag-search")
    page.fill("#tag-search", tag)
    result = page.locator(f'#tag-results .tag-result[data-tag="{tag}"]')
    expect(result).to_be_visible()
    result.click()


def test_library_loads_with_keyword_only_interface(logged_in_page):
    page = logged_in_page
    expect(page.locator("#grid .card")).to_have_count(10)
    expect(page.locator("#result-summary")).to_have_text("33 assets")
    expect(page.locator("#search-mode")).to_have_count(0)
    expect(page.locator("#facets")).to_have_count(0)
    expect(page.locator("#btn-clear-filters")).to_be_disabled()
    expect(page.locator("#page-label")).to_have_text("Page 1")
    row_tops = page.locator("#grid .card").evaluate_all(
        "cards => cards.map(card => Math.round(card.getBoundingClientRect().top))"
    )
    assert row_tops.count(row_tops[0]) == 5


def test_tag_candidates_load_more(logged_in_page):
    page = logged_in_page
    page.click("#tag-search")
    expect(page.locator("#tag-results .tag-result")).to_have_count(20)
    more = page.locator("#tag-results .tag-more")
    expect(more).to_be_visible()
    more.click()
    expect(page.locator("#tag-results .tag-result")).to_have_count(29)
    expect(page.locator("#tag-results .tag-more")).to_have_count(0)


def test_combined_filters_active_chips_and_clear(logged_in_page):
    page = logged_in_page
    page.fill("#search", "harbor")
    expect(page.locator("#result-summary")).to_have_text("30 assets")

    page.select_option("#filter-collection", label="Harbor Campaign")
    expect(page.locator("#result-summary")).to_have_text("20 assets")

    _add_tag(page, "approved")
    expect(page.locator("#result-summary")).to_have_text("18 assets")
    _add_tag(page, "campaign")
    expect(page.locator("#result-summary")).to_have_text("15 assets")
    _add_tag(page, "internal")
    expect(page.locator("#result-summary")).to_have_text("2 assets")

    page.locator(
        '#active-filters button[data-action="toggle-tag"][data-tag="internal"]'
    ).click()
    expect(page.locator("#result-summary")).to_have_text("13 assets")
    expect(page.locator('.filter-chip.include[data-tag="approved"]')).to_be_visible()
    expect(page.locator('.filter-chip.include[data-tag="campaign"]')).to_be_visible()
    expect(page.locator('.filter-chip.exclude[data-tag="internal"]')).to_be_visible()
    expect(page.locator("#active-filters .filter-chip")).to_have_count(5)

    page.select_option("#sort", "oldest")
    expect(page.locator("#result-summary")).to_have_text("13 assets")
    page.select_option("#page-size", "20")
    expect(page.locator("#result-summary")).to_have_text("13 assets")

    page.click("#btn-clear-filters")
    expect(page.locator("#result-summary")).to_have_text("33 assets")
    expect(page.locator("#search")).to_have_value("")
    expect(page.locator("#filter-collection")).to_have_value("")
    expect(page.locator("#sort")).to_have_value("oldest")
    expect(page.locator("#page-size")).to_have_value("20")
    expect(page.locator("#active-filters")).to_be_hidden()


def test_filters_and_sort_round_trip_through_url(logged_in_page):
    page = logged_in_page
    page.fill("#search", "harbor")
    expect(page.locator("#result-summary")).to_have_text("30 assets")
    _add_tag(page, "approved")
    expect(page.locator("#result-summary")).to_have_text("18 assets")
    page.select_option("#sort", "oldest")

    assert "q=harbor" in page.url
    assert "tags=approved" in page.url
    assert "sort=oldest" in page.url

    page.reload()
    expect(page.locator("#grid .card").first).to_be_visible()
    expect(page.locator("#result-summary")).to_have_text("18 assets")
    expect(page.locator("#search")).to_have_value("harbor")
    expect(page.locator('.filter-chip.include[data-tag="approved"]')).to_be_visible()
    expect(page.locator("#sort")).to_have_value("oldest")
    expect(page.locator("#page-label")).to_have_text("Page 1")


def test_cursor_pagination_is_stable_with_equal_timestamps(logged_in_page):
    page = logged_in_page
    page.fill("#search", "harbor")
    expect(page.locator("#result-summary")).to_have_text("30 assets")
    expect(page.locator("#grid .card")).to_have_count(10)
    expect(page.locator("#prev-page")).to_be_disabled()
    expect(page.locator("#next-page")).to_be_enabled()
    first_ids = _card_ids(page)

    page.click("#next-page")
    expect(page.locator("#page-label")).to_have_text("Page 2")
    expect(page.locator("#grid .card")).to_have_count(10)
    expect(page.locator("#prev-page")).to_be_enabled()
    expect(page.locator("#next-page")).to_be_enabled()
    second_ids = _card_ids(page)

    page.click("#next-page")
    expect(page.locator("#page-label")).to_have_text("Page 3")
    expect(page.locator("#grid .card")).to_have_count(10)
    expect(page.locator("#prev-page")).to_be_enabled()
    expect(page.locator("#next-page")).to_be_disabled()
    third_ids = _card_ids(page)

    all_ids = first_ids + second_ids + third_ids
    assert set(first_ids).isdisjoint(set(second_ids))
    assert set(first_ids).isdisjoint(set(third_ids))
    assert set(second_ids).isdisjoint(set(third_ids))
    assert len(set(all_ids)) == 30

    page.click("#prev-page")
    expect(page.locator("#page-label")).to_have_text("Page 2")
    page.click("#prev-page")
    expect(page.locator("#page-label")).to_have_text("Page 1")
    expect(page.locator("#grid .card")).to_have_count(10)
    assert _card_ids(page) == first_ids


def test_export_columns_download_and_preferences(logged_in_page, tmp_path):
    page = logged_in_page
    page.fill("#search", "harbor")
    expect(page.locator("#result-summary")).to_have_text("30 assets")

    page.click("#btn-export")
    expect(page.locator("#export-modal")).to_be_visible()
    expect(page.locator("#export-count")).to_contain_text("30 matching assets")
    page.check('input[data-export-column="alt_text"]')
    page.check('input[data-export-column="size"]')

    with page.expect_download() as download_info:
        page.click("#export-download")
    download = download_info.value
    target = tmp_path / "export.csv"
    download.save_as(target)

    raw = target.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert b"\r\n" in raw
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    header = rows[0]
    assert header == [
        "uuid",
        "title",
        "description",
        "filename",
        "media_type",
        "tags",
        "created_at",
        "alt_text",
        "size",
    ]
    assert len(rows) == 31
    assert download.suggested_filename.startswith("keepstack-assets-")
    assert re.match(r"^keepstack-assets-\d{8}T\d{6}Z\.csv$", download.suggested_filename)

    by_title = {row[header.index("title")]: row for row in rows[1:]}
    tricky = by_title["Harbor campaign asset 00"]
    assert tricky[header.index("description")] == '中文说明, 含逗号, "双引号"\n以及换行'
    assert json.loads(tricky[header.index("tags")]) == ["approved", "campaign"]
    assert tricky[header.index("alt_text")] == "港口归档图片"
    formula = by_title["Harbor campaign asset 05"]
    assert formula[header.index("description")] == "'=SUM(A1:A2)"

    stored = json.loads(
        page.evaluate("localStorage.getItem('keepstack_export_optional_columns')")
    )
    assert set(stored) == {"alt_text", "size"}

    page.click("#export-close")
    page.click("#btn-export")
    expect(page.locator('input[data-export-column="alt_text"]')).to_be_checked()
    expect(page.locator('input[data-export-column="size"]')).to_be_checked()
    expect(page.locator('input[data-export-column="mime_type"]')).not_to_be_checked()


def test_export_over_limit_error_is_shown(logged_in_page):
    page = logged_in_page
    page.fill("#search", "harbor")
    expect(page.locator("#result-summary")).to_have_text("30 assets")

    def too_large(route):
        route.fulfill(
            status=413,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "code": "export_too_large",
                        "matched": 30,
                        "limit": 2,
                        "message": "Export matched 30 rows, exceeding the limit of 2",
                    }
                }
            ),
        )

    page.route("**/api/exports/assets.csv*", too_large)
    page.click("#btn-export")
    page.click("#export-download")
    expect(page.locator("#export-error")).to_be_visible()
    expect(page.locator("#export-error")).to_contain_text("above the 2-row limit")
    expect(page.locator("#export-download")).to_be_enabled()


def test_list_failure_clears_results_and_retry_recovers(logged_in_page):
    page = logged_in_page

    def fail(route):
        route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({"detail": "Temporary failure"}),
        )

    page.route("**/api/assets?*", fail)
    page.select_option("#sort", "oldest")
    expect(page.locator("#library-error")).to_be_visible()
    expect(page.locator("#library-error-text")).to_contain_text("Temporary failure")
    expect(page.locator("#btn-export")).to_be_disabled()
    expect(page.locator("#grid .card")).to_have_count(0)

    page.unroute("**/api/assets?*", fail)
    page.click("#btn-retry")
    expect(page.locator("#library-error")).to_be_hidden()
    expect(page.locator("#grid .card").first).to_be_visible()


def test_asset_original_download_uses_authentication(logged_in_page, tmp_path):
    page = logged_in_page
    page.locator("#grid .card").first.click()
    expect(page.locator("#drawer")).to_be_visible()
    expect(page.locator("#d-download")).to_be_visible()

    with page.expect_download() as download_info:
        page.click("#d-download")
    download = download_info.value
    assert download.suggested_filename.endswith(".jpg")
    target = tmp_path / download.suggested_filename
    download.save_as(target)
    assert target.stat().st_size > 0


def test_latest_filter_wins_when_an_older_request_is_slow(page, e2e_server):
    page.add_init_script(
        """
        (() => {
          const realFetch = window.fetch.bind(window);
          let delayed = false;
          window.fetch = (input, init = {}) => {
            const url = typeof input === "string" ? input : input.url;
            const method = (init.method || "GET").toUpperCase();
            if (!delayed && method === "GET" && url.includes("/api/assets?")
                && url.includes("q=harbor")) {
              delayed = true;
              return new Promise((resolve, reject) => {
                const timer = setTimeout(() => realFetch(input, init).then(resolve, reject), 900);
                const signal = init.signal;
                if (signal) {
                  if (signal.aborted) {
                    clearTimeout(timer);
                    reject(new DOMException("Aborted", "AbortError"));
                  } else {
                    signal.addEventListener("abort", () => {
                      clearTimeout(timer);
                      reject(new DOMException("Aborted", "AbortError"));
                    }, { once: true });
                  }
                }
              });
            }
            return realFetch(input, init);
          };
        })();
        """
    )
    page.goto(e2e_server, wait_until="networkidle")
    page.fill("#login-user", "admin")
    page.fill("#login-pass", "admin")
    page.click("#login-form button[type=submit]")
    expect(page.locator("#grid .card").first).to_be_visible()

    page.fill("#search", "harbor")
    page.wait_for_timeout(400)
    page.fill("#search", "mountain")
    expect(page.locator("#result-summary")).to_have_text("3 assets")
    page.wait_for_timeout(1200)
    expect(page.locator("#result-summary")).to_have_text("3 assets")
    assert "q=mountain" in page.url


def test_asset_tag_can_be_added_with_button(logged_in_page):
    page = logged_in_page
    tag = "bulk-tag-24"
    page.locator("#grid .card").first.click()
    expect(page.locator("#drawer")).to_be_visible()
    expect(page.locator("#tag-add-submit")).to_be_visible()
    expect(page.locator("#tag-add-submit")).to_be_enabled()

    page.fill("#tag-add", tag)
    page.click("#tag-add-submit")
    tag_chip = page.locator("#tag-row .tag").filter(has_text=tag)
    expect(tag_chip).to_be_visible()

    page.locator(f'#tag-row .rm[data-tag="{tag}"]').click()
    expect(page.locator("#tag-row .tag").filter(has_text=tag)).to_have_count(0)
