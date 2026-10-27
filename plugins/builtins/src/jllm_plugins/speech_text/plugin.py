from . import _apply_trailing_speed, _parse_pieces, _parse_speed_pieces, parse_commands


class SpeechText:
    parse_commands = staticmethod(parse_commands)
    parse_pieces = staticmethod(_parse_pieces)
    parse_speed_pieces = staticmethod(_parse_speed_pieces)
    apply_trailing_speed = staticmethod(_apply_trailing_speed)


class Plugin:
    async def setup(self, ctx):
        return {"speech.text": SpeechText()}

    async def start(self):
        pass

    async def stop(self):
        pass
