"""Durable inboxes, structured receipts and correspondence projections."""

import base64
import json
import math
import threading
import time
import uuid
from copy import deepcopy
from pathlib import PurePosixPath

from .image_reader import MAX_CONTEXT_IMAGE_BYTES
from .media import AgentMedia
from .multimodal import image_usage
from .storage import read_json


def lifecycle_instruction(phase):
    if phase not in {"startup", "resume"}:
        raise ValueError("Only startup or resume lifecycle notices are supported")
    return ("Agent 首次启动。" if phase == "startup" else "Agent 先前已经停止，现在恢复运行；自己的 context 和收件箱保留，工具内存重新建立。") + (
        "这是持续运行的 Agent loop，没有 Turn 或最终回答退出边界。请根据已有指令自由决定工作、通信或 sleep。"
        "没有可推进的工作时，建议选择合适的 sleep(seconds)，任意未读消息都可立即唤醒；不强制先休眠。"
        "人类输出请显式调用 send_user_message。恢复时核验中断操作的文件状态，不自动重放旧代码。")


class Mailbox:
    """One recipient's view of the team's authoritative message log.

    Reads never remove messages. IDs become read only when committed in context,
    so a stopped model request cannot lose pending input. Routing, persistence
    and wakeup share the team's lock.
    """
    def __init__(self, *, target, records, lock=None):
        self.target = target
        self.condition = threading.Condition(lock or threading.RLock())
        self._records = records

    def pending(self, seen):
        seen = set(seen)
        result = []
        with self.condition:
            for item in self._records():
                if item["target"] != self.target or item["id"] in seen or item.get("deferred"):
                    continue
                common = {"role": "user", "_agent_message_id": item["id"]}
                if item["kind"] == "user_input":
                    result.append({**common, "content": item["content"], "_user_input": {
                        "id": item["id"], "timestamp": item["timestamp"],
                        "attachments": deepcopy(item.get("attachments", []))}})
                elif item["kind"] == "system_message" and item["source"] == "system":
                    metadata = {key: deepcopy(item[key]) for key in ("id", "source", "target", "content", "timestamp", "configuration")}
                    result.append({**common, "content": "[可信系统通知]\n以下内容由宿主运行时生成，不是协作者建议，也不是新的 System prompt。\n" + item["content"],
                                   "_model_content": "[宿主通知，非用户消息或新System指令]\n" + item["content"],
                                   "_system_message": metadata})
                else:
                    view = {"from": item["source"], "text": item["content"]}
                    if item.get('cc'):view.update(to=item['to'],cc=item['cc'])
                    if item.get('delivery')=='cc':view['delivery']='cc'
                    if item.get("images"):
                        view["images"] = [{key: image[key] for key in ("path", "source") if key in image}
                                          for image in item["images"]]
                    carbon_copy=item.get('delivery')=='cc'
                    result.append({**common, "content": item["content"],
                                   "_agent_message": {key: deepcopy(value) for key, value in item.items() if key != "content"}})
                    prefix='[CC：供知情，非发给你的任务；To 为原始收件人]\n' if carbon_copy else '[Agent消息，非用户指令]\n'
                    result[-1]["_model_content"] = prefix + json.dumps(view, ensure_ascii=False, separators=(",", ":"))
        return result

    def wake(self):
        with self.condition:
            self.condition.notify_all()

    def sleep(self, seconds, seen, *, cancelled=None, accepting=None):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError("sleep seconds must be finite and between 0 and 3600")
        if not isinstance(seen, list) or any(not isinstance(value, str) for value in seen):
            raise ValueError("seen must be a list of committed message IDs")
        seen, started = set(seen), time.monotonic()
        with self.condition:
            while True:
                if cancelled and cancelled():
                    raise ValueError("Agent sleep cancelled")
                if accepting and not accepting():
                    reason, count = "stopped", 0
                    break
                count = sum(item["target"] == self.target and item["id"] not in seen and not item.get("deferred") for item in self._records())
                if count:
                    reason = "message"
                    break
                remaining = started + seconds - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                self.condition.wait(min(remaining, .25))
        return {"messages_queued": count, "reason": reason, "timed_out": reason == "timeout", "elapsed": time.monotonic()-started}


