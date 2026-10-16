import time
import threading
from typing import Dict, List

from app.logger import logger
from app.notion_client import NotionOpusAPI

class AccountPool:
    def __init__(self, accounts: List[dict]):
        """
        从配置列表初始化，每个 dict 对应一组凭据。
        同时初始化客户端实例和它们的状态。
        """
        if not accounts:
            raise ValueError("账号池初始化失败：未提供任何账号配置。")
            
        self.clients = [NotionOpusAPI(acc) for acc in accounts]
        # 记录每个客户端的冷却释放时间戳（0 表示可用）
        self.cooldown_until = [0.0 for _ in self.clients]
        
        self._lock = threading.Lock()
        
    def get_client(self, wait_if_cooling: bool = True) -> NotionOpusAPI:
        """
        顺序返回第一个可用客户端。
        过滤掉正处于冷却期中的客户端。

        如果 wait_if_cooling=True（默认），当所有账号都在冷却时，
        会等待最近的冷却结束后返回，而不是抛异常。
        """
        now = time.time()
        with self._lock:
            # 顺序使用：优先第一个可用账号，只有它冷却了才顺延到下一个。
            # 这样请求都打在同一个账号上，其他账号是备用，不会来回切换。
            while True:
                for idx in range(len(self.clients)):
                    if self.cooldown_until[idx] <= now:
                        return self.clients[idx]

                # 所有账号都在冷却
                next_available = min(self.cooldown_until)
                wait_seconds = max(0.5, next_available - now)

                if wait_if_cooling and wait_seconds <= 15:
                    # 等待冷却结束后重新尝试
                    logger.info(
                        f"All accounts cooling, waiting {wait_seconds:.1f}s",
                        extra={
                            "request_info": {
                                "event": "account_pool_wait_cooling",
                                "wait_seconds": round(wait_seconds, 1),
                            }
                        },
                    )
                    # 释放锁再 sleep，避免阻塞其他线程
                    self._lock.release()
                    try:
                        time.sleep(wait_seconds)
                    finally:
                        self._lock.acquire()
                    # 更新时间后重新扫描
                    now = time.time()
                    continue

                raise RuntimeError(
                    f"所有账号冷却中，请在 {max(1, int(wait_seconds))} 秒后重试。"
                )

    def get_status_summary(self) -> Dict[str, int]:
        """返回账号池简要状态，供健康检查和日志使用。"""
        now = time.time()
        with self._lock:
            active = sum(1 for ts in self.cooldown_until if ts <= now)
            cooling = len(self.cooldown_until) - active
            return {
                "total": len(self.clients),
                "active": active,
                "cooling": cooling,
            }
                    
    def mark_failed(self, client: NotionOpusAPI, cooldown_seconds: int = 3):
        """
        标记某个客户端为临时不可用（默认冷却 3 秒后恢复）。
        """
        with self._lock:
            try:
                idx = self.clients.index(client)
                # 记录未来的冷却解封时间
                self.cooldown_until[idx] = time.time() + cooldown_seconds
                logger.warning(
                    "Account marked as failed",
                    extra={
                        "request_info": {
                            "event": "account_failed",
                            "account": client.account_key,
                            "space_id": client.space_id,
                            "cooldown_seconds": cooldown_seconds,
                        }
                    },
                )
            except ValueError:
                logger.warning(
                    "Attempted to mark unknown account as failed",
                    extra={"request_info": {"event": "account_failed_unknown"}},
                )
