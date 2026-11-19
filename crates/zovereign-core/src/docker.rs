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
        // Inspect errors are text; keep diagnostics independent of the host locale.
        c.env("LC_ALL", "C").env("LANG", "C");
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
            if is_missing_object(kind, &err) {
                return Ok(None);
            }
            bail!("Docker inspection failed: {}", err.trim());
        }
        Ok(serde_json::from_slice::<Vec<Value>>(&out.stdout)?
            .into_iter()
            .next())
    }
}

fn is_missing_object(kind: &str, error: &str) -> bool {
    let message = error.trim();
    let message = message
        .strip_prefix("Error response from daemon: ")
        .or_else(|| message.strip_prefix("Error: "))
        .unwrap_or(message);
    // Context, SSH and socket failures can also contain "not found" or "No such".
    // Unknown diagnostic formats must remain errors instead of implying absence.
    [kind, "object"].into_iter().any(|object| {
        let prefix = format!("No such {object}: ");
        message.strip_prefix(prefix.as_str()).is_some_and(|name| {
            !name.trim().is_empty() && !name.contains('\n') && !name.contains('\r')
        })
    })
}

/// Own a small list of command arguments for passing directly to Docker.
pub fn s(args: &[&str]) -> Vec<String> {
    args.iter().map(|s| s.to_string()).collect()
}

#[cfg(test)]
mod tests {
    use super::is_missing_object;

    #[test]
    fn missing_objects_require_a_recognized_docker_diagnostic() {
        for (kind, error) in [
            ("container", "Error: No such container: zovereign"),
            (
                "image",
                "Error response from daemon: No such image: example:latest",
            ),
            ("image", "Error: No such object: sha256:missing"),
            ("container", "No such container: zovereign"),
        ] {
            assert!(is_missing_object(kind, error), "missing {kind}: {error}");
        }
        for error in [
            "Docker context not found",
            "Cannot connect: No such file or directory",
            "SSH transport failed: No such image: example:latest",
            "Error: No such image: ",
            "Error: No such container: zovereign",
            "Error: No such image: example:latest\nSSH connection failed",
            "Error: image not found",
            "Aucun objet de ce type",
        ] {
            assert!(
                !is_missing_object("image", error),
                "must retain error: {error}"
            );
        }
    }
}
