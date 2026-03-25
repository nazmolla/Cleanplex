"""Segment library conflict resolution: voting, confidence weighting, and merge logic."""

import json
from typing import Any
from .logger import get_logger

logger = get_logger(__name__)


class SegmentMerger:
    """Handles merging segments from multiple sources with conflict resolution."""
    
    def __init__(
        self,
        local_segments: list[dict[str, Any]],
        cloud_sources: list[dict[str, Any]],
        timing_tolerance_ms: int = 2000,
        verified_threshold: int = 2,
        prefer_local: bool = True,
    ):
        """
        Initialize merger with local and cloud segments.
        
        Args:
            local_segments: List of local detected segments
            cloud_sources: List of dicts with 'segments', 'source_instance', 'confidence_level'
            timing_tolerance_ms: ±ms threshold for considering segments the same scene
            verified_threshold: Minimum sources needed to mark segment as "verified"
            prefer_local: Whether to prefer local scans over cloud (default: True)
        """
        self.local_segments = local_segments or []
        self.cloud_sources = cloud_sources or []
        self.timing_tolerance_ms = timing_tolerance_ms
        self.verified_threshold = verified_threshold
        self.prefer_local = prefer_local
    
    def _segments_match(self, seg1: dict, seg2: dict) -> bool:
        """
        Check if two segments represent the same scene.
        Uses timing tolerance to account for detector variations.
        """
        start_diff = abs(seg1["start_ms"] - seg2["start_ms"])
        end_diff = abs(seg1["end_ms"] - seg2["end_ms"])
        
        return (start_diff <= self.timing_tolerance_ms and 
                end_diff <= self.timing_tolerance_ms)
    
    def _calculate_confidence_score(self, segment: dict, source_type: str) -> float:
        """
        Calculate overall confidence for a segment based on source and detector confidence.
        
        Args:
            segment: Segment dict with 'confidence' field
            source_type: 'local', 'verified', or 'unverified'
        
        Returns: Float between 0 and 1
        """
        base_confidence = segment.get("confidence", 0.5)
        
        # Weight by source type
        source_weights = {
            "local": 1.0,           # Local scans are most trusted
            "verified": 0.85,       # Verified (2+ sources) are highly trusted
            "unverified": 0.6,      # Single source is less trusted
        }
        
        weight = source_weights.get(source_type, 0.5)
        return base_confidence * weight
    
    def _cluster_segments(self) -> list[list[dict]]:
        """
        Group segments from all sources that match each other.
        Returns list of clusters, where each cluster contains matching segments.
        """
        all_segments = []
        
        # Add local segments with metadata
        for seg in self.local_segments:
            all_segments.append({
                **seg,
                "_source_type": "local",
                "_source_instance": "local",
            })
        
        # Add cloud segments with metadata
        for source in self.cloud_sources:
            source_instance = source["source_instance"]
            for seg in source["segments"]:
                all_segments.append({
                    **seg,
                    "_source_type": source["confidence_level"],
                    "_source_instance": source_instance,
                })
        
        # Cluster matching segments
        clusters = []
        matched = set()
        
        for i, seg1 in enumerate(all_segments):
            if i in matched:
                continue
            
            cluster = [seg1]
            matched.add(i)
            
            for j, seg2 in enumerate(all_segments[i + 1:], start=i + 1):
                if j in matched:
                    continue
                
                if self._segments_match(seg1, seg2):
                    cluster.append(seg2)
                    matched.add(j)
            
            clusters.append(cluster)
        
        return clusters
    
    def _resolve_cluster(self, cluster: list[dict]) -> dict:
        """
        Resolve a cluster of matching segments into a single merged segment.
        Uses voting, confidence weighting, and source preference.
        """
        # Count sources
        source_instances = {seg["_source_instance"] for seg in cluster}
        sources_count = len(source_instances)
        
        # Determine confidence level based on voting
        is_local = any(seg["_source_type"] == "local" for seg in cluster)
        is_verified = (sources_count >= self.verified_threshold)
        
        if self.prefer_local and is_local:
            confidence_level = "local"
        elif is_verified:
            confidence_level = "verified"
        else:
            confidence_level = "unverified"
        
        # Use average timing (median is also option, but average is simpler)
        avg_start = sum(seg["start_ms"] for seg in cluster) / len(cluster)
        avg_end = sum(seg["end_ms"] for seg in cluster) / len(cluster)
        
        # Use highest detector confidence score
        max_detector_confidence = max(
            self._calculate_confidence_score(seg, confidence_level) 
            for seg in cluster
        )
        
        # Use most common labels (or union if diverse)
        label_counts = {}
        for seg in cluster:
            labels = seg.get("labels", "")
            label_counts[labels] = label_counts.get(labels, 0) + 1
        
        merged_labels = max(label_counts.items(), key=lambda x: x[1])[0]
        
        return {
            "start_ms": int(round(avg_start)),
            "end_ms": int(round(avg_end)),
            "confidence": max_detector_confidence,
            "labels": merged_labels,
            "sources": list(source_instances),
            "sources_count": sources_count,
            "confidence_level": confidence_level,
        }
    
    def merge(self) -> tuple[list[dict], dict[str, Any]]:
        """
        Perform full merge operation.
        
        Returns: (merged_segments, merge_stats)
        """
        if not self.cloud_sources and not self.local_segments:
            return [], {"status": "no_data", "merged_count": 0}
        
        # If no cloud data, just return local
        if not self.cloud_sources:
            return self.local_segments, {
                "status": "local_only",
                "merged_count": len(self.local_segments),
            }
        
        # Cluster and merge
        clusters = self._cluster_segments()
        merged = []
        
        for cluster in clusters:
            resolved = self._resolve_cluster(cluster)
            merged.append(resolved)
        
        # Sort by start time
        merged.sort(key=lambda s: s["start_ms"])
        
        stats = {
            "status": "merged",
            "merged_count": len(merged),
            "input_clusters": len(clusters),
            "total_sources": len({s["_source_instance"] for c in clusters for s in c}),
            "verified_count": sum(1 for s in merged if s["confidence_level"] == "verified"),
        }
        
        logger.info(f"Merged {stats['merged_count']} segments from {stats['total_sources']} sources")
        return merged, stats


async def resolve_segments(
    file_hash: str,
    local_segments: list[dict[str, Any]],
    cloud_sources: list[dict[str, Any]],
    timing_tolerance_ms: int = 2000,
    verified_threshold: int = 2,
    prefer_local: bool = True,
) -> tuple[list[dict], dict]:
    """
    High-level segment resolution API.
    Merges local and cloud segments for a single file.
    
    Returns: (merged_segments, stats)
    """
    merger = SegmentMerger(
        local_segments=local_segments,
        cloud_sources=cloud_sources,
        timing_tolerance_ms=timing_tolerance_ms,
        verified_threshold=verified_threshold,
        prefer_local=prefer_local,
    )
    
    merged, stats = merger.merge()
    
    logger.info(
        f"File {file_hash[:8]}...: "
        f"{len(local_segments)} local + {sum(len(s['segments']) for s in cloud_sources)} cloud "
        f"→ {len(merged)} merged"
    )
    
    return merged, stats
