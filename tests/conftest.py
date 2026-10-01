import os
import sys
from pathlib import Path

import pytest

# Phase 4: the notifier must never pop a toast on the user's desktop or send a Telegram message from the unit suite
# (engine / executor / management tests build real services). tests/unit/test_notify.py removes it where needed.
os.environ["TS_NOTIFY_DISABLE"] = "1"
# … and never start a billed Claude diagnosis session (tools/monitor.py); test_monitor.py removes it in its fixture
os.environ["TS_MONITOR_NO_DIAGNOSE"] = "1"
# … and never download the news calendar (Phase 5 B8): the engines of the tests that run XAUUSD would fetch it
os.environ["TS_NEWS_NO_FETCH"] = "1"

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

# D-051: every pair is its own project folder whose config/config.yaml names that one pair. The unit suite tests the
# code (the same in every folder) against the three-pair config it was written for: tests/fixtures/config_three_pairs.yaml
# - in this process (DEFAULT_CONFIG) and in the tools it starts (TRADINGSYSTEM_CONFIG) - and never reads the folder's
# config.local.yaml. The folder's real config is checked by test_split_pairs.py (THIS_FOLDER_CONFIG).
THREE_PAIRS_CONFIG = ROOT / "tests" / "fixtures" / "config_three_pairs.yaml"
THIS_FOLDER_CONFIG = ROOT / "config" / "config.yaml"
from tradingsystem.core import settings as _settings  # noqa: E402
from tradingsystem.ai.providers import base as _provider_base  # noqa: E402

_settings.DEFAULT_CONFIG = THREE_PAIRS_CONFIG
_settings.LOCAL_CONFIG = ROOT / "tests" / "fixtures" / "no_config.local.yaml"      # never exists
os.environ["TRADINGSYSTEM_CONFIG"] = str(THREE_PAIRS_CONFIG)
# ... nor the folder's .env (imported at the switch, H33): a load_settings() without env_path would copy its
# EXECUTION_MODE / EXECUTION_TRIGGER / keys into os.environ for every later test (test_default_config_is_valid saw
# 'demo'); the providers' secret() and the notifier re-read it through base.ENV_FILE.
_settings.DEFAULT_ENV = ROOT / "tests" / "fixtures" / "no.env"                     # never exists
_provider_base.ENV_FILE = _settings.DEFAULT_ENV


def no_desk(s):
    """The settings with every pair's desk removed. config.yaml puts XAUUSD in shadow (D-049 / B14: never traded), so
    the executor tests that trade XAUUSD on paper (a stand-in for any traded pair) run with the desk off."""
    return s.model_copy(update={"pairs": {k: p.model_copy(update={"desk": None}) for k, p in s.pairs.items()}})


@pytest.fixture(scope="session")
def real_aggtrades():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "btcusdt_aggtrades_3000.csv")


@pytest.fixture(scope="session")
def real_candles_1m():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "btcusdt_candles_1m_3000.csv")


@pytest.fixture(scope="session")
def real_xau_ticks():
    import pyarrow.csv as pacsv
    return pacsv.read_csv(FIXTURES / "xauusd_ticks_3000.csv")
