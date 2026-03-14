/// Wraps a slice and enables reading arbitrary amounts of bits
/// from that slice.
pub struct BitReader<'s> {
    idx: usize, //index counts bits already read
    source: &'s [u8],
}

/// Load 1..=7 bytes from `src[byte_idx..]` into a `u64` (little-endian), without a loop.
///
/// Each byte past the first is conditionally included based on the runtime value of `bytes`,
/// which the compiler typically lowers to `cmov` instructions rather than branches.
///
/// SAFETY: the caller must ensure `src[byte_idx .. byte_idx + bytes]` is a valid range.
macro_rules! load_le_partial {
    ($src:expr, $byte_idx:expr, $bytes:expr) => {{
        let p: *const u8 = unsafe { $src.as_ptr().add($byte_idx) };
        let n: usize = $bytes;
        let b0 =                          unsafe { *p         } as u64;
        let b1 = if n > 1 { (unsafe { *p.add(1) } as u64) <<  8 } else { 0 };
        let b2 = if n > 2 { (unsafe { *p.add(2) } as u64) << 16 } else { 0 };
        let b3 = if n > 3 { (unsafe { *p.add(3) } as u64) << 24 } else { 0 };
        let b4 = if n > 4 { (unsafe { *p.add(4) } as u64) << 32 } else { 0 };
        let b5 = if n > 5 { (unsafe { *p.add(5) } as u64) << 40 } else { 0 };
        let b6 = if n > 6 { (unsafe { *p.add(6) } as u64) << 48 } else { 0 };
        b0 | b1 | b2 | b3 | b4 | b5 | b6
    }};
}

impl<'s> BitReader<'s> {
    pub fn new(source: &'s [u8]) -> BitReader<'s> {
        BitReader { idx: 0, source }
    }

    pub fn bits_left(&self) -> usize {
        self.source.len() * 8 - self.idx
    }

    pub fn bits_read(&self) -> usize {
        self.idx
    }

    pub fn return_bits(&mut self, n: usize) {
        if n > self.idx {
            panic!("Cant return this many bits");
        }
        self.idx -= n;
    }

    #[inline]
    pub fn get_bits(&mut self, n: usize) -> Result<u64, GetBitsError> {
        if n > 64 {
            return Err(GetBitsError::TooManyBits {
                num_requested_bits: n,
                limit: 64,
            });
        }
        if self.bits_left() < n {
            return Err(GetBitsError::NotEnoughRemainingBits {
                requested: n,
                remaining: self.bits_left(),
            });
        }
        if n == 0 {
            return Ok(0);
        }

        let byte_idx = self.idx >> 3;
        let bit_offset = self.idx & 7; // 0..=7

        // Mask for the lowest n bits. n in 1..=64 so (64 - n) in 0..=63 — no shift overflow.
        let mask = u64::MAX >> (64 - n);

        // Bytes available from the current byte position to the end of source.
        let remaining = self.source.len() - byte_idx;

        let value = if remaining >= 8 {
            // Fast path: load 8 bytes with a single unaligned word read.
            // SAFETY: remaining >= 8 ⟹ byte_idx + 8 <= source.len().
            let raw = u64::from_le(unsafe {
                (self.source.as_ptr().add(byte_idx) as *const u64).read_unaligned()
            });

            if bit_offset + n <= 64 {
                // All requested bits are contained in the 8-byte load.
                (raw >> bit_offset) & mask
            } else {
                // 9-byte case: bit_offset + n > 64.
                // When remaining == 8: bits_left() = 8*8 - bit_offset = 64 - bit_offset,
                // and bits_left() >= n forces n <= 64 - bit_offset, i.e. bit_offset + n <= 64.
                // That contradicts bit_offset + n > 64, so remaining >= 9 here.
                // SAFETY: remaining >= 9 ⟹ source[byte_idx + 8] is in bounds.
                let extra = unsafe { *self.source.as_ptr().add(byte_idx + 8) } as u64;
                ((raw >> bit_offset) | (extra << (64 - bit_offset))) & mask
            }
        } else {
            // Slow path: remaining is 1..=7.
            // bits_left() >= n ⟹ bit_offset + n <= remaining * 8 <= 56, so no 9-byte case.
            // SAFETY: source[byte_idx .. source.len()] has exactly `remaining` valid bytes.
            let raw = load_le_partial!(self.source, byte_idx, remaining);
            (raw >> bit_offset) & mask
        };

        self.idx += n;
        Ok(value)
    }
}

#[derive(Debug)]
#[non_exhaustive]
pub enum GetBitsError {
    TooManyBits {
        num_requested_bits: usize,
        limit: u8,
    },
    NotEnoughRemainingBits {
        requested: usize,
        remaining: usize,
    },
}

#[cfg(feature = "std")]
impl std::error::Error for GetBitsError {}

impl core::fmt::Display for GetBitsError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            GetBitsError::TooManyBits {
                num_requested_bits,
                limit,
            } => {
                write!(
                    f,
                    "Cant serve this request. The reader is limited to {limit} bits, requested {num_requested_bits} bits"
                )
            }
            GetBitsError::NotEnoughRemainingBits {
                requested,
                remaining,
            } => {
                write!(
                    f,
                    "Can\'t read {requested} bits, only have {remaining} bits left"
                )
            }
        }
    }
}
