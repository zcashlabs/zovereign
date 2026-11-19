//! Installation lifecycle operations that retain the existing data volume.
use crate::{
    config::{valid_name, Config, StateStore},
    docker::{s, Docker},
};
use crate::{error::bail, Result};
use anyhow::Context;
use serde_json::{json, Value};

/// The published image selected by setup unless the caller chooses another.
pub const DEFAULT_IMAGE: &str = "ghcr.io/zcashlabs/zovereign:latest";
// Keep this approved runtime contract aligned with the root Dockerfile.
const SUPPORTED_ZAKURA_VERSION: &str = "1.8.7";
const SUPPORTED_DATABASE_MAJOR: &str = "30";
const SUPPORTED_LIGHTWALLETD_VERSION: &str = "v0.6.0";
const STATUS_SCRIPT: &str = "import json,pathlib,sys; sys.path.insert(0,'/opt/zovereign'); import stack; print(json.dumps(stack.status(pathlib.Path('/data'))) if hasattr(stack,'status') else json.dumps({'phase':'unknown','message':'This image predates structured status; use logs.'}))";
const CAPACITY_SCRIPT: &str = "import json,shutil,pathlib,platform; d=shutil.disk_usage('/data'); c=pathlib.Path('/proc/cpuinfo').read_text(); print(json.dumps({'free_bytes':d.free,'total_bytes':d.total,'cpu_compatible':platform.machine() not in ('x86_64','amd64') or all(any(x==f for x in c.split()) for f in ('bmi2','adx'))}))";
#[derive(Debug)]
/// Coordinates one installation while retaining its data and desired state.
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
            bail!("Installation already configured; use start or status");
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
        };
        self.store.save(&c)?;
        Ok(json!({"adopted":name,"data_preserved":true,"configuration_preserved":true}))
    }
    fn installation(&self, c: &Config) -> Result<Value> {
        self.check_engine(c)?;
        let i = self.docker.inspect("container", &c.container)?.context(
            "Managed container is missing; inspect the Docker context and installation record",
        )?;
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
            json!({"schema_version":1,"context":c.context,"container":c.container,"image":i["Image"],"state":i["State"]["Status"],"health":i["State"]["Health"]["Status"],"desired_running":c.desired_running,"restart_count":i["RestartCount"],"runtime":runtime}),
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
    /// Returns an error if identity checks, Docker or state operations fail.
    pub fn start(&self) -> Result<Value> {
        let _lock = self.store.lock()?;
        let mut c = self.config()?;
        self.installation(&c)?;
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
        // Persist intention before shutdown so it survives an interrupted stop.
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
