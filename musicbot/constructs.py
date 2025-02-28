import inspect
import json
import logging
import pydoc

from .utils import _get_variable

log = logging.getLogger(__name__)

class BetterLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.relativeCreated /= 1000


class SkipState:
    __slots__ = ['skippers', 'skip_msgs']

    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    __slots__ = ['_content', 'reply', 'delete_after', 'codeblock', '_codeblock']

    def __init__(self, content, reply=False, delete_after=0, codeblock=None):
        self._content = content
        self.reply = reply
        self.delete_after = delete_after
        self.codeblock = codeblock
        self._codeblock = "```{!s}\n{{}}\n```".format('' if codeblock is True else codeblock)

    @property
    def content(self):
        if self.codeblock:
            return self._codeblock.format(self._content)
        else:
            return self._content

class AnimatedResponse(Response):
    def __init__(self, content, *sequence, delete_after=0):
        super().__init__(content, delete_after=delete_after)
        self.sequence = sequence


class Serializer(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, '__json__'):
            return o.__json__()

        return super().default(o)

    @classmethod
    def deserialize(cls, data):
        if all(x in data for x in Serializable._class_signature):
            factory = pydoc.locate(data['__module__'] + '.' + data['__class__'])
            if factory and issubclass(factory, Serializable):
                return factory._deserialize(data['data'], **cls._get_vars(factory._deserialize))

        return data

    @classmethod
    def _get_vars(cls, func):
        params = inspect.signature(func).parameters.copy()
        args = {}

        for name, param in params.items():
            if param.kind is param.POSITIONAL_OR_KEYWORD and param.default is None:
                args[name] = _get_variable(name)

        return args


class Serializable:
    _class_signature = ('__class__', '__module__', 'data')

    def _enclose_json(self, data):
        return {
            '__class__': self.__class__.__qualname__,
            '__module__': self.__module__,
            'data': data
        }

    @staticmethod
    def _bad(arg):
        raise TypeError('Argument "%s" must not be None' % arg)

    def serialize(self, *, cls=Serializer, **kwargs):
        return json.dumps(self, cls=cls, **kwargs)

    def __json__(self):
        raise NotImplementedError

    @classmethod
    def _deserialize(cls, raw_json, **kwargs):
        raise NotImplementedError