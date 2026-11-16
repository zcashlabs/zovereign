//! Docker command transport using a named context.
use crate::{error::bail, Result};
use anyhow::Context;
use serde_json::Value;
use std::process::{Command, Output, Stdio};

#[derive(Clone, Debug)]
/// Runs Docker commands against the selected engine without using a shell.
pub struct Docker {
    pub(crate) executable: std::path::PathBuf,
    pub(crate) context: Option<String>,
}
impl Docker {
    /// Choose a Docker context and executable without contacting the engine.
    pub fn new(context: Option<String>) -> Self {
        Self {
            executable: std::env::var_os("ZOVEREIGN_DOCKER")
                .map_or_else(|| "docker".into(), Into::into),
            context,
        }
    }
    /// Return the selected Docker context, or use the CLI default.
    pub fn context(&self) -> Option<&str> {
        self.context.as_deref()
    }
    pub(crate) fn command(&self, args: &[String]) -> Command {
        let mut c = Command::new(&self.executable);
        if let Some(ctx) = &self.context {
            c.args(["--context", ctx]);
        }
        c.args(args);
        c
    }
    /// Capture a Docker command result while leaving exit-status handling to the caller.
    ///
    /// # Errors
    ///
    /// Returns an error if the Docker executable cannot be started or its output cannot be collected.
    pub fn output(&self, args: &[String]) -> Result<Output> {
        Ok(self.command(args).stdin(Stdio::null()).output().with_context(|| {
            format!("Could not execute Docker CLI '{}'; ensure its executable is available on this process's PATH", self.executable.display())
        })?)
    }
    /// Run Docker and return trimmed UTF-8 stdout after a successful exit.
    ///
    /// # Errors
    ///
    /// Returns an error if execution fails, Docker exits unsuccessfully or stdout is not UTF-8.
    pub fn run(&self, args: &[String]) -> Result<String> {
        let out = self.output(args)?;
        if !out.status.success() {
            bail!(
                "Docker {} failed: {}",
                args.first().map(String::as_str).unwrap_or("command"),
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        Ok(String::from_utf8(out.stdout)?.trim().to_owned())
    }
    /// Stream Docker progress and logs to stderr without buffering an unbounded stream.
    ///
    /// # Errors
    ///
    /// Returns an error if the command cannot start or exits unsuccessfully.
    pub fn stream(&self, args: &[String]) -> Result<()> {
        let status = self
            .command(args)
            .stdin(Stdio::null())
            .stdout(Stdio::from(std::io::stderr()))
            .stderr(Stdio::inherit())
            .status()?;
        if !status.success() {
            bail!("Docker command failed with {status}");
        }
        Ok(())
    }
    /// Run Docker and decode a successful JSON response.
    ///
    /// # Errors
    ///
    /// Returns an error on command failure or malformed JSON.
    pub fn json(&self, args: &[String]) -> Result<Value> {
        Ok(serde_json::from_str(&self.run(args)?)?)
    }
    /// Inspect a Docker object, returning absence only when Docker reports it missing.
    ///
    /// # Errors
    ///
    /// Returns an error for command failures other than missing objects or invalid response data.
    pub fn inspect(&self, kind: &str, id: &str) -> Result<Option<Value>> {
        let out = self.output(&s(&[kind, "inspect", id]))?;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            if err.contains("No such") || err.contains("not found") {
                return Ok(None);
            }
            bail!("Docker inspection failed: {}", err.trim());
        }
        Ok(serde_json::from_slice::<Vec<Value>>(&out.stdout)?
            .into_iter()
            .next())
    }
}
/// Own a small list of command arguments for passing directly to Docker.
pub fn s(args: &[&str]) -> Vec<String> {
    args.iter().map(|s| s.to_string()).collect()
}
