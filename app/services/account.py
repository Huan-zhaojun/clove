import asyncio
import json
import os
import tempfile
import threading
import uuid
from collections import defaultdict
from datetime import datetime, UTC
from typing import List, Optional, Dict, Set, Tuple

from loguru import logger

from app.core.config import settings
from app.core.exceptions import (
    NoAccountsAvailableError,
    ClaudeAuthenticationError,
    ClaudeRateLimitedError,
)
from app.core.account import Account, AccountStatus, AuthType, OAuthToken
from app.services.oauth import oauth_authenticator


class AccountManager:
    """
    Singleton manager for Claude.ai accounts with load balancing and rate limit recovery.
    Supports both cookie and OAuth authentication.
    """

    _instance: Optional["AccountManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        """Implement singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the AccountManager."""
        self._accounts: Dict[str, Account] = {}  # organization_uuid -> Account
        self._cookie_to_uuid: Dict[str, str] = {}  # cookie_value -> organization_uuid
        self._session_accounts: Dict[str, str] = {}  # session_id -> organization_uuid
        self._account_sessions: Dict[str, Set[str]] = defaultdict(
            set
        )  # organization_uuid -> set of session_ids
        self._account_task: Optional[asyncio.Task] = None
        self._max_sessions_per_account = settings.max_sessions_per_cookie
        self._account_task_interval = settings.account_task_interval
        # 异步写操作锁，保护账户增删改的并发安全
        self._write_lock = asyncio.Lock()

        logger.info("AccountManager initialized")

    # 添加账户（DCL 双重检查锁：慢 I/O 在锁外并行，快操作在锁内串行）
    async def add_account(
        self,
        cookie_value: Optional[str] = None,
        oauth_token: Optional[OAuthToken] = None,
        organization_uuid: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> Account:
        """Add a new account to the manager.

        Uses double-checked locking to allow concurrent get_organization_info()
        calls while serializing fast dict mutations and disk writes.

        Args:
            cookie_value: The cookie value (optional)
            oauth_token: The OAuth token (optional)
            organization_uuid: The organization UUID (optional, will be fetched or generated if not provided)
            capabilities: The account capabilities (optional)

        Raises:
            ValueError: If neither cookie_value nor oauth_token is provided
        """
        if not cookie_value and not oauth_token:
            raise ValueError("Either cookie_value or oauth_token must be provided")

        # Phase 1 (锁内，快): 检查 cookie 是否已存在，已存在则直接返回
        async with self._write_lock:
            if cookie_value and cookie_value in self._cookie_to_uuid:
                return self._accounts[self._cookie_to_uuid[cookie_value]]

        # Phase 2 (锁外，慢): 获取 org UUID，多个不同 cookie 可并行执行
        if cookie_value and (not organization_uuid or not capabilities):
            (
                fetched_uuid,
                capabilities,
            ) = await oauth_authenticator.get_organization_info(cookie_value)
            if fetched_uuid:
                organization_uuid = fetched_uuid

        # Phase 3 (锁内，快): 二次检查 + 创建 + 持久化
        async with self._write_lock:
            # 二次检查 cookie 去重（其他并发请求可能已添加同一 cookie）
            if cookie_value and cookie_value in self._cookie_to_uuid:
                return self._accounts[self._cookie_to_uuid[cookie_value]]

            # 二次检查 org UUID 去重（更新已有账户的 cookie）
            if organization_uuid and organization_uuid in self._accounts:
                existing_account = self._accounts[organization_uuid]

                if cookie_value and existing_account.cookie_value != cookie_value:
                    if existing_account.cookie_value:
                        del self._cookie_to_uuid[existing_account.cookie_value]
                    existing_account.cookie_value = cookie_value
                    self._cookie_to_uuid[cookie_value] = organization_uuid
                return existing_account

            if not organization_uuid:
                organization_uuid = str(uuid.uuid4())
                logger.info(f"Generated new organization UUID: {organization_uuid}")

            # 创建新账户
            if cookie_value and oauth_token:
                auth_type = AuthType.BOTH
            elif cookie_value:
                auth_type = AuthType.COOKIE_ONLY
            else:
                auth_type = AuthType.OAUTH_ONLY

            account = Account(
                organization_uuid=organization_uuid,
                capabilities=capabilities,
                cookie_value=cookie_value,
                oauth_token=oauth_token,
                auth_type=auth_type,
            )
            self._accounts[organization_uuid] = account
            self.save_accounts()

            if cookie_value:
                self._cookie_to_uuid[cookie_value] = organization_uuid

        logger.info(
            f"Added new account: {organization_uuid[:8]}... "
            f"(auth_type: {auth_type.value}, "
            f"cookie: {cookie_value[:20] + '...' if cookie_value else 'None'}, "
            f"oauth: {'Yes' if oauth_token else 'No'})"
        )

        # 锁外: 启动后台 OAuth 认证任务
        if auth_type == AuthType.COOKIE_ONLY:
            asyncio.create_task(self._attempt_oauth_authentication(account))

        return account

    # 仅从内存中移除账户，不持久化到磁盘
    def _remove_account_from_memory(self, organization_uuid: str) -> None:
        """Remove an account from memory only, without saving to disk."""
        if organization_uuid in self._accounts:
            account = self._accounts[organization_uuid]
            sessions_to_remove = list(
                self._account_sessions.get(organization_uuid, set())
            )

            for session_id in sessions_to_remove:
                if session_id in self._session_accounts:
                    del self._session_accounts[session_id]

            if account.cookie_value and account.cookie_value in self._cookie_to_uuid:
                del self._cookie_to_uuid[account.cookie_value]

            del self._accounts[organization_uuid]

            if organization_uuid in self._account_sessions:
                del self._account_sessions[organization_uuid]

            logger.info(f"Removed account from memory: {organization_uuid[:8]}...")

    # 移除账户并持久化（保持原有单删行为）
    async def remove_account(self, organization_uuid: str) -> None:
        """Remove an account from the manager and persist to disk."""
        async with self._write_lock:
            self._remove_account_from_memory(organization_uuid)
            self.save_accounts()

    # 批量移除账户并单次持久化
    async def batch_remove_accounts(self, organization_uuids: List[str]) -> Dict:
        """Batch remove accounts and persist once. Returns success/failure stats."""
        async with self._write_lock:
            success_count = 0
            failures: List[Dict] = []

            for org_uuid in organization_uuids:
                if org_uuid not in self._accounts:
                    failures.append(
                        {"organization_uuid": org_uuid, "error": "Account not found"}
                    )
                    continue
                try:
                    self._remove_account_from_memory(org_uuid)
                    success_count += 1
                except Exception as e:
                    failures.append({"organization_uuid": org_uuid, "error": str(e)})

            if success_count > 0:
                self.save_accounts()

            logger.info(
                f"Batch remove: {success_count} succeeded, {len(failures)} failed"
            )

            return {
                "success_count": success_count,
                "failure_count": len(failures),
                "failures": failures,
            }

    async def get_account_for_session(
        self,
        session_id: str,
        is_pro: Optional[bool] = None,
        is_max: Optional[bool] = None,
    ) -> Account:
        """
        Get an available account for the session with load balancing.

        Args:
            session_id: Unique identifier for the session
            is_pro: Filter by pro capability. None means any.
            is_max: Filter by max capability. None means any.

        Returns:
            Account instance if available
        """
        # Convert single auth_type to list for uniform handling
        if session_id in self._session_accounts:
            organization_uuid = self._session_accounts[session_id]
            if organization_uuid in self._accounts:
                account = self._accounts[organization_uuid]
                if account.status == AccountStatus.VALID:
                    return account
                else:
                    del self._session_accounts[session_id]
                    self._account_sessions[organization_uuid].discard(session_id)

        best_account = None
        min_sessions = float("inf")
        earliest_last_used = None

        for organization_uuid, account in self._accounts.items():
            if account.status != AccountStatus.VALID:
                continue

            # Filter by auth type if specified
            if account.auth_type not in [AuthType.BOTH, AuthType.COOKIE_ONLY]:
                continue

            # Filter by capabilities if specified
            if is_pro is not None and account.is_pro != is_pro:
                continue
            if is_max is not None and account.is_max != is_max:
                continue

            session_count = len(self._account_sessions[organization_uuid])
            if session_count >= self._max_sessions_per_account:
                continue

            # Select account with least sessions
            # If multiple accounts have the same least sessions, select the one with earliest last_used
            if session_count < min_sessions or (
                session_count == min_sessions
                and (
                    earliest_last_used is not None
                    and account.last_used < earliest_last_used
                )
            ):
                min_sessions = session_count
                earliest_last_used = account.last_used
                best_account = account

        if best_account:
            self._session_accounts[session_id] = best_account.organization_uuid
            self._account_sessions[best_account.organization_uuid].add(session_id)

            logger.debug(
                f"Assigned account to session {session_id}, "
                f"account now has {len(self._account_sessions[best_account.organization_uuid])} sessions"
            )

            return best_account

        raise NoAccountsAvailableError()

    async def get_account_for_oauth(
        self,
        is_pro: Optional[bool] = None,
        is_max: Optional[bool] = None,
    ) -> Account:
        """
        Get an available account for OAuth authentication.

        Args:
            is_pro: Filter by pro capability. None means any.
            is_max: Filter by max capability. None means any.

        Returns:
            Account instance if available
        """
        earliest_account = None
        earliest_last_used = None

        for account in self._accounts.values():
            if account.status != AccountStatus.VALID:
                continue

            if account.auth_type not in [AuthType.OAUTH_ONLY, AuthType.BOTH]:
                continue

            # Filter by capabilities if specified
            if is_pro is not None and account.is_pro != is_pro:
                continue
            if is_max is not None and account.is_max != is_max:
                continue

            if earliest_last_used is None or account.last_used < earliest_last_used:
                earliest_last_used = account.last_used
                earliest_account = account

        if earliest_account:
            logger.debug(
                f"Selected OAuth account: {earliest_account.organization_uuid[:8]}... "
                f"(last used: {earliest_account.last_used.isoformat()})"
            )
            return earliest_account

        raise NoAccountsAvailableError()

    async def get_account_by_id(self, account_id: str) -> Optional[Account]:
        """
        Get an account by its organization UUID.

        Args:
            account_id: The organization UUID of the account

        Returns:
            Account instance if found and valid, None otherwise
        """
        account = self._accounts.get(account_id)

        if account and account.status == AccountStatus.VALID:
            logger.debug(f"Retrieved account by ID: {account_id[:8]}...")
            return account

        if account:
            logger.debug(
                f"Account {account_id[:8]}... found but not valid: status={account.status}"
            )
        else:
            logger.debug(f"Account {account_id[:8]}... not found")

        return None

    async def release_session(self, session_id: str) -> None:
        """Release a session's account assignment."""
        if session_id in self._session_accounts:
            organization_uuid = self._session_accounts[session_id]
            del self._session_accounts[session_id]

            if organization_uuid in self._account_sessions:
                self._account_sessions[organization_uuid].discard(session_id)

            logger.debug(f"Released account for session {session_id}")

    async def start_task(self) -> None:
        """Start the background task for AccountManager."""
        if self._account_task is None or self._account_task.done():
            self._account_task = asyncio.create_task(self._task_loop())

    async def stop_task(self) -> None:
        """Stop the background task for AccountManager."""
        if self._account_task and not self._account_task.done():
            self._account_task.cancel()
            try:
                await self._account_task
            except asyncio.CancelledError:
                pass

    async def _task_loop(self) -> None:
        """Background loop for AccountManager."""
        while True:
            try:
                await self._check_and_recover_accounts()
                await self._check_and_refresh_accounts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in task loop: {e}")
            finally:
                await asyncio.sleep(self._account_task_interval)

    async def _check_and_recover_accounts(self) -> None:
        """Check and recover rate-limited accounts."""
        current_time = datetime.now(UTC)

        for account in self._accounts.values():
            # Check rate-limited accounts
            if (
                account.status == AccountStatus.RATE_LIMITED
                and account.resets_at
                and current_time >= account.resets_at
            ):
                account.status = AccountStatus.VALID
                account.resets_at = None
                logger.info(
                    f"Recovered rate-limited account: {account.organization_uuid[:8]}..."
                )

    async def _check_and_refresh_accounts(self) -> None:
        """Check and refresh expired/expiring tokens."""
        current_timestamp = datetime.now(UTC).timestamp()

        for account in self._accounts.values():
            if (
                account.auth_type in [AuthType.OAUTH_ONLY, AuthType.BOTH]
                and account.oauth_token
                and account.oauth_token.refresh_token
                and account.oauth_token.expires_at
            ):
                if account.oauth_token.expires_at - current_timestamp < 300:
                    asyncio.create_task(self._refresh_account_token(account))

    async def _refresh_account_token(self, account: Account) -> None:
        """Refresh OAuth token for an account."""
        logger.info(
            f"Refreshing OAuth token for account: {account.organization_uuid[:8]}..."
        )

        success = await oauth_authenticator.refresh_account_token(account)
        if success:
            logger.info(
                f"Successfully refreshed OAuth token for account: {account.organization_uuid[:8]}..."
            )
        else:
            logger.warning(
                f"Failed to refresh OAuth token for account: {account.organization_uuid[:8]}..."
            )
            if account.auth_type == AuthType.BOTH:
                account.auth_type = AuthType.COOKIE_ONLY
                account.oauth_token = None
            else:
                account.status = AccountStatus.INVALID
                logger.error(
                    f"Account {account.organization_uuid[:8]} is now invalid due to OAuth refresh failure"
                )
            self.save_accounts()

    async def _attempt_oauth_authentication(self, account: Account) -> None:
        """Attempt OAuth authentication for an account."""

        logger.info(
            f"Attempting OAuth authentication for account: {account.organization_uuid[:8]}..."
        )

        success = await oauth_authenticator.authenticate_account(account)
        if not success:
            logger.warning(
                f"OAuth authentication failed for account: {account.organization_uuid[:8]}..., keeping as CookieOnly"
            )
        else:
            logger.info(
                f"OAuth authentication successful for account: {account.organization_uuid[:8]}..."
            )

    async def get_status(self) -> Dict:
        """Get the current status of all accounts."""
        status = {
            "total_accounts": len(self._accounts),
            "valid_accounts": sum(
                1 for a in self._accounts.values() if a.status == AccountStatus.VALID
            ),
            "rate_limited_accounts": sum(
                1
                for a in self._accounts.values()
                if a.status == AccountStatus.RATE_LIMITED
            ),
            "invalid_accounts": sum(
                1 for a in self._accounts.values() if a.status == AccountStatus.INVALID
            ),
            "active_sessions": len(self._session_accounts),
            "accounts": [],
        }

        for organization_uuid, account in self._accounts.items():
            account_info = {
                "organization_uuid": organization_uuid[:8] + "...",
                "cookie": account.cookie_value[:20] + "..."
                if account.cookie_value
                else "None",
                "status": account.status.value,
                "auth_type": account.auth_type.value,
                "sessions": len(self._account_sessions[organization_uuid]),
                "last_used": account.last_used.isoformat(),
                "resets_at": account.resets_at.isoformat()
                if account.resets_at
                else None,
                "has_oauth": account.oauth_token is not None,
            }
            status["accounts"].append(account_info)

        return status

    # 最小聊天测试，探测限流是否已解除
    async def _probe_rate_limit(
        self, account: Account
    ) -> Tuple[str, Optional[datetime]]:
        """Probe whether a rate-limited account has recovered.

        Returns: ('valid', None) | ('rate_limited', resets_at) | ('error', None)
        """
        from app.services.proxy import proxy_service

        has_oauth = account.auth_type in (AuthType.OAUTH_ONLY, AuthType.BOTH)

        if has_oauth and account.oauth_token:
            # OAuth 路径：直接 POST /v1/messages（最小请求）
            from app.core.http_client import create_session

            proxy_url = await proxy_service.get_proxy(
                account_id=account.organization_uuid
            )
            session = create_session(
                timeout=30,
                impersonate="chrome",
                proxy=proxy_url,
            )
            try:
                api_base = settings.claude_api_baseurl.encoded_string().rstrip("/")
                url = f"{api_base}/v1/messages"
                headers = {
                    "Authorization": f"Bearer {account.oauth_token.access_token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                }

                response = await session.request(
                    "POST", url, headers=headers, json=payload
                )

                if response.status_code == 200:
                    return ("valid", None)

                if response.status_code == 429:
                    # 从 header 提取官方重置时间
                    reset_header = response.headers.get(
                        "anthropic-ratelimit-unified-reset"
                    )
                    resets_at = None
                    if reset_header:
                        try:
                            resets_at = datetime.fromisoformat(
                                reset_header.replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            pass
                    return ("rate_limited", resets_at)

                return ("error", None)
            except Exception as e:
                logger.warning(
                    f"OAuth probe failed for {account.organization_uuid[:8]}...: {e}"
                )
                return ("error", None)
            finally:
                await session.close()
        else:
            # Cookie-only 路径：使用 ClaudeWebClient
            from app.core.external.claude_client import ClaudeWebClient

            client = ClaudeWebClient(account)
            conv_uuid = None
            try:
                await client.initialize()
                conv_uuid, _ = await client.create_conversation()
                # 发送最小消息
                payload = {
                    "prompt": "hi",
                    "timezone": "UTC",
                    "attachments": [],
                }
                await client.send_message(payload, conv_uuid)
                return ("valid", None)
            except ClaudeRateLimitedError as e:
                return ("rate_limited", e.resets_at)
            except Exception as e:
                logger.warning(
                    f"Cookie probe failed for {account.organization_uuid[:8]}...: {e}"
                )
                return ("error", None)
            finally:
                if conv_uuid:
                    try:
                        await client.delete_conversation(conv_uuid)
                    except Exception:
                        pass
                await client.cleanup()

    # 刷新单个账户状态
    async def refresh_account_status(self, organization_uuid: str) -> Dict:
        """Refresh a single account's status by validating credentials and probing rate limits.

        Returns a dict with refresh result details.
        """
        account = self._accounts.get(organization_uuid)
        if not account:
            return {
                "organization_uuid": organization_uuid,
                "previous_status": "unknown",
                "new_status": "unknown",
                "auth_type": "unknown",
                "error": "Account not found",
            }

        previous_status = account.status.value
        cookie_valid: Optional[bool] = None  # True / False / None(不确定)
        new_capabilities: Optional[list] = None

        # Phase 1 (锁外): Cookie 验证
        if account.cookie_value:
            try:
                _, capabilities = await oauth_authenticator.get_organization_info(
                    account.cookie_value
                )
                cookie_valid = True
                new_capabilities = capabilities
            except ClaudeAuthenticationError:
                cookie_valid = False
            except Exception as e:
                # 网络/代理等非认证错误，不误判
                logger.warning(
                    f"Cookie validation inconclusive for {organization_uuid[:8]}...: {e}"
                )
                cookie_valid = None

        # 锁外: OAuth 刷新（如有 OAuth token）
        if (
            account.auth_type in (AuthType.OAUTH_ONLY, AuthType.BOTH)
            and account.oauth_token
            and account.oauth_token.refresh_token
        ):
            try:
                await oauth_authenticator.refresh_account_token(account)
            except Exception as e:
                logger.warning(
                    f"OAuth refresh failed for {organization_uuid[:8]}...: {e}"
                )

        # Phase 2 (锁外): 限流探测（仅 RATE_LIMITED + Cookie 有效时）
        probe_result: Optional[str] = None
        probe_resets_at: Optional[datetime] = None
        if account.status == AccountStatus.RATE_LIMITED and cookie_valid is True:
            probe_result, probe_resets_at = await self._probe_rate_limit(account)

        # 锁内: 状态更新
        async with self._write_lock:
            if account.status == AccountStatus.RATE_LIMITED:
                # RATE_LIMITED 账户的状态转换
                if cookie_valid is False:
                    account.status = AccountStatus.INVALID
                    account.resets_at = None
                elif cookie_valid is True:
                    if new_capabilities:
                        account.capabilities = new_capabilities
                    if probe_result == "valid":
                        account.status = AccountStatus.VALID
                        account.resets_at = None
                    elif probe_result == "rate_limited":
                        if probe_resets_at is not None:
                            account.resets_at = probe_resets_at
                        # 无官方重置时间则保留已有 resets_at
                    # probe_result == 'error' 或 None: 不变

            elif account.status == AccountStatus.INVALID:
                # INVALID 账户的状态转换
                if cookie_valid is True:
                    account.status = AccountStatus.VALID
                    account.resets_at = None
                    if new_capabilities:
                        account.capabilities = new_capabilities
                # cookie_valid False 或 None: 不变

            elif account.status == AccountStatus.VALID:
                # VALID 账户的状态转换
                if cookie_valid is False:
                    account.status = AccountStatus.INVALID
                elif cookie_valid is True and new_capabilities:
                    account.capabilities = new_capabilities
                # cookie_valid None: 不变

            self.save_accounts()

        new_status = account.status.value
        logger.info(
            f"Refreshed account {organization_uuid[:8]}...: "
            f"{previous_status} -> {new_status}"
        )

        return {
            "organization_uuid": organization_uuid,
            "previous_status": previous_status,
            "new_status": new_status,
            "auth_type": account.auth_type.value,
            "capabilities": account.capabilities,
        }

    # 批量刷新账户状态（并发执行）
    async def batch_refresh_accounts(
        self, organization_uuids: List[str], concurrency: int = 5
    ) -> Dict:
        """Batch refresh account statuses with controlled concurrency."""
        sem = asyncio.Semaphore(min(concurrency, 20))

        async def _refresh_one(org_uuid: str) -> Dict:
            async with sem:
                try:
                    return await self.refresh_account_status(org_uuid)
                except Exception as e:
                    logger.error(f"Unexpected error refreshing {org_uuid[:8]}...: {e}")
                    return {
                        "organization_uuid": org_uuid,
                        "previous_status": "unknown",
                        "new_status": "unknown",
                        "auth_type": "unknown",
                        "error": str(e),
                    }

        results = await asyncio.gather(
            *[_refresh_one(uid) for uid in organization_uuids]
        )

        success_count = sum(1 for r in results if not r.get("error"))
        failure_count = sum(1 for r in results if r.get("error"))

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "results": list(results),
        }

    # 保存所有账户到 JSON 文件（原子写入：临时文件 + os.replace）
    def save_accounts(self) -> None:
        """Save all accounts to JSON file using atomic write."""
        if settings.no_filesystem_mode:
            logger.debug("No-filesystem mode enabled, skipping account save to disk")
            return

        settings.data_folder.mkdir(parents=True, exist_ok=True)

        accounts_file = settings.data_folder / "accounts.json"

        accounts_data = {
            organization_uuid: account.to_dict()
            for organization_uuid, account in self._accounts.items()
        }

        # 原子写入：先写临时文件，再替换正式文件
        fd, tmp_path = tempfile.mkstemp(dir=settings.data_folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(accounts_data, f, indent=2)
            os.replace(tmp_path, str(accounts_file))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        logger.info(f"Saved {len(accounts_data)} accounts to {accounts_file}")

    def load_accounts(self) -> None:
        """Load accounts from JSON file.

        Args:
            data_folder: Optional data folder path. If not provided, uses settings.data_folder
        """
        if settings.no_filesystem_mode:
            logger.debug("No-filesystem mode enabled, skipping account load from disk")
            return

        accounts_file = settings.data_folder / "accounts.json"

        if not accounts_file.exists():
            logger.info(f"No accounts file found at {accounts_file}")
            return

        try:
            with open(accounts_file, "r", encoding="utf-8") as f:
                accounts_data = json.load(f)

            for organization_uuid, account_data in accounts_data.items():
                account = Account.from_dict(account_data)
                self._accounts[organization_uuid] = account

                # Rebuild cookie mapping
                if account.cookie_value:
                    self._cookie_to_uuid[account.cookie_value] = organization_uuid

            logger.info(f"Loaded {len(accounts_data)} accounts from {accounts_file}")

        except Exception as e:
            logger.error(f"Failed to load accounts from {accounts_file}: {e}")

    def __repr__(self) -> str:
        """String representation of the AccountManager."""
        return f"<AccountManager accounts={len(self._accounts)} sessions={len(self._session_accounts)}>"


account_manager = AccountManager()
