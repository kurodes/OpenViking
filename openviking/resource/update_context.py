# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from openviking.resource.resource_lock import LockInfo
from openviking.server.identity import RequestContext


@dataclass
class UpdateContext:
    """Context for an full/incremental update operation."""
    
    source_url: str
    target_uri:str
    temp_local_path: Optional[str] = None
    temp_vikingfs_path: Optional[str] = None
    request_context: Optional[RequestContext] = None
    lock_info: Optional[LockInfo] = None
    is_incremental: bool = False
    source_format: Optional[str] = None
    source_scope: Optional[str] = "resources"
    document_name: Optional[str] = None  # Document name determined by parser (e.g., "org/repo" for GitHub repos)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_url": self.source_url,
            "target_uri": self.target_uri,
            "temp_local_path": self.temp_local_path,
            "temp_vikingfs_path": self.temp_vikingfs_path,
            "lock_id": self.lock_info.lock_id if self.lock_info else None,
            "is_incremental": self.is_incremental,
            "source_format": self.source_format,
            "source_scope": self.source_scope,
            "trigger_semantic": self.trigger_semantic,
            "document_name": self.document_name,
        }
