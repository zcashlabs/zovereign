//! Terminal presentation and prompts for the shared Zovereign installation operations.
use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use serde_json::{json, Value};
use std::{
    io::{self, IsTerminal, Write},
    path::PathBuf,
    thread,
    time::Duration,
};
use zovereign_core::{
    config::atomic_json,
    manager::{epoch, ImageSource, LogMode, DEFAULT_IMAGE},
    startup, Manager, StateStore,
};

// Shared by Clap's explicit setup and the command-free first-run path.
const DEFAULT_CONTAINER: &str = "zovereign";
const DEFAULT_VOLUME: &str = "zovereign-data";
const DEFAULT_GRPC_PORT: u16 = 9067;
const DEFAULT_P2P_PORT: u16 = 8233;
const DEFAULT_LOCAL_IMAGE: bool = false;

#[derive(Parser)]
#[command(
    name = "zovereign",
    version = option_env!("ZOVEREIGN_RELEASE_VERSION").unwrap_or(env!("CARGO_PKG_VERSION")),
    about = "Install and manage your own persistent Zcash infrastructure"
)]
struct Cli {
    #[arg(long, global = true)]
    config_dir: Option<PathBuf>,
    #[arg(long, global = true)]
    context: Option<String>,
    /// Emit JSON on stdout; progress and streamed logs use stderr.
    #[arg(long, global = true)]
    json: bool,
    /// Skip interactive confirmation for nondestructive operations.
    #[arg(short = 'y', long, global = true)]
    yes: bool,
    #[arg(long,conflicts_with_all=["stats","update"])]
    health: bool,
    #[arg(long,conflicts_with_all=["health","update"])]
    stats: bool,
    #[arg(long,conflicts_with_all=["health","stats"])]
    update: bool,
    #[command(subcommand)]
    command: Option<Commands>,
}
#[derive(Subcommand)]
enum Commands {
    /// Install the existing combined runtime; downloads begin after checks.
    Setup {
        #[arg(long,default_value=DEFAULT_IMAGE)]
        image: String,
        /// Use a local/preloaded image without contacting its registry.
        #[arg(long, default_value_t = DEFAULT_LOCAL_IMAGE)]
        local_image: bool,
        #[arg(long, default_value = DEFAULT_CONTAINER)]
        container: String,
        #[arg(long, default_value = DEFAULT_VOLUME)]
        volume: String,
        #[arg(long, default_value_t = DEFAULT_GRPC_PORT)]
        grpc_port: u16,
        #[arg(long, default_value_t = DEFAULT_P2P_PORT)]
        p2p_port: u16,
    },
    /// Adopt a README/Compose deployment without changing its container.
    Adopt {
        #[arg(long, default_value = DEFAULT_CONTAINER)]
        container: String,
    },
    /// Check resources visible to the selected Docker engine.
    Check,
    Start,
    Stop,
    Status {
        #[arg(long)]
        watch: bool,
    },
    Health,
    Stats {
        #[arg(long)]
        watch: bool,
    },
    Logs {
        #[arg(short, long)]
        follow: bool,
        #[arg(long, default_value_t = 100)]
        tail: u32,
    },
    Connect,
    Doctor,
    /// Safe targeted recovery. Databases are never deleted by this command.
    Repair {
        #[arg(long,value_parser=["restart","rollback"],default_value="restart")]
        action: String,
    },
    /// Pull/check a candidate image, update, roll back, or accept the new runtime.
    Update {
        #[arg(long,default_value=DEFAULT_IMAGE)]
        image: String,
        /// Use an image already present on the selected Docker engine.
        #[arg(long, conflicts_with_all=["rollback", "accept"])]
        local_image: bool,
        #[arg(long,conflicts_with_all=["rollback","accept"])]
        check: bool,
        #[arg(long, conflicts_with = "accept")]
        rollback: bool,
        #[arg(long)]
        accept: bool,
    },
    /// Configure Docker restarts or print a host monitor service template.
    Startup {
        #[arg(value_parser=["enable","disable","render","install","uninstall"],default_value="render")]
        action: String,
        #[arg(long)]
        platform: Option<String>,
        /// Install a Linux system service instead of a user service (requires root).
        #[arg(long)]
        system: bool,
    },
    /// Persist periodic health observations and daily release notices.
    Agent {
        #[arg(long, default_value_t = 60)]
        interval: u64,
        #[arg(long)]
        once: bool,
        /// Disable network release checks for offline installations.
        #[arg(long)]
        no_update_check: bool,
    },
}
fn confirm(message: &str, cli: &Cli) -> Result<()> {
    if cli.yes {
        return Ok(());
    }
    if !io::stdin().is_terminal() {
        bail!("{message} Run interactively or pass --yes.");
    }
    eprint!("{message} [y/N] ");
    io::stderr().flush()?;
    let mut answer = String::new();
    io::stdin().read_line(&mut answer)?;
    if !["y", "yes"].contains(&answer.trim().to_lowercase().as_str()) {
        bail!("Cancelled");
    }
    Ok(())
}
fn gibibytes(bytes: u64) -> String {
    const GIB: u64 = 1_073_741_824;
    {
        let centis = (u128::from(bytes) * 100 + u128::from(GIB) / 2) / u128::from(GIB);
        format!("{}.{:02}", centis / 100, centis % 100)
    }
}
fn print_value(v: &Value, cli: &Cli) -> Result<()> {
    let mut output = io::stdout().lock();
    if cli.json {
        writeln!(output, "{v}")?;
        return Ok(());
    }
    if v.get("runtime").is_some() {
        let runtime = &v["runtime"];
        writeln!(
            output,
            "Zovereign: {} ({})",
            runtime["phase"].as_str().unwrap_or("unknown"),
            v["state"].as_str().unwrap_or("unknown")
        )?;
        if let Some(message) = runtime["message"].as_str() {
            writeln!(output, "{message}")?;
        }
        if let (Some(done), Some(total)) = (
            runtime["completed_bytes"].as_u64(),
            runtime["total_bytes"].as_u64(),
        ) {
            let tenths = (u128::from(done) * 1_000 + u128::from(total.max(1)) / 2)
                / u128::from(total.max(1));
            writeln!(
                output,
                "{}: {}.{}% — {} / {} GiB",
                runtime["service"].as_str().unwrap_or("download"),
                tenths / 10,
                tenths % 10,
                gibibytes(done),
                gibibytes(total)
            )?;
        }
        if let Some(height) = runtime["node"]["blocks"].as_u64() {
            writeln!(
                output,
                "Node height: {height}; estimated tip: {}",
                runtime["node"]["estimatedheight"]
            )?;
        }
        writeln!(
            output,
            "Health: {}; container: {}",
            v["health"].as_str().unwrap_or("unknown"),
            v["container"].as_str().unwrap_or("unknown")
        )?;
        return Ok(());
    }
    if let Some(object) = v.as_object() {
        for (key, value) in object {
            if !value.is_null() {
                if let Some(text) = value.as_str() {
                    writeln!(output, "{}: {text}", key.replace('_', " "))?;
                } else {
                    writeln!(output, "{}: {value}", key.replace('_', " "))?;
                }
            }
        }
    } else {
        writeln!(output, "{v}")?;
    }
    Ok(())
}

