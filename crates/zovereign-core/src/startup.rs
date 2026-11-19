//! OS service templates monitor status. Docker exclusively owns crash restarts.
use crate::{error::bail, Result};
use anyhow::Context;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

fn systemd_quote(s: &str) -> String {
    format!(
        "\"{}\"",
        s.replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('%', "%%")
    )
}

fn systemd_arg(s: &str) -> String {
    systemd_quote(&s.replace('$', "$$"))
}

fn xml(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn absolute_utf8_path(value: std::ffi::OsString, cwd: &Path, name: &str) -> Result<String> {
    let path = PathBuf::from(value);
    let path = if path.is_absolute() {
        path
    } else {
        cwd.join(path)
    };
    Ok(path
        .into_os_string()
        .into_string()
        .map_err(|_| anyhow::anyhow!("{name} must be UTF-8 for startup configuration"))?)
}

fn monitor_environment() -> Result<BTreeMap<String, String>> {
    let cwd = std::env::current_dir().context("Resolve installation working directory")?;
    let path = std::env::var_os("PATH")
        .context("Run startup install/render with PATH configured for Docker")?;
    if path.is_empty() {
        bail!("The monitor PATH must be nonempty");
    }
    // Relative PATH entries refer to the installer's directory, not the service manager's.
    let paths = std::env::split_paths(&path).map(|path| {
        if path.is_absolute() {
            path
        } else {
            cwd.join(path)
        }
    });
    let path = std::env::join_paths(paths)
        .context("Normalize monitor PATH")?
        .into_string()
        .map_err(|_| anyhow::anyhow!("Monitor PATH must be UTF-8"))?;
    let mut values = BTreeMap::from([("PATH".to_string(), path)]);
    let docker_config = std::env::var_os("DOCKER_CONFIG")
        .filter(|value| !value.is_empty())
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|home| PathBuf::from(home).join(".docker").into_os_string())
        });
    if let Some(value) = docker_config {
        values.insert(
            "DOCKER_CONFIG".into(),
            absolute_utf8_path(value, &cwd, "DOCKER_CONFIG")?,
        );
    }
    if let Some(value) = std::env::var_os("ZOVEREIGN_DOCKER") {
        if value.is_empty() {
            bail!("ZOVEREIGN_DOCKER must name a Docker executable");
        }
        let path = PathBuf::from(&value);
        let value = if path.components().count() > 1 {
            absolute_utf8_path(value, &cwd, "ZOVEREIGN_DOCKER")?
        } else {
            value
                .into_string()
                .map_err(|_| anyhow::anyhow!("ZOVEREIGN_DOCKER must be UTF-8"))?
        };
        values.insert("ZOVEREIGN_DOCKER".into(), value);
    }
    Ok(values)
}

/// Render a native monitor definition with the installer's Docker environment.
/// Linux/systemd and macOS/launchd retain PATH, Docker configuration and an explicit executable override.
///
/// # Errors
///
/// Returns an error for unsupported platforms, invalid paths or an unusable monitor environment.
pub fn render(platform: &str, exe: &Path, config: &Path) -> Result<String> {
    if !["linux", "macos"].contains(&platform) {
        bail!("Startup templates support linux/systemd and macos/launchd. Other init systems can run 'zovereign agent' with their service manager.");
    }
    render_with_environment(platform, exe, config, &monitor_environment()?)
}

