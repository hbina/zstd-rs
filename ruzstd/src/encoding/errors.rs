//! Error types for the encoding pipeline.

use super::CompressionLevel;

/// Errors that can occur during compression.
#[derive(Debug)]
#[non_exhaustive]
pub enum EncodeError {
    /// The requested compression level has not been implemented yet.
    UnsupportedCompressionLevel(CompressionLevel),
    /// An I/O error occurred writing to the drain.
    WriteFailed(crate::io::Error),
    /// An I/O error occurred reading from the source.
    ReadFailed(crate::io::Error),
    /// `compress()` was called without setting a source first.
    NoSource,
    /// `compress()` was called without setting a drain first.
    NoDrain,
}

impl core::fmt::Display for EncodeError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            EncodeError::UnsupportedCompressionLevel(level) => {
                write!(f, "Compression level {:?} is not yet implemented", level)
            }
            EncodeError::WriteFailed(e) => write!(f, "Write failed: {e}"),
            EncodeError::ReadFailed(e) => write!(f, "Read failed: {e}"),
            EncodeError::NoSource => write!(f, "No source was set before calling compress()"),
            EncodeError::NoDrain => write!(f, "No drain was set before calling compress()"),
        }
    }
}

#[cfg(feature = "std")]
impl std::error::Error for EncodeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            EncodeError::WriteFailed(e) => Some(e),
            EncodeError::ReadFailed(e) => Some(e),
            _ => None,
        }
    }
}
