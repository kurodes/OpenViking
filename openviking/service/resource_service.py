# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
Resource Service for OpenViking.

Provides resource management operations: add_resource, add_skill, wait_processed.
"""

from typing import Any, Dict, List, Optional

from openviking.resource import (
    IncrementalUpdater,
    ResourceLockConflictError,
)
from openviking.server.identity import RequestContext
from openviking.storage import VikingDBManager
from openviking.storage.queuefs import get_queue_manager
from openviking.storage.viking_fs import VikingFS, get_viking_fs
from openviking.utils.resource_processor import ResourceProcessor
from openviking.utils.skill_processor import SkillProcessor
from openviking_cli.exceptions import (
    ConflictError,
    DeadlineExceededError,
    InvalidArgumentError,
    NotInitializedError,
)
from openviking_cli.utils import get_logger
from openviking_cli.utils.uri import VikingURI
from openviking.storage.viking_fs import AGFSHTTPError

logger = get_logger(__name__)


class ResourceService:
    """Resource management service."""

    def __init__(
        self,
        vikingdb: Optional[VikingDBManager] = None,
        viking_fs: Optional[VikingFS] = None,
        resource_processor: Optional[ResourceProcessor] = None,
        skill_processor: Optional[SkillProcessor] = None,
        incremental_updater: Optional[IncrementalUpdater] = None,
    ):
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._resource_processor = resource_processor
        self._skill_processor = skill_processor
        self._incremental_updater = incremental_updater

    def set_dependencies(
        self,
        vikingdb: VikingDBManager,
        viking_fs: VikingFS,
        resource_processor: ResourceProcessor,
        skill_processor: SkillProcessor,
        incremental_updater: Optional[IncrementalUpdater] = None,
    ) -> None:
        """Set dependencies (for deferred initialization)."""
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._resource_processor = resource_processor
        self._skill_processor = skill_processor
        self._incremental_updater = incremental_updater

    def _ensure_initialized(self) -> None:
        """Ensure all dependencies are initialized."""
        if not self._resource_processor:
            raise NotInitializedError("ResourceProcessor")
        if not self._skill_processor:
            raise NotInitializedError("SkillProcessor")
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")

    async def add_resource(
        self,
        path: str,
        ctx: RequestContext,
        target: Optional[str] = None,
        reason: str = "",
        instruction: str = "",
        wait: bool = False,
        timeout: Optional[float] = None,
        build_index: bool = True,
        summarize: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Add resource to OpenViking (only supports resources scope).

        Args:
            path: Resource path (local file or URL)
            target: Target URI
            reason: Reason for adding
            instruction: Processing instruction
            wait: Whether to wait for semantic extraction and vectorization to complete
            timeout: Wait timeout in seconds
            build_index: Whether to build vector index immediately (default: True).
            summarize: Whether to generate summary (default: False).
            **kwargs: Extra options forwarded to the parser chain.

        Returns:
            Processing result
        """
        self._ensure_initialized()

        # add_resource only supports resources scope
        if target and target.startswith("viking://"):
            parsed = VikingURI(target)
            if parsed.scope != "resources":
                raise InvalidArgumentError(
                    f"add_resource only supports resources scope, use dedicated interface to add {parsed.scope} content"
                )
            
           
            try:
                viking_fs = get_viking_fs()
                await viking_fs.stat(parsed.full_path, ctx=ctx)
                # 如果执行到这里，说明资源存在
                logger.info(
                    f"Resource exists, performing incremental update: {target}"
                )
                
                update_result = await self._incremental_updater.update_resource(
                    resource_uri=target,
                    source_path=path,
                    ctx=ctx,
                    wait=wait,
                )
                
                result = {
                    "status": "success" if update_result.success else "error",
                    "root_uri": target,
                    "is_incremental": update_result.is_incremental,
                    "diff_stats": update_result.diff_stats,
                    "reuse_stats": update_result.reuse_stats,
                    "duration_ms": update_result.duration_ms,
                }
                
                if not update_result.success:
                    result["error"] = update_result.error_message
                    result["error_stage"] = update_result.error_stage
                
                return result
            except AGFSHTTPError as e:
                if e.status_code == 404:
                    logger.info(f"Resource not found, performing full update: {target}")
            except ResourceLockConflictError as e:
                logger.warning(f"Resource lock conflict: {e}")
                raise ConflictError(
                    f"Resource '{target}' is currently being updated by another operation"
                ) from e

        result = await self._resource_processor.process_resource(
            path=path,
            ctx=ctx,
            reason=reason,
            instruction=instruction,
            scope="resources",
            target=target,
            build_index=build_index,
            summarize=summarize,
            **kwargs,
        )

        if wait:
            qm = get_queue_manager()
            try:
                status = await qm.wait_complete(timeout=timeout)
            except TimeoutError as exc:
                raise DeadlineExceededError("queue processing", timeout) from exc
            result["queue_status"] = {
                name: {
                    "processed": s.processed,
                    "error_count": s.error_count,
                    "errors": [{"message": e.message} for e in s.errors],
                }
                for name, s in status.items()
            }

        return result

    async def add_skill(
        self,
        data: Any,
        ctx: RequestContext,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Add skill to OpenViking.

        Args:
            data: Skill data (directory path, file path, string, or dict)
            wait: Whether to wait for vectorization to complete
            timeout: Wait timeout in seconds

        Returns:
            Processing result
        """
        self._ensure_initialized()

        result = await self._skill_processor.process_skill(
            data=data,
            viking_fs=self._viking_fs,
            ctx=ctx,
        )

        if wait:
            qm = get_queue_manager()
            try:
                status = await qm.wait_complete(timeout=timeout)
            except TimeoutError as exc:
                raise DeadlineExceededError("queue processing", timeout) from exc
            result["queue_status"] = {
                name: {
                    "processed": s.processed,
                    "error_count": s.error_count,
                    "errors": [{"message": e.message} for e in s.errors],
                }
                for name, s in status.items()
            }

        return result

    async def build_index(
        self,
        resource_uris: List[str],
        ctx: RequestContext,
        **kwargs
    ) -> Dict[str, Any]:
        """Manually trigger index building.

        Args:
            resource_uris: List of resource URIs to index.
            ctx: Request context.

        Returns:
            Processing result
        """
        self._ensure_initialized()
        return await self._resource_processor.build_index(resource_uris, ctx, **kwargs)

    async def summarize(
        self,
        resource_uris: List[str],
        ctx: RequestContext,
        **kwargs
    ) -> Dict[str, Any]:
        """Manually trigger summarization.

        Args:
            resource_uris: List of resource URIs to summarize.
            ctx: Request context.

        Returns:
            Processing result
        """
        self._ensure_initialized()
        return await self._resource_processor.summarize(resource_uris, ctx, **kwargs)

    async def wait_processed(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Wait for all queued processing to complete.

        Args:
            timeout: Wait timeout in seconds

        Returns:
            Queue status
        """
        qm = get_queue_manager()
        try:
            status = await qm.wait_complete(timeout=timeout)
        except TimeoutError as exc:
            raise DeadlineExceededError("queue processing", timeout) from exc
        return {
            name: {
                "processed": s.processed,
                "error_count": s.error_count,
                "errors": [{"message": e.message} for e in s.errors],
            }
            for name, s in status.items()
        }
