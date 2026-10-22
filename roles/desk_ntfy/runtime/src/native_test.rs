//! Exercise the actual pinned upstream, not a substitute notification protocol.
use super::*;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn native_auth_cache_and_ios_poll_forward_keep_the_stable_topic_url() {
    let _ = rustls::crypto::aws_lc_rs::default_provider().install_default();
    let dir = tempfile::tempdir().unwrap();
    let auth_file = dir.path().join("auth.db");
    let seed = auth::Auth::seed("secret").unwrap();
    let (seed, _) = seed
        .mutate(
            "admin",
            "/v1/admin/users",
            "POST",
            json!({"username":"publisher","password":"secret"}),
        )
        .unwrap();
    let (seed, _) = seed
        .mutate(
            "admin",
            "/v1/admin/users/publisher/access",
            "POST",
            json!({"topic":"desk-events","write":true}),
        )
        .unwrap();
    let (seed, publisher) = seed
        .mutate(
            "admin",
            "/v1/admin/users/publisher/token",
            "POST",
            json!({"label":"native integration"}),
        )
        .unwrap();
    seed.apply(auth_file.to_str().unwrap()).unwrap();
    let seen = Arc::new(Mutex::new(Vec::<(Uri, HeaderMap, Bytes)>::new()));
    let capture = seen.clone();
    let upstream = Router::new().fallback(move |uri: Uri, headers: HeaderMap, body: Bytes| {
        let capture = capture.clone();
        async move {
            capture.lock().unwrap().push((uri, headers, body));
            StatusCode::OK
        }
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream_url = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });
    let address = "127.0.0.1:0";
    let file: ntfy_rs::FileConfig = toml::from_str(&format!(
        r#"
listen-http = "{address}"
base-url = "http://192.168.1.186:2586"
upstream-base-url = "{upstream_url}"
auth-file = {:?}
cache-file = {:?}
default-access = "deny-all"
"#,
        auth_file.to_str().unwrap(),
        dir.path().join("cache.db").to_str().unwrap()
    ))
    .unwrap();
    let config = ntfy_rs::Config::resolve(file, &ntfy_rs::ServeArgs::default());
    let handle = ntfy_rs::start_async(config).await.unwrap();
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let url = format!("http://127.0.0.1:{}/desk-events", handle.port());
    let base = format!("http://127.0.0.1:{}", handle.port());
    // The web client's topic credential check must use the requested topic's ACL.
    for (topic, token, expected) in [
        ("desk-events", seed.panel_token().unwrap(), StatusCode::OK),
        (
            "other-topic",
            seed.panel_token().unwrap(),
            StatusCode::UNAUTHORIZED,
        ),
        (
            "desk-events",
            publisher["token"].as_str().unwrap().into(),
            StatusCode::UNAUTHORIZED,
        ),
    ] {
        assert_eq!(
            client
                .get(format!("{base}/{topic}/auth"))
                .bearer_auth(token)
                .send()
                .await
                .unwrap()
                .status(),
            expected
        );
    }
    for (path, body) in [
        ("/v1/account", json!({"username":"intruder","password":"p"})),
        (
            "/v1/account/access",
            json!({"topic":"private","read":true,"write":true}),
        ),
        (
            "/v1/admin/users",
            json!({"username":"bypass","password":"p"}),
        ),
        ("/v1/account/token", json!({"label":"bypass"})),
    ] {
        assert!(
            client
                .post(format!("{base}{path}"))
                .basic_auth("admin", Some("secret"))
                .json(&body)
                .send()
                .await
                .unwrap()
                .status()
                .is_client_error(),
            "native auth mutation bypass at {path}"
        );
    }
    assert_eq!(
        client
            .post(&url)
            .body("denied")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .post(&url)
            .bearer_auth(seed.panel_token().unwrap())
            .body("denied")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        client
            .post(format!("{base}/other-topic"))
            .bearer_auth(publisher["token"].as_str().unwrap())
            .body("denied")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        client
            .get(format!("{url}/json?poll=1"))
            .bearer_auth(publisher["token"].as_str().unwrap())
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    // Shoutrrr sends an empty Basic username with the bearer token as password.
    let published: Value = client
        .post(&url)
        .basic_auth("", publisher["token"].as_str())
        .body("Pi test")
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let poll = client
        .get(format!("{url}/json?poll=1"))
        .bearer_auth(seed.panel_token().unwrap())
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(poll.contains(published["id"].as_str().unwrap()));
    for _ in 0..30 {
        if !seen.lock().unwrap().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let captured = seen.lock().unwrap();
    assert_eq!(captured.len(), 1);
    use sha2::Digest;
    assert_eq!(
        captured[0].0.path(),
        format!(
            "/{}",
            hex::encode(sha2::Sha256::digest(
                b"http://192.168.1.186:2586/desk-events"
            ))
        )
    );
    assert_eq!(
        captured[0].1["x-poll-id"],
        published["id"].as_str().unwrap()
    );
    assert!(!String::from_utf8_lossy(&captured[0].2).contains("Pi test"));
    drop(captured);
    let (revoked, _) = seed
        .mutate(
            "admin",
            &format!(
                "/v1/admin/users/publisher/token/{}",
                publisher["token"].as_str().unwrap()
            ),
            "DELETE",
            json!({}),
        )
        .unwrap();
    revoked.apply(auth_file.to_str().unwrap()).unwrap();
    assert_eq!(
        client
            .post(&url)
            .bearer_auth(publisher["token"].as_str().unwrap())
            .body("revoked")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    handle.shutdown();
    server.abort();
}
