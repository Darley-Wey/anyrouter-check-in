import json
from unittest.mock import MagicMock

import httpx
import pytest

from utils.weixin_push import WeixinPushClient, WeixinPushConfig, _client_version, _split_text


def test_client_version():
	assert _client_version('2.4.6') == 132102


def test_split_text():
	assert _split_text('abc', 2) == ['ab', 'c']


def test_config_loads_from_secret(monkeypatch):
	monkeypatch.setenv(
		'WEIXIN_PUSH_CONFIG',
		json.dumps({'token': 'token', 'to_user_id': 'user@im.wechat', 'context_token': 'context'}),
	)

	config = WeixinPushConfig.load()

	assert config.bot_token == 'token'
	assert config.to_user_id == 'user@im.wechat'


def test_send_text_builds_ilink_payload():
	config = WeixinPushConfig(
		bot_token='token',
		to_user_id='user@im.wechat',
		context_token='context-token',
	)
	mock_client = MagicMock()
	mock_client.post.return_value = httpx.Response(200, json={'ret': 0})
	client = WeixinPushClient(config, client=mock_client)

	client.send_text('hello')

	assert mock_client.post.call_count == 3
	send_call = mock_client.post.call_args_list[1]
	assert send_call.args[0].endswith('/ilink/bot/sendmessage')
	assert send_call.kwargs['headers']['Authorization'] == 'Bearer token'
	assert send_call.kwargs['headers']['AuthorizationType'] == 'ilink_bot_token'
	message = send_call.kwargs['json']['msg']
	assert message['to_user_id'] == 'user@im.wechat'
	assert message['context_token'] == 'context-token'
	assert message['item_list'][0]['text_item']['text'] == 'hello'


def test_send_text_requires_bound_recipient():
	client = WeixinPushClient(WeixinPushConfig(bot_token='token'), client=MagicMock())

	with pytest.raises(ValueError, match='尚未绑定接收人'):
		client.send_text('hello')


def test_api_error_is_reported():
	response = httpx.Response(200, json={'ret': -14, 'errmsg': 'session timeout'})

	with pytest.raises(RuntimeError, match='session timeout'):
		WeixinPushClient._payload(response, '发送微信消息')
