# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
Incremental update orchestrator.

Coordinates the complete incremental update flow:
Lock → Staging → Diff → Reuse → Publish → Cleanup
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openviking.resource.diff_detector import DiffDetector, DiffResult
from openviking.resource.publication_manager import PublicationManager, PublicationResult
from openviking.resource.resource_lock import (
    LockInfo,
    ResourceLockConflictError,
    ResourceLockManager,
)
from openviking.resource.staging_manager import StagingArea, StagingManager
from openviking.resource.vector_reuse_manager import VectorReuseManager
from openviking.server.identity import RequestContext
from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend
from openviking_cli.utils import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)


@dataclass
class IncrementalUpdateResult:
    """Result of an incremental update operation."""
    
    success: bool
    resource_uri: str
    is_incremental: bool
    lock_id: Optional[str] = None
    staging_id: Optional[str] = None
    diff_stats: Optional[Dict[str, int]] = None
    reuse_stats: Optional[Dict[str, Any]] = None
    publication_result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    error_stage: Optional[str] = None
    duration_ms: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "resource_uri": self.resource_uri,
            "is_incremental": self.is_incremental,
            "lock_id": self.lock_id,
            "staging_id": self.staging_id,
            "diff_stats": self.diff_stats,
            "reuse_stats": self.reuse_stats,
            "publication_result": self.publication_result,
            "error_message": self.error_message,
            "error_stage": self.error_stage,
            "duration_ms": self.duration_ms,
        }


@dataclass
class UpdateContext:
    """Context for an incremental update operation."""
    
    resource_uri: str
    lock_info: Optional[LockInfo] = None
    staging_area: Optional[StagingArea] = None
    old_hashes: Dict[str, Any] = field(default_factory=dict)
    new_hashes: Dict[str, Any] = field(default_factory=dict)
    diff_result: Optional[DiffResult] = None
    reuse_plan: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_uri": self.resource_uri,
            "lock_id": self.lock_info.lock_id if self.lock_info else None,
            "staging_id": self.staging_area.staging_id if self.staging_area else None,
            "diff_stats": self.diff_result.get_stats() if self.diff_result else None,
        }


