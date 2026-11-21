import sys
sys.path.insert(0, r'D:\Games\new folder\clip')
from heatmap_bridge import find_heatmap_segments, best_heatmap_segment, get_video_duration

# Test with a known video
segments = find_heatmap_segments('dQw4w9WgXcQ', min_score=0.40, max_duration=60)
print(f'Found {len(segments)} heatmap segments')
if segments:
    print(f'Best segment: {segments[0]}')
    print(f'Type of best segment: {type(segments[0])}')
    # Check keys
    print(f'Keys in segment: {list(segments[0].keys()) if segments else "N/A"}')

# Test best single segment
best = best_heatmap_segment('dQw4w9WgXcQ', min_score=0.40, max_duration=60)
print(f'\nBest single segment: {best}')
if best:
    print(f'Best segment keys: {list(best.keys())}')

# Test duration
dur = get_video_duration('dQw4w9WgXcQ')
print(f'\nVideo duration: {dur}')