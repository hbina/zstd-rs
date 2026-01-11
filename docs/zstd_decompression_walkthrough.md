# ZSTD Decompression Walkthrough

This document provides a detailed walkthrough of the ZSTD decompression pipeline as implemented in ruzstd, with references to the actual source code.

## Overview: The Big Picture

ZSTD compression works by encoding data as a series of **sequences**. Each sequence says:
1. **Literal Length (LL)**: Copy N literal bytes verbatim
2. **Match Length (ML)**: Copy M bytes from a previous position in the output
3. **Offset (OF)**: How far back to look for the match

The decompression pipeline reverses this process.

## Pipeline Architecture

```
User Code
    ↓
StreamingDecoder (io::Read wrapper) - Main entry point for most users
    ↓
FrameDecoder (Low-level frame controller)
    ↓
BlockDecoder (Individual block processing)
    ↓
[Literals Section Decoder + Sequence Section Decoder]
    ↓
[Sequence Execution]
    ↓
DecodeBuffer (Output buffer with ringbuffer)
```

---

## Step 1: Compressed Block Entry Point

**File:** `ruzstd/src/decoding/block_decoder.rs:114-119`

```rust
BlockType::Compressed => {
    self.decompress_block(header, workspace, source)?;
    self.internal_state = DecoderState::ReadyToDecodeNextHeader;
    Ok(u64::from(header.content_size))
}
```

When a compressed block is detected, it calls `decompress_block()`.

---

## Step 2: Decompress Block - Main Orchestration

**File:** `ruzstd/src/decoding/block_decoder.rs:123-223`

This is the core orchestration function. It does 5 things in order:

### 2a. Read the entire block content into memory

```rust
// block_decoder.rs:129-134
workspace.block_content_buffer.resize(header.content_size as usize, 0);
source.read_exact(workspace.block_content_buffer.as_mut_slice())?;
let raw = workspace.block_content_buffer.as_slice();
```

### 2b. Parse the Literals Section Header

```rust
// block_decoder.rs:136-138
let mut section = LiteralsSection::new();
let bytes_in_literals_header = section.parse_from_header(raw)?;
let raw = &raw[bytes_in_literals_header as usize..];
```

The `LiteralsSection` header tells us:
- Type (Raw, RLE, Compressed, Treeless)
- Regenerated size (decompressed size)
- Compressed size (if applicable)
- Number of Huffman streams (1 or 4)

### 2c. Decode the Literals

```rust
// block_decoder.rs:165-171
workspace.literals_buffer.clear();
let bytes_used_in_literals_section = decode_literals(
    &section,
    &mut workspace.huf,
    raw_literals,
    &mut workspace.literals_buffer,
)?;
```

### 2d. Parse and Decode Sequences

```rust
// block_decoder.rs:183-207
let mut seq_section = SequencesHeader::new();
let bytes_in_sequence_header = seq_section.parse_from_header(raw)?;
let raw = &raw[bytes_in_sequence_header as usize..];

if seq_section.num_sequences != 0 {
    decode_sequences(&seq_section, raw, &mut workspace.fse, &mut workspace.sequences)?;
    execute_sequences(workspace)?;
} else {
    // No sequences = just push literals directly to output
    workspace.buffer.push(&workspace.literals_buffer);
}
```

---

## Step 3: Literals Decoding

**File:** `ruzstd/src/decoding/literals_section_decoder.rs:12-34`

```rust
pub fn decode_literals(
    section: &LiteralsSection,
    scratch: &mut HuffmanScratch,
    source: &[u8],
    target: &mut Vec<u8>,
) -> Result<u32, DecompressLiteralsError> {
    match section.ls_type {
        LiteralsSectionType::Raw => {
            // Just copy bytes as-is
            target.extend(&source[0..section.regenerated_size as usize]);
            Ok(section.regenerated_size)
        }
        LiteralsSectionType::RLE => {
            // Repeat single byte N times
            target.resize(target.len() + section.regenerated_size as usize, source[0]);
            Ok(1)
        }
        LiteralsSectionType::Compressed | LiteralsSectionType::Treeless => {
            // Huffman decompression
            let bytes_read = decompress_literals(section, scratch, source, target)?;
            Ok(bytes_read)
        }
    }
}
```