fn render_with_environment(
    platform: &str,
    exe: &Path,
    config: &Path,
    environment: &BTreeMap<String, String>,
) -> Result<String> {
    let exe = exe
        .to_str()
        .context("Service executable path must be UTF-8")?;
    let config = config
        .to_str()
        .context("Service configuration path must be UTF-8")?;
    let invalid = |value: &str| value.chars().any(char::is_control);
    if invalid(exe) || invalid(config) || environment.values().any(|value| invalid(value)) {
        bail!("Startup paths and environment values must not contain control characters");
    }
    if environment.get("PATH").is_none_or(String::is_empty) {
        bail!("The monitor PATH must be nonempty");
    }
    match platform {
        "linux" => {
            // Environment= expands percent specifiers, but leaves dollar signs literal.
            let variables: String = environment
                .iter()
                .map(|(key, value)| {
                    format!("Environment={}\n", systemd_quote(&format!("{key}={value}")))
                })
                .collect();
            Ok(format!("[Unit]\nDescription=Zovereign status and update monitor\nAfter=network-online.target\n\n[Service]\nType=simple\n{variables}ExecStart={} --config-dir {} agent\nRestart=on-failure\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n", systemd_arg(exe), systemd_arg(config)))
        }
        "macos" => {
            let variables: String = environment
                .iter()
                .map(|(key, value)| {
                    format!("<key>{}</key><string>{}</string>", xml(key), xml(value))
                })
                .collect();
            Ok(format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict><key>Label</key><string>io.zovereign.monitor</string><key>ProgramArguments</key><array><string>{}</string><string>--config-dir</string><string>{}</string><string>agent</string></array><key>EnvironmentVariables</key><dict>{variables}</dict><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer></dict></plist>\n", xml(exe), xml(config)))
        }
        _ => bail!("Unsupported monitor platform"),
    }
}

fn service_file(
    platform: &str,
    scope: ServiceScope,
    home: Option<&Path>,
    config_home: Option<&Path>,
) -> Result<PathBuf> {
    match (platform, scope) {
        ("linux", ServiceScope::System) => Ok(PathBuf::from(
            "/etc/systemd/system/zovereign-monitor.service",
        )),
        ("linux", ServiceScope::User) => {
            let root = match config_home.filter(|path| path.is_absolute()) {
                Some(path) => path.to_path_buf(),
                None => home
                    .context("Set HOME or an absolute XDG_CONFIG_HOME for a user service")?
                    .join(".config"),
            };
            Ok(root.join("systemd/user/zovereign-monitor.service"))
        }
        ("macos", ServiceScope::User) => Ok(home
            .context("HOME is required for a macOS login agent")?
            .join("Library/LaunchAgents/io.zovereign.monitor.plist")),
        ("macos", ServiceScope::System) => {
            bail!("System services are supported on Linux; macOS uses a login agent")
        }
        _ => bail!("Use startup render or your VM's service manager to run 'zovereign agent'"),
    }
}

/// The operating-system service account that runs the status monitor.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ServiceScope {
    /// A login service owned by the current user.
    User,
    /// An administrator-managed Linux system service.
    System,
}

/// The requested change to the host monitor service.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ServiceAction {
    /// Write and enable the service configuration.
    Install,
    /// Disable and remove the recognized service configuration.
    Uninstall,
}

