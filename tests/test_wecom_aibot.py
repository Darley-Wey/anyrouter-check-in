import io
import json
from collections import deque
from unittest.mock import MagicMock

import pytest

from utils import wecom_aibot
from utils.wecom_aibot import WecomAiBotClient, WecomAiBotConfig, _split_text


class FakeSocket:
	def __init__(self):
		self.sent = []
		self.frames: deque[str] = deque()
		self.errors = {}
		self.closed = False

	def send(self, raw):
		frame = json.loads(raw)
		self.sent.append(frame)
		if frame['cmd'] != 'aibot_ping':
			self.frames.append(
				json.dumps(
					{
						'headers': frame['headers'],
						'errcode': self.errors.get(frame['cmd'], 0),
						'errmsg': 'test-secret invalid' if frame['cmd'] in self.errors else 'ok',
					}
				)
			)

	def recv(self, timeout):
		assert timeout > 0
		if not self.frames:
			raise TimeoutError('no frame')
		return self.frames.popleft()

	def close(self):
		self.closed = True


@pytest.fixture
def socket(monkeypatch):
	websocket = FakeSocket()
	monkeypatch.setattr(wecom_aibot, 'connect', lambda *args, **kwargs: websocket)
	return websocket


@pytest.fixture
def config():
	return WecomAiBotConfig(bot_id='test-bot', secret='test-secret', to_user_id='test-receiver')


def message(user, content, chat_type='single'):
	return json.dumps(
		{
			'cmd': 'aibot_msg_callback',
			'body': {
				'chattype': chat_type,
				'msgtype': 'text',
				'from': {'userid': user},
				'text': {'content': content},
			},
		}
	)


def test_config_loads_and_does_not_expose_credentials(monkeypatch, config):
	monkeypatch.setenv('WECOM_AIBOT_CONFIG', config.secret_json())
	loaded = WecomAiBotConfig.load()
	assert loaded == config
	assert 'test-secret' not in repr(loaded)
	assert 'test-receiver' not in repr(loaded)


@pytest.mark.parametrize('raw', ['{', '[]', '{"bot_id":123}'])
def test_invalid_config(monkeypatch, raw):
	monkeypatch.setenv('WECOM_AIBOT_CONFIG', raw)
	with pytest.raises(ValueError):
		WecomAiBotConfig.load()


def test_unbound_recipient_is_rejected():
	with pytest.raises(ValueError, match='接收人尚未绑定'):
		WecomAiBotConfig(bot_id='test-bot', secret='test-secret').validate_send()


def test_connection_uses_protocol_heartbeats_only(socket, config, monkeypatch):
	connector = MagicMock(return_value=socket)
	monkeypatch.setattr(wecom_aibot, 'connect', connector)
	with WecomAiBotClient(config):
		pass
	assert connector.call_args.kwargs['ping_interval'] is None
	assert connector.call_args.kwargs['proxy'] is None
	assert connector.call_args.args == ('wss://openws.work.weixin.qq.com',)


def test_authentication_and_active_send_wait_for_ack(socket, config):
	with WecomAiBotClient(config) as client:
		client.send_text('测试通知')
	assert socket.sent[0]['cmd'] == 'aibot_subscribe'
	assert socket.sent[0]['body'] == {'bot_id': 'test-bot', 'secret': 'test-secret'}
	assert socket.sent[1]['cmd'] == 'aibot_send_msg'
	assert socket.sent[1]['body'] == {
		'chatid': 'test-receiver',
		'msgtype': 'markdown',
		'markdown': {'content': '测试通知'},
	}
	assert socket.closed


def test_wrong_ack_and_unrelated_callback_are_ignored(socket, config):
	socket.frames.extend([message('someone', 'hello'), json.dumps({'headers': {'req_id': 'wrong'}, 'errcode': 0})])
	with WecomAiBotClient(config) as client:
		client.send_text('hello')
	assert len(socket.sent) == 2


def test_auth_failure_closes_and_redacts(socket, config):
	socket.errors['aibot_subscribe'] = 40001
	with pytest.raises(RuntimeError) as exc:
		with WecomAiBotClient(config):
			pytest.fail('must not authenticate')
	assert 'test-secret' not in str(exc.value)
	assert '[redacted]' in str(exc.value)
	assert socket.closed


def test_send_failure_is_not_reported_as_success(socket, config):
	with WecomAiBotClient(config) as client:
		socket.errors['aibot_send_msg'] = 40001
		with pytest.raises(RuntimeError, match='请求失败'):
			client.send_text('hello')
	assert socket.closed


def test_malformed_response_is_rejected(socket, config):
	with WecomAiBotClient(config) as client:
		socket.frames.append('not json')
		with pytest.raises(RuntimeError, match='无效 JSON'):
			client.send_text('hello')


def test_bind_ignores_groups_wrong_phrase_and_invalid_content(socket, config):
	with WecomAiBotClient(config) as client:
		socket.frames.extend(
			[
				message('group-user', 'bind 123', 'group'),
				message('wrong-user', 'something else'),
				message('malformed-user', 123),
				message('correct-user', 'bind 123'),
			]
		)
		client.bind_single_chat('bind 123')
	assert config.to_user_id == 'correct-user'


def test_binding_sends_protocol_heartbeat(socket, config, monkeypatch):
	with WecomAiBotClient(config) as client:
		socket.frames.append(message('correct-user', 'bind 123'))
		times = iter([0, 0, 25])
		monkeypatch.setattr(wecom_aibot.time, 'monotonic', lambda: next(times))
		client.bind_single_chat('bind 123', timeout_seconds=100)
	assert socket.sent[-1]['cmd'] == 'aibot_ping'


def test_binding_timeout_does_not_change_recipient(socket, config, monkeypatch):
	with WecomAiBotClient(config) as client:
		times = iter([0, 0, 10])
		monkeypatch.setattr(wecom_aibot.time, 'monotonic', lambda: next(times))
		with pytest.raises(TimeoutError, match='绑定超时'):
			client.bind_single_chat('bind 123', timeout_seconds=10)
	assert config.to_user_id == 'test-receiver'


def test_split_preserves_unicode_within_byte_limit():
	text = '中文😀' * 2000
	parts = _split_text(text)
	assert ''.join(parts) == text
	assert all(len(part.encode('utf-8')) <= 4000 for part in parts)
	assert _split_text('') == []


def test_long_notification_sends_all_chunks(socket, config):
	text = '中文😀' * 2000
	with WecomAiBotClient(config) as client:
		client.send_text(text)
	parts = [frame['body']['markdown']['content'] for frame in socket.sent[1:]]
	assert ''.join(parts) == text
	assert all(len(part.encode('utf-8')) <= 4000 for part in parts)


def test_bind_does_not_store_secret_when_test_send_fails(monkeypatch, socket):
	monkeypatch.setattr(wecom_aibot.shutil, 'which', lambda name: '/usr/bin/gh')
	monkeypatch.setattr('sys.argv', ['wecom_aibot', 'bind', '--repo', 'owner/repo', '--phrase', 'bind 123'])
	monkeypatch.setattr('sys.stdin', io.StringIO('{"bot_id":"test-bot","secret":"test-secret"}'))
	monkeypatch.setattr(WecomAiBotClient, 'bind_single_chat', lambda self, *args, **kwargs: None)
	save = MagicMock()
	monkeypatch.setattr(wecom_aibot.subprocess, 'run', save)
	assert wecom_aibot.main() == 1
	save.assert_not_called()
