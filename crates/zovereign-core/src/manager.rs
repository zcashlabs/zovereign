//! Installation lifecycle operations that retain the existing data volume.
use crate::{
    config::{valid_name, Config, Pending, Previous, StateStore, UpdateStage},
    docker::{s, Docker},
};
use crate::{error::bail, Result};
use anyhow::Context;
use serde_json::{json, Value};
use std::time::{SystemTime, UNIX_EPOCH};

/// The published image selected by setup unless the caller chooses another.
pub const DEFAULT_IMAGE: &str = "ghcr.io/zcashlabs/zovereign:latest";
// Keep this approved runtime contract aligned with the root Dockerfile.
const SUPPORTED_ZAKURA_VERSION: &str = "1.8.7";
const SUPPORTED_DATABASE_MAJOR: &str = "30";
const SUPPORTED_LIGHTWALLETD_VERSION: &str = "v0.6.0";
const STATUS_SCRIPT: &str = "import json,pathlib,sys; sys.path.insert(0,'/opt/zovereign'); import stack; print(json.dumps(stack.status(pathlib.Path('/data'))) if hasattr(stack,'status') else json.dumps({'phase':'unknown','message':'This image predates structured status; use logs.'}))";
const CAPACITY_SCRIPT: &str = "import json,shutil,pathlib,platform; d=shutil.disk_usage('/data'); c=pathlib.Path('/proc/cpuinfo').read_text(); print(json.dumps({'free_bytes':d.free,'total_bytes':d.total,'cpu_compatible':platform.machine() not in ('x86_64','amd64') or all(any(x==f for x in c.split()) for f in ('bmi2','adx'))}))";
#[derive(Debug)]
/// Coordinates one installation while retaining its data and recovery state.
pub struct Manager {
    docker: Docker,
    store: StateStore,
}
/// How to read the container's log stream.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum LogMode {
    /// Print the current tail and return.
    Tail,
    /// Continue streaming as new records arrive.
    Follow,
}

/// Where a requested runtime image must be obtained.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum ImageSource {
    /// Pull the requested reference from its registry before inspecting it.
    Registry,
    /// Inspect an image already present on the selected Docker engine.
    Local,
}

impl Manager {
    /// Return the immutable Docker transport for this installation.
    pub fn docker(&self) -> &Docker {
        &self.docker
    }
    /// Return the installation's state store.
    pub fn store(&self) -> &StateStore {
        &self.store
    }

