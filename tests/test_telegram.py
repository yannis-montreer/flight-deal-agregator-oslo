"""Tests de flightdeals.notify.telegram."""
from __future__ import annotations

import httpx
import pytest

from flightdeals.notify.telegram import (
    DealMessage,
    TelegramError,
    render_message,
    send_message,
    throttled_send,
)


def _deal(**overrides) -> DealMessage:
    defaults = dict(
        origin="OSL",
        destination="NRT",
        destination_name="Tokyo",
        price=3200.0,
        currency="NOK",
        departure_date="2027-01-12",
        return_date="2027-01-19",
        airline="SAS",
        stops=1,
        duration_minutes=1135,
        discount=0.40,
        score=87,
        source_url="https://www.google.com/travel/flights?example",
    )
    defaults.update(overrides)
    return DealMessage(**defaults)


class TestRenderMessage:
    def test_matches_spec_example_shape(self):
        text = render_message(_deal())
        assert "FLIGHT DEAL" in text
        assert "OSL → Tokyo (NRT)" in text
        assert "3 200 NOK A/R" in text
        assert "-40% vs historique" in text
        assert "12 → 19 janvier" in text
        assert "SAS" in text
        assert "1 escale" in text
        assert "Deal score: 87/100" in text
        assert "https://www.google.com/travel/flights?example" in text

    def test_nonstop_shows_vol_direct(self):
        assert "Vol direct" in render_message(_deal(stops=0))

    def test_multiple_stops_pluralizes(self):
        assert "2 escales" in render_message(_deal(stops=2))

    def test_unknown_stops(self):
        assert "Escales inconnues" in render_message(_deal(stops=None))

    def test_missing_airline_omits_airline_line(self):
        assert "SAS" not in render_message(_deal(airline=None))

    def test_one_way_omits_ar_suffix_and_return_date(self):
        text = render_message(_deal(return_date=None))
        assert "A/R" not in text
        assert "12 janvier" in text

    def test_dates_spanning_two_months(self):
        text = render_message(_deal(departure_date="2027-01-28", return_date="2027-02-03"))
        assert "28 janvier → 3 fevrier" in text

    def test_no_source_url_omits_link_lines(self):
        assert "Voir le vol" not in render_message(_deal(source_url=None))

    def test_duration_shown_as_hours_and_minutes(self):
        # 1135 minutes = 18h55 (exemple reel Spike : OSL -> Tokyo)
        assert "Duree totale: 18h55" in render_message(_deal(duration_minutes=1135))

    def test_duration_exact_hours_omits_minutes(self):
        assert "Duree totale: 4h" in render_message(_deal(duration_minutes=240))
        assert "Duree totale: 4h00" not in render_message(_deal(duration_minutes=240))

    def test_missing_duration_omits_the_line_entirely(self):
        assert "Duree totale" not in render_message(_deal(duration_minutes=None))

    def test_price_formatted_with_space_thousands_separator(self):
        assert "12 345 NOK" in render_message(_deal(price=12345))

    def test_no_destination_name_falls_back_to_code_only(self):
        text = render_message(_deal(destination_name=None))
        assert "OSL → NRT" in text


class TestSendMessage:
    def test_successful_send_does_not_raise(self, monkeypatch):
        def fake_post(url, data=None, timeout=None):
            assert "sendMessage" in url
            assert data["chat_id"] == "123"
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr("flightdeals.notify.telegram.httpx.post", fake_post)
        send_message("token", "123", "hello")  # ne doit pas lever

    def test_non_200_status_raises_telegram_error(self, monkeypatch):
        monkeypatch.setattr(
            "flightdeals.notify.telegram.httpx.post",
            lambda url, data=None, timeout=None: httpx.Response(404, text="not found"),
        )
        with pytest.raises(TelegramError):
            send_message("bad-token", "123", "hello")

    def test_ok_false_in_body_raises_telegram_error(self, monkeypatch):
        monkeypatch.setattr(
            "flightdeals.notify.telegram.httpx.post",
            lambda url, data=None, timeout=None: httpx.Response(
                200, json={"ok": False, "description": "chat not found"}
            ),
        )
        with pytest.raises(TelegramError):
            send_message("token", "bad-chat", "hello")

    def test_network_error_raises_telegram_error(self, monkeypatch):
        def raise_network_error(url, data=None, timeout=None):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr("flightdeals.notify.telegram.httpx.post", raise_network_error)
        with pytest.raises(TelegramError):
            send_message("token", "123", "hello")


class TestThrottledSend:
    def test_all_succeed_returns_all_deals(self, monkeypatch):
        monkeypatch.setattr(
            "flightdeals.notify.telegram.httpx.post",
            lambda url, data=None, timeout=None: httpx.Response(200, json={"ok": True}),
        )
        monkeypatch.setattr("flightdeals.notify.telegram.time.sleep", lambda _: None)

        deals = [_deal(destination="NRT"), _deal(destination="BKK")]
        sent = throttled_send("token", "123", deals, send_delay_seconds=0.01)
        assert len(sent) == 2

    def test_one_failure_does_not_block_others(self, monkeypatch):
        call_count = 0

        def fake_post(url, data=None, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, text="down")
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr("flightdeals.notify.telegram.httpx.post", fake_post)
        monkeypatch.setattr("flightdeals.notify.telegram.time.sleep", lambda _: None)

        deals = [_deal(destination="NRT"), _deal(destination="BKK")]
        sent = throttled_send("token", "123", deals, send_delay_seconds=0.01)
        assert len(sent) == 1
        assert sent[0].destination == "BKK"

    def test_sleeps_between_sends_but_not_after_the_last_one(self, monkeypatch):
        monkeypatch.setattr(
            "flightdeals.notify.telegram.httpx.post",
            lambda url, data=None, timeout=None: httpx.Response(200, json={"ok": True}),
        )
        sleep_calls: list[float] = []
        monkeypatch.setattr("flightdeals.notify.telegram.time.sleep", lambda s: sleep_calls.append(s))

        deals = [_deal(destination="NRT"), _deal(destination="BKK"), _deal(destination="ATH")]
        throttled_send("token", "123", deals, send_delay_seconds=1.5)
        assert sleep_calls == [1.5, 1.5]  # 2 sleeps entre 3 envois, aucun apres le dernier
