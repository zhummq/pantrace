class AstrBotConfig(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _Log:
    def debug(self, *a, **k): pass
    def info(self, msg, *a): print("    [INFO] " + (msg % a if a else msg))
    def warning(self, msg, *a): print("    [WARN] " + (msg % a if a else msg))
    def error(self, *a, **k): pass


logger = _Log()
