"""Window-local navigation and stale-result rejection, without Kodi imports."""
from urllib.parse import parse_qsl, urlsplit


def item_query(url, addon_id='script.ai.movie.scanner'):
    parsed = urlsplit(url)
    if parsed.scheme != 'plugin' or parsed.netloc != addon_id:
        raise ValueError('Foreign scanner command')
    return dict(parse_qsl(parsed.query))


def context_query(command, addon_id='script.ai.movie.scanner'):
    prefix = 'RunScript(%s,?' % addon_id
    if not command.startswith(prefix) or not command.endswith(')'):
        raise ValueError('Foreign scanner context command')
    return dict(parse_qsl(command[len(prefix):-1]))


class Frame:
    def __init__(self, query=None):
        self.query = dict(query or {})
        self.selected = 0
        self.selected_url = ''
        self.rows = None
        self.title = ''
        self.signature = None


class Navigation:
    def __init__(self, query=None):
        self.frames = [Frame(dict(query or {}))]
        self.generation = 0
        self.closed = False

    @property
    def current(self):
        return self.frames[-1]

    def begin(self):
        self.generation += 1
        return self.generation

    def accepts(self, generation):
        return not self.closed and generation == self.generation

    def enter(self, query, selected, selected_url=''):
        self.current.selected = selected
        self.current.selected_url = selected_url
        same_directory = (query.get('action') == 'view_directory' and
                          query.get('directory') == self.current.query.get('directory') and
                          query.get('group', '') == self.current.query.get('group', '') and
                          self.current.query.get('action') == 'view_directory')
        if same_directory:
            self.frames[-1] = Frame(dict(query))  # Pagination is not a new parent.
        else:
            self.frames.append(Frame(dict(query)))
        return self.begin()

    def back(self):
        self.begin()  # Invalidate even a still-running initial load.
        if len(self.frames) > 1:
            self.frames.pop()
            return self.current
        self.closed = True
        return None

    def invalidate_cache(self):
        for frame in self.frames:
            frame.signature = None