    /// Open installation management with a pinned context and an existing state directory.
    ///
    /// # Errors
    ///
    /// Returns an error if stored configuration is invalid or an explicit context conflicts with it.
    pub fn new(root: std::path::PathBuf, context: Option<String>) -> Result<Self> {
        let store = StateStore::new(root);
        let config = store.load()?;
        if let (Some(c), Some(ctx)) = (&config, &context) {
            if c.context != *ctx {
                bail!("Installation belongs to Docker context '{}'; use a different --config-dir for another host",c.context);
            }
        }
        Ok(Self {
            docker: Docker::new(context.or_else(|| config.map(|c| c.context))),
            store,
        })
    }
    /// Load the managed installation identity and its desired state.
    ///
    /// # Errors
    ///
    /// Returns an error if no installation exists or its record cannot be read.
    pub fn config(&self) -> Result<Config> {
        Ok(self
            .store
            .load()?
            .context("No managed installation. Run 'zovereign setup' or 'zovereign adopt'.")?)
    }
    /// Read resource and identity information from the selected Docker engine.
    ///
    /// # Errors
    ///
    /// Returns an error when Docker is unavailable or its response is malformed.
    pub fn engine(&self) -> Result<Value> {
        self.docker.json(&s(&["info", "--format", "{{json .}}"]))
    }
    fn check_engine(&self, c: &Config) -> Result<()> {
        if self.engine()?["ID"].as_str() != Some(c.engine_id.as_str()) {
            bail!("Docker engine identity changed. Refusing to change a different machine's installation.");
        }
        Ok(())
    }
    /// Check the operating system, architecture and resources reported by Docker.
    ///
    /// # Errors
    ///
    /// Returns an error if the engine cannot be inspected; failed capacity checks appear in the report.
    pub fn preflight(&self) -> Result<Value> {
        let info = self.engine()?;
        let mut issues = Vec::new();
        let mut warnings = Vec::new();
        if info["OSType"] != "linux" {
            issues.push("Docker must run Linux containers".to_string());
        }
        let arch = info["Architecture"].as_str().unwrap_or("unknown");
        if !["x86_64", "amd64", "aarch64", "arm64"].contains(&arch) {
            issues.push(format!("Unsupported Docker CPU architecture: {arch}"));
        }
        if info["NCPU"].as_u64().unwrap_or(0) < 4 {
            issues.push("Allocate at least 4 CPUs to Docker".into());
        }
        // A nominal 16 GiB VM reports less usable RAM after kernel reservations.
        if info["MemTotal"].as_u64().unwrap_or(0) < 15 * 1024_u64.pow(3) {
            issues.push("Allocate 16 GiB RAM to Docker (at least 15 GiB usable)".into());
        }
        if info["Driver"] == "vfs" {
            warnings
                .push("The vfs storage driver can use substantial extra disk space".to_string());
        }
        Ok(
            json!({"ready":issues.is_empty(),"engine_id":info["ID"],"os":info["OSType"],"architecture":arch,"cpus":info["NCPU"],"memory_bytes":info["MemTotal"],"issues":issues,"warnings":warnings,"storage":"Checked on the data volume during setup; plan for a 1 TB SSD."}),
        )
    }
    /// Create and start the runtime after validating resources on the Docker host.
    /// Registry mode refreshes the reference; local mode requires a cached image.
    /// Both modes validate the supported versions and create by immutable image ID.
    ///
    /// # Errors
    ///
    /// Returns an error for failed checks, conflicting installations or Docker and state-write failures. Existing volumes are retained.
    pub fn setup(
        &self,
        image: &str,
        source: ImageSource,
        container: &str,
        volume: &str,
        grpc_port: u16,
        p2p_port: u16,
    ) -> Result<Value> {
        let _lock = self.store.lock()?;
        valid_name(container)?;
        valid_name(volume)?;
        if grpc_port == 0 || p2p_port == 0 || grpc_port == p2p_port {
            bail!("Use two distinct, nonzero ports");
        }
        if self.store.load()?.is_some() {
            bail!("Installation already configured; use start, status, or update");
        }
        if std::env::var_os("DOCKER_HOST").is_some() && self.docker.context.is_none() {
            bail!("Create/select a named Docker context or pass --context when using DOCKER_HOST");
        }
        let report = self.preflight()?;
        if report["ready"] != true {
            bail!("Machine checks failed: {}", report["issues"]);
        }
        let context = match &self.docker.context {
            Some(c) => c.clone(),
            None => self.docker.run(&s(&["context", "show"]))?,
        };
        let d = Docker {
            context: Some(context.clone()),
            ..self.docker.clone()
        };
        if d.inspect("container", container)?.is_some() {
            bail!("Container {container} exists. Use 'zovereign adopt --container {container}' to preserve its settings.");
        }

        if source == ImageSource::Registry {
            d.stream(&s(&["pull", image]))?;
        }
        let image_info = d
            .inspect("image", image)?
            .context("Setup image unavailable on the selected Docker engine")?;
        validate_supported_runtime(&image_info)?;
        let image_id = text(&image_info["Id"])?;
        d.run(&s(&["volume", "create", volume]))?;

        let capacity = d.json(&s(&[
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            &format!("type=volume,src={volume},dst=/data,readonly"),
            "--entrypoint",
            "python3",
            &image_id,
            "-c",
            CAPACITY_SCRIPT,
        ]))?;
        if capacity["cpu_compatible"] != true {
            bail!("The Docker VM's x86 CPU needs BMI2 and ADX; choose a supported CPU or portable runtime build");
        }
        // A conservative initial gate, not an exact estimate of future chain growth.
        if capacity["free_bytes"].as_u64().unwrap_or(0) < 700 * 1024_u64.pow(3) {
            bail!("Setup needs at least 700 GiB free on the data volume; use a 1 TB SSD. Existing volume data was preserved.");
        }
        let c = Config {
            schema_version: 1,
            context,
            engine_id: text(&report["engine_id"])?,
            container: container.into(),
            volume: volume.into(),
            image: image_id.clone(),
            desired_running: false,
            previous: None,
            pending: None,
        };
        let mut args = s(&[
            "create",
            "--name",
            container,
            "--label",
            "io.zovereign.managed=1",
            "--restart",
            "unless-stopped",
            "--stop-timeout",
            "120",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=3",
            "--mount",
            &format!("type=volume,src={volume},dst=/data"),
            "-p",
            &format!("{p2p_port}:8233"),
            "-p",
            &format!("127.0.0.1:{grpc_port}:9067"),
        ]);
        args.push(image_id);
        d.run(&args)?;
        // Persist before start: an interrupted start is resumable through `start`.
        self.store.save(&c)?;
        d.run(&s(&["start", container]))?;
        self.store.save(&Config {
            desired_running: true,
            ..c
        })?;
        Ok(
            json!({"installed":true,"capacity":capacity,"next":"Run 'zovereign status --watch' to follow snapshot restoration and synchronization."}),
        )
    }
    /// Record an existing combined runtime without changing its settings or data.
    ///
    /// # Errors
    ///
    /// Returns an error if the container identity, writable data volume or stored configuration is unsuitable.
    pub fn adopt(&self, name: &str) -> Result<Value> {
        let _lock = self.store.lock()?;
        if self.store.load()?.is_some() {
            bail!("An installation is already configured");
        }
        valid_name(name)?;
        let i = self
            .docker
            .inspect("container", name)?
            .context("Container does not exist")?;
        let volume = validate_installation(&i)?;
        let info = self.engine()?;
        let context = match &self.docker.context {
            Some(c) => c.clone(),
            None => self.docker.run(&s(&["context", "show"]))?,
        };
        if std::env::var_os("DOCKER_HOST").is_some() && self.docker.context.is_none() {
            bail!("Pass a named --context to adopt a DOCKER_HOST deployment");
        }
        let c = Config {
            schema_version: 1,
            context,
            engine_id: text(&info["ID"])?,
            container: name.into(),
            volume,
            image: text(&i["Image"])?,
            desired_running: i["State"]["Running"] == true,
            previous: None,
            pending: None,
        };
        self.store.save(&c)?;
        Ok(json!({"adopted":name,"data_preserved":true,"configuration_preserved":true}))
    }
    fn installation(&self, c: &Config) -> Result<Value> {
        self.check_engine(c)?;
        let i = self
            .docker
            .inspect("container", &c.container)?
            .context("Managed container is missing; run doctor")?;
        if validate_installation(&i)? != c.volume {
            bail!("Container volume differs from the managed installation");
        }
        Ok(i)
    }
    /// Observe process state, health and structured bootstrap progress without starting services.
    ///
    /// # Errors
    ///
    /// Returns an error if installation identity cannot be verified; unavailable runtime progress is represented in the report.
    pub fn status(&self) -> Result<Value> {
        let c = self.config()?;
        let i = self.installation(&c)?;
        let running = i["State"]["Running"] == true;
        let runtime = if running {
            self.docker
                .json(&s(&["exec", &c.container, "python3", "-c", STATUS_SCRIPT]))
                .unwrap_or_else(|e| json!({"phase":"unknown","error":e.to_string()}))
        } else {
            json!({"phase":"stopped"})
        };
        Ok(
            json!({"schema_version":1,"context":c.context,"container":c.container,"image":i["Image"],"state":i["State"]["Status"],"health":i["State"]["Health"]["Status"],"desired_running":c.desired_running,"restart_count":i["RestartCount"],"runtime":runtime,"pending_operation":c.pending}),
        )
    }
    /// Check the actual compact-block serving path of a running installation.
    ///
    /// # Errors
    ///
    /// Returns an error for transport or identity failures; a failing readiness probe returns a report with ready set to false.
    pub fn health(&self) -> Result<Value> {
        let c = self.config()?;
        let i = self.installation(&c)?;
        if i["State"]["Running"] != true {
            return Ok(json!({"ready":false,"reason":"Container is stopped"}));
        }
        let out = self.docker.output(&s(&[
            "exec",
            &c.container,
            "python3",
            "/opt/zovereign/stack.py",
            "health",
        ]))?;
        Ok(
            json!({"ready":out.status.success(),"message":String::from_utf8_lossy(&out.stdout).trim(),"error":String::from_utf8_lossy(&out.stderr).trim()}),
        )
    }
    /// Start the existing container and record the intention to keep it running.
    ///
    /// # Errors
    ///
    /// Returns an error if an update is pending or Docker or state operations fail.
    pub fn start(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        self.installation(&c)?;
        if c.pending.is_some() {
            bail!("An update was interrupted. Run doctor and repair --action rollback first.");
        }
        self.docker.run(&s(&["start", &c.container]))?;
        c.desired_running = true;
        self.store.save(&c)?;
        Ok(json!({"started":c.container,"data_preserved":true}))
    }
    /// Record an intentional stop before gracefully shutting down the runtime.
    ///
    /// # Errors
    ///
    /// Returns an error if identity checks, state writes or shutdown fail. Data volumes are retained.
    pub fn stop(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        self.installation(&c)?;
        // Record intention before stopping so the monitor cannot undo it.
        c.desired_running = false;
        self.store.save(&c)?;
        self.docker.run(&s(&["stop", "-t", "120", &c.container]))?;
        Ok(json!({"stopped":c.container,"data_preserved":true}))
    }
    /// Read one CPU and memory observation from the managed Docker container.
    ///
    /// # Errors
    ///
    /// Returns an error if identity checks, statistics collection or response decoding fail.
    pub fn stats(&self) -> Result<Value> {
        let c = self.config()?;
        self.installation(&c)?;
        self.docker.json(&s(&[
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            &c.container,
        ]))
    }
    /// Print the current log tail or follow subsequent records according to the requested mode.
    ///
    /// # Errors
    ///
    /// Returns an error if identity checks or Docker log streaming fail.
    pub fn logs(&self, mode: LogMode, tail: u32) -> Result<()> {
        let c = self.config()?;
        self.installation(&c)?;
        let mut args = s(&["logs", "--tail", &tail.to_string()]);
        if mode == LogMode::Follow {
            args.push("--follow".into());
        }
        args.push(c.container);
        // Stream without buffering potentially unbounded logs. Human logs use stderr.
        self.docker.stream(&args)
    }
    /// Describe wallet bindings on the Docker host and the required transport.
    ///
    /// # Errors
    ///
    /// Returns an error if the managed container cannot be verified.
    pub fn connect(&self) -> Result<Value> {
        let c = self.config()?;
        let i = self.installation(&c)?;
        let e = environment(&i);
        Ok(
            json!({"bindings":i["HostConfig"]["PortBindings"]["9067/tcp"],"tls":e.contains_key("TLS_CERT"),"docker_context":c.context,"instructions":"Bindings are on the Docker host. For localhost access use a wallet supporting custom servers. For another device use a trusted tunnel or TLS; never expose plaintext gRPC publicly."}),
        )
    }
    /// Collect actionable observations without modifying services or databases.
    ///
    /// # Errors
    ///
    /// Returns an error if installation identity or required Docker observations cannot be read.
    pub fn doctor(&self) -> Result<Value> {
        let c = self.config()?;
        self.check_engine(&c)?;
        let mut findings = Vec::new();
        if c.pending.is_some() {
            findings.push("Interrupted update: use repair --action rollback to restore the retained container".to_string());
        }
        if let Some(i) = self.docker.inspect("container", &c.container)? {
            validate_installation(&i)?;
            if i["State"]["OOMKilled"] == true {
                findings.push("Out of memory: increase Docker/VM memory".into());
            }
            if !i["State"]["Error"].as_str().unwrap_or("").is_empty() {
                findings.push(i["State"]["Error"].to_string());
            }
            if i["State"]["Health"]["Status"] == "unhealthy" {
                findings.push(
                    "Health checks failed: inspect logs and runtime progress before restarting"
                        .into(),
                );
            }
            if i["State"]["Running"] == true {
                let disk = self.docker.json(&s(&[
                    "exec",
                    &c.container,
                    "python3",
                    "-c",
                    CAPACITY_SCRIPT,
                ]))?;
                if disk["free_bytes"].as_u64().unwrap_or(0) < 20 * 1024_u64.pow(3) {
                    findings.push("Less than 20 GiB free: expand the data disk".into());
                }
                let logs = self
                    .docker
                    .output(&s(&["logs", "--tail", "200", &c.container]))?;
                if !logs.status.success() {
                    bail!(
                        "Docker logs failed: {}",
                        String::from_utf8_lossy(&logs.stderr).trim()
                    );
                }
                if String::from_utf8_lossy(&logs.stdout).contains("CORRUPTION")
                    || String::from_utf8_lossy(&logs.stderr).contains("CORRUPTION")
                {
                    findings.push("lightwalletd reported cache corruption; observe its native cache recovery before rebuilding".into());
                }
            }
        } else {
            findings.push("Managed container is missing; retained update container may be recoverable with repair --action rollback".into());
        }
        Ok(
            json!({"findings":findings,"pending":c.pending,"previous":c.previous,"data_preserved":true}),
        )
    }
    /// Restart a runtime that is intended to run and has no pending update.
    ///
    /// # Errors
    ///
    /// Returns an error for an intentional stop, pending update, identity failure or unsuccessful restart.
    pub fn restart(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let c = self.config()?;
        self.installation(&c)?;
        if !c.desired_running || c.pending.is_some() {
            bail!("Installation is intentionally stopped or has a pending update; use start or rollback explicitly");
        }
        self.docker
            .run(&s(&["restart", "-t", "120", &c.container]))?;
        Ok(json!({"restarted":c.container,"data_preserved":true}))
    }
    /// Pull a candidate image and compare it with the running image contract.
    ///
    /// # Errors
    ///
    /// Returns an error if image retrieval, identity verification or metadata validation fails.
    pub fn updates(&self, image: &str, source: ImageSource) -> Result<Value> {
        let c = self.config()?;
        let i = self.installation(&c)?;
        if source == ImageSource::Registry {
            self.docker.stream(&s(&["pull", image]))?;
        }
        let target = self
            .docker
            .inspect("image", image)?
            .context("Update image missing")?;
        let old = self
            .docker
            .inspect("image", text(&i["Image"])?.as_str())?
            .context("Current image missing")?;
        let old_contract = compatibility(&old)?;
        let new_contract = compatibility(&target)?;
        Ok(
            json!({"available":i["Image"]!=target["Id"],"current_image":i["Image"],"target_image":target["Id"],"compatible":old_contract==new_contract,"current_contract":old_contract,"target_contract":new_contract,"image":image}),
        )
    }
    /// Replace a compatible runtime while retaining its old container and durable recovery journal.
    ///
    /// # Errors
    ///
    /// Returns an error if compatibility or configuration cannot be preserved, or a Docker or journal operation fails. An interrupted transition remains recoverable.
    pub fn update(&self, image: &str, source: ImageSource) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        let i = self.installation(&c)?;
        if c.pending.is_some() || c.previous.is_some() {
            bail!("Resolve the previous update first: rollback, or 'update --accept' after verifying health");
        }
        if source == ImageSource::Registry {
            self.docker.stream(&s(&["pull", image]))?;
        }
        let target = self
            .docker
            .inspect("image", image)?
            .context("Target image not available")?;
        let old_image = self
            .docker
            .inspect("image", text(&i["Image"])?.as_str())?
            .context("Current image missing")?;
        let contract = compatibility(&old_image)?;
        if contract != compatibility(&target)? {
            bail!("Bundled versions/database contract differ. A reviewed migration is required; running installation was not stopped.");
        }
        if target["Id"] == i["Image"] {
            return Ok(json!({"updated":false,"reason":"Already using this image"}));
        }
        let args = recreate_args(&i, &old_image, text(&target["Id"])?.as_str(), &c.container)?;
        let old_id = text(&i["Id"])?;
        let was_running = i["State"]["Running"] == true;
        let policy = text(&i["HostConfig"]["RestartPolicy"]["Name"])?;
        c.previous = Some(Previous {
            container_id: old_id.clone(),
            image_id: text(&i["Image"])?,
            compatibility: contract,
            restart_policy: policy,
        });
        let mut pending = Pending {
            stage: UpdateStage::Prepared,
            old_id: old_id.clone(),
            new_image: text(&target["Id"])?,
            was_running,
        };
        c.pending = Some(pending.clone());
        self.store.save(&c)?;
        // Stop and disable restart of the retained container before a replacement
        // can open the volume. The journal survives a crash at any later step.
        self.docker.run(&s(&["stop", "-t", "120", &old_id]))?;
        self.docker
            .run(&s(&["update", "--restart", "no", &old_id]))?;
        self.docker.run(&s(&[
            "rename",
            &old_id,
            &format!("{}-previous-{}", c.container, epoch()),
        ]))?;
        pending.stage = UpdateStage::OldRetained;
        c.pending = Some(pending.clone());
        self.store.save(&c)?;
        self.docker.run(&args)?;
        pending.stage = UpdateStage::ReplacementCreated;
        c.pending = Some(pending);
        self.store.save(&c)?;
        if was_running {
            self.docker.run(&s(&["start", &c.container]))?;
        }
        c.image = text(&target["Id"])?;
        c.pending = None;
        self.store.save(&c)?;
        Ok(
            json!({"updated":true,"readiness_pending":was_running,"previous_retained":true,"next":"Check health, then 'zovereign update --accept'; use rollback if needed."}),
        )
    }
    /// Accept a verified replacement and remove only its retained stopped predecessor.
    ///
    /// # Errors
    ///
    /// Returns an error if an update is pending, the running replacement is unready or container/state operations fail.
    pub fn accept_update(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        self.installation(&c)?;
        if c.pending.is_some() {
            bail!("Resolve interrupted update before acceptance");
        }
        let p = c.previous.clone().context("No previous update")?;
        if c.desired_running && self.health()?["ready"] != true {
            bail!("New runtime is not ready; previous container retained");
        }
        if let Some(old) = self.docker.inspect("container", &p.container_id)? {
            if old["State"]["Running"] == true {
                bail!("Previous container is unexpectedly running");
            }
            self.docker.run(&s(&["rm", &p.container_id]))?;
        }
        c.previous = None;
        self.store.save(&c)?;
        Ok(json!({"accepted":true,"volume_preserved":true}))
    }
    /// Restore the retained compatible runtime while keeping a failed replacement for diagnosis.
    ///
    /// # Errors
    ///
    /// Returns an error for missing recovery state, incompatible data contracts or Docker/state failures.
    pub fn rollback(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        self.check_engine(&c)?;
        let p = c
            .previous
            .clone()
            .context("No retained container available")?;
        let old = self
            .docker
            .inspect("container", &p.container_id)?
            .context("Retained container missing")?;
        if validate_installation(&old)? != c.volume {
            bail!("Retained container has a different volume");
        }
        let mut failed = None;
        if let Some(current) = self.docker.inspect("container", &c.container)? {
            if current["Id"] != old["Id"] {
                if validate_installation(&current)? != c.volume {
                    bail!("Current container has a different volume; rollback refused before shutdown");
                }
                let image = self
                    .docker
                    .inspect("image", text(&current["Image"])?.as_str())?
                    .context("Current image missing")?;
                if compatibility(&image)? != p.compatibility {
                    bail!("Database compatibility is unproven; automatic rollback refused");
                }
                self.docker.run(&s(&["stop", "-t", "120", &c.container]))?;
                self.docker
                    .run(&s(&["update", "--restart", "no", &c.container]))?;
                let n = format!("{}-failed-{}", c.container, epoch());
                self.docker.run(&s(&["rename", &c.container, &n]))?;
                failed = Some(n);
            }
        }
        if old["Name"] != format!("/{}", c.container) {
            self.docker
                .run(&s(&["rename", &p.container_id, &c.container]))?;
        }
        self.docker.run(&s(&[
            "update",
            "--restart",
            &p.restart_policy,
            &p.container_id,
        ]))?;
        if c.desired_running {
            self.docker.run(&s(&["start", &p.container_id]))?;
        }
        c.image = p.image_id;
        c.previous = None;
        c.pending = None;
        self.store.save(&c)?;
        Ok(json!({"rolled_back":true,"failed_container_retained":failed,"data_preserved":true}))
    }
    /// Persist one observation without restarting or changing the runtime.
    ///
    /// # Errors
    /// Returns an error if the monitor record cannot be written durably.
    pub fn record_observation(&self) -> Result<()> {
        let report = self
            .status()
            .unwrap_or_else(|e| json!({"error":format!("{e:#}")}));
        crate::config::atomic_json(
            &self.store.root().join("monitor.json"),
            &json!({"checked_at":epoch(),"status":report}),
        )
    }
}

