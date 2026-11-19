//! Errors returned by shared installation operations.
use std::{error, fmt, io, string::FromUtf8Error};

/// An installation failure that retains the context and original error chain.
#[derive(Debug)]
pub struct Error {
    source: anyhow::Error,
}
impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.source, formatter)
    }
}
impl error::Error for Error {
    fn source(&self) -> Option<&(dyn error::Error + 'static)> {
        let source: &(dyn error::Error + Send + Sync + 'static) = self.source.as_ref();
        source.source()
    }
}
impl From<anyhow::Error> for Error {
    fn from(source: anyhow::Error) -> Self {
        Self { source }
    }
}
impl From<io::Error> for Error {
    fn from(source: io::Error) -> Self {
        Self {
            source: source.into(),
        }
    }
}
impl From<serde_json::Error> for Error {
    fn from(source: serde_json::Error) -> Self {
        Self {
            source: source.into(),
        }
    }
}
impl From<FromUtf8Error> for Error {
    fn from(source: FromUtf8Error) -> Self {
        Self {
            source: source.into(),
        }
    }
}

/// The result of a shared installation operation.
pub type Result<T> = std::result::Result<T, Error>;

// Keep contextual error construction internal to the implementation.
macro_rules! bail {
    ($($arg:tt)*) => { return Err($crate::Error::from(anyhow::anyhow!($($arg)*))) };
}
pub(crate) use bail;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn contextual_errors_keep_the_original_cause() {
        use std::error::Error as _;
        let source = io::Error::new(io::ErrorKind::PermissionDenied, "permission denied");
        let failure = Error::from(
            anyhow::Error::new(source).context("reading /srv/zovereign/installation.json"),
        );
        assert!(
            failure
                .to_string()
                .contains("/srv/zovereign/installation.json"),
            "diagnostics must identify the failing file"
        );
        let cause = failure
            .source()
            .and_then(|source| source.downcast_ref::<io::Error>());
        assert_eq!(
            cause.map(io::Error::kind),
            Some(io::ErrorKind::PermissionDenied),
            "callers must retain the original I/O cause"
        );
    }
    #[test]
    fn shared_errors_are_safe_to_transfer_between_threads() {
        fn assert_error<T: std::error::Error + Send + Sync + 'static>() {}
        assert_error::<Error>();
    }
}