/// Install only the read-only monitor; container restarts belong to Docker.
/// System mode must be invoked by an administrator. No implicit sudo is used.
///
/// # Errors
///
/// Returns an error if the platform is unsupported, an existing service is
/// unrecognized, paths are invalid, or filesystem/service-manager operations fail.
pub fn configure(
    exe: &Path,
    config: &Path,
    scope: ServiceScope,
    action: ServiceAction,
) -> Result<String> {
    let system = scope == ServiceScope::System;
    let remove = action == ServiceAction::Uninstall;
    use std::{fs, process::Command};
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let config_home = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
    let platform = std::env::consts::OS;
    if system && platform != "linux" {
        bail!("System services are supported on Linux; macOS uses a login agent");
    }
    let file = service_file(platform, scope, home.as_deref(), config_home.as_deref())?;
    // Removal must work even if the original Docker installation/PATH is broken.
    let template = if remove {
        String::new()
    } else {
        let template = render(platform, exe, config)?;
        if system {
            template.replace("WantedBy=default.target", "WantedBy=multi-user.target")
        } else {
            template
        }
    };
    // Avoid overwriting an unrelated service with the same name.
    if file.exists() {
        let current = fs::read_to_string(&file)?;
        if !current.contains("Zovereign status and update monitor")
            && !current.contains("io.zovereign.monitor")
        {
            bail!(
                "Refusing to overwrite an unrecognized service at {}",
                file.display()
            );
        }
    }
    let checked = |program: &str, args: &[&str]| -> Result<()> {
        let out = Command::new(program).args(args).output().with_context(|| {
            format!("Could not run {program}; use startup render for your init system")
        })?;
        if !out.status.success() {
            bail!("{program}: {}", String::from_utf8_lossy(&out.stderr).trim());
        }
        Ok(())
    };
    if platform == "linux" {
        let mut prefix = Vec::new();
        if !system {
            prefix.push("--user");
        }
        if !remove {
            let mut probe = prefix.clone();
            probe.push("show-environment");
            checked("systemctl", &probe).context(
                "systemd monitor installation is unavailable; run 'zovereign agent' with this host's existing supervisor",
            )?;
        }
        if remove {
            let mut args = prefix.clone();
            args.extend(["disable", "--now", "zovereign-monitor.service"]);
            checked("systemctl", &args)?;
            if file.exists() {
                fs::remove_file(&file)?;
            }
        } else {
            fs::create_dir_all(file.parent().context("Service path has no parent")?)?;
            fs::write(&file, template)?;
        }
        let mut reload = prefix.clone();
        reload.push("daemon-reload");
        checked("systemctl", &reload)?;
        if !remove {
            let mut args = prefix;
            args.extend(["enable", "--now", "zovereign-monitor.service"]);
            checked("systemctl", &args)?;
        }
    } else {
        let uid = Command::new("id").arg("-u").output()?;
        if !uid.status.success() {
            bail!("Could not determine login user");
        }
        let domain = format!("gui/{}", String::from_utf8(uid.stdout)?.trim());
        let path = file.to_str().context("Service path is not UTF-8")?;
        if remove {
            checked("launchctl", &["bootout", &domain, path])?;
            fs::remove_file(&file)?;
        } else {
            if file.exists() {
                bail!("Monitor already installed; uninstall it before changing its configuration");
            }
            fs::create_dir_all(file.parent().context("Service path has no parent")?)?;
            fs::write(&file, template)?;
            checked("launchctl", &["bootstrap", &domain, path])?;
        }
    }
    Ok(file.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn service_paths_are_escaped_for_each_manager() {
        let p = Path::new("/opt/a b/zo%v$er\"eign");
        let s = render("linux", p, Path::new("/tmp/config dir")).unwrap();
        assert!(s.contains("zo%%v$$er\\\"eign"));
        let s = render("macos", Path::new("/opt/a&b/cli"), Path::new("/tmp/config")).unwrap();
        assert!(s.contains("/opt/a&amp;b/cli"));
        assert!(render("linux", Path::new("/bad\npath"), Path::new("/tmp")).is_err());
    }
    #[test]
    fn monitors_reject_an_unusable_search_path() {
        for path in ["", "/opt/tools/bin\n/usr/bin", "/opt/tools/bin\r/usr/bin"] {
            for platform in ["linux", "macos"] {
                assert!(
                    render_with_environment(
                        platform,
                        Path::new("/opt/zovereign"),
                        Path::new("/tmp/config"),
                        &BTreeMap::from([("PATH".into(), path.into())])
                    )
                    .is_err(),
                    "invalid {platform} monitor PATH {path:?} must be rejected before installation"
                );
            }
        }
    }
    #[test]
    fn service_directories_follow_platform_and_user_configuration() {
        let home = Path::new("/home/example");
        assert_eq!(
            service_file(
                "linux",
                ServiceScope::User,
                Some(home),
                Some(Path::new("/custom/config"))
            )
            .unwrap(),
            Path::new("/custom/config/systemd/user/zovereign-monitor.service")
        );
        assert_eq!(
            service_file(
                "linux",
                ServiceScope::User,
                Some(home),
                Some(Path::new("relative"))
            )
            .unwrap(),
            Path::new("/home/example/.config/systemd/user/zovereign-monitor.service")
        );
        assert_eq!(
            service_file("linux", ServiceScope::System, None, None).unwrap(),
            Path::new("/etc/systemd/system/zovereign-monitor.service")
        );
        assert_eq!(
            service_file("macos", ServiceScope::User, Some(home), None).unwrap(),
            Path::new("/home/example/Library/LaunchAgents/io.zovereign.monitor.plist")
        );
        assert!(service_file("macos", ServiceScope::System, Some(home), None).is_err());
        assert!(service_file("freebsd", ServiceScope::User, Some(home), None).is_err());
    }
}
