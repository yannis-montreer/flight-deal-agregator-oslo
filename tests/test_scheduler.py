"""Tests de flightdeals.scheduler."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from flightdeals.scheduler import run_forever, seconds_until_next_run


class TestSecondsUntilNextRun:
    def test_target_later_today_returns_seconds_until_then(self):
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "14:00") == 4 * 3600

    def test_target_already_passed_today_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
        # de 10h aujourd'hui a 2h demain = 16h
        assert seconds_until_next_run(now, "02:00") == 16 * 3600

    def test_target_exactly_now_rolls_to_tomorrow_not_zero(self):
        now = datetime(2026, 8, 18, 2, 0, 0, tzinfo=timezone.utc)
        # pas 0 : un sleep(0) tournerait en rafale plusieurs fois la meme seconde
        assert seconds_until_next_run(now, "02:00") == 24 * 3600

    def test_target_one_second_from_now(self):
        now = datetime(2026, 8, 18, 1, 59, 59, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "02:00") == 1

    def test_seconds_and_microseconds_of_now_are_not_ignored_in_the_wait(self):
        now = datetime(2026, 8, 18, 1, 59, 30, 500000, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "02:00") == pytest.approx(29.5)

    def test_crossing_midnight_from_late_evening(self):
        now = datetime(2026, 8, 18, 23, 0, 0, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "00:30") == 1.5 * 3600

    def test_restart_shortly_after_target_time_rolls_to_tomorrow(self):
        # simule un redemarrage du conteneur juste apres l'heure planifiee : ne doit jamais
        # redeclencher immediatement un 2e run le meme jour (voir plan, risque "clock drift")
        now = datetime(2026, 8, 18, 2, 0, 1, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "02:00") == pytest.approx(24 * 3600 - 1)


class TestSecondsUntilNextRunWithTimezone:
    """Demande utilisateur : "10:30 heure d'Oslo" toute l'annee, sans derive au changement
    heure ete/hiver. zoneinfo (stdlib + tzdata, voir requirements.txt) gere ca nativement —
    ces tests verifient que le MEME "10:30" produit un equivalent UTC different selon la
    saison, ce qui prouve que le fuseau est reellement pris en compte (pas un decalage fixe)."""

    def test_summer_uses_cest_offset_utc_plus_2(self):
        # 2026-08-18 est en plein ete (CEST, UTC+2) -> 10:30 Oslo = 08:30 UTC
        now = datetime(2026, 8, 18, 6, 0, 0, tzinfo=timezone.utc)
        seconds = seconds_until_next_run(now, "10:30", "Europe/Oslo")
        assert seconds == pytest.approx(2.5 * 3600)  # 06:00 -> 08:30 UTC

    def test_winter_uses_cet_offset_utc_plus_1(self):
        # 2026-01-15 est en plein hiver (CET, UTC+1) -> 10:30 Oslo = 09:30 UTC
        now = datetime(2026, 1, 15, 6, 0, 0, tzinfo=timezone.utc)
        seconds = seconds_until_next_run(now, "10:30", "Europe/Oslo")
        assert seconds == pytest.approx(3.5 * 3600)  # 06:00 -> 09:30 UTC (1h de plus qu'en ete)

    def test_defaults_to_utc_when_timezone_omitted(self):
        # retro-compatibilite : trip_watch appelle cette fonction sans 3e argument
        now = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
        assert seconds_until_next_run(now, "14:00") == 4 * 3600

    def test_now_in_a_different_timezone_still_computes_correctly(self):
        # `now` n'a pas besoin d'etre en UTC : seconds_until_next_run doit gerer n'importe
        # quel fuseau d'entree (astimezone() le convertit correctement avant de comparer)
        now_ny = datetime(2026, 8, 18, 2, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # = 06:00 UTC
        seconds = seconds_until_next_run(now_ny, "10:30", "Europe/Oslo")
        assert seconds == pytest.approx(2.5 * 3600)


class _FakeCollectionConfig:
    def __init__(self, enabled: bool, schedule: str, timezone: str = "UTC"):
        self.enabled = enabled
        self.schedule = schedule
        self.timezone = timezone


class _FakeConfig:
    def __init__(self, enabled: bool, schedule: str = "02:00", timezone: str = "UTC"):
        self.collection = _FakeCollectionConfig(enabled, schedule, timezone)


class _StopLoop(Exception):
    """Sentinelle utilisee par les tests pour sortir proprement de la boucle infinie de
    run_forever apres un nombre controle d'iterations."""


