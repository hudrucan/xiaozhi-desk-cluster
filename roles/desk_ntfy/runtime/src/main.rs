mod auth;
mod cluster;
mod journal;
#[cfg(test)]
mod native_test;
#[cfg(test)]
mod peer_tls_test;
mod web;

use anyhow::{ensure, Result};
use axum::{
    body::Bytes,
    extract::{ConnectInfo, DefaultBodyLimit, Path, State},
    http::{HeaderMap, Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::URL_SAFE, Engine};
use cluster::{Cluster, Config};
use rand::RngCore;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    net::SocketAddr,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

#[derive(Clone)]
struct App {
    cluster: Arc<Cluster>,
    nonces: Arc<Mutex<BTreeMap<String, Instant>>>,
}
fn reply(code: StatusCode, body: Value) -> Response {
    (
        code,
        [
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ],
        Json(body),
    )
        .into_response()
}
async fn ready(State(app): State<App>) -> Response {
    let mut ready = app.cluster.fresh();
    if ready {
        ready = app
            .cluster
            .client
            .get("http://127.0.0.1:2587/v1/health")
            .timeout(Duration::from_secs(1))
            .send()
            .await
            .is_ok_and(|r| r.status().is_success());
    }
    reply(
        if ready {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        json!({"ready":ready}),
    )
}
fn mutation_path(path: &str) -> bool {
    path.starts_with("/v1/admin/users")
        || path == "/v1/account/password"
        || path == "/v1/account/token"
        || path.starts_with("/v1/account/token/")
}
async fn mutate(
    State(app): State<App>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if !matches!(method, Method::POST | Method::PUT | Method::DELETE) {
        return reply(
            StatusCode::METHOD_NOT_ALLOWED,
            json!({"error":"mutation endpoint"}),
        );
    }
    if uri.query().is_some() || uri.path().contains('%') || !mutation_path(uri.path()) {
        return reply(
            StatusCode::FORBIDDEN,
            json!({"error":"registration and self-service ACL disabled"}),
        );
    }
    let host = headers
        .get("host")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    let origin = headers.get("origin").and_then(|h| h.to_str().ok());
    let site = headers.get("sec-fetch-site").and_then(|h| h.to_str().ok());
    if origin.is_some_and(|o| o != format!("http://{host}") && o != format!("https://{host}"))
        || site.is_some_and(|s| s != "same-origin" && s != "none")
    {
        return reply(
            StatusCode::FORBIDDEN,
            json!({"error":"same-origin admin mutation required"}),
        );
    }
    let raw = if body.is_empty() {
        b"{}".as_slice()
    } else {
        body.as_ref()
    };
    let Ok(value) = serde_json::from_slice(raw) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"invalid JSON"}));
    };
    let credential = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .unwrap_or("");
    if credential.is_empty() {
        return reply(
            StatusCode::UNAUTHORIZED,
            json!({"error":"administrator required"}),
        );
    }
    match app
        .cluster
        .mutate(credential, uri.path(), method.as_str(), value)
        .await
    {
        Ok(value) => reply(StatusCode::OK, value),
        Err(error) if error.downcast_ref::<cluster::RequestFailure>().is_some() => reply(
            error.downcast_ref::<cluster::RequestFailure>().unwrap().0,
            json!({"error":"auth request rejected"}),
        ),
        // A chosen revision can survive an unsuccessful Save. Do not claim rollback or retry a token creation automatically.
        Err(_) => reply(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error":"auth update not acknowledged; inspect current account before retrying"}),
        ),
    }
}
async fn peer(
    State(app): State<App>,
    ConnectInfo(remote): ConnectInfo<SocketAddr>,
    Path(action): Path<String>,
    uri: Uri,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let header = |key: &str| {
        headers
            .get(key)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string()
    };
    let stamp = header("x-desk-time");
    let nonce = header("x-desk-nonce");
    let signature = header("x-desk-signature");
    let permitted = app
        .cluster
        .config
        .members
        .values()
        .any(|ip| ip == &remote.ip().to_string());
    if !permitted
        || uri.query().is_some()
        || uri.path().contains('%')
        || stamp
            .parse::<u64>()
            .ok()
            .is_none_or(|t| t.abs_diff(cluster::unix()) > 15)
        || nonce.len() != 32
        || !nonce.bytes().all(|c| c.is_ascii_alphanumeric())
    {
        return reply(
            StatusCode::FORBIDDEN,
            json!({"error":"private peer endpoint"}),
        );
    }
    use sha2::Digest;
    let signed = format!(
        "POST\n{}\n{stamp}\n{nonce}\n{}",
        uri.path(),
        hex::encode(sha2::Sha256::digest(&body))
    );
    if !journal::verify(&app.cluster.key, signed.as_bytes(), &signature) {
        return reply(
            StatusCode::FORBIDDEN,
            json!({"error":"invalid peer signature"}),
        );
    }
    {
        let mut nonces = app.nonces.lock().unwrap();
        nonces.retain(|_, at| at.elapsed() < Duration::from_secs(20));
        if nonces.len() >= 1024 || nonces.insert(nonce, Instant::now()).is_some() {
            return reply(
                StatusCode::FORBIDDEN,
                json!({"error":"replayed peer operation"}),
            );
        }
    }
    let Ok(value) = serde_json::from_slice(&body) else {
        return reply(StatusCode::BAD_REQUEST, json!({"error":"invalid JSON"}));
    };
    match app.cluster.peer(&action, value).await {
        Ok(value) => reply(StatusCode::OK, value),
        Err(_) => reply(
            StatusCode::CONFLICT,
            json!({"error":"peer operation rejected"}),
        ),
    }
}
fn initialize(dir: PathBuf) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    ensure!(
        !dir.exists(),
        "use a new private secret directory; existing identity is never replaced"
    );
    let password = rpassword::prompt_password("Initial ntfy admin password: ")?;
    let confirm = rpassword::prompt_password("Confirm password: ")?;
    ensure!(password == confirm, "passwords differ");
    let seed = auth::Auth::seed(&password)?;
    seed.validate()?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)?;
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
    let mut key = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut key);
    journal::atomic(
        &dir.join("credentials.json"),
        &journal::bytes(&json!({"key":URL_SAFE.encode(key)})),
    )?;
    journal::atomic(
        &dir.join("seed.enc"),
        &journal::encrypt(&key, &journal::bytes(&seed))?,
    )?;
    generate_peer_certificate(&dir, "IP:10.10.10.11,IP:10.10.10.12,IP:10.10.10.13")?;
    eprintln!("Shared ntfy identity created. Retain these ignored files for all replicas.");
    Ok(())
}

