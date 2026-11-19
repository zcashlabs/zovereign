//! Persistent installation configuration and exclusive operation locking.
use crate::{error::bail, Result};
use anyhow::Context;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
#[non_exhaustive]
/// Durable identity and desired state for one installation.
pub struct Config {
    /// The version of the installation record format.
    pub schema_version: u32,
    /// The named Docker context selected for this installation.
    pub context: String,
    /// The engine identity checked before mutations.
    pub engine_id: String,
    /// The managed container name.
    pub container: String,
    /// The persistent named data volume.
    pub volume: String,
    /// The installed immutable image ID.
    pub image: String,
    /// Whether the user intends the runtime to remain running.
    pub desired_running: bool,
}
#[derive(Debug)]
/// Reads and durably writes installation records under one configuration directory.
pub struct StateStore {
    root: PathBuf,
}
impl StateStore {
    /// Return the directory containing installation state and operation records.
    pub fn root(&self) -> &Path {
        &self.root
    }
    /// Choose the state directory without creating it or reading files.
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }
    /// Resolve the state directory from explicit environment overrides or the user home.
    ///
    /// # Errors
    ///
    /// Returns an error if no override and no home directory are available.
    pub fn default_root() -> Result<PathBuf> {
        if let Some(p) = std::env::var_os("ZOVEREIGN_CONFIG_DIR") {
            return Ok(p.into());
        }
        if let Some(p) = std::env::var_os("XDG_CONFIG_HOME") {
            return Ok(PathBuf::from(p).join("zovereign"));
        }
        Ok(
            PathBuf::from(std::env::var_os("HOME").context("Set HOME or --config-dir")?)
                .join(".config/zovereign"),
        )
    }
    /// Read and validate the existing installation record, if present.
    ///
    /// # Errors
    ///
    /// Returns an error for unreadable files, malformed records or unsupported schema versions.
    pub fn load(&self) -> Result<Option<Config>> {
        let p = self.root.join("installation.json");
        let bytes = match fs::read(&p) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => {
                return Err(anyhow::Error::new(e)
                    .context(format!("reading {}", p.display()))
                    .into())
            }
        };
        let c: Config = serde_json::from_slice(&bytes).with_context(|| {
            format!(
                "parsing {}; preserve the file and inspect before continuing",
                p.display()
            )
        })?;
        if c.schema_version != 1 {
            bail!("Unsupported installation schema {}", c.schema_version);
        }
        valid_name(&c.container)?;
        valid_name(&c.volume)?;
        Ok(Some(c))
    }
    /// Acquire an exclusive operation lock that remains held until the returned file closes.
    ///
    /// # Errors
    ///
    /// Returns an error if the state directory is inaccessible or another operation holds the lock.
    pub fn lock(&self) -> Result<File> {
        fs::create_dir_all(&self.root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))?;
        }
        let f = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(self.root.join("operation.lock"))?;
        f.try_lock()
            .context("Another Zovereign operation is active")?;
        Ok(f)
    }
    /// Atomically replace and synchronize the installation record.
    ///
    /// # Errors
    ///
    /// Returns an error if serialization, replacement or synchronization fails.
    pub fn save(&self, config: &Config) -> Result<()> {
        atomic_json(&self.root.join("installation.json"), config)
    }
}
/// Atomically replace a JSON record and synchronize its containing directory.
///
/// # Errors
///
/// Returns an error if the path is invalid or serialization or filesystem operations fail.
pub fn atomic_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path.parent().context("State path has no parent")?;
    fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
    let tmp = path.with_extension("tmp");
    let mut opts = OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut f = opts
        .open(&tmp)
        .with_context(|| format!("opening {}", tmp.display()))?;
    serde_json::to_writer_pretty(&mut f, value)?;
    f.write_all(b"\n")?;
    f.sync_all()?;
    fs::rename(&tmp, path).with_context(|| format!("replacing {}", path.display()))?;
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub(crate) fn valid_name(name: &str) -> Result<()> {
    if !name
        .bytes()
        .next()
        .is_some_and(|first| first.is_ascii_alphanumeric())
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
    {
        bail!("Use a Docker name containing only letters, numbers, '.', '_' or '-'");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_names_return_errors_without_panicking() {
        for name in ["", "-option", "name/with/path", "name with spaces"] {
            assert!(
                valid_name(name).is_err(),
                "invalid Docker name {name:?} must be rejected"
            );
        }
        assert!(
            valid_name("zovereign-vm_1").is_ok(),
            "supported names must remain accepted"
        );
    }
}
