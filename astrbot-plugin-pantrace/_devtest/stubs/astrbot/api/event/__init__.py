class AstrMessageEvent:
    pass


class _Filter:
    class EventMessageType:
        GROUP_MESSAGE = "group"

    @staticmethod
    def event_message_type(kind):
        def deco(fn):
            fn.__pantrace_handler__ = kind
            return fn
        return deco


filter = _Filter()