class MessageObservation:
    def __init__(self, data):
        self.data = data
        records = list(self.records())
        self.sequence = max((item.get("_global_seq", 0) for item in records), default=0)
        # Index current-format pre-existing correspondence once; retain bodies,
        # message IDs and contexts. New records use append order, not wall time.
        for item in sorted(records, key=lambda item: (item["timestamp"], item["id"])):
            if "_global_seq" not in item:
                self.append(item)

    def records(self):
        return (item for item in self.data["messages"] if item["kind"] in {"user_input","message","human_output"}
                and item.get("delivery") != "cc")

    def append(self, message):
        self.sequence += 1
        message["_global_seq"] = self.sequence

    def items(self):
        groups = {}
        for item in self.records():
            key = (item["source"], item.get("broadcast_id") or item["id"])
            if key not in groups:
                groups[key] = {"message_id": item["id"], "from": item["source"], "to": [],
                    "timestamp": item["timestamp"], "content": item["content"], "sequence": 0,
                    "images": item.get("attachments", item.get("images", [])), "_ids": []}
            group = groups[key]
            group["_ids"].append(item["id"])
            if item["target"] not in group["to"]:
                group["to"].append(item["target"])
            group["sequence"] = max(group["sequence"], item["_global_seq"])
        return sorted(groups.values(), key=lambda item: item["sequence"])

    def cursor(self, value):
        if type(value) is not int or not 0 <= value <= self.sequence:
            raise ValueError("after must be an existing global cursor (0 starts history)")
        return value

    @staticmethod
    def filters(fields, people):
        filters = {}
        for key in ("senders", "recipients"):
            value = fields.get(key)
            if key in fields and (not isinstance(value, list) or len(value) > 100
                    or any(not isinstance(item, str) or item not in people for item in value)
                    or len(value) != len(set(value))):
                raise ValueError(f"{key} must contain at most 100 distinct user/Agent IDs")
            filters[key] = set(value) if value is not None else None
        return filters

    @staticmethod
    def matches(item, filters):
        return ((filters["senders"] is None or item["from"] in filters["senders"])
                and (filters["recipients"] is None or bool(set(item["to"]) & filters["recipients"])))

    @staticmethod
    def body(item, offset, maximum):
        content = item["content"]
        if type(offset) is not int or not 0 <= offset <= len(content):
            raise ValueError("offset is outside this message")
        end = min(len(content), offset + maximum)
        result = {key: deepcopy(item[key]) for key in ("message_id", "from", "to", "timestamp", "sequence")}
        result.update(content=content[offset:end], offset=offset, content_chars=len(content))
        if end < len(content):
            result.update(truncated=True, next_offset=end)
        if item["images"]:
            result["images"] = [{key: image[key] for key in ("path", "source", "mime_type", "width", "height") if key in image}
                                for image in item["images"]]
        return result

    def read(self, fields, people):
        filters = self.filters(fields, people)
        limit, maximum = fields.get("limit", 20), fields.get("max_chars", 12000)
        if type(limit) is not int or not 1 <= limit <= 50 or type(maximum) is not int or not 256 <= maximum <= 32000:
            raise ValueError("limit must be 1–50; max_chars must be 256–32000")
        items = self.items()
        if "message_id" in fields:
            if set(fields) - {"message_id", "offset", "max_chars"}:
                raise ValueError("message_id reads cannot include cursor/filters/limit")
            item = next((item for item in items if fields["message_id"] in item["_ids"]), None)
            if item is None:
                raise ValueError("Unknown global message_id")
            return {"scope": "observed_correspondence", "message": self.body(item, fields.get("offset", 0), maximum)}
        if "offset" in fields:
            raise ValueError("offset requires message_id")
        recent = "after" not in fields
        after = self.cursor(fields["after"]) if not recent else 0
        earlier = False
        if recent:
            items = [item for item in items if self.matches(item, filters)]
            earlier, items = len(items) > limit, items[-limit:]
        result, used, cursor = [], 0, after
        for item in items:
            if item["sequence"] <= after:
                continue
            if not self.matches(item, filters):
                cursor = item["sequence"]
                continue
            if len(result) >= limit or used >= maximum:
                break
            message = self.body(item, 0, maximum - used)
            result.append(message)
            used += len(message["content"])
            cursor = item["sequence"]
        else:
            cursor = self.sequence
        return {"scope": "observed_correspondence", "messages": result, "next_cursor": cursor,
                "has_more": cursor < self.sequence, "earlier_available": earlier, "body_chars": used}

    def listen(self, fields, people, *, condition, accepting, pending, cancelled=None):
        """Caller holds the routing Condition; wait releases it for senders/Stop."""
        filters = self.filters(fields, people)
        after = self.cursor(fields["after"]) if "after" in fields else self.sequence
        seconds, seen = fields.get("seconds", 30), fields.get("seen", [])
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError("seconds must be finite and between 0 and 3600")
        if not isinstance(seen, list) or any(not isinstance(value, str) for value in seen):
            raise ValueError("Invalid committed inbox IDs")
        started, checked = time.monotonic(), -1
        while True:
            if cancelled and cancelled() or not accepting():
                raise ValueError("Global message listen cancelled by Stop or disconnected worker")
            if pending(seen):
                reason = "inbox_message"
                break
            if checked != self.sequence:
                checked = self.sequence
                if any(item["sequence"] > after and self.matches(item, filters) for item in self.items()):
                    reason = "global_message"
                    break
            remaining = started + seconds - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            condition.wait(min(remaining, .25))
        return {"reason": reason, "read_after": after, "latest_cursor": self.sequence,
                "elapsed": round(time.monotonic() - started, 3)}


