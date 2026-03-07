# RFC 8878 Feature Gaps in ruzstd

This document identifies features specified in RFC 8878 (Zstandard) that are missing,
incomplete, or only partially implemented in this Rust library.

---

## Decompression (Decoding)

Decompression is the most complete part of the library. All mandatory features from RFC 8878
are implemented. Known gaps are ergonomic or behavioral rather than correctness issues.

### Skippable Frames — Not Automatically Skipped

RFC 8878 §3.1.2 specifies that a compliant decoder must skip skippable frames
(magic numbers `0x184D2A50`–`0x184D2A5F`) and resume decoding afterwards.

**Current behavior:** `FrameDecoder` detects a skippable frame and returns
`ReadFrameHeaderError::SkipFrame(n)`, requiring the caller to manually advance
`n` bytes and retry. `StreamingDecoder` does not handle this automatically.

**Impact:** A stream containing interleaved skippable frames cannot be decoded
transparently by `StreamingDecoder`. The caller must implement the skip logic manually.

**Relevant code:** `decoding/streaming_decoder.rs:21-28`, `decoding/frame.rs:14-22`

---

### Multiple Concatenated Frames — No Automatic Chaining in StreamingDecoder

RFC 8878 §3.1 says the decompressed content of multiple concatenated frames is the
concatenation of each frame's decompressed content.

**Current behavior:** `StreamingDecoder` only decodes a single frame. The low-level
`FrameDecoder` exposes `decode_frame_multiple()` but it is not surfaced through the
`io::Read` interface.

**Impact:** A `.zst` file containing multiple frames (common when files are concatenated
or when skippable frames are present) cannot be decoded through the `Read` interface.

---

## Compression (Encoding)

The encoder is incomplete. Only the "Fastest" (Level 1) compression strategy is
implemented. Several RFC-required behaviors and optimizations are absent.

### Compression Levels — Only Level 1 Implemented

RFC 8878 §3 requires that a compliant compressor produce output conforming to the spec.
It does not mandate specific levels, but the library exposes an API implying higher levels:

| Level | `CompressionLevel` variant | Status |
|-------|---------------------------|--------|
| 0 | `Uncompressed` | Implemented (raw blocks only) |
| 1 | `Fastest` | Implemented |
| 3 | `Default` | `unimplemented!()` panic |
| 7 | `Better` | `unimplemented!()` panic |
| 11 | `Best` | `unimplemented!()` panic |

**Relevant code:** `encoding/frame_compressor.rs:184-200`

---

### Repeat Offset History — Not Used During Encoding

RFC 8878 §3.1.1.5 defines three repeat offsets (`Repeated_Offset1/2/3`). Using them
avoids encoding the full offset value when a recent offset is reused, improving compression.

**Current behavior:** The encoder always encodes raw offset values, adding 3 to convert
from Offset_Value. The existing offset history (`repeat_offsets`) is never consulted.

**Impact:** Sequences that reference recently-used offsets are encoded less efficiently
than they could be.

**Relevant code:** `encoding/blocks/compressed.rs:27`
```
// TODO make use of the offset history
(offset + 3) as u32,
```

---

### Repeat FSE Tables (`Repeat_Mode`) — Not Used During Encoding

RFC 8878 §3.1.1.3.2.1 allows reusing FSE tables from the previous compressed block
(`Repeat_Mode`). This avoids re-encoding the table description when it has not changed,
saving bytes.

**Current behavior:** Every compressed block encodes its FSE tables from scratch
(`FSE_Compressed_Mode`).

**Relevant code:** `encoding/blocks/compressed.rs:54`

---

### Treeless Literals Block — Not Used During Encoding

RFC 8878 §3.1.1.3.1.1 defines `Treeless_Literals_Block` (type 3), which reuses the
Huffman tree from the previous compressed literals block, avoiding re-encoding the tree.

**Current behavior:** Every compressed literals block includes the full Huffman tree
description (`Compressed_Literals_Block`, type 2).

---

### Frame Content Size — Missing from Frame Header

RFC 8878 §3.1.1.1.4 defines `Frame_Content_Size` as the original uncompressed size,
stored in the frame header to help decoders pre-allocate memory.

**Current behavior:** The encoder does not include `Frame_Content_Size` in the header.

**Relevant code:** `encoding/frame_header.rs:74`

---

### Window Descriptor — Always Encoded at Maximum Size

RFC 8878 §3.1.1.1.2 defines the `Window_Descriptor` as a compact exponent+mantissa
format. Using the smallest valid descriptor saves space and helps decoders.

**Current behavior:** The encoder always writes the maximum window size.

**Relevant code:** `encoding/frame_header.rs:44`

---

### Cross-Block Match References — Not Supported

RFC 8878 §3.1.1.4 allows match copy commands to reference data from previous blocks
within `Window_Size` bytes. This is fundamental to achieving good compression across
block boundaries.

**Current behavior:** The `MatchGeneratorDriver` uses `max_slices_in_window = 1`,
which means it evicts the previous block before processing the next one. All matches
are constrained to the current 128 KB block.

**Impact:** Repetitions that span block boundaries are not compressed. Particularly
visible on larger files with patterns that repeat over long distances.

**Relevant code:** `encoding/match_generator.rs` — `MatchGeneratorDriver::new(1024 * 128, 1)`

---

### Dictionary Support During Encoding — Not Implemented

RFC 8878 §5 describes the dictionary format and §6 describes their use. A dictionary
provides pre-populated offset history, Huffman tables, and FSE tables to improve
compression on data similar to the training corpus.

**Current behavior:** The `dict_id` field can be set in the frame header, but the
encoder does not use a dictionary's content, entropy tables, or offset history during
compression.

---

### Skippable Frame Generation — Not Implemented

RFC 8878 §3.1.2 describes skippable frames for embedding user metadata in a zstd stream.

**Current behavior:** No API exists to emit skippable frames.

---

## Huffman Encoder

### Table Size Strategy — Heuristic Incomplete

The Huffman encoder uses a heuristic to decide whether to emit the tree using direct
weight encoding or FSE-compressed weights.

**Relevant code:** `huff0/huff0_encoder.rs:120`

---

## Summary Table

| RFC Section | Feature | Decoding | Encoding |
|-------------|---------|----------|----------|
| §3.1.1 | Zstandard frames | Complete | Partial |
| §3.1.1.1.4 | Frame_Content_Size in header | Complete | Missing |
| §3.1.1.1.2 | Optimal Window_Descriptor | Complete | Not optimized |
| §3.1.1.2 | Raw / RLE / Compressed blocks | Complete | Levels 0 and 1 only |
| §3.1.1.3.1.1 | Treeless_Literals_Block | Complete | Not used |
| §3.1.1.3.2.1 | Repeat_Mode (FSE tables) | Complete | Not used |
| §3.1.1.4 | Cross-block back-references | Complete | Not supported |
| §3.1.1.5 | Repeat offset history | Complete | Not used |
| §3.1.2 | Skippable frames (detect/skip) | Manual only | Not implemented |
| §3.1.2 | Skippable frames (generation) | N/A | Not implemented |
| §4.1 | FSE codec | Complete | Partial |
| §4.2 | Huffman codec | Complete | Partial |
| §5 | Dictionary format | Complete | Not used |
| Multi-frame | Concatenated frame streams | Low-level only | N/A |
| Compression levels | Levels 3, 7, 11 | N/A | Not implemented |