class IncrementalUpdater:
    """
    Orchestrates incremental resource updates.
    
    Implements the complete incremental update flow:
    1. Acquire resource lock
    2. Create staging area
    3. Upload new content to staging
    4. Compute content hashes and detect diff
    5. Prepare reuse plan for unchanged files
    6. Publish atomically (filesystem + vector index)
    7. Cleanup staging and lock
    """
    
    def __init__(
        self,
        agfs: Any,
        vector_backend: VikingVectorIndexBackend,
        lock_manager: Optional[ResourceLockManager] = None,
        staging_manager: Optional[StagingManager] = None,
        diff_detector: Optional[DiffDetector] = None,
        vector_reuse_manager: Optional[VectorReuseManager] = None,
        publication_manager: Optional[PublicationManager] = None,
    ):
        """
        Initialize IncrementalUpdater.
        
        Args:
            agfs: AGFS client instance
            vector_backend: VikingVectorIndexBackend instance
            lock_manager: Optional ResourceLockManager instance
            staging_manager: Optional StagingManager instance
            diff_detector: Optional DiffDetector instance
            vector_reuse_manager: Optional VectorReuseManager instance
            publication_manager: Optional PublicationManager instance
        """
        self._agfs = agfs
        self._vector_backend = vector_backend
        
        self._lock_manager = lock_manager or ResourceLockManager(agfs)
        self._staging_manager = staging_manager or StagingManager(agfs)
        self._diff_detector = diff_detector or DiffDetector(agfs)
        self._vector_reuse_manager = vector_reuse_manager or VectorReuseManager(vector_backend)
        self._publication_manager = publication_manager or PublicationManager(agfs, vector_backend)
    
    def _log_stage(self, stage: str, ctx: UpdateContext, **kwargs) -> None:
        """Log a stage with structured context."""
        logger.info(
            f"[IncrementalUpdate] Stage: {stage}, "
            f"resource_uri={ctx.resource_uri}, "
            + ", ".join(f"{k}={v}" for k, v in kwargs.items())
        )
    
    def _log_error(self, stage: str, ctx: UpdateContext, error: Exception) -> None:
        """Log an error with structured context."""
        logger.error(
            f"[IncrementalUpdate] Error in stage '{stage}': {error}, "
            f"resource_uri={ctx.resource_uri}"
        )
    
    async def _acquire_lock(
        self,
        ctx: UpdateContext,
        operation: str = "incremental_update",
        ttl: int = 3600,
    ) -> LockInfo:
        """Acquire resource lock."""
        self._log_stage("acquire_lock", ctx)
        
        lock_info = self._lock_manager.acquire_lock(
            resource_uri=ctx.resource_uri,
            operation=operation,
            ttl=ttl,
        )
        
        ctx.lock_info = lock_info
        self._log_stage("lock_acquired", ctx, lock_id=lock_info.lock_id)
        
        return lock_info
    
    def _release_lock(self, ctx: UpdateContext) -> bool:
        """Release resource lock."""
        if not ctx.lock_info:
            return True
        
        self._log_stage("release_lock", ctx, lock_id=ctx.lock_info.lock_id)
        
        return self._lock_manager.release_lock(
            resource_uri=ctx.resource_uri,
            lock_id=ctx.lock_info.lock_id,
        )
    
    def _create_staging_area(self, ctx: UpdateContext) -> StagingArea:
        """Create staging area."""
        self._log_stage("create_staging", ctx)
        
        staging_area = self._staging_manager.create_staging_area(ctx.resource_uri)
        ctx.staging_area = staging_area
        
        self._log_stage("staging_created", ctx, staging_id=staging_area.staging_id)
        
        return staging_area
    
    def _cleanup_staging(self, ctx: UpdateContext) -> bool:
        """Cleanup staging area."""
        if not ctx.staging_area:
            return True
        
        self._log_stage("cleanup_staging", ctx, staging_id=ctx.staging_area.staging_id)
        
        return self._staging_manager.cleanup_staging_area(ctx.staging_area)
    
    def _collect_old_hashes(self, ctx: UpdateContext) -> Dict[str, Any]:
        """Collect hashes for old version."""
        self._log_stage("collect_old_hashes", ctx)
        
        old_hashes = self._diff_detector.collect_resource_hashes(ctx.resource_uri)
        ctx.old_hashes = old_hashes
        
        self._log_stage(
            "old_hashes_collected",
            ctx,
            files=len([h for h in old_hashes.values() if not h.is_directory]),
            directories=len([h for h in old_hashes.values() if h.is_directory]),
        )
        
        return old_hashes
    
    def _collect_new_hashes(self, ctx: UpdateContext) -> Dict[str, Any]:
        """Collect hashes for new version in staging."""
        self._log_stage("collect_new_hashes", ctx)
        
        new_hashes = self._diff_detector.collect_resource_hashes(ctx.staging_area.staging_uri)
        ctx.new_hashes = new_hashes
        
        self._log_stage(
            "new_hashes_collected",
            ctx,
            files=len([h for h in new_hashes.values() if not h.is_directory]),
            directories=len([h for h in new_hashes.values() if h.is_directory]),
        )
        
        return new_hashes
    
    def _detect_diff(self, ctx: UpdateContext) -> DiffResult:
        """Detect differences between old and new versions."""
        self._log_stage("detect_diff", ctx)
        
        diff_result = self._diff_detector.detect_diff(ctx.old_hashes, ctx.new_hashes)
        ctx.diff_result = diff_result
        
        stats = diff_result.get_stats()
        self._log_stage(
            "diff_detected",
            ctx,
            added_files=stats["added_files"],
            modified_files=stats["modified_files"],
            deleted_files=stats["deleted_files"],
            unchanged_files=stats["unchanged_files"],
        )
        
        return diff_result
    
    async def _prepare_reuse_plan(self, ctx: UpdateContext) -> Dict[str, Any]:
        """Prepare reuse plan for unchanged files."""
        self._log_stage("prepare_reuse_plan", ctx)
        
        reuse_plan = await self._vector_reuse_manager.prepare_reuse_plan(
            diff_result=ctx.diff_result,
            old_resource_uri=ctx.resource_uri,
            new_resource_uri=ctx.staging_area.staging_uri,
        )
        ctx.reuse_plan = reuse_plan
        
        stats = reuse_plan.get("stats", {})
        self._log_stage(
            "reuse_plan_prepared",
            ctx,
            reused_summaries=stats.get("reused_summaries", 0),
            reused_vectors=stats.get("reused_vectors", 0),
            reuse_rate=stats.get("reuse_rate_summaries", 0),
        )
        
        return reuse_plan
    
    async def _publish(self, ctx: UpdateContext) -> PublicationResult:
        """Publish the update atomically."""
        self._log_stage("publish", ctx)
        
        publication_result = await self._publication_manager.publish(
            staging_area=ctx.staging_area,
            old_resource_uri=ctx.resource_uri,
            new_vectors=[],
        )
        
        self._log_stage(
            "publish_completed",
            ctx,
            success=publication_result.success,
            fs_switched=publication_result.fs_switched,
            vector_switched=publication_result.vector_switched,
            corrupted=publication_result.corrupted,
        )
        
        return publication_result
    
    async def update_resource(
        self,
        resource_uri: str,
        source_path: str,
        ctx: Optional[RequestContext] = None,
        wait: bool = False,
    ) -> IncrementalUpdateResult:
        """
        Perform incremental resource update.
        
        Args:
            resource_uri: Target resource URI
            source_path: Source path (local path or URL)
            ctx: Request context
            wait: Whether to wait for completion
            
        Returns:
            IncrementalUpdateResult object
        """
        start_time = time.time()
        
        update_ctx = UpdateContext(resource_uri=resource_uri)
        
        result = IncrementalUpdateResult(
            success=False,
            resource_uri=resource_uri,
            is_incremental=False,
        )
        
        try:
            viking_uri = VikingURI(resource_uri)
            logger.info(
                f"Updating resource: {resource_uri}, source_path: {source_path}"
            )
            resource_path = viking_uri.local_path
            
            is_incremental = self._agfs.exists(resource_path)
            logger.info(
                f"Resource exists, performing incremental update: {resource_uri}"
            )
            result.is_incremental = is_incremental
            
            self._log_stage(
                "start_update",
                update_ctx,
                is_incremental=is_incremental,
                source_path=source_path,
            )
            
            if not is_incremental:
                logger.info(
                    f"Resource does not exist, performing full update: {resource_uri}"
                )
                return await self._full_update(
                    resource_uri=resource_uri,
                    source_path=source_path,
                    ctx=ctx,
                    start_time=start_time,
                )
            
            lock_info = await self._acquire_lock(update_ctx)
            result.lock_id = lock_info.lock_id
            
            staging_area = self._create_staging_area(update_ctx)
            result.staging_id = staging_area.staging_id
            
            self._upload_to_staging(update_ctx, source_path)
            
            old_hashes = self._collect_old_hashes(update_ctx)
            new_hashes = self._collect_new_hashes(update_ctx)
            
            diff_result = self._detect_diff(update_ctx)
            result.diff_stats = diff_result.get_stats()
            
            if not diff_result.has_changes():
                logger.info(f"No changes detected, skipping update: {resource_uri}")
                result.success = True
                result.duration_ms = int((time.time() - start_time) * 1000)
                return result
            
            reuse_plan = await self._prepare_reuse_plan(update_ctx)
            result.reuse_stats = reuse_plan.get("stats")
            
            publication_result = await self._publish(update_ctx)
            result.publication_result = publication_result.to_dict()
            
            result.success = publication_result.success
            if not result.success:
                result.error_message = publication_result.error_message
                result.error_stage = "publish"
            
        except ResourceLockConflictError as e:
            self._log_error("acquire_lock", update_ctx, e)
            result.error_message = str(e)
            result.error_stage = "acquire_lock"
            
        except Exception as e:
            self._log_error("unknown", update_ctx, e)
            result.error_message = str(e)
            result.error_stage = "unknown"
            
        finally:
            self._cleanup_staging(update_ctx)
            self._release_lock(update_ctx)
            
            result.duration_ms = int((time.time() - start_time) * 1000)
            
            self._log_stage(
                "update_completed",
                update_ctx,
                success=result.success,
                duration_ms=result.duration_ms,
            )
        
        return result
    
    async def _full_update(
        self,
        resource_uri: str,
        source_path: str,
        ctx: Optional[RequestContext],
        start_time: float,
    ) -> IncrementalUpdateResult:
        """Perform full update (non-incremental)."""
        update_ctx = UpdateContext(resource_uri=resource_uri)
        
        result = IncrementalUpdateResult(
            success=False,
            resource_uri=resource_uri,
            is_incremental=False,
        )
        
        try:
            lock_info = await self._acquire_lock(update_ctx, operation="full_update")
            result.lock_id = lock_info.lock_id
            
            staging_area = self._create_staging_area(update_ctx)
            result.staging_id = staging_area.staging_id
            
            self._upload_to_staging(update_ctx, source_path)
            
            publication_result = await self._publication_manager.publish(
                staging_area=staging_area,
            )
            
            result.success = publication_result.success
            result.publication_result = publication_result.to_dict()
            
            if not result.success:
                result.error_message = publication_result.error_message
                result.error_stage = "publish"
            
        except Exception as e:
            self._log_error("full_update", update_ctx, e)
            result.error_message = str(e)
            result.error_stage = "full_update"
            
        finally:
            self._cleanup_staging(update_ctx)
            self._release_lock(update_ctx)
            
            result.duration_ms = int((time.time() - start_time) * 1000)
        
        return result
    
    def _upload_to_staging(self, ctx: UpdateContext, source_path: str) -> None:
        """Upload source content to staging area."""
        self._log_stage("upload_to_staging", ctx, source_path=source_path)
        
        import os
        
        if os.path.exists(source_path):
            success = self._staging_manager.upload_to_staging(
                staging_area=ctx.staging_area,
                local_path=source_path,
                relative_path="",
            )
        else:
            success = self._staging_manager.copy_to_staging(
                staging_area=ctx.staging_area,
                source_path=source_path,
                relative_path="",
            )
        
        if not success:
            raise RuntimeError(f"Failed to upload source to staging: {source_path}")
        
        self._log_stage("upload_completed", ctx)
    
    def is_resource_locked(self, resource_uri: str) -> bool:
        """Check if a resource is locked."""
        return self._lock_manager.is_locked(resource_uri)
    
    def get_resource_lock_info(self, resource_uri: str) -> Optional[LockInfo]:
        """Get lock information for a resource."""
        return self._lock_manager.get_lock_info(resource_uri)
    
    def is_resource_corrupted(self, resource_uri: str) -> bool:
        """Check if a resource is corrupted."""
        return self._publication_manager.is_resource_corrupted(resource_uri)
