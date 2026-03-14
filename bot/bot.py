# See full file deployed on droplet at /root/kalshi-bot/bot.py
# Key fix on line 209: self.client = KalshiClient(self.auth, use_demo=config.USE_DEMO)
# Previously was: self.client = KalshiClient(self.auth, use_demo=not use_live)
# This caused 401 errors because paper mode (use_live=False) forced demo API URL
# but the API key is registered on production Kalshi.