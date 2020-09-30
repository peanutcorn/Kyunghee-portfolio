import shutil
import textwrap

class MusicbotException(Exception):
    def __init__(self, message, *, expire_in=0):
        super().__init__(message)
        self._message = message
        self.expire_in = expire_in

    @property
    def message(self):
        return self._message

    @property
    def message_no_format(self):
        return self._message

class CommandError(MusicbotException):
    pass

class ExtractionError(MusicbotException):
    pass

# TODO: Add typing options instead of is_playlist
class WrongEntryTypeError(ExtractionError):
    def __init__(self, message, is_playlist, use_url):
        super().__init__(message)
        self.is_playlist = is_playlist
        self.use_url = use_url

class FFmpegError(MusicbotException):
    pass

class FFmpegWarning(MusicbotException):
    pass

class SpotifyError(MusicbotException):
    pass

class PermissionsError(CommandError):
    @property
    def message(self):
        return "You don't have permission to use that command.\nReason: " + self._message

class HelpfulError(MusicbotException):
    def __init__(self, issue, solution, *, preface="An error has occured:", footnote='', expire_in=0):
        self.issue = issue
        self.solution = solution
        self.preface = preface
        self.footnote = footnote
        self.expire_in = expire_in
        self._message_fmt = "\n{preface}\n{problem}\n\n{solution}\n\n{footnote}"

    @property
    def message(self):
        return self._message_fmt.format(
            preface  = self.preface,
            problem  = self._pretty_wrap(self.issue,    "  Problem:"),
            solution = self._pretty_wrap(self.solution, "  Solution:"),
            footnote = self.footnote
        )

    @property
    def message_no_format(self):
        return self._message_fmt.format(
            preface  = self.preface,
            problem  = self._pretty_wrap(self.issue,    "  Problem:", width=None),
            solution = self._pretty_wrap(self.solution, "  Solution:", width=None),
            footnote = self.footnote
        )

    @staticmethod
    def _pretty_wrap(text, pretext, *, width=-1):
        if width is None:
            return '\n'.join((pretext.strip(), text))
        elif width == -1:
            pretext = pretext.rstrip() + '\n'
            width = shutil.get_terminal_size().columns

        lines = textwrap.wrap(text, width=width - 5)
        lines = (('    ' + line).rstrip().ljust(width-1).rstrip() + '\n' for line in lines)

        return pretext + ''.join(lines).rstrip()

class HelpfulWarning(HelpfulError):
    pass

class Signal(Exception):
    pass

class RestartSignal(Signal):
    pass

class TerminateSignal(Signal):
    pass