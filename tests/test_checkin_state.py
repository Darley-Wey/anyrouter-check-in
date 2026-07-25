import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import checkin
from checkin import generate_balance_hash


def test_balance_hash_changes_when_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 125.0, 'used': 20.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_changes_when_used_quota_changes():
	before = {'account_1': {'quota': 100.0, 'used': 20.0}}
	after = {'account_1': {'quota': 100.0, 'used': 21.0}}

	assert generate_balance_hash(before) != generate_balance_hash(after)


def test_balance_hash_is_stable_for_equivalent_balances():
	left = {
		'account_2': {'quota': 50.0, 'used': 1.0},
		'account_1': {'quota': 100.0, 'used': 20.0},
	}
	right = {
		'account_1': {'used': 20.0, 'quota': 100.0},
		'account_2': {'used': 1.0, 'quota': 50.0},
	}

	assert generate_balance_hash(left) == generate_balance_hash(right)


@pytest.mark.asyncio
async def test_main_always_notifies_after_success_when_user_info_is_unavailable(monkeypatch):
	account = MagicMock()
	account.get_display_name.return_value = 'any1'
	push_message = MagicMock()

	monkeypatch.setattr(checkin, 'is_debug_enabled', lambda: False)
	monkeypatch.setattr(checkin.AppConfig, 'load_from_env', lambda: SimpleNamespace(providers={}))
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: [account])
	monkeypatch.setattr(checkin, 'load_balance_hash', lambda: None)
	monkeypatch.setattr(
		checkin,
		'check_in_account',
		AsyncMock(return_value=(True, None, {'success': False, 'error': 'HTTP 401'})),
	)
	monkeypatch.setattr(checkin.notify, 'push_message', push_message)

	with pytest.raises(SystemExit) as exc_info:
		await checkin.main()

	assert exc_info.value.code == 0
	push_message.assert_called_once()
	title, content = push_message.call_args.args
	assert title == 'AnyRouter Check-in Alert'
	assert '[SUCCESS] any1' in content
	assert 'HTTP 401' in content
