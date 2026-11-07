"""Observable model transport, protocol and retry handling."""

import hashlib
import http.client
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .chat_stream import MAX_RESPONSE_BYTES, StreamError, read_chat_stream
from .errors import AgentError
from .model_config import DEFAULT_MODEL, DEFAULT_URL, profile_for
from .models import model_messages
from .multimodal import MAX_REQUEST_BYTES
from .protocol import validate_message


class ChatCompletions:
    def endpoint(self, base_url):
        return base_url.rstrip("/") + "/chat/completions"

    def headers(self, key):
        return {"Authorization":"Bearer "+key,"Content-Type":"application/json"}

    def body(self, model, messages, tools, tool_choice, reasoning_effort, profile):
        # Preserve the existing wire order/fingerprints when a profile adds no options.
        body={"model":model,"messages":model_messages(messages,model),"tools":tools,
              "tool_choice":deepcopy(tool_choice),"stream":True,"stream_options":{"include_usage":True}}
        if not tools:
            body.pop("tools");body.pop("tool_choice")
        if reasoning_effort is not None:body["reasoning_effort"]=reasoning_effort
        if set(profile.request_options)&{"model","messages","tools","tool_choice"}:
            raise ValueError("Model request options cannot override conversation/tool fields")
        body.update(deepcopy(profile.request_options))
        if not body.get("stream"):body.pop("stream_options",None)
        return body

    def read(self,response,*,started,timeout,observe,redact):
        content_type=getattr(response,"headers",{}).get("content-type","")
        if isinstance(content_type,str) and content_type.lower().startswith("text/event-stream"):
            def progress(fields,first):observe("stream_output" if first else "reading_body",**fields)
            raw=read_chat_stream(response,started=started,timeout=timeout,observe=progress,redact=redact,max_bytes=MAX_RESPONSE_BYTES)
            observe("parsing",body_received_seconds=round(time.monotonic()-started,3))
        else:
            data=response.read(MAX_RESPONSE_BYTES+1)
            observe("parsing",response_bytes=len(data),body_received_seconds=round(time.monotonic()-started,3))
            if len(data)>MAX_RESPONSE_BYTES:raise AgentError("API response exceeds 4 MiB")
            raw=json.loads(data)
        choice=raw["choices"][0]
        reason=choice.get("finish_reason")
        return {"message":choice.get("message"),"usage":raw.get("usage"),"model":raw.get("model"),
                "finish_reason":reason,"error":"API stopped early: "+reason if reason in {"length","content_filter","error"} else None}


PROTOCOLS = {"chat_completions":ChatCompletions()}


def protocol_for(name):
    try:return PROTOCOLS[name]
    except KeyError:raise ValueError("Unsupported model API protocol: "+str(name)) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fingerprint(value):
    return hashlib.sha256(value).hexdigest()