/// Return the current Unix timestamp, using zero if the clock predates the epoch.
pub fn epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn text(v: &Value) -> Result<String> {
    Ok(v.as_str()
        .map(str::to_string)
        .context("Required Docker metadata is missing")?)
}
pub(crate) fn environment(i: &Value) -> std::collections::BTreeMap<String, String> {
    i["Config"]["Env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str()?.split_once('='))
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
}
pub(crate) fn compatibility(image: &Value) -> Result<Value> {
    let e = environment(image);
    Ok(
        json!({"zakura":e.get("ZAKURA_VERSION").context("Image is missing ZAKURA_VERSION")?,"database_major":e.get("ZAKURA_DB_MAJOR").context("Image is missing ZAKURA_DB_MAJOR")?,"lightwalletd":e.get("LIGHTWALLETD_VERSION").context("Image is missing LIGHTWALLETD_VERSION")?}),
    )
}

fn validate_supported_runtime(image: &Value) -> Result<()> {
    let declared = compatibility(image)?;
    let supported = json!({
        "zakura": SUPPORTED_ZAKURA_VERSION,
        "database_major": SUPPORTED_DATABASE_MAJOR,
        "lightwalletd": SUPPORTED_LIGHTWALLETD_VERSION,
    });
    if declared != supported {
        bail!("Unsupported runtime versions: {declared}; this CLI supports {supported}");
    }
    Ok(())
}

pub(crate) fn validate_installation(i: &Value) -> Result<String> {
    let entry = i["Config"]["Entrypoint"]
        .as_array()
        .context("Container has no entrypoint")?;
    if !entry.iter().any(|x| x == "/opt/zovereign/stack.py") {
        bail!("Container is not the combined Zovereign runtime");
    }
    validate_supported_runtime(i)?;
    let e = environment(i);
    if e.get("DATA_DIR").map(String::as_str).unwrap_or("/data") != "/data" {
        bail!("Custom DATA_DIR is not supported by this CLI yet");
    }
    let mounts = i["Mounts"]
        .as_array()
        .context("Container has no data mount")?;
    let m = mounts
        .iter()
        .find(|m| m["Destination"] == "/data")
        .context("Container does not persist /data")?;
    if m["Type"] != "volume" || m["RW"] != true {
        bail!("CLI adoption currently requires a writable named volume at /data");
    }
    text(&m["Name"])
}

/// Recreate only settings we can faithfully express; reject other settings
/// before stopping the user's container. Preserve TLS mounts, ports and overrides.
pub(crate) fn recreate_args(
    i: &Value,
    old_image: &Value,
    new_image: &str,
    name: &str,
) -> Result<Vec<String>> {
    let h = &i["HostConfig"];
    for key in [
        "Privileged",
        "ReadonlyRootfs",
        "OomKillDisable",
        "PublishAllPorts",
        "AutoRemove",
        "Init",
    ] {
        if h[key] == true {
            bail!("Update cannot yet preserve HostConfig.{key}; use the documented manual update procedure");
        }
    }
    for key in [
        "CapAdd",
        "CapDrop",
        "Devices",
        "DeviceRequests",
        "SecurityOpt",
        "ExtraHosts",
        "Dns",
        "DnsSearch",
        "Binds",
        "Links",
        "VolumesFrom",
        "Tmpfs",
        "Ulimits",
        "PortSpecs",
        "GroupAdd",
        "DeviceCgroupRules",
        "Sysctls",
        "StorageOpt",
    ] {
        if !h[key].is_null() && h[key] != json!([]) && h[key] != json!({}) {
            bail!("Update cannot yet preserve HostConfig.{key}");
        }
    }
    for key in [
        "Memory",
        "MemorySwap",
        "MemoryReservation",
        "NanoCpus",
        "CpuShares",
        "CpuQuota",
        "CpuPeriod",
        "CpuRealtimePeriod",
        "CpuRealtimeRuntime",
        "BlkioWeight",
        "PidsLimit",
        "ShmSize",
    ] {
        let n = h[key].as_i64().unwrap_or(0);
        if n != 0 && !(key == "ShmSize" && n == 67108864) {
            bail!("Update cannot yet preserve resource setting {key}");
        }
    }
    for key in [
        "CpusetCpus",
        "CpusetMems",
        "PidMode",
        "UTSMode",
        "CgroupParent",
        "VolumeDriver",
    ] {
        if !h[key].as_str().unwrap_or("").is_empty() {
            bail!("Update cannot yet preserve HostConfig.{key}");
        }
    }
    if !["", "private"].contains(&h["IpcMode"].as_str().unwrap_or("")) {
        bail!("Custom IPC mode requires a manual update");
    }
    if !["", "runc"].contains(&h["Runtime"].as_str().unwrap_or("")) {
        bail!("Custom container runtime requires a manual update");
    }
    if i["Config"]["Tty"] == true || i["Config"]["OpenStdin"] == true {
        bail!("Interactive containers require a manual update");
    }
    let hostname = i["Config"]["Hostname"].as_str().unwrap_or("");
    if !hostname.is_empty() && !i["Id"].as_str().unwrap_or("").starts_with(hostname) {
        bail!("Custom hostname requires a manual update");
    }
    let net = h["NetworkMode"].as_str().unwrap_or("default");
    if !["default", "bridge"].contains(&net) {
        bail!("Update cannot yet preserve custom network '{net}'");
    }
    let mut args = s(&[
        "create",
        "--name",
        name,
        "--network",
        net,
        "--restart",
        h["RestartPolicy"]["Name"]
            .as_str()
            .unwrap_or("unless-stopped"),
        "--stop-timeout",
        &i["Config"]["StopTimeout"]
            .as_u64()
            .unwrap_or(120)
            .to_string(),
    ]);
    if h["RestartPolicy"]["MaximumRetryCount"]
        .as_u64()
        .unwrap_or(0)
        != 0
    {
        bail!("Custom restart retry count is not supported for managed updates");
    }
    for m in i["Mounts"].as_array().context("Missing mounts")? {
        let kind = text(&m["Type"])?;
        if !["volume", "bind"].contains(&kind.as_str()) {
            bail!("Unsupported mount type {kind}");
        }
        let src = if kind == "volume" {
            text(&m["Name"])?
        } else {
            text(&m["Source"])?
        };
        let dst = text(&m["Destination"])?;
        if src.contains(',') || dst.contains(',') {
            bail!("Mount paths containing commas require manual update");
        }
        let propagation = m["Propagation"].as_str().unwrap_or("");
        if !["", "rprivate"].contains(&propagation) {
            bail!("Custom mount propagation requires a manual update");
        }
        let mut mount = format!("type={kind},src={src},dst={dst}");
        if m["RW"] != true {
            mount.push_str(",readonly");
        }
        args.extend(s(&["--mount", &mount]));
    }
    if let Some(ports) = h["PortBindings"].as_object() {
        for (port, bindings) in ports {
            for b in bindings.as_array().context("Invalid port bindings")? {
                let ip = b["HostIp"].as_str().unwrap_or("");
                let ip = if ip.contains(':') {
                    format!("[{ip}]")
                } else {
                    ip.into()
                };
                args.extend(s(&[
                    "-p",
                    &format!("{ip}:{}:{port}", text(&b["HostPort"])?),
                ]));
            }
        }
    }
    let old_env = environment(old_image);
    for (k, v) in environment(i) {
        if old_env.get(&k) != Some(&v) {
            if ["ZAKURA_VERSION", "ZAKURA_DB_MAJOR", "LIGHTWALLETD_VERSION"].contains(&k.as_str()) {
                bail!("Runtime version metadata was overridden");
            }
            args.extend(s(&["-e", &format!("{k}={v}")]));
        }
    }
    if let Some(labels) = i["Config"]["Labels"].as_object() {
        for (k, v) in labels {
            // New image provenance must come from the candidate image itself.
            if old_image["Config"]["Labels"].get(k) != Some(v) {
                args.extend(s(&[
                    "--label",
                    &format!("{k}={}", v.as_str().unwrap_or("")),
                ]));
            }
        }
    }
    if let Some(driver) = h["LogConfig"]["Type"].as_str() {
        if !driver.is_empty() {
            args.extend(s(&["--log-driver", driver]));
        }
    }
    if let Some(opts) = h["LogConfig"]["Config"].as_object() {
        for (k, v) in opts {
            args.extend(s(&["--log-opt", &format!("{k}={}", text(v)?)]));
        }
    }
    for (key, flag) in [
        ("User", "--user"),
        ("WorkingDir", "--workdir"),
        ("StopSignal", "--stop-signal"),
    ] {
        if let Some(v) = i["Config"][key].as_str() {
            if !v.is_empty() {
                args.extend(s(&[flag, v]));
            }
        }
    }
    if i["Config"]["Entrypoint"] != old_image["Config"]["Entrypoint"]
        || i["Config"]["Cmd"] != old_image["Config"]["Cmd"]
        || i["Config"]["Healthcheck"] != old_image["Config"]["Healthcheck"]
    {
        bail!("Custom entrypoint, command, or healthcheck requires a manual update");
    }
    args.push(new_image.into());
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn image() -> Value {
        json!({"Config":{"Env":["ZAKURA_VERSION=1.8.7","ZAKURA_DB_MAJOR=30","LIGHTWALLETD_VERSION=v0.6.0","DATA_DIR=/data"],"Entrypoint":["/usr/bin/tini","--","python3","/opt/zovereign/stack.py"],"Cmd":["run"]}})
    }
    fn container() -> Value {
        let mut i = image();
        i["Mounts"] = json!([{"Type":"volume","Name":"zovereign-data","Destination":"/data","RW":true},{"Type":"bind","Source":"/srv/tls","Destination":"/tls","RW":false}]);
        i["HostConfig"] = json!({"NetworkMode":"bridge","RestartPolicy":{"Name":"unless-stopped","MaximumRetryCount":0},"PortBindings":{"9067/tcp":[{"HostIp":"127.0.0.1","HostPort":"19067"}]},"LogConfig":{"Type":"json-file","Config":{"max-size":"20m"}}});
        i["Config"]["Env"].as_array_mut().unwrap().extend([
            json!("TLS_CERT=/tls/cert.pem"),
            json!("TLS_KEY=/tls/key.pem"),
            json!("DOWNLOAD_WORKERS=5"),
        ]);
        i
    }
    #[test]
    fn recreation_keeps_ports_tls_and_overrides() {
        let args = recreate_args(&container(), &image(), "sha256:new", "zovereign").unwrap();
        for expected in [
            "127.0.0.1:19067:9067/tcp",
            "type=volume,src=zovereign-data,dst=/data",
            "type=bind,src=/srv/tls,dst=/tls,readonly",
            "TLS_CERT=/tls/cert.pem",
            "TLS_KEY=/tls/key.pem",
            "DOWNLOAD_WORKERS=5",
        ] {
            assert!(args.contains(&expected.to_string()), "{expected}");
        }
        assert!(!args.contains(&"ZAKURA_DB_MAJOR=30".into()));
    }
    #[test]
    fn update_uses_new_image_provenance_and_keeps_user_labels() {
        let mut old = image();
        old["Config"]["Labels"] = json!({"org.opencontainers.image.revision":"old"});
        let mut i = container();
        i["Config"]["Labels"] =
            json!({"org.opencontainers.image.revision":"old","io.zovereign.managed":"1"});
        let args = recreate_args(&i, &old, "new", "zovereign").unwrap();
        assert!(!args.contains(&"org.opencontainers.image.revision=old".into()));
        assert!(args.contains(&"io.zovereign.managed=1".into()));
    }
    #[test]
    fn unpreserved_settings_are_refused_before_update() {
        let mut i = container();
        i["HostConfig"]["Privileged"] = json!(true);
        assert!(recreate_args(&i, &image(), "new", "zovereign").is_err());
        let mut i = container();
        i["HostConfig"]["Memory"] = json!(1024);
        assert!(recreate_args(&i, &image(), "new", "zovereign").is_err());
    }
    #[test]
    fn adoption_requires_combined_runtime_and_persistence() {
        assert_eq!(
            validate_installation(&container()).unwrap(),
            "zovereign-data"
        );
        let mut i = container();
        i["Config"]["Entrypoint"] = json!(["nginx"]);
        assert!(validate_installation(&i).is_err());
        let mut i = container();
        i["Mounts"] = json!([]);
        assert!(validate_installation(&i).is_err());
    }
    #[test]
    fn runtime_versions_must_match_the_supported_contract() {
        assert!(validate_supported_runtime(&image()).is_ok());
        for (key, value) in [
            ("ZAKURA_VERSION", "2.0.0"),
            ("ZAKURA_DB_MAJOR", "31"),
            ("LIGHTWALLETD_VERSION", "v99.0.0"),
            ("ZAKURA_VERSION", ""),
        ] {
            let mut unsupported = image();
            let env = unsupported["Config"]["Env"].as_array_mut().unwrap();
            for entry in env {
                if entry.as_str().unwrap().starts_with(&format!("{key}=")) {
                    *entry = json!(format!("{key}={value}"));
                }
            }
            assert!(
                validate_supported_runtime(&unsupported).is_err(),
                "unsupported {key}={value} must be rejected"
            );
        }
        assert!(validate_supported_runtime(&json!({})).is_err());
    }

    #[test]
    fn approved_versions_match_the_bundled_runtime() {
        let dockerfile = include_str!("../../../Dockerfile");
        for (key, value) in [
            ("ZAKURA_VERSION", SUPPORTED_ZAKURA_VERSION),
            ("ZAKURA_DB_MAJOR", SUPPORTED_DATABASE_MAJOR),
            ("LIGHTWALLETD_VERSION", SUPPORTED_LIGHTWALLETD_VERSION),
        ] {
            assert!(
                dockerfile.contains(&format!("{key}={value}")),
                "review the CLI compatibility policy when changing Dockerfile {key}"
            );
        }
    }
}