fn release_notice(m: &Manager) -> Result<()> {
    let path = m.store().root().join("release.json");
    if let Ok(b) = std::fs::read(path) {
        let v: Value = serde_json::from_slice(&b)?;
        if v["available"] == true {
            eprintln!("Zovereign release {} is available. Review it with 'zovereign update --check --image {}'.",v["tag"],v["image"]);
        }
    }
    Ok(())
}
fn check_release(m: &Manager) -> Result<()> {
    let p = m.store().root().join("release.json");
    if let Ok(b) = std::fs::read(&p) {
        if let Ok(v) = serde_json::from_slice::<Value>(&b) {
            if epoch().saturating_sub(v["checked_at"].as_u64().unwrap_or(0)) < 86400 {
                return Ok(());
            }
        }
    }
    let out = std::process::Command::new("curl")
        .args([
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "5",
            "--max-time",
            "15",
            "--max-filesize",
            "1048576",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "User-Agent: zovereign-cli",
            "https://api.github.com/repos/zcashlabs/zovereign/releases/latest",
        ])
        .output()
        .context("Install curl for background release checks")?;
    if !out.status.success() {
        bail!("Release check unavailable; services continue running");
    }
    let r: Value = serde_json::from_slice(&out.stdout)?;
    let tag = r["tag_name"].as_str().context("Release has no tag")?;
    if !tag.starts_with('v')
        || !tag
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".-".contains(&b))
    {
        bail!("Invalid release tag");
    }
    let c = m.config()?;
    let info = m
        .docker()
        .inspect("image", &c.image)?
        .context("Current image unavailable")?;
    let current = info["Config"]["Labels"]["org.opencontainers.image.revision"]
        .as_str()
        .unwrap_or("");
    let target = r["target_commitish"].as_str().unwrap_or("");
    let comparable =
        current.len() == 40 && target.len() == 40 && target.bytes().all(|b| b.is_ascii_hexdigit());
    Ok(atomic_json(
        &p,
        &json!({"checked_at":epoch(),"tag":tag,"image":format!("ghcr.io/zcashlabs/zovereign:{tag}"),"available":comparable && current!=target,"comparison_known":comparable,"url":r["html_url"]}),
    )?)
}
fn run(cli: &Cli) -> Result<i32> {
    if cli.command.is_some() && (cli.health || cli.stats || cli.update) {
        bail!("Use a command or a --health/--stats/--update alias, not both");
    }
    let root = cli
        .config_dir
        .clone()
        .map(Ok)
        .unwrap_or_else(StateStore::default_root)?;
    let m = Manager::new(root, cli.context.clone())?;
    let command = if cli.health {
        Commands::Health
    } else if cli.stats {
        Commands::Stats { watch: false }
    } else if cli.update {
        Commands::Update {
            image: DEFAULT_IMAGE.into(),
            local_image: false,
            check: false,
            rollback: false,
            accept: false,
        }
    } else {
        match &cli.command {
            Some(command) => return dispatch(cli, &m, command),
            None => {
                if m.store().load()?.is_some() {
                    Commands::Status { watch: false }
                } else {
                    Commands::Setup {
                        image: DEFAULT_IMAGE.into(),
                        local_image: DEFAULT_LOCAL_IMAGE,
                        container: DEFAULT_CONTAINER.into(),
                        volume: DEFAULT_VOLUME.into(),
                        grpc_port: DEFAULT_GRPC_PORT,
                        p2p_port: DEFAULT_P2P_PORT,
                    }
                }
            }
        }
    };
    dispatch(cli, &m, &command)
}
fn dispatch(cli: &Cli, m: &Manager, command: &Commands) -> Result<i32> {
    if !cli.json {
        release_notice(m).ok();
    }
    let v = match command {
        Commands::Check => m.preflight()?,
        Commands::Setup {
            image,
            local_image,
            container,
            volume,
            grpc_port,
            p2p_port,
        } => {
            if m.store().load()?.is_some() {
                bail!("Installation already configured; use start or status");
            }
            if m.docker().inspect("container", container)?.is_some() {
                confirm(
                    "An existing container was found. Adopt it with its data and settings?",
                    cli,
                )?;
                m.adopt(container)?
            } else {
                let checks = m.preflight()?;
                if !cli.json {
                    print_value(&checks, cli)?;
                }
                if checks["ready"] != true {
                    return Ok(1);
                }
                confirm("Install Zovereign? Initial setup downloads about 300 GB and needs a 1 TB SSD. Snapshot history depends on its publishers.",cli)?;
                m.setup(
                    image,
                    if *local_image {
                        ImageSource::Local
                    } else {
                        ImageSource::Registry
                    },
                    container,
                    volume,
                    *grpc_port,
                    *p2p_port,
                )?
            }
        }
        Commands::Adopt { container } => {
            if m.store().load()?.is_some() {
                bail!("An installation is already configured");
            }
            confirm(
                "Adopt this Zovereign installation without changing its runtime?",
                cli,
            )?;
            m.adopt(container)?
        }
        Commands::Start => m.start()?,
        Commands::Stop => m.stop()?,
        Commands::Status { watch } => loop {
            print_value(&m.status()?, cli)?;
            if !watch {
                return Ok(0);
            }
            thread::sleep(Duration::from_secs(5));
        },
        Commands::Health => {
            let v = m.health()?;
            print_value(&v, cli)?;
            return Ok(if v["ready"] == true { 0 } else { 1 });
        }
        Commands::Stats { watch } => loop {
            print_value(&m.stats()?, cli)?;
            if !watch {
                return Ok(0);
            }
            thread::sleep(Duration::from_secs(3));
        },
        Commands::Logs { follow, tail } => {
            m.logs(
                if *follow {
                    LogMode::Follow
                } else {
                    LogMode::Tail
                },
                *tail,
            )?;
            return Ok(0);
        }
        Commands::Connect => m.connect()?,
        Commands::Doctor => m.doctor()?,
        Commands::Repair { action } => {
            confirm(
                "Apply this recovery action? Databases and volumes will be retained.",
                cli,
            )?;
            if action == "rollback" {
                m.rollback()?
            } else {
                m.restart()?
            }
        }
        Commands::Update {
            image,
            local_image,
            check,
            rollback,
            accept,
        } => {
            if *check {
                m.updates(
                    image,
                    if *local_image {
                        ImageSource::Local
                    } else {
                        ImageSource::Registry
                    },
                )?
            } else {
                confirm("Change the runtime? Services may be briefly unavailable; databases will be retained.",cli)?;
                if *rollback {
                    m.rollback()?
                } else if *accept {
                    m.accept_update()?
                } else {
                    m.update(
                        image,
                        if *local_image {
                            ImageSource::Local
                        } else {
                            ImageSource::Registry
                        },
                    )?
                }
            }
        }
        Commands::Startup {
            action,
            platform,
            system,
        } => {
            if action == "install" || action == "uninstall" {
                m.config()?;
                confirm("Install/remove the host status monitor service?", cli)?;
                let root = m.store().root().canonicalize()?;
                let path = startup::configure(
                    &std::env::current_exe()?,
                    &root,
                    if *system {
                        startup::ServiceScope::System
                    } else {
                        startup::ServiceScope::User
                    },
                    if action == "uninstall" {
                        startup::ServiceAction::Uninstall
                    } else {
                        startup::ServiceAction::Install
                    },
                )?;
                json!({"service_file":path,"action":action,"note":"Docker must start independently at boot/login. A Linux user service requires login or lingering."})
            } else if action == "render" {
                let platform = platform
                    .clone()
                    .unwrap_or_else(|| std::env::consts::OS.into());
                let root = m
                    .store()
                    .root()
                    .canonicalize()
                    .context("Run setup/adopt before rendering startup configuration")?;
                let template = startup::render(&platform, &std::env::current_exe()?, &root)?;
                if cli.json {
                    json!({"platform":platform,"template":template})
                } else {
                    io::stdout().write_all(template.as_bytes())?;
                    return Ok(0);
                }
            } else {
                confirm("Change this container's Docker restart policy?", cli)?;
                // No shell/systemd assumption: works with rootless Docker and any init.
                let _lock = m.store().lock()?;
                let c = m.config()?;
                m.status()?;
                let policy = if action == "enable" {
                    "unless-stopped"
                } else {
                    "no"
                };
                m.docker().run(&zovereign_core::docker::s(&[
                    "update",
                    "--restart",
                    policy,
                    &c.container,
                ]))?;
                json!({"restart_policy":policy,"host_startup":"Ensure Docker itself starts at boot/login. Use startup render for the optional monitor. Linux user services require login or lingering."})
            }
        }
        Commands::Agent {
            interval,
            once,
            no_update_check,
        } => loop {
            m.record_observation()?;
            if !no_update_check {
                if let Err(e) = check_release(m) {
                    eprintln!("{e}");
                }
            }
            if *once {
                return Ok(0);
            }
            thread::sleep(Duration::from_secs((*interval).max(10)));
        },
    };
    let failed = matches!(command, Commands::Check) && v["ready"] != true;
    print_value(&v, cli)?;
    Ok(if failed { 1 } else { 0 })
}
fn main() {
    let cli = Cli::parse();
    match run(&cli) {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            if e.downcast_ref::<io::Error>()
                .is_some_and(|error| error.kind() == io::ErrorKind::BrokenPipe)
            {
                return;
            }
            if cli.json {
                println!("{}", json!({"error":format!("{e:#}")}));
            } else {
                eprintln!("Error: {e:#}");
            }
            std::process::exit(1);
        }
    }
}
