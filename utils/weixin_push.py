#!/usr/bin/env python3
"""基于腾讯 iLink HTTP 协议的独立个人微信推送客户端。

协议参考：https://github.com/Tencent/openclaw-weixin
本模块不依赖 OpenClaw，仅复用其公开的登录、收取消息和发送消息协议。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx

DEFAULT_BASE_URL = 'https://ilinkai.weixin.qq.com'
DEFAULT_CHANNEL_VERSION = '2.4.6'
DEFAULT_BOT_AGENT = 'AnyRouterCheckIn/0.1.0'
DEFAULT_CONFIG_PATH = Path.home() / '.anyrouter-check-in' / 'weixin-push.json'
MESSAGE_CHUNK_LIMIT = 4000


def _client_version(version: str) -> int:
	parts: list[int] = []
	for raw in version.split('.')[:3]:
		try:
			parts.append(int(raw))
		except ValueError:
			parts.append(0)
	while len(parts) < 3:
		parts.append(0)
	major, minor, patch = parts
	return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _random_wechat_uin() -> str:
	random_value = secrets.randbits(32)
	return base64.b64encode(str(random_value).encode()).decode()


def _split_text(text: str, limit: int = MESSAGE_CHUNK_LIMIT) -> list[str]:
	if not text:
		return []
	return [text[index : index + limit] for index in range(0, len(text), limit)]


@dataclass
class WeixinPushConfig:
	bot_token: str = ''
	bot_id: str = ''
	base_url: str = DEFAULT_BASE_URL
	to_user_id: str = ''
	context_token: str = ''
	get_updates_buf: str = ''
	channel_version: str = DEFAULT_CHANNEL_VERSION
	bot_agent: str = DEFAULT_BOT_AGENT
	route_tag: str = ''

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> 'WeixinPushConfig':
		return cls(
			bot_token=str(data.get('bot_token') or data.get('token') or '').strip(),
			bot_id=str(data.get('bot_id') or data.get('account_id') or '').strip(),
			base_url=str(data.get('base_url') or DEFAULT_BASE_URL).strip().rstrip('/'),
			to_user_id=str(data.get('to_user_id') or '').strip(),
			context_token=str(data.get('context_token') or '').strip(),
			get_updates_buf=str(data.get('get_updates_buf') or ''),
			channel_version=str(data.get('channel_version') or DEFAULT_CHANNEL_VERSION).strip(),
			bot_agent=str(data.get('bot_agent') or DEFAULT_BOT_AGENT).strip(),
			route_tag=str(data.get('route_tag') or '').strip(),
		)

	@classmethod
	def load(cls, path: Path | None = None) -> 'WeixinPushConfig':
		secret_json = os.getenv('WEIXIN_PUSH_CONFIG', '').strip()
		if secret_json:
			try:
				payload = json.loads(secret_json)
			except json.JSONDecodeError as exc:
				raise ValueError(f'WEIXIN_PUSH_CONFIG 不是有效 JSON: {exc}') from exc
			if not isinstance(payload, dict):
				raise ValueError('WEIXIN_PUSH_CONFIG 必须是 JSON 对象')
			return cls.from_dict(payload)

		config_path = path or Path(os.getenv('WEIXIN_PUSH_CONFIG_FILE', DEFAULT_CONFIG_PATH))
		if not config_path.exists():
			return cls()
		try:
			payload = json.loads(config_path.read_text(encoding='utf-8'))
		except (OSError, json.JSONDecodeError) as exc:
			raise ValueError(f'无法读取微信推送配置 {config_path}: {exc}') from exc
		if not isinstance(payload, dict):
			raise ValueError(f'微信推送配置 {config_path} 必须是 JSON 对象')
		return cls.from_dict(payload)

	def save(self, path: Path | None = None) -> Path:
		config_path = path or Path(os.getenv('WEIXIN_PUSH_CONFIG_FILE', DEFAULT_CONFIG_PATH))
		config_path.parent.mkdir(parents=True, exist_ok=True)
		config_path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding='utf-8')
		try:
			config_path.chmod(0o600)
		except OSError:
			pass
		return config_path

	def validate_login(self) -> None:
		if not self.bot_token:
			raise ValueError('尚未登录，请先运行 login 命令扫码获取 bot_token')

	def validate_send(self) -> None:
		self.validate_login()
		if not self.to_user_id or not self.context_token:
			raise ValueError('尚未绑定接收人，请先运行 bind 命令并向机器人发送一条消息')

	def secret_json(self) -> str:
		return json.dumps(asdict(self), ensure_ascii=False, separators=(',', ':'))


class WeixinPushClient:
	def __init__(self, config: WeixinPushConfig, client: httpx.Client | None = None):
		self.config = config
		self._owns_client = client is None
		self.client = client or httpx.Client(timeout=httpx.Timeout(40.0, connect=15.0))

	@classmethod
	def from_env(cls) -> 'WeixinPushClient':
		return cls(WeixinPushConfig.load())

	def close(self) -> None:
		if self._owns_client:
			self.client.close()

	def __enter__(self) -> 'WeixinPushClient':
		return self

	def __exit__(self, *_args: object) -> None:
		self.close()

	def _url(self, endpoint: str, *, base_url: str | None = None) -> str:
		base = (base_url or self.config.base_url or DEFAULT_BASE_URL).rstrip('/') + '/'
		return urljoin(base, endpoint.lstrip('/'))

	def _common_headers(self) -> dict[str, str]:
		headers = {
			'iLink-App-Id': 'bot',
			'iLink-App-ClientVersion': str(_client_version(self.config.channel_version)),
		}
		if self.config.route_tag:
			headers['SKRouteTag'] = self.config.route_tag
		return headers

	def _auth_headers(self, *, include_token: bool = True) -> dict[str, str]:
		headers = {
			'Content-Type': 'application/json',
			'AuthorizationType': 'ilink_bot_token',
			'X-WECHAT-UIN': _random_wechat_uin(),
			**self._common_headers(),
		}
		if include_token and self.config.bot_token:
			headers['Authorization'] = f'Bearer {self.config.bot_token}'
		return headers

	def _base_info(self) -> dict[str, str]:
		return {
			'channel_version': self.config.channel_version,
			'bot_agent': self.config.bot_agent,
		}

	@staticmethod
	def _payload(response: httpx.Response, service: str) -> dict[str, Any]:
		if response.status_code >= 400:
			raise RuntimeError(f'{service} 请求失败: HTTP {response.status_code}')
		try:
			payload = response.json()
		except ValueError as exc:
			raise RuntimeError(f'{service} 返回了无效 JSON') from exc
		if not isinstance(payload, dict):
			raise RuntimeError(f'{service} 返回格式错误')
		ret = payload.get('ret')
		errcode = payload.get('errcode')
		if ret not in (None, 0) or errcode not in (None, 0):
			code = errcode if errcode not in (None, 0) else ret
			message = payload.get('errmsg') or '未知错误'
			raise RuntimeError(f'{service} 请求失败: {message} ({code})')
		return payload

	def _post(self, endpoint: str, body: dict[str, Any], service: str) -> dict[str, Any]:
		response = self.client.post(self._url(endpoint), headers=self._auth_headers(), json=body)
		return self._payload(response, service)

	def _notify_session(self, action: str) -> None:
		self._post(
			f'ilink/bot/msg/notify{action}',
			{'base_info': self._base_info()},
			f'notify{action}',
		)

	def login(self, *, timeout_seconds: int = 480, open_browser: bool = True) -> WeixinPushConfig:
		qr_response = self.client.post(
			self._url('ilink/bot/get_bot_qrcode?bot_type=3', base_url=DEFAULT_BASE_URL),
			headers=self._auth_headers(include_token=False),
			json={'local_token_list': []},
		)
		qr_payload = self._payload(qr_response, '获取登录二维码')
		qrcode = str(qr_payload.get('qrcode') or '')
		qrcode_url = str(qr_payload.get('qrcode_img_content') or '')
		if not qrcode or not qrcode_url:
			raise RuntimeError('登录接口未返回完整二维码信息')

		print(f'请使用微信扫描二维码链接：\n{qrcode_url}')
		if open_browser:
			webbrowser.open(qrcode_url)

		deadline = time.monotonic() + max(timeout_seconds, 30)
		poll_base_url = DEFAULT_BASE_URL
		verify_code = ''
		while time.monotonic() < deadline:
			endpoint = f'ilink/bot/get_qrcode_status?qrcode={quote(qrcode)}'
			if verify_code:
				endpoint += f'&verify_code={quote(verify_code)}'
			try:
				response = self.client.get(
					self._url(endpoint, base_url=poll_base_url),
					headers=self._common_headers(),
					timeout=40.0,
				)
				payload = self._payload(response, '查询扫码状态')
			except httpx.TimeoutException:
				continue

			status = payload.get('status')
			if status == 'confirmed':
				self.config.bot_token = str(payload.get('bot_token') or '').strip()
				self.config.bot_id = str(payload.get('ilink_bot_id') or '').strip()
				self.config.base_url = str(payload.get('baseurl') or poll_base_url).strip().rstrip('/')
				self.config.validate_login()
				if not self.config.bot_id:
					raise RuntimeError('扫码已确认，但接口未返回 ilink_bot_id')
				return self.config
			if status == 'need_verifycode':
				verify_code = input('请输入手机微信显示的数字：').strip()
				continue
			if status == 'verify_code_blocked':
				raise RuntimeError('验证码多次输入错误，请稍后重新登录')
			if status == 'scaned_but_redirect':
				redirect_host = str(payload.get('redirect_host') or '').strip()
				if redirect_host:
					poll_base_url = f'https://{redirect_host}'
			if status == 'binded_redirect':
				raise RuntimeError('该微信已绑定过其它客户端，请先解除旧绑定或使用原凭证')
			if status == 'expired':
				raise RuntimeError('二维码已过期，请重新运行 login')
			time.sleep(1)

		raise TimeoutError('等待微信扫码确认超时')

	def bind_recipient(self, *, timeout_seconds: int = 180) -> tuple[str, str]:
		self.config.validate_login()
		deadline = time.monotonic() + max(timeout_seconds, 30)
		cursor = self.config.get_updates_buf
		self._notify_session('start')
		try:
			while time.monotonic() < deadline:
				payload = self._post(
					'ilink/bot/getupdates',
					{'get_updates_buf': cursor, 'base_info': self._base_info()},
					'获取微信消息',
				)
				cursor = str(payload.get('get_updates_buf') or cursor)
				self.config.get_updates_buf = cursor
				for message in payload.get('msgs') or []:
					if not isinstance(message, dict):
						continue
					from_user_id = str(message.get('from_user_id') or '').strip()
					context_token = str(message.get('context_token') or '').strip()
					if from_user_id and context_token:
						self.config.to_user_id = from_user_id
						self.config.context_token = context_token
						return from_user_id, context_token
		finally:
			try:
				self._notify_session('stop')
			except Exception:
				pass
		raise TimeoutError('等待接收人消息超时，请确认已向机器人发送消息')

	def send_text(self, text: str) -> None:
		self.config.validate_send()
		chunks = _split_text(text)
		if not chunks:
			raise ValueError('推送内容不能为空')

		self._notify_session('start')
		try:
			for chunk in chunks:
				body = {
					'msg': {
						'from_user_id': '',
						'to_user_id': self.config.to_user_id,
						'client_id': f'anyrouter-{uuid.uuid4()}',
						'message_type': 2,
						'message_state': 2,
						'item_list': [{'type': 1, 'text_item': {'text': chunk}}],
						'context_token': self.config.context_token,
					},
					'base_info': self._base_info(),
				}
				self._post('ilink/bot/sendmessage', body, '发送微信消息')
		finally:
			try:
				self._notify_session('stop')
			except Exception:
				pass


def _redacted_config(config: WeixinPushConfig) -> dict[str, Any]:
	data = asdict(config)
	for key in ('bot_token', 'context_token', 'get_updates_buf'):
		value = str(data.get(key) or '')
		data[key] = f'{value[:6]}***{value[-4:]}' if len(value) > 12 else ('***' if value else '')
	return data


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description='独立个人微信消息推送工具（腾讯 iLink 协议）')
	parser.add_argument('--config', type=Path, help=f'配置文件路径，默认 {DEFAULT_CONFIG_PATH}')
	subparsers = parser.add_subparsers(dest='command', required=True)

	login_parser = subparsers.add_parser('login', help='生成二维码并扫码登录')
	login_parser.add_argument('--timeout', type=int, default=480)
	login_parser.add_argument('--no-browser', action='store_true', help='不自动打开二维码链接')

	bind_parser = subparsers.add_parser('bind', help='监听一条微信消息并绑定接收人')
	bind_parser.add_argument('--timeout', type=int, default=180)

	send_parser = subparsers.add_parser('send', help='发送文本消息')
	send_parser.add_argument('text', nargs='?', help='消息正文；省略时从 stdin 读取')
	send_parser.add_argument('--title', default='', help='可选消息标题')

	subparsers.add_parser('show', help='脱敏显示当前配置')
	subparsers.add_parser('secret', help='输出完整 Secret JSON，便于通过管道写入 GitHub Secret')

	args = parser.parse_args(argv)
	config = WeixinPushConfig.load(args.config)

	try:
		with WeixinPushClient(config) as client:
			if args.command == 'login':
				client.login(timeout_seconds=args.timeout, open_browser=not args.no_browser)
				path = config.save(args.config)
				print(f'登录成功，凭证已保存到 {path}')
			elif args.command == 'bind':
				print('请在微信中向刚连接的机器人发送任意一条消息...')
				to_user_id, _ = client.bind_recipient(timeout_seconds=args.timeout)
				path = config.save(args.config)
				print(f'接收人绑定成功：{to_user_id}，配置已保存到 {path}')
			elif args.command == 'send':
				text = args.text if args.text is not None else sys.stdin.read()
				message = f'{args.title}\n{text}' if args.title else text
				client.send_text(message.strip())
				print('微信消息发送成功')
			elif args.command == 'show':
				print(json.dumps(_redacted_config(config), ensure_ascii=False, indent=2))
			elif args.command == 'secret':
				config.validate_send()
				print(config.secret_json())
		return 0
	except (ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
		print(f'微信推送失败：{exc}', file=sys.stderr)
		return 1


if __name__ == '__main__':
	raise SystemExit(main())