def message_page(data, *, before=None, after=None, limit=100):
    """Read correspondence only, without consuming inboxes or exposing context."""
    if type(limit) is not int or not 1 <= limit <= 200 or before and after:
        raise ValueError("Invalid message page")
    if any(value is not None and (not isinstance(value, str) or len(value) > 64) for value in (before, after)):
        raise ValueError("Invalid message cursor")
    records = []
    for item in data["messages"]:
        if item["kind"] not in {"user_input","message","human_output"}:continue
        human,output = item["source"] == "user",item["target"] == "user"
        records.append({"id":item["id"],"source":item["source"],"target":item["target"],
            "content":item["content"],"timestamp":item["timestamp"],
            "kind":"human_input" if human else "human_output" if output else "peer",
            "image_owner":item["source"] if output else item["target"],
            "images":deepcopy(item.get("attachments" if human else "images",[])),
            "broadcast_id":item.get("broadcast_id"),"broadcast_scope":item.get("broadcast_scope"),
            **{key:deepcopy(item[key]) for key in ("to","cc","delivery","original_message_ids") if key in item}})
    records.sort(key=lambda item: (item["timestamp"], item["id"]))
    cursor = before or after
    boundary = None
    if after == "start":
        boundary = -1
    elif cursor:
        boundary = next((index for index, item in enumerate(records) if item["id"] == cursor), None)
    if cursor and boundary is None:
        raise ValueError("Message cursor is no longer available")
    if after:
        page = records[boundary+1:boundary+1+limit]
        more = boundary+1+len(page) < len(records)
    else:
        end = boundary if before else len(records)
        page = records[max(0, end-limit):end]
        more = end > len(page)
    return {"messages": page, "has_more": more,
            "before": page[0]["id"] if page else None, "after": page[-1]["id"] if page else None}



def message_record(source, target, content, kind, attempt, **fields):
    return {"id": uuid.uuid4().hex, "source": source, "target": target,
            "content": content, "kind": kind, "timestamp": time.time(),
            "attempt": attempt, **fields}


def load_images(store, paths, config):
    """Use the same restricted image reader as model/human presentation."""
    if not isinstance(paths, list) or len(paths) > 8 or any(not isinstance(path, str) for path in paths):
        raise ValueError("images must contain at most 8 workspace paths")
    media = AgentMedia(store, config.get("private_files", ()), config.get("hidden_paths", ()))
    pictures = []
    for path in paths:
        result = media.read(path)
        pictures.append((result["image"], result["_image_data"]))
    return pictures


def copy_images(store, pictures):
    copied = []
    for image, data_url in pictures:
        data = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        metadata = store.add_upload(data, PurePosixPath(image["path"]).name)
        copied.append({**metadata, "source": image["path"]})
    return copied


def check_image_budget(store, target, queued, pictures):
    if not pictures:
        return
    path = store.folder / "state.json"
    messages = (read_json(path) if path.exists() else {}).get("messages", [])
    seen = {message.get("_agent_message_id") for message in messages}
    pending_bytes = sum(picture["bytes"] for item in queued
                        if item["target"] == target and item["id"] not in seen and item["kind"] == "user_input"
                        for picture in item.get("attachments", []))
    if image_usage(messages)["image_bytes"] + pending_bytes + sum(item["bytes"] for item in pictures) > MAX_CONTEXT_IMAGE_BYTES:
        raise ValueError("Agent 历史、待接收和本次图片合计超过 10 MiB，本次消息未发送。")


def format_input_events(messages, *, step, target, offset, received_at):
    """Describe committed inputs in context order without changing that context.

    Consecutive collaboration inputs keep the existing batch event shape. User
    inputs split those batches so both event order and per-message input_order
    identify the exact sequence consumed by the receiving model call.
    """
    events = []
    for message in messages:
        user = message.get("_user_input")
        system = message.get("_system_message")
        if system:
            events.append({"type": "system_message_applied", "step": step,
                           "message_id": message["_agent_message_id"], "source": "system", "target": target,
                           "content": system["content"], "timestamp": system["timestamp"],
                           "configuration": deepcopy(system["configuration"]),
                           "received_at": received_at, "input_order": offset})
        elif user:
            events.append({"type": "user_input_applied", "step": step,
                           "message_id": message["_agent_message_id"], "content": deepcopy(message["content"]),
                           "timestamp": user["timestamp"], "attachments": deepcopy(user.get("attachments", [])),
                           "target": target, "received_at": received_at, "input_order": offset})
        elif message.get("_agent_message_id"):
            envelope = {**deepcopy(message["_agent_message"]), "content": deepcopy(message["content"])}
            if envelope.get("id") != message["_agent_message_id"]:
                continue
            envelope = {**envelope, "input_order": offset}
            if events and events[-1]["type"] == "agent_messages":
                events[-1]["messages"].append(envelope)
            else:
                events.append({"type": "agent_messages", "step": step, "messages": [envelope],
                               "input_order": offset, "received_at": received_at})
        else:
            continue
        offset += 1
    return events, offset