class TestRunForever:
    def test_sleeps_then_calls_run_once_each_iteration(self, monkeypatch):
        # NB: le signal d'arret (_StopLoop) doit etre leve depuis load_config_fn, PAS depuis
        # run_once_fn — run_forever avale volontairement les exceptions de run_once_fn (c'est
        # le comportement de resilience teste plus bas), donc une exception levee depuis
        # run_once_fn y serait silencieusement absorbee et la boucle ne s'arreterait jamais.
        sleep_calls: list[float] = []
        monkeypatch.setattr("flightdeals.scheduler.time.sleep", lambda s: sleep_calls.append(s))

        run_once_calls: list = []
        load_count = 0

        def fake_load_config():
            nonlocal load_count
            load_count += 1
            if load_count > 4:  # 2 iterations completes (2 lectures de config chacune)
                raise _StopLoop()
            return _FakeConfig(enabled=True)

        with pytest.raises(_StopLoop):
            run_forever(lambda c: run_once_calls.append(c), fake_load_config)

        assert len(run_once_calls) == 2
        assert len(sleep_calls) == 2

    def test_disabled_collection_never_calls_run_once(self, monkeypatch):
        monkeypatch.setattr("flightdeals.scheduler.time.sleep", lambda s: None)

        call_count = 0

        def fake_load_config():
            nonlocal call_count
            call_count += 1
            if call_count > 4:  # 2 iterations completes (2 lectures de config chacune)
                raise _StopLoop()
            return _FakeConfig(enabled=False)

        run_once_calls: list = []

        with pytest.raises(_StopLoop):
            run_forever(lambda c: run_once_calls.append(c), fake_load_config)

        assert run_once_calls == []

    def test_exception_in_run_once_does_not_crash_the_loop(self, monkeypatch):
        monkeypatch.setattr("flightdeals.scheduler.time.sleep", lambda s: None)

        run_once_calls: list = []

        def flaky_run_once(config):
            run_once_calls.append(config)
            if len(run_once_calls) == 1:
                raise RuntimeError("panne simulee dans run_once")  # doit etre absorbee par run_forever

        load_count = 0

        def fake_load_config():
            nonlocal load_count
            load_count += 1
            if load_count > 4:  # laisse le temps a 2 iterations completes de run_once_fn
                raise _StopLoop()
            return _FakeConfig(enabled=True)

        with pytest.raises(_StopLoop):
            run_forever(flaky_run_once, fake_load_config)

        # la 1ere panne (RuntimeError) n'a pas arrete la boucle : un 2e appel a bien eu lieu
        assert len(run_once_calls) == 2

    def test_config_reloaded_fresh_on_each_iteration(self, monkeypatch):
        monkeypatch.setattr("flightdeals.scheduler.time.sleep", lambda s: None)

        load_count = 0

        def fake_load_config():
            nonlocal load_count
            load_count += 1
            if load_count > 4:
                raise _StopLoop()
            return _FakeConfig(enabled=True)

        run_once_calls: list = []

        with pytest.raises(_StopLoop):
            run_forever(lambda c: run_once_calls.append(c), fake_load_config)

        # 2 lectures de config par iteration (avant le sleep, puis apres) -> au moins 2 runs
        assert len(run_once_calls) >= 2
