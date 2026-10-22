//! Exercise the real TLS and signed peer boundary; quorum unit tests bypass transport.
use super::*;
use rustls::pki_types::{pem::PemObject, CertificateDer, ServerName};
use std::io::{Read, Write};

async fn signed_status(dir: &std::path::Path) -> Result<(u16, Value)> {
    let seed = auth::Auth::seed("disposable test password")?;
    let members = BTreeMap::from([
        ("a".into(), "127.0.0.1".into()),
        ("b".into(), "127.0.0.2".into()),
        ("c".into(), "127.0.0.3".into()),
    ]);
    let journal = journal::Journal::open(
        dir.join("journal.enc"),
        [7; 32],
        "a".into(),
        members.keys().cloned().collect(),
        serde_json::to_value(seed)?,
    )?;
    let cluster = Cluster::new(
        Config {
            node: "a".into(),
            members,
            key: String::new(),
            certificate: String::new(),
            tls_key: String::new(),
            seed: String::new(),
            journal: String::new(),
            database: dir.join("auth.db").to_string_lossy().into(),
            events: String::new(),
            toml: String::new(),
        },
        journal,
        [7; 32],
        reqwest::Client::new(),
    );
    let app = App {
        cluster: cluster.clone(),
        nonces: Arc::new(Mutex::new(BTreeMap::new())),
    };
    let router = Router::new()
        .route("/peer/:action", post(peer))
        .with_state(app);
    let tls = axum_server::tls_rustls::RustlsConfig::from_pem_file(
        dir.join("tls.crt"),
        dir.join("tls.key"),
    )
    .await?;
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    listener.set_nonblocking(true)?;
    let address = listener.local_addr()?;
    let server = tokio::spawn(async move {
        axum_server::from_tcp_rustls(listener, tls)
            .unwrap()
            .serve(router.into_make_service_with_connect_info::<SocketAddr>())
            .await
    });
    let certificate = CertificateDer::from_pem_slice(&std::fs::read(dir.join("tls.crt"))?)?;
    let mut roots = rustls::RootCertStore::empty();
    roots.add(certificate)?;
    // Linux's platform verifier uses WebPKI. Avoid Apple's unrelated lifetime policy.
    let config = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    let path = "/peer/status";
    let stamp = cluster::unix().to_string();
    let nonce = auth::identity();
    let body = journal::bytes(&json!({}));
    let signature = cluster.signature(path, &stamp, &nonce, &body);
    let result = tokio::task::spawn_blocking(move || -> Result<(u16, Value)> {
        let socket = std::net::TcpStream::connect(address)?;
        socket.set_read_timeout(Some(Duration::from_secs(8)))?;
        socket.set_write_timeout(Some(Duration::from_secs(8)))?;
        let tls = rustls::ClientConnection::new(Arc::new(config), ServerName::try_from("127.0.0.1")?)?;
        let mut stream = rustls::StreamOwned::new(tls, socket);
        write!(stream, "POST {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\nX-Desk-Time: {stamp}\r\nX-Desk-Nonce: {nonce}\r\nX-Desk-Signature: {signature}\r\n\r\n", body.len())?;
        stream.write_all(&body)?;
        let mut response = Vec::new();
        stream.take(4096).read_to_end(&mut response)?;
        let response = String::from_utf8(response)?;
        let (headers, body) = response.split_once("\r\n\r\n").ok_or_else(|| anyhow::anyhow!("missing HTTP response"))?;
        let status = headers.split_whitespace().nth(1).ok_or_else(|| anyhow::anyhow!("missing HTTP status"))?.parse()?;
        Ok((status, serde_json::from_str(body)?))
    }).await?;
    server.abort();
    result
}

#[tokio::test]
async fn generated_peer_leaf_passes_real_tls_and_signed_status() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    let dir = tempfile::tempdir().unwrap();
    generate_peer_certificate(dir.path(), "IP:127.0.0.1").unwrap();
    let response = signed_status(dir.path())
        .await
        .expect("generated peer certificate must pass Rust TLS validation");
    let (code, status) = response;
    assert_eq!(code, 200);
    assert_eq!(status["head"]["revision"], 0);
    assert_eq!(status["head"]["applied_revision"], -1);
    assert!(
        !dir.path().join("auth.db").exists(),
        "a status probe cannot mutate auth"
    );
}

#[tokio::test]
async fn legacy_ca_as_server_is_rejected_before_auth_apply() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    let dir = tempfile::tempdir().unwrap();
    let output = std::process::Command::new("openssl")
        .args([
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=legacy-peer",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
        ])
        .arg(dir.path().join("tls.key"))
        .arg("-out")
        .arg(dir.path().join("tls.crt"))
        .output()
        .unwrap();
    assert!(output.status.success());
    let error = signed_status(dir.path())
        .await
        .expect_err("CA must not be accepted as a server leaf");
    assert!(
        format!("{error:?}").contains("CaUsedAsEndEntity"),
        "unexpected TLS failure: {error:?}"
    );
    assert!(!dir.path().join("auth.db").exists());
}
