"""
Frame deduplication using perceptual hashing (pHash).
Eliminates near-duplicate frames before Vision API calls.
Typically reduces API calls by 30-40% on reels that revisit locations.
"""
import logging
from PIL import Image
import imagehash

from app.pipeline.frame_extractor import ExtractedFrame

logger = logging.getLogger(__name__)

# Hamming distance threshold: frames with distance < this are considered duplicates.
# Range 0–64. Lower = stricter dedup. 10 is a good balance for travel content.
PHASH_DISTANCE_THRESHOLD = 10


def deduplicate_frames(frames: list[ExtractedFrame]) -> list[ExtractedFrame]:
    """
    Remove near-duplicate frames using pHash.
    Preserves the first occurrence of each unique visual.
    Returns a filtered list maintaining original order.
    """
    if not frames:
        return []

    unique_frames: list[ExtractedFrame] = []
    seen_hashes: list[imagehash.ImageHash] = []

    for frame in frames:
        try:
            img = Image.open(frame.frame_path)
            h = imagehash.phash(img)

            is_duplicate = any(
                abs(h - existing) < PHASH_DISTANCE_THRESHOLD
                for existing in seen_hashes
            )

            if not is_duplicate:
                unique_frames.append(frame)
                seen_hashes.append(h)
            else:
                logger.debug(
                    f"Skipping duplicate frame {frame.scene_index} "
                    f"(t={frame.timestamp_seconds:.1f}s)"
                )

        except Exception as e:
            # If we can't hash a frame, include it (better to over-analyze than miss a location)
            logger.warning(f"Failed to hash frame {frame.scene_index}: {e}, keeping it")
            unique_frames.append(frame)

    removed = len(frames) - len(unique_frames)
    logger.info(
        f"Deduplication: {len(frames)} → {len(unique_frames)} frames "
        f"({removed} duplicates removed)"
    )
    return unique_frames