class Client:
    def __init__(self, key, base_url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=120, *, tools=None, reasoning_effort=None, max_retries=0):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise AgentError("Model API base URL must be an HTTPS URL without credentials, query or fragment")
        self.key, self.model, self.timeout = key, model, timeout
        self.base_url = base_url.rstrip("/")
        self.tools = [] if tools is None else tools
        self.reasoning_effort = reasoning_effort
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be an integer from 0 to 10")
        self.max_retries = max_retries
        self.tool_choice = "auto"
        self.opener = urllib.request.build_opener(NoRedirect())
        self.last_usage, self.last_call = {}, {}
        self.observer = None

    @property
    def adapter(self):
        return protocol_for(profile_for(self.model).protocol)

    @property
    def url(self):
        return self.adapter.endpoint(self.base_url)

    def set_observer(self, observer):
        self.observer = observer

    def _redact(self, value):
        return str(value).replace(self.key, "[REDACTED]") if self.key else str(value)

    def configuration(self):
        proxies = urllib.request.getproxies()
        proxy = proxies.get("https", proxies.get("all"))
        bypass = urllib.request.proxy_bypass(urllib.parse.urlsplit(self.url).hostname)
        safe_proxy = None
        if proxy:
            try:
                parsed = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
                safe_proxy = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
            except ValueError:
                safe_proxy = "configured"
        return {"endpoint": self.url, "model": self.model, "socket_timeout_seconds": self.timeout,
                "proxy_mode": "bypassed" if proxy and bypass else "environment" if proxy else "direct",
                "proxy": safe_proxy, "key_fingerprint": fingerprint(self.key.encode())[:12],
                "automatic_retries": self.max_retries, "user_agent": "recoding/0.8.0",
                "user_query_format": "xml", "stream": profile_for(self.model).request_options.get("stream",True),
                "protocol":profile_for(self.model).protocol,
                "reasoning_effort": self.reasoning_effort}

    def prepare_request(self, messages):
        """Build exactly the production request without performing any network IO."""
        body = self.adapter.body(self.model,messages,self.tools,self.tool_choice,self.reasoning_effort,profile_for(self.model))
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise AgentError("API request exceeds 20 MiB; use smaller images or reset the context")
        request = urllib.request.Request(self.url, data=encoded, headers={
            **self.adapter.headers(self.key),
            "User-Agent": "recoding/0.8.0"}, method="POST")
        systems = [message for message in body["messages"] if message.get("role") == "system"]
        history_models = {}
        foreign_messages = foreign_calls = 0
        for message in messages:
            if message.get("role") != "assistant":
                continue
            source = message.get("_model") or "unknown"
            history_models[source] = history_models.get(source, 0) + 1
            if source != self.model:
                foreign_messages += 1
                foreign_calls += len(message.get("tool_calls") or [])
        metadata = {"request_bytes": len(encoded), "request_sha256": fingerprint(encoded),
                    "message_count": len(body["messages"]),
                    "message_roles": [m.get("role") for m in body["messages"]],
                    "system_sha256": fingerprint(json.dumps(systems, ensure_ascii=False).encode()),
                    "tools_sha256": fingerprint(json.dumps(self.tools, ensure_ascii=False).encode()),
                    "tool_names": [t["function"]["name"] for t in self.tools],
                    "history_models": history_models, "foreign_assistant_messages": foreign_messages,
                    "foreign_tool_calls": foreign_calls,
                    "stream": body.get("stream",False), "tool_choice": deepcopy(body.get("tool_choice"))}
        return request, metadata

    def _emit(self, phase, **fields):
        self.last_call.update(fields, phase=phase, elapsed_seconds=round(time.monotonic() - self.started, 3))
        if self.observer:
            self.observer(deepcopy(self.last_call))

    def _headers(self, response, status=None):
        headers = getattr(response, "headers", None)
        def header(name):
            value = headers.get(name) if headers is not None else None
            return self._redact(value[:300]) if isinstance(value, str) else None
        code = status if status is not None else getattr(response, "status", None)
        self._emit("headers_received", http_status=code if type(code) is int else None,
                   request_id=header("x-request-id") or header("request-id"),
                   retry_after=header("retry-after"),
                   headers_seconds=round(time.monotonic() - self.started, 3))

    def complete(self, messages):
        """Retry only uncommitted model responses; tools are outside this adapter."""
        failures = []
        for attempt in range(1, self.max_retries + 2):
            try:
                return self._complete_once(messages, attempt, failures)
            except AgentError:
                if attempt > self.max_retries or not self._retryable():
                    raise
                failures.append({key: self.last_call.get(key) for key in
                    ("attempt", "call_id", "http_status", "error_category", "error", "elapsed_seconds", "request_id")})
                delay = self._retry_delay(attempt)
                try:
                    self._emit("retry_wait", retry_delay_seconds=delay, next_attempt=attempt + 1,
                               failed_attempts=deepcopy(failures))
                    time.sleep(delay)  # SIGINT interrupts backoff as well as IO.
                except KeyboardInterrupt:
                    self._emit("cancelled", interrupted_phase="retry_wait")
                    raise

    def _retryable(self):
        status = self.last_call.get("http_status")
        if self.last_call.get("error_category") in {"http_error", "rate_limit", "upstream_credentials"} and status:
            return status in {408, 425, 429} or 500 <= status <= 599
        return self.last_call.get("error_category") in {
            "timeout", "tls_error", "connection_error", "incomplete_response",
            "empty_response", "upstream_error", "upstream_credentials"}

    def _retry_delay(self, attempt):
        delay = min(2 ** (attempt - 1), 30)
        value = self.last_call.get("retry_after")
        if value:
            try:
                requested = float(value)
            except ValueError:
                try:
                    requested = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    requested = 0
            if 0 <= requested < float("inf"):
                delay = min(max(delay, requested), 30)
        return delay

    def _complete_once(self, messages, attempt, failures):
        self.started = time.monotonic()
        self.last_usage = {}
        self.last_call = {**self.configuration(), "call_id": uuid.uuid4().hex[:12],
                          "phase": "preparing", "response_bytes": 0,
                          "attempt": attempt, "max_attempts": self.max_retries + 1,
                          "retry_count": attempt - 1, "failed_attempts": deepcopy(failures)}
        try:
            request, metadata = self.prepare_request(messages)
            self.last_call.update(metadata)
            self._emit("waiting_headers")
            with self.opener.open(request, timeout=self.timeout) as response:
                self._headers(response)
                self._emit("reading_body")
                data = self.adapter.read(response, started=self.started, timeout=self.timeout,
                                         observe=self._emit, redact=self._redact)
            self.last_call.update(response_model=data.get("model"), finish_reason=data.get("finish_reason"))
            if data.get("error"):
                raise AgentError(data["error"])
            message = data["message"]
            validate_message(message)
            usage = data.get("usage")
            if isinstance(usage, dict):
                self.last_usage = {name: value for name, value in usage.items()
                                   if name in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                   and type(value) is int and value >= 0}
                for container, field in (("prompt_tokens_details", "cached_tokens"),
                                         ("completion_tokens_details", "reasoning_tokens")):
                    details = usage.get(container)
                    value = details.get(field) if isinstance(details, dict) else None
                    if type(value) is int and value >= 0:
                        self.last_call[field] = value
            self._emit("complete", response_model=data.get("model"),
                       finish_reason=data.get("finish_reason"), usage=self.last_usage)
            return message
        except StreamError as error:
            self.last_call.update(error_category=error.category, **error.metadata)
            raise AgentError(self._redact(error)) from None
        except urllib.error.HTTPError as error:
            self._headers(error, error.code)
            self._emit("reading_error")
            try:
                raw = error.read(2000)
                detail = self._redact(raw.decode("utf-8", errors="replace"))
                self.last_call["response_bytes"] = len(raw)
            except (http.client.HTTPException, urllib.error.URLError, OSError):
                detail = "error response body was interrupted"
            finally:
                error.close()
            category = "authentication" if error.code in {401, 403} else "rate_limit" if error.code == 429 else "http_error"
            self.last_call.update(error_body=detail, error_category=category)
            try:
                value = json.loads(detail).get("error")
            except (ValueError, AttributeError):
                value = None
            if isinstance(value, dict):
                for source, target in (("type", "upstream_type"), ("code", "upstream_code"), ("message", "upstream_message")):
                    if isinstance(value.get(source), str):
                        self.last_call[target] = value[source][:1500]
                match = re.search(r"\brequest\s+(dfr_[A-Za-z0-9]+)", value.get("message", "")) if isinstance(value.get("message"), str) else None
                if not self.last_call.get("request_id") and match:
                    self.last_call["request_id"] = match[1]
            if self.last_call.get("upstream_code") == "credential_unavailable":
                self.last_call["error_category"] = "upstream_credentials"
            message = self.last_call.get("upstream_message", detail)
            code = self.last_call.get("upstream_code")
            tag = f" [{code}]" if code else ""
            request_id = self.last_call.get("request_id")
            suffix = f" (request_id={request_id})" if request_id and request_id not in message else ""
            raise AgentError(f"API HTTP {error.code}{tag}: {message}{suffix}") from None
        except http.client.IncompleteRead as error:
            self.last_call.update(error_category="incomplete_response",
                                  response_bytes=max(self.last_call.get("response_bytes", 0), len(error.partial)))
            raise AgentError("API response interrupted (IncompleteRead): the gateway closed the HTTP body "
                             "before it was complete; no partial tool call was committed.") from None
        except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            category = ("tls_certificate" if isinstance(reason, ssl.SSLCertVerificationError) else
                        "timeout" if isinstance(reason, TimeoutError) else "tls_error" if isinstance(reason, ssl.SSLError) else "connection_error")
            self.last_call.update(error_category=category, exception_type=type(reason).__name__)
            raise AgentError(f"API connection failed: {self._redact(error)}") from None
        except (KeyError, IndexError, TypeError, ValueError) as error:
            self.last_call["error_category"] = "invalid_response"
            raise AgentError(f"invalid API response: {self._redact(error)}") from None
        finally:
            if self.last_call.get("phase") != "complete":
                error = sys.exc_info()[1]
                previous = self.last_call.get("phase")
                if not self.last_call.get("error_category") and not isinstance(error, KeyboardInterrupt):
                    self.last_call["error_category"] = "request_error" if previous == "preparing" else "response_error"
                self._emit("cancelled" if isinstance(error, KeyboardInterrupt) else "error",
                           interrupted_phase=previous, error=self._redact(error) if error else None)
