"""Scrollable message projection; no terminal IO, inbox consumption or core state."""

from bisect import bisect_right

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseEventType


class Conversation(UIControl):
    def __init__(self, renderer, invalidate):
        self.renderer, self.invalidate = renderer, invalidate
        self.records, self.cache = {}, {}
        self.scope, self.agent = 'human', None
        self.offset, self.follow, self.unread = 0, True, 0
        self.rows, self.spans = [], []
        self.width, self.height, self.revision = 0, 0, None
        self.dirty = True
        self.restore_anchor = None

    def accepts(self, item):
        return (self.scope == 'all' or item['kind'] != 'peer') and (
            not self.agent or self.agent in {item['source'], item['target']})

    def add(self, item, *, new=True):
        if item['id'] in self.records: return
        self.records[item['id']] = item
        if new and not self.follow and self.accepts(item): self.unread += 1
        self.dirty = True
        self.invalidate()

    def anchor(self):
        if not self.spans: return None
        starts = [span[1] for span in self.spans]
        index = max(0, bisect_right(starts, self.offset)-1)
        identifier, start, end = self.spans[index]
        return identifier, (self.offset-start)/max(1, end-start)

    def rebuild(self, width):
        revision = tuple(sorted((key, value.get('name'), value.get('icon')) for key, value in self.renderer.agents.items()))
        if not self.dirty and self.width == width and self.revision == revision: return
        anchor = (self.restore_anchor or self.anchor()) if not self.follow else None
        self.restore_anchor = None
        self.width, self.revision = width, revision
        rows, spans = [], []
        for item in sorted(self.records.values(), key=lambda row: (row['timestamp'], row['id'])):
            if not self.accepts(item): continue
            cached = self.cache.get(item['id'])
            if not cached or cached[:2] != (width, revision):
                fragments = to_formatted_text(ANSI(self.renderer.card_text(item, width)))
                lines = list(split_lines(fragments))
                cached = self.cache[item['id']] = (width, revision, lines)
            start = len(rows)
            rows.extend(cached[2])
            spans.append((item['id'], start, len(rows)))
        self.rows, self.spans, self.dirty = rows, spans, False
        if anchor:
            span = next((span for span in spans if span[0] == anchor[0]), None)
            if span: self.offset = span[1]+int(anchor[1]*(span[2]-span[1]))

    def create_content(self, width, height):
        self.rebuild(width)
        self.height = height
        end = max(0, len(self.rows)-height)
        self.offset = end if self.follow else min(max(0, self.offset), end)
        return UIContent(get_line=lambda i: self.rows[i] if self.rows else [('class:muted', '  发送一条消息开始对话；@ 选择 Agent，/ 打开命令。')],
            line_count=max(1,len(self.rows)), cursor_position=Point(0,self.offset), show_cursor=False)

    def scroll(self, delta):
        end = max(0,len(self.rows)-self.height)
        self.offset = max(0,min(end,self.offset+delta))
        self.follow = self.offset == end
        if self.follow: self.unread = 0
        self.invalidate()

    def latest(self):
        self.follow, self.unread = True, 0
        self.invalidate()

    def mouse_handler(self, event):
        if event.event_type in {MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN}:
            self.scroll(-3 if event.event_type == MouseEventType.SCROLL_UP else 3)
            return None
        return NotImplemented
