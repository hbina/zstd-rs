# StreamingDecoder Multi-Frame and Skippable Frame Support

## Summary

`StreamingDecoder` now handles multiple concatenated zstd frames and skippable frames
transparently, as required by RFC 8878.

## Changes

### `ruzstd/src/decoding/streaming_decoder.rs`

- Added `all_frames_done: bool` field to `StreamingDecoder` to track when no more frames
  remain in the stream.
- Added `skip_bytes` free function to consume skippable frame bodies by reading and
  discarding bytes in 4 KB chunks.
- `new()` and `new_with_decoder()` now loop over skippable frames at the start of the
  stream before finding the first real frame. If the stream contains only skippable frames
  (or is empty), `all_frames_done` is set to `true` and `Ok(...)` is returned with an
  empty-output decoder.
- `Read::read()` now advances to the next frame when the current frame is finished and
  fully drained. The same skip-frame loop is used. When no more frames are found (EOF or
  unrecognized header), `all_frames_done` is set to `true` and `Ok(0)` is returned.

### `ruzstd/src/tests/mod.rs`

Added `streaming_decoder_multi_frame` test covering:
- Two concatenated real frames
- Skippable frame at start followed by a real frame
- Skippable frames interleaved with real frames and at the end
- Stream with only skippable frames (empty output)

## Behavior

| Stream content | Before | After |
|---|---|---|
| Single frame | OK | OK (unchanged) |
| Multiple concatenated frames | Returns after first frame | All frames decoded |
| Skippable frame at start | `SkipFrame` error | Transparent skip |
| Skippable frames interleaved | `SkipFrame` error on second init | Transparent skip |
| Only skippable frames | `SkipFrame` error | Empty output, no error |
| Empty stream | `MagicNumberReadError` | Empty output, no error |