### Huffman Decompression (when compressed)

**File:** `ruzstd/src/decoding/literals_section_decoder.rs:40-159`

For **Compressed** literals:
1. Build the Huffman table from the bitstream (`scratch.table.build_decoder()`)
2. For **Treeless** literals: reuse the previous block's Huffman table

The data is typically split into **4 streams** for parallelism:

```rust
// literals_section_decoder.rs:71-93
if num_streams == 4 {
    // Build jump table (6 bytes)
    let jump1 = source[0] as usize + ((source[1] as usize) << 8);
    let jump2 = jump1 + source[2] as usize + ((source[3] as usize) << 8);
    let jump3 = jump2 + source[4] as usize + ((source[5] as usize) << 8);

    let stream1 = &source[..jump1];
    let stream2 = &source[jump1..jump2];
    let stream3 = &source[jump2..jump3];
    let stream4 = &source[jump3..];
    // Decode each stream...
}
```

Each stream is decoded by:

```rust
// literals_section_decoder.rs:95-116
for stream in &[stream1, stream2, stream3, stream4] {
    let mut decoder = HuffmanDecoder::new(&scratch.table);
    let mut br = BitReaderReversed::new(stream);  // REVERSED - reads from end!

    // Skip padding bits (find first '1' bit)
    loop {
        let val = br.get_bits(1);
        if val == 1 || skipped_bits > 8 { break; }
    }

    decoder.init_state(&mut br);
    while br.bits_remaining() > -(scratch.table.max_num_bits as isize) {
        target.push(decoder.decode_symbol());
        decoder.next_state(&mut br);
    }
}
```

**Key insight**: ZSTD uses **reversed bit readers** - data is read from the end of the stream backwards!

---

## Step 4: Sequence Decoding

**File:** `ruzstd/src/decoding/sequence_section_decoder.rs:14-47`

```rust
pub fn decode_sequences(
    section: &SequencesHeader,
    source: &[u8],
    scratch: &mut FSEScratch,
    target: &mut Vec<Sequence>,
) -> Result<(), DecodeSequenceError> {
    // Step 1: Update FSE tables if needed
    let bytes_read = maybe_update_fse_tables(section, source, scratch)?;

    let bit_stream = &source[bytes_read..];
    let mut br = BitReaderReversed::new(bit_stream);  // Again, reversed!

    // Skip padding to find the start marker
    // ...

    // Step 2: Decode all sequences
    if scratch.ll_rle.is_some() || scratch.ml_rle.is_some() || scratch.of_rle.is_some() {
        decode_sequences_with_rle(section, &mut br, scratch, target)
    } else {
        decode_sequences_without_rle(section, &mut br, scratch, target)
    }
}
```

### FSE Table Modes

**File:** `ruzstd/src/decoding/sequence_section_decoder.rs:294-410`

Each of the 3 symbol types (LL, ML, OF) can use one of 4 modes:
- **FSECompressed**: Read a new FSE table from the bitstream
- **RLE**: All symbols have the same value (1 byte)
- **Predefined**: Use the RFC-specified default distribution
- **Repeat**: Reuse the previous block's table

### Decoding Each Sequence

**File:** `ruzstd/src/decoding/sequence_section_decoder.rs:154-221`

```rust
for _seq_idx in 0..section.num_sequences {
    // Get codes from FSE decoders
    let ll_code = ll_dec.decode_symbol();
    let ml_code = ml_dec.decode_symbol();
    let of_code = of_dec.decode_symbol();

    // Convert codes to (base_value, extra_bits_needed)
    let (ll_value, ll_num_bits) = lookup_ll_code(ll_code);
    let (ml_value, ml_num_bits) = lookup_ml_code(ml_code);

    // Read extra bits for all three at once (optimization)
    let (obits, ml_add, ll_add) = br.get_bits_triple(of_code, ml_num_bits, ll_num_bits);
    let offset = obits as u32 + (1u32 << of_code);

    target.push(Sequence {
        ll: ll_value + ll_add as u32,
        ml: ml_value + ml_add as u32,
        of: offset,
    });

    // Update FSE states for next iteration
    if target.len() < section.num_sequences as usize {
        ll_dec.update_state(br);
        ml_dec.update_state(br);
        of_dec.update_state(br);
    }
}
```