fn generate_peer_certificate(dir: &std::path::Path, subject_alt_name: &str) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let status = std::process::Command::new("openssl")
        .args([
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "3650",
            "-subj",
            "/CN=desk-ntfy-peer",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "extendedKeyUsage=serverAuth",
            "-addext",
            &format!("subjectAltName={subject_alt_name}"),
            "-keyout",
        ])
        .arg(dir.join("tls.key"))
        .arg("-out")
        .arg(dir.join("tls.crt"))
        .output()?;
    ensure!(
        status.status.success(),
        "peer certificate generation failed"
    );
    std::fs::set_permissions(dir.join("tls.key"), std::fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    let args: Vec<_> = std::env::args().collect();
    ensure!(
        args.len() == 3,
        "usage: desk-ntfy init PRIVATE_DIRECTORY | serve CONFIG_JSON | native SERVER_TOML"
    );
    match args[1].as_str() {
        "init" => initialize(PathBuf::from(&args[2])),
        "native" => {
            let file: ntfy_rs::FileConfig = toml::from_str(&std::fs::read_to_string(&args[2])?)?;
            let config = ntfy_rs::Config::resolve(file, &ntfy_rs::ServeArgs::default());
            let handle = ntfy_rs::start_async(config).await?;
            tokio::signal::ctrl_c().await?;
            handle.shutdown();
            Ok(())
        }
        "serve" => {
            let config: Config = serde_json::from_slice(&std::fs::read(&args[2])?)?;
            ensure!(
                config.members.len() == 3
                    && config.members.contains_key(&config.node)
                    && config
                        .members
                        .values()
                        .collect::<std::collections::BTreeSet<_>>()
                        .len()
                        == 3,
                "exactly three auth replicas required"
            );
            let key: [u8; 32] = URL_SAFE
                .decode(&config.key)?
                .try_into()
                .map_err(|_| anyhow::anyhow!("invalid auth key"))?;
            let seed: auth::Auth =
                serde_json::from_slice(&journal::decrypt(&key, &std::fs::read(&config.seed)?)?)?;
            seed.validate()?;
            let journal = journal::Journal::open(
                PathBuf::from(&config.journal),
                key,
                config.node.clone(),
                config.members.keys().cloned().collect(),
                serde_json::to_value(seed)?,
            )?;
            let certificate = reqwest::Certificate::from_pem(&std::fs::read(&config.certificate)?)?;
            let client = reqwest::Client::builder()
                .no_proxy()
                .add_root_certificate(certificate)
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(1))
                .timeout(Duration::from_secs(8))
                .build()?;
            let local = config.members[&config.node].clone();
            let tls = axum_server::tls_rustls::RustlsConfig::from_pem_file(
                &config.certificate,
                &config.tls_key,
            )
            .await?;
            let cluster = Cluster::new(config, journal, key, client);
            let app = App {
                cluster: cluster.clone(),
                nonces: Arc::new(Mutex::new(BTreeMap::new())),
            };
            let peer_router = Router::new()
                .route("/peer/:action", post(peer))
                .layer(DefaultBodyLimit::max(786432))
                .with_state(app.clone());
            let local_router = Router::new()
                .route("/ready", get(ready))
                .fallback(mutate)
                .layer(DefaultBodyLimit::max(262144))
                .with_state(app)
                .merge(web::router("/opt/desk-ntfy/web"));
            let listener = tokio::net::TcpListener::bind("127.0.0.1:2590").await?;
            let local_server = axum::serve(listener, local_router);
            let peer_server =
                axum_server::bind_rustls(format!("{local}:2591").parse::<SocketAddr>()?, tls)
                    .serve(peer_router.into_make_service_with_connect_info::<SocketAddr>());
            let worker = async {
                let mut pending = false;
                loop {
                    if cluster.reconcile().await.is_ok() {
                        if pending {
                            eprintln!("ntfy auth quorum and apply recovered");
                        }
                        pending = false;
                        let _ = cluster.publish_events().await;
                    } else {
                        cluster.fence();
                        if !pending {
                            eprintln!("ntfy auth freshness or apply pending; replica fenced");
                        }
                        pending = true;
                    }
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            };
            tokio::select! {
                result=local_server => {result?; anyhow::bail!("local adapter stopped")},
                result=peer_server => {result?; anyhow::bail!("peer adapter stopped")},
                _=worker => anyhow::bail!("reconciliation stopped"),
            }
        }
        _ => anyhow::bail!("unknown command"),
    }
}
