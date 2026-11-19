"""Lead-lag recorder and shadow engine (OKX ANT leader, other venues followers).

Separate strategy from Omnibook: it has its own version line.
Public market data only. This package never places orders and never reads keys.
"""

VERSION = "leadlag-0.2.2"   # the owner calls this strategy "the OKX bot"
