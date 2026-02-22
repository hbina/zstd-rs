use super::super::blocks::block::BlockHeader;
use super::super::blocks::block::BlockType;
use super::super::blocks::literals_section::LiteralsSection;
use super::super::blocks::literals_section::LiteralsSectionType;
use super::super::blocks::sequence_section::SequencesHeader;
use super::literals_section_decoder::decode_literals;
use super::sequence_section_decoder::decode_sequences;
use crate::decoding::errors::DecodeSequenceError;
use crate::decoding::errors::{DecodeBlockContentError, DecompressBlockError};
use crate::decoding::scratch::DecoderScratch;
use crate::decoding::sequence_execution::execute_sequences;
use crate::io::Read;

pub fn decode_block_content(
    header: &BlockHeader,
    workspace: &mut DecoderScratch,
    mut source: impl Read,
) -> Result<u64, DecodeBlockContentError> {
    let block_type = header.block_type;
    match block_type {
        BlockType::RLE => {
            const BATCH_SIZE: usize = 512;
            let mut buf = [0u8; BATCH_SIZE];
            let full_reads = header.decompressed_size / BATCH_SIZE as u32;
            let single_read_size = header.decompressed_size % BATCH_SIZE as u32;

            source.read_exact(&mut buf[0..1]).map_err(|err| {
                DecodeBlockContentError::ReadError {
                    step: block_type,
                    source: err,
                }
            })?;

            for i in 1..BATCH_SIZE {
                buf[i] = buf[0];
            }

            for _ in 0..full_reads {
                workspace.buffer.push(&buf[..]);
            }
            let smaller = &mut buf[..single_read_size as usize];
            workspace.buffer.push(smaller);

            Ok(1)
        }
        BlockType::Raw => {
            const BATCH_SIZE: usize = 128 * 1024;
            let mut buf = [0u8; BATCH_SIZE];
            let full_reads = header.decompressed_size / BATCH_SIZE as u32;
            let single_read_size = header.decompressed_size % BATCH_SIZE as u32;

            for _ in 0..full_reads {
                source.read_exact(&mut buf[..]).map_err(|err| {
                    DecodeBlockContentError::ReadError {
                        step: block_type,
                        source: err,
                    }
                })?;
                workspace.buffer.push(&buf[..]);
            }

            let smaller = &mut buf[..single_read_size as usize];
            source
                .read_exact(smaller)
                .map_err(|err| DecodeBlockContentError::ReadError {
                    step: block_type,
                    source: err,
                })?;
            workspace.buffer.push(smaller);

            Ok(u64::from(header.decompressed_size))
        }

        BlockType::Reserved => {
            panic!("How did you even get this. The decoder should error out if it detects a reserved-type block");
        }

        BlockType::Compressed => {
            decompress_block(header, workspace, source)?;
            Ok(u64::from(header.content_size))
        }
    }
}

fn decompress_block(
    header: &BlockHeader,
    workspace: &mut DecoderScratch,
    mut source: impl Read,
) -> Result<(), DecompressBlockError> {
    workspace
        .block_content_buffer
        .resize(header.content_size as usize, 0);

    source.read_exact(workspace.block_content_buffer.as_mut_slice())?;
    let raw = workspace.block_content_buffer.as_slice();

    let (section, raw) = LiteralsSection::parse(raw)?;
    vprintln!(
        "Found {} literalssection with regenerated size: {}, and compressed size: {:?}",
        section.ls_type,
        section.regenerated_size,
        section.compressed_size
    );

    let upper_limit_for_literals = match section.compressed_size {
        Some(x) => x as usize,
        None => match section.ls_type {
            LiteralsSectionType::RLE => 1,
            LiteralsSectionType::Raw => section.regenerated_size as usize,
            _ => panic!("Bug in this library"),
        },
    };

    if raw.len() < upper_limit_for_literals {
        return Err(DecompressBlockError::MalformedSectionHeader {
            expected_len: upper_limit_for_literals,
            remaining_bytes: raw.len(),
        });
    }

    let raw_literals = &raw[..upper_limit_for_literals];
    vprintln!("Slice for literals: {}", raw_literals.len());

    workspace.literals_buffer.clear();
    let bytes_used_in_literals_section = decode_literals(
        &section,
        &mut workspace.huf,
        raw_literals,
        &mut workspace.literals_buffer,
    )?;
    assert_eq!(
        section.regenerated_size,
        workspace.literals_buffer.len() as u32,
        "Wrong number of literals: {}, Should have been: {}",
        workspace.literals_buffer.len(),
        section.regenerated_size
    );
    assert_eq!(
        bytes_used_in_literals_section,
        upper_limit_for_literals as u32
    );

    let raw = &raw[upper_limit_for_literals..];
    vprintln!("Slice for sequences with headers: {}", raw.len());

    let (seq_section, raw) = SequencesHeader::parse(raw)?;
    vprintln!(
        "Found sequencessection with sequences: {} and size: {}",
        seq_section.num_sequences,
        raw.len()
    );

    vprintln!("Slice for sequences: {}", raw.len());

    if seq_section.num_sequences != 0 {
        decode_sequences(
            &seq_section,
            raw,
            &mut workspace.fse,
            &mut workspace.sequences,
        )?;
        vprintln!("Executing sequences");
        execute_sequences(workspace)?;
    } else {
        if !raw.is_empty() {
            return Err(DecompressBlockError::DecodeSequenceError(
                DecodeSequenceError::ExtraBits {
                    bits_remaining: raw.len() as isize * 8,
                },
            ));
        }
        workspace.buffer.push(&workspace.literals_buffer);
        workspace.sequences.clear();
    }

    Ok(())
}
