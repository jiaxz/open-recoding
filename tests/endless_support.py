"""Finite observation probes for endless Agents; no production Turn API."""

from multiagent.multimodal import make_user_message


def drive(agent, prompt=None, *, attachments=()):
    """Queue one test input, observe a plain response, then explicitly Stop.

    A genuine earlier model/tool interruption still propagates. Protocol tests
    can inspect a bounded request sequence without adding a finite core path.
    """
    incoming = [make_user_message(agent.messages, prompt, attachments)] if prompt is not None else []
    provider, observer = agent.input_provider, agent._on_event
    answer = []
    def inputs(messages):
        result = list(incoming)
        incoming.clear()
        return result + (provider(messages) if provider else [])
    def observe(event):
        observer(event)
        if event['type'] == 'model_end' and not event['tool_calls']:
            answer.append(event.get('content'))
            raise KeyboardInterrupt()
    agent.input_provider, agent._on_event = inputs, observe
    try:
        agent.run()
    except KeyboardInterrupt:
        if not answer:
            raise
    finally:
        agent.input_provider, agent._on_event = provider, observer
    return answer[0]
