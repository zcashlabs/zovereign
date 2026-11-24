//! Replace this CLI with a newer signed release.
//!
//! The installer is compiled into the binary, so the signature and checksum checks
//! that verified this release also verify its replacement. No script is fetched at
//! update time.
use anyhow::{bail, Context, Result};
use serde_json::{json, Map, Value};
use std::{
    ffi::OsString,
    io,
    os::fd::AsFd,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

const REPO: &str = "https://github.com/zcashlabs/zovereign";
const INSTALLER: &str = include_str!("../../../scripts/install-cli.sh");
/// Set by the release workflow. Source builds have no release to compare against.
const RELEASE_VERSION: Option<&str> = option_env!("ZOVEREIGN_RELEASE_VERSION");

type Version = (u64, u64, u64);

fn parse(version: &str) -> Option<Version> {
    let digits = version.strip_prefix('v').unwrap_or(version);
    if !digits.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
        return None;
    }
    let mut parts = digits.split('.').map(|part| part.parse().ok());
    let version = (parts.next()??, parts.next()??, parts.next()??);
    parts.next().is_none().then_some(version)
}

/// The result of comparing this CLI with the latest published release.
#[derive(Debug)]
pub(crate) struct Check {
    current: Option<&'static str>,
    latest: Option<String>,
}

impl Check {
    /// The release tag to install, when it is newer than this CLI.
    pub(crate) fn available(&self) -> Option<&str> {
        let current = parse(self.current?)?;
        let latest = self.latest.as_deref()?;
        (parse(latest)? > current).then_some(latest)
    }

    /// Fields merged into the JSON result of an update command.
    pub(crate) fn fields(&self) -> Map<String, Value> {
        let update = match (self.current, &self.latest) {
            (None, _) => "unavailable for source builds; rerun the installer",
            (Some(_), None) => "no CLI release published",
            (Some(_), Some(_)) if self.available().is_some() => "available",
            (Some(_), Some(_)) => "up to date",
        };
        let mut fields = Map::new();
        fields.insert(
            "cli_version".into(),
            json!(self.current.unwrap_or(env!("CARGO_PKG_VERSION"))),
        );
        if let Some(latest) = &self.latest {
            fields.insert("cli_latest".into(), json!(latest));
        }
        fields.insert("cli_update".into(), json!(update));
        fields
    }
}

/// Compare this CLI with the latest release on GitHub.
///
/// # Errors
///
/// Returns an error if curl is missing, the release lookup fails, or GitHub
/// returns an unexpected release tag.
pub(crate) fn check() -> Result<Check> {
    let Some(current) = RELEASE_VERSION else {
        return Ok(Check {
            current: None,
            latest: None,
        });
    };
    Ok(Check {
        current: Some(current),
        latest: latest_release()?,
    })
}

fn latest_release() -> Result<Option<String>> {
    // The redirect target names the tag without using the rate-limited API.
    let output = Command::new("curl")
        .args([
            "--silent",
            "--show-error",
            "--head",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "5",
            "--max-time",
            "15",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code} %{url_effective}",
            &format!("{REPO}/releases/latest"),
        ])
        .output()
        .context("Install curl to check for CLI releases")?;
    if !output.status.success() {
        bail!(
            "Could not check for a newer CLI release: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let response = String::from_utf8(output.stdout).context("Unexpected release response")?;
    let (code, url) = response
        .split_once(' ')
        .context("Unexpected release response")?;
    match code {
        "404" => return Ok(None),
        "200" => {}
        _ => bail!("CLI release lookup returned HTTP {code}"),
    }
    let Some(tag) = url.strip_prefix(&format!("{REPO}/releases/tag/")) else {
        return Ok(None);
    };
    if !tag.starts_with('v') || parse(tag).is_none() {
        bail!("Latest release has an invalid tag: {tag}");
    }
    Ok(Some(tag.into()))
}

/// Verify and install `tag` over the running binary, returning its path.
///
/// # Errors
///
/// Returns an error if the running binary cannot be located or the installer
/// rejects the release. A failed installation leaves the current binary in place.
pub(crate) fn install(tag: &str) -> Result<PathBuf> {
    let exe = std::env::current_exe()
        .and_then(|path| path.canonicalize())
        .context("Locate the running CLI")?;
    if exe.file_name() != Some("zovereign".as_ref()) {
        bail!(
            "Self-update replaces a binary named zovereign, not {}. Rerun the installer instead.",
            exe.display()
        );
    }
    let dir = exe.parent().context("Locate the CLI directory")?;
    // Installer messages go to stderr so --json output stays parseable.
    let stderr = io::stderr().as_fd().try_clone_to_owned()?;
    let status = Command::new("/bin/sh")
        .arg("-c")
        .arg(INSTALLER)
        .arg("install-cli.sh")
        .arg(tag)
        .env("ZOVEREIGN_INSTALL_DIR", dir)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stderr))
        .status()
        .context("Run the CLI installer")?;
    if !status.success() {
        bail!("CLI update to {tag} failed; the current CLI is unchanged");
    }
    Ok(exe)
}

/// How the updated CLI reports its result.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Output {
    /// Human-readable output goes straight to the terminal.
    Text,
    /// One JSON object is captured so the caller can add CLI fields to it.
    Json,
}

/// Run the runtime update with the newly installed CLI, which may support newer
/// runtime versions than this process.
///
/// Returns the exit code and, for [`Output::Json`], the new CLI's parsed result.
///
/// # Errors
///
/// Returns an error if the new CLI cannot be started or its JSON output is invalid.
pub(crate) fn hand_off(
    exe: &Path,
    args: &[OsString],
    output: Output,
) -> Result<(i32, Option<Value>)> {
    let mut command = Command::new(exe);
    command.args(args);
    match output {
        Output::Text => {
            let status = command.status().context("Run the updated CLI")?;
            Ok((status.code().unwrap_or(1), None))
        }
        Output::Json => {
            let result = command
                .stderr(Stdio::inherit())
                .output()
                .context("Run the updated CLI")?;
            let value = serde_json::from_slice(&result.stdout)
                .context("The updated CLI returned invalid JSON")?;
            Ok((result.status.code().unwrap_or(1), Some(value)))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{parse, Check};

    #[test]
    fn versions_compare_numerically() {
        assert_eq!(parse("v1.10.0"), Some((1, 10, 0)));
        assert_eq!(parse("0.2.3"), Some((0, 2, 3)));
        for invalid in ["v1.2", "v1.2.3.4", "v1.2.x", "v+1.2.3", ""] {
            assert_eq!(parse(invalid), None, "{invalid}");
        }
        let check = |current, latest: &str| Check {
            current: Some(current),
            latest: Some(latest.into()),
        };
        assert_eq!(check("0.9.0", "v0.10.0").available(), Some("v0.10.0"));
        assert_eq!(check("0.10.0", "v0.10.0").available(), None);
        assert_eq!(check("0.11.0", "v0.10.0").available(), None);
    }

    #[test]
    fn source_builds_never_self_update() {
        let check = Check {
            current: None,
            latest: Some("v9.9.9".into()),
        };
        assert_eq!(check.available(), None);
        assert_eq!(
            check.fields()["cli_update"],
            "unavailable for source builds; rerun the installer"
        );
    }
}
