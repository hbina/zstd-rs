//! Block header definitions.
use crate::common::MAX_BLOCK_SIZE;
use crate::decoding::errors::{BlockHeaderReadError, BlockSizeError};
use crate::io::Read;

/// There are 4 different kinds of blocks, and the type of block influences the meaning of `Block_Size`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockType {
    /// An uncompressed block.
    Raw,
    /// A single byte, repeated `Block_Size` times (Run Length Encoding).
    #[allow(clippy::upper_case_acronyms)]
    RLE,
    /// A Zstandard compressed block. `Block_Size` is the length of the compressed data.
    Compressed,
    /// This is not a valid block, and this value should not be used.
    /// If this value is present, it should be considered corrupted data.
    Reserved,
}

impl core::fmt::Display for BlockType {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> Result<(), core::fmt::Error> {
        match self {
            BlockType::Compressed => write!(f, "Compressed"),
            BlockType::Raw => write!(f, "Raw"),
            BlockType::RLE => write!(f, "RLE"),
            BlockType::Reserved => write!(f, "Reserverd"),
        }
    }
}

/// A representation of a single block header. As well as containing a frame header,
/// each Zstandard frame contains one or more blocks.
pub struct BlockHeader {
    /// Whether this block is the last block in the frame.
    /// It may be followed by an optional `Content_Checksum` if it is.
    pub last_block: bool,
    pub block_type: BlockType,
    /// The size of the decompressed data. If the block type
    /// is [BlockType::Reserved] or [BlockType::Compressed],
    /// this value is set to zero and should not be referenced.
    pub decompressed_size: u32,
    /// The size of the block. If the block is [BlockType::RLE],
    /// this value will be 1.
    pub content_size: u32,
}

impl BlockHeader {
    pub const SIZE: u64 = 3;

    /// Read 3 bytes from `r`, parse them as a block header, and return the header together
    /// with `r` so the caller can continue reading from where the header ended.
    pub fn parse<R: Read>(mut r: R) -> Result<(Self, R), BlockHeaderReadError> {
        let mut buf = [0u8; 3];
        r.read_exact(&mut buf)?;

        let last_block = buf[0] & 0x1 == 1;
        let block_type = match (buf[0] >> 1) & 0x3 {
            0 => BlockType::Raw,
            1 => BlockType::RLE,
            2 => BlockType::Compressed,
            3 => return Err(BlockHeaderReadError::FoundReservedBlock),
            _ => unreachable!(),
        };

        let block_content_size =
            u32::from(buf[0] >> 3) | (u32::from(buf[1]) << 5) | (u32::from(buf[2]) << 13);

        if block_content_size > MAX_BLOCK_SIZE {
            return Err(BlockSizeError::BlockSizeTooLarge {
                size: block_content_size,
            }
            .into());
        }

        let decompressed_size = match block_type {
            BlockType::Raw | BlockType::RLE => block_content_size,
            BlockType::Compressed | BlockType::Reserved => 0,
        };
        let content_size = match block_type {
            BlockType::Raw | BlockType::Compressed => block_content_size,
            BlockType::RLE => 1,
            BlockType::Reserved => 0,
        };

        Ok((
            BlockHeader {
                last_block,
                block_type,
                decompressed_size,
                content_size,
            },
            r,
        ))
    }
}
