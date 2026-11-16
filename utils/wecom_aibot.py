"""企业微信智能机器人长连接通知与单聊接收人绑定。

协议参考：https://github.com/wecomteam/wecom-aibot-python-sdk
仅连接企业微信官方 WebSocket 地址，凭据不写入日志或本地文件。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil

# 仅调用固定的 gh CLI；凭据经标准输入传递，不使用 shell。
import subprocess  # nosec B404
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from websockets.sync.client import ClientConnection, connect

WS_URL = 'wss://openws.work.weixin.qq.com'
REQUEST_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 25.0
MESSAGE_BYTE_LIMIT = 4000


@dataclass
class WecomAiBotConfig:
	bot_id: str = field(default='', repr=False)
	secret: str = field(default='', repr=False)
	to_user_id: str = field(default='', repr=False)

	@classmethod
	def from_dict(cls, data: Any) -> 'WecomAiBotConfig':
		if not isinstance(data, dict):
			raise ValueError('WECOM_AIBOT_CONFIG 必须是 JSON 对象')
		values = {}
		for key in ('bot_id', 'secret', 'to_user_id'):
			value = data.get(key, '')
			if not isinstance(value, str):
				raise ValueError(f'WECOM_AIBOT_CONFIG 的 {key} 必须是字符串')
			values[key] = value.strip()
		return cls(**values)

	@classmethod
	def load(cls) -> 'WecomAiBotConfig':
		raw = os.getenv('WECOM_AIBOT_CONFIG', '').strip()
		if not raw:
			raise ValueError('WeCom AI Bot configuration not set')
		try:
			data = json.loads(raw)
		except json.JSONDecodeError:
			raise ValueError('WECOM_AIBOT_CONFIG 不是有效 JSON') from None
		return cls.from_dict(data)

	def validate_credentials(self) -> None:
		if not self.bot_id or not self.secret:
			raise ValueError('企业微信智能机器人凭据不完整')

	def validate_send(self) -> None:
		self.validate_credentials()
		if not self.to_user_id:
			raise ValueError('企业微信单聊接收人尚未绑定，请先运行 bind 命令')

	def secret_json(self) -> str:
		return json.dumps(asdict(self), ensure_ascii=False, separators=(',', ':'))

	def redact(self, text: str) -> str:
		for value in (self.secret, self.bot_id, self.to_user_id):
			if value:
				text = text.replace(value, '[redacted]')
		return text


def _split_text(text: str, limit: int = MESSAGE_BYTE_LIMIT) -> list[str]:
	"""按 UTF-8 字节切分，避免截断中文或 emoji。"""
	if limit < 4:
		raise ValueError('消息分片字节上限不得小于 4')
	parts = []
	chars: list[str] = []
	size = 0
	for char in text:
		char_size = len(char.encode('utf-8'))
		if size + char_size > limit:
			parts.append(''.join(chars))
			chars = []
			size = 0
		chars.append(char)
		size += char_size
	if chars:
		parts.append(''.join(chars))
	return parts


class WecomAiBotClient:
	def __init__(self, config: WecomAiBotConfig):
		self.config = config
		self.websocket: ClientConnection | None = None

	@classmethod
	def from_env(cls) -> 'WecomAiBotClient':
		return cls(WecomAiBotConfig.load())

	def __enter__(self) -> 'WecomAiBotClient':
		self.config.validate_credentials()
		try:
			# 企业微信使用 aibot_ping 心跳，不响应底层 WebSocket Ping。
			self.websocket = connect(
				WS_URL, proxy=None, ping_interval=None, open_timeout=REQUEST_TIMEOUT, close_timeout=5
			)
			self._request('aibot_subscribe', {'bot_id': self.config.bot_id, 'secret': self.config.secret})
		except Exception as exc:
			self.close()
			raise RuntimeError(self.config.redact(f'企业微信机器人连接/认证失败: {exc}')) from None
		return self

	def close(self) -> None:
		if self.websocket is not None:
			try:
				self.websocket.close()
			finally:
				self.websocket = None

	def __exit__(self, *_args: object) -> None:
		self.close()

	def _receive(self, timeout: float) -> dict[str, Any]:
		if self.websocket is None:
			raise RuntimeError('企业微信机器人未连接')
		try:
			frame = json.loads(self.websocket.recv(timeout=timeout))
		except (json.JSONDecodeError, UnicodeDecodeError):
			raise RuntimeError('企业微信机器人返回了无效 JSON') from None
		if not isinstance(frame, dict):
			raise RuntimeError('企业微信机器人返回格式错误')
		return frame

	def _send(self, cmd: str, body: dict[str, Any] | None = None) -> str:
		if self.websocket is None:
			raise RuntimeError('企业微信机器人未连接')
		req_id = f'{cmd}_{uuid.uuid4().hex}'
		frame: dict[str, Any] = {'cmd': cmd, 'headers': {'req_id': req_id}}
		if body is not None:
			frame['body'] = body
		self.websocket.send(json.dumps(frame, ensure_ascii=False))
		return req_id

	def _request(self, cmd: str, body: dict[str, Any]) -> dict[str, Any]:
		req_id = self._send(cmd, body)
		deadline = time.monotonic() + REQUEST_TIMEOUT
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise TimeoutError('企业微信机器人回执超时')
			frame = self._receive(remaining)
			headers = frame.get('headers')
			if not isinstance(headers, dict) or headers.get('req_id') != req_id:
				continue
			if frame.get('errcode') != 0:
				message = self.config.redact(str(frame.get('errmsg') or '未知错误'))
				raise RuntimeError(f'企业微信机器人请求失败: {message}')
			return frame

	def send_text(self, text: str) -> None:
		self.config.validate_send()
		if not text.strip():
			raise ValueError('企业微信通知内容为空')
		for part in _split_text(text):
			self._request(
				'aibot_send_msg',
				{'chatid': self.config.to_user_id, 'msgtype': 'markdown', 'markdown': {'content': part}},
			)

	def bind_single_chat(self, phrase: str, timeout_seconds: float = 480) -> None:
		"""仅接受包含一次性绑定口令的单聊文本，绝不从群聊或其他消息绑定。"""
		if not phrase.strip() or timeout_seconds <= 0:
			raise ValueError('绑定口令不能为空，超时必须大于零')
		deadline = time.monotonic() + timeout_seconds
		next_ping = time.monotonic() + HEARTBEAT_INTERVAL
		while True:
			now = time.monotonic()
			if now >= deadline:
				raise TimeoutError('单聊绑定超时，请重新启动绑定并发送新的口令')
			if now >= next_ping:
				self._send('aibot_ping')
				next_ping = now + HEARTBEAT_INTERVAL
			try:
				frame = self._receive(min(deadline - now, next_ping - now))
			except TimeoutError:
				continue
			if frame.get('cmd') != 'aibot_msg_callback':
				continue
			body = frame.get('body')
			if not isinstance(body, dict) or body.get('chattype') != 'single' or body.get('msgtype') != 'text':
				continue
			text = body.get('text')
			sender = body.get('from')
			if not isinstance(text, dict):
				continue
			content = text.get('content')
			if not isinstance(content, str) or content.strip() != phrase:
				continue
			if not isinstance(sender, dict):
				continue
			user = sender.get('userid')
			if isinstance(user, str) and user.strip():
				self.config.to_user_id = user.strip()
				return


def main() -> int:
	parser = argparse.ArgumentParser(description='企业微信智能机器人单聊通知')
	commands = parser.add_subparsers(dest='command', required=True)
	bind = commands.add_parser('bind', help='从标准输入读取凭据，绑定单聊并写入 GitHub secret')
	bind.add_argument('--repo', required=True)
	bind.add_argument('--environment', default='production')
	bind.add_argument('--timeout', type=int, default=480)
	bind.add_argument('--phrase', default='')
	commands.add_parser('test', help='读取 WECOM_AIBOT_CONFIG 并发送测试通知')
	args = parser.parse_args()
	config = WecomAiBotConfig()
	try:
		if args.command == 'test':
			config = WecomAiBotConfig.load()
			config.validate_send()
			with WecomAiBotClient(config) as client:
				client.send_text('AnyRouter 通知测试\n\n企业微信机器人单聊推送正常。')
			print('测试消息已获企业微信成功回执。')
			return 0

		gh_executable = shutil.which('gh')
		if not gh_executable:
			raise RuntimeError('绑定需要先安装并授权 GitHub CLI')
		config = WecomAiBotConfig.from_dict(json.load(sys.stdin))
		phrase = args.phrase or f'绑定签到通知 {secrets.randbelow(1_000_000):06d}'
		with WecomAiBotClient(config) as client:
			print(f'机器人认证成功，请在机器人单聊中发送：{phrase}', flush=True)
			client.bind_single_chat(phrase, timeout_seconds=args.timeout)
			client.send_text('AnyRouter 通知测试\n\n单聊推送测试成功，正在完成通知配置。')
		# 使用 gh 的绝对路径和固定参数列表，凭据仅经标准输入传递。
		result = subprocess.run(  # nosec B603
			[gh_executable, 'secret', 'set', 'WECOM_AIBOT_CONFIG', '-R', args.repo, '--env', args.environment],
			input=config.secret_json(),
			text=True,
			capture_output=True,
			timeout=60,
			check=False,
		)
		if result.returncode:
			raise RuntimeError('测试发送成功，但 GitHub secret 写入失败，请检查 GitHub 权限')
		print('单聊接收人已绑定，测试发送成功，production secret 已更新。')
		return 0
	except Exception as exc:
		print(f'企业微信通知操作失败：{config.redact(str(exc))}', file=sys.stderr)
		return 1


if __name__ == '__main__':
	sys.exit(main())