### Code Lookup Tables

**File:** `ruzstd/src/decoding/sequence_section_decoder.rs:227-284`

The RFC defines lookup tables for converting codes to values:

```rust
fn lookup_ll_code(code: u8) -> (u32, u8) {
    match code {
        0..=15 => (u32::from(code), 0),  // Codes 0-15 = literal values, no extra bits
        16 => (16, 1),   // Base 16, read 1 extra bit
        17 => (18, 1),   // Base 18, read 1 extra bit
        // ...
        35 => (65536, 16),  // Base 65536, read 16 extra bits
        _ => unreachable!()
    }
}
```

---

## Step 5: Sequence Execution

**File:** `ruzstd/src/decoding/sequence_execution.rs:5-54`

This is where the actual decompression happens:

```rust
pub fn execute_sequences(scratch: &mut DecoderScratch) -> Result<(), ExecuteSequencesError> {
    let mut literals_copy_counter = 0;

    for idx in 0..scratch.sequences.len() {
        let seq = scratch.sequences[idx];

        // Step 1: Copy literal bytes
        if seq.ll > 0 {
            let literals = &scratch.literals_buffer[literals_copy_counter..high];
            literals_copy_counter += seq.ll as usize;
            scratch.buffer.push(literals);
        }

        // Step 2: Handle offset history (repeat offsets)
        let actual_offset = do_offset_history(seq.of, seq.ll, &mut scratch.offset_hist);

        // Step 3: Copy match bytes from history
        if seq.ml > 0 {
            scratch.buffer.repeat(actual_offset as usize, seq.ml as usize)?;
        }
    }

    // Don't forget leftover literals after last sequence
    if literals_copy_counter < scratch.literals_buffer.len() {
        let rest_literals = &scratch.literals_buffer[literals_copy_counter..];
        scratch.buffer.push(rest_literals);
    }
}
```

### Offset History (Repeat Offsets)

**File:** `ruzstd/src/decoding/sequence_execution.rs:59-115`

ZSTD uses a clever optimization: the 3 most recent offsets are cached. Offsets 1-3 in the stream refer to these cached values:

```rust
fn do_offset_history(offset_value: u32, lit_len: u32, scratch: &mut [u32; 3]) -> u32 {
    let actual_offset = if lit_len > 0 {
        match offset_value {
            1..=3 => scratch[offset_value as usize - 1],  // Use cached offset
            _ => offset_value - 3,  // New offset (subtract 3 because 1-3 are reserved)
        }
    } else {
        // Special case when lit_len == 0
        match offset_value {
            1..=2 => scratch[offset_value as usize],
            3 => scratch[0] - 1,
            _ => offset_value - 3,
        }
    };

    // Update the history...
    actual_offset
}
```

---

## Step 6: Decode Buffer (Output Management)

**File:** `ruzstd/src/decoding/decode_buffer.rs:9-17`

```rust
pub struct DecodeBuffer {
    buffer: RingBuffer,           // Circular buffer
    pub dict_content: Vec<u8>,    // Dictionary data
    pub window_size: usize,       // History window size
    total_output_counter: u64,
    pub hash: twox_hash::XxHash64,  // For checksum validation
}
```

### The `repeat()` Operation

**File:** `ruzstd/src/decoding/decode_buffer.rs:67-99`

This is the heart of LZ77-style decompression:

