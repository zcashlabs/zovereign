//! Management operations shared by the terminal and a future desktop interface.
//! Docker owns process restarts. This library never deletes database volumes.
mod error;
pub use error::{Error, Result};

pub mod config;
pub mod docker;
pub mod manager;
pub use config::{Config, StateStore};
pub use docker::Docker;
pub use manager::Manager;
