# This file is managed via automated deployment.
# See settlement_checker.py for the KXBTCD settlement logic.
# Full bot.py is deployed directly to the server via SCP.
# GitHub copy kept for reference — server copy is authoritative.
#
# v2.3 Changes:
# - Added SettlementChecker import and initialization
# - Added _check_btc_settlements() method in main loop (every 60s)
# - Settlement resolves expired KXBTCD positions automatically
# - Credits paper balance, records trades, updates journal
# - Verified: 28 positions settled on first cycle (28W/0L, +$23.71)
#
# The actual bot.py on the server is 1125 lines and includes:
# - Weather scanning (ECMWF ensemble)
# - Sports scanning (ESPN NBA/NCAA)
# - BTC hourly price farming (KXBTCD)
# - Arbitrage scanning
# - Settlement checker (v2.3)
# - Trade journal with Brier score safety gate
# - Systemd watchdog integration
# - Paper trading with full balance tracking
#
# Server: 143.244.141.197 /root/kalshi-bot/bot.py
# Deploy: via SCP expect scripts
