"""Connection templates; saved resources retain their own endpoint and request policy."""
from __future__ import annotations

import re
from typing import Any

PROVIDERS = {
    'volcengine': {'name': '火山方舟', 'endpoint': 'https://ark.cn-beijing.volces.com/api/v3', 'help': '填写火山方舟控制台中的模型 ID 或推理接入点 ID。'},
    'qwen': {'name': '通义千问（阿里云百炼）', 'help': '选择 API Key 所属地域；需要业务空间时，填写对应的 Workspace ID。'},
    'glm': {'name': '智谱 GLM', 'endpoint': 'https://open.bigmodel.cn/api/paas/v4', 'help': '填写模型 API 的模型 ID 和 API Key。'},
    'kimi': {'name': 'Kimi', 'endpoint': 'https://api.moonshot.cn/v1', 'help': '填写模型 API 的模型 ID 和 API Key。'},
    'custom': {'name': '自定义 OpenAI 兼容服务', 'help': '服务需支持 Chat Completions 接口。填写后请检查所选用途是否可用。'},
}
REGIONS = {'cn-beijing': '北京', 'ap-southeast-1': '新加坡', 'ap-northeast-1': '东京', 'eu-central-1': '法兰克福', 'us-east-1': '美国弗吉尼亚'}


def preset_endpoint(provider: str, *, region: str = '', workspace: str = '') -> str:
    if provider not in PROVIDERS or provider == 'custom':
        raise ValueError('provider_requires_custom_endpoint')
    if provider != 'qwen':
        return str(PROVIDERS[provider]['endpoint'])
    if region not in REGIONS:
        raise ValueError('region_required')
    if region == 'us-east-1' and not workspace:
        return 'https://dashscope-us.aliyuncs.com/compatible-mode/v1'
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', workspace):
        raise ValueError('workspace_required')
    return f'https://{workspace}.{region}.maas.aliyuncs.com/compatible-mode/v1'


def request_parameters(profile: str, *, temperature: float, max_tokens: int) -> dict[str, Any]:
    if profile == 'kimi-k2.6-default':
        # Official K2.6 contract fixes sampling by thinking mode; use that service default.
        return {'max_tokens': max_tokens}
    if profile != 'chat-completions-v1':
        raise ValueError('unsupported_request_profile')
    return {'temperature': temperature, 'max_tokens': max_tokens}
