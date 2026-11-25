"""aiohttp 的最小替身：这几条用例都不打真实 HTTP（_report 被替换掉）。"""


class ClientTimeout:
    def __init__(self, total=None, **kw):
        self.total = total


class ClientError(Exception):
    pass


class ClientSession:
    def __init__(self, *a, **kw):
        self.closed = False

    def post(self, *a, **kw):
        raise RuntimeError("测试环境不打真实 HTTP")

    async def close(self):
        self.closed = True