```rust
pub fn repeat(&mut self, offset: usize, match_length: usize) -> Result<(), DecodeBufferError> {
    if offset > self.buffer.len() {
        // Need to copy from dictionary
        self.repeat_from_dict(offset, match_length)
    } else {
        let buf_len = self.buffer.len();
        let start_idx = buf_len - offset;
        let end_idx = start_idx + match_length;

        self.buffer.reserve(match_length);
        if end_idx > buf_len {
            // Overlapping copy - need to copy in chunks
            self.repeat_in_chunks(offset, match_length, start_idx);
        } else {
            // Non-overlapping - can copy directly
            unsafe {
                self.buffer.extend_from_within_unchecked(start_idx, match_length)
            };
        }
        Ok(())
    }
}
```

**Overlapping copies** are interesting: if `match_length > offset`, you're copying bytes that don't exist yet! For example, with offset=1 and match_length=10, you're repeating the last byte 10 times.

---

## Visual Summary

```
┌─────────────────────────────────────────────────────────────┐
│                    COMPRESSED BLOCK                         │
├─────────────────────────────────────────────────────────────┤
│ Literals Section              │ Sequences Section           │
│ ┌─────────┬─────────────────┐ │ ┌────────┬─────────────────┐│
│ │ Header  │ Huffman Data    │ │ │ Header │ FSE Bitstream   ││
│ │ (1-5B)  │ (4 streams)     │ │ │ (1-4B) │ (LL/ML/OF)      ││
│ └─────────┴─────────────────┘ │ └────────┴─────────────────┘│
└─────────────────────────────────────────────────────────────┘
                    │                         │
                    ▼                         ▼
        ┌───────────────────┐     ┌───────────────────────┐
        │ literals_buffer   │     │ sequences Vec         │
        │ [H,e,l,l,o,W,o,r] │     │ [(ll:5,ml:3,of:8),   │
        └───────────────────┘     │  (ll:2,ml:4,of:1)]   │
                    │             └───────────────────────┘
                    │                         │
                    └────────────┬────────────┘
                                 ▼
                    ┌─────────────────────────┐
                    │   execute_sequences()   │
                    │   For each sequence:    │
                    │   1. Copy ll literals   │
                    │   2. Copy ml from -of   │
                    └─────────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │    DecodeBuffer         │
                    │    (RingBuffer)         │
                    │    [H,e,l,l,o,H,e,l...] │
                    └─────────────────────────┘
```

---

## Key Source Files Reference

| File | Purpose |
|------|---------|
| `ruzstd/src/decoding/streaming_decoder.rs` | `io::Read` wrapper (recommended entry point) |
| `ruzstd/src/decoding/frame_decoder.rs` | Low-level frame controller with block loop |
| `ruzstd/src/decoding/frame.rs` | Frame header parsing (`read_frame_header()`) |
| `ruzstd/src/decoding/block_decoder.rs` | Block header parsing & content decompression |
| `ruzstd/src/decoding/literals_section_decoder.rs` | Huffman literal decompression (`decode_literals()`) |
| `ruzstd/src/decoding/sequence_section_decoder.rs` | FSE sequence decoding (`decode_sequences()`) |
| `ruzstd/src/decoding/sequence_execution.rs` | Sequence replay to output (`execute_sequences()`) |
| `ruzstd/src/decoding/decode_buffer.rs` | Output buffer with ringbuffer & window management |
| `ruzstd/src/decoding/ringbuffer.rs` | Circular buffer for efficient memory use |
| `ruzstd/src/decoding/scratch.rs` | Workspace structs: `DecoderScratch`, `FSEScratch`, `HuffmanScratch` |

---

## Performance Hotspots

1. **sequence_section_decoder.rs** - FSE decoding with bit-stream processing
2. **sequence_execution.rs** - Executing thousands of copy operations
3. **literals_section_decoder.rs** - Huffman decompression with 4-stream parallelism

---

## Further Reading

- [RFC 8878 - Zstandard Compression](https://datatracker.ietf.org/doc/html/rfc8878)
- [Facebook's ZSTD Format Specification](https://github.com/facebook/zstd/blob/dev/doc/zstd_compression_format.md)
