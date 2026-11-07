"""Validate settings drafts before acquiring an exclusive running Session."""

from multiagent.models import validate_model


def validate_settings(values):
    if not isinstance(values,dict) or set(values)-{'model','max_subagents','idle_sleep_seconds','execution_mode'}:
        raise ValueError('Settings: model, max_subagents, idle_sleep_seconds, execution_mode')
    if 'model' in values:validate_model(values['model'])
    if 'max_subagents' in values and (type(values['max_subagents']) is not int or not 0<=values['max_subagents']<=99):
        raise ValueError('max_subagents 必须是 0–99 的整数')
    if 'idle_sleep_seconds' in values and (type(values['idle_sleep_seconds']) not in (int,float) or not .1<=values['idle_sleep_seconds']<=3600):
        raise ValueError('idle_sleep_seconds 必须为 0.1–3600 秒')
    if 'execution_mode' in values and values['execution_mode'] not in ('sandbox','host'):
        raise ValueError('execution_mode must be sandbox or host')
    return dict(values)
