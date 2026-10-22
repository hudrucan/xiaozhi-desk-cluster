use axum::Router;
use std::path::Path;
use tower_http::services::{ServeDir, ServeFile};

// Only public, Ansible-owned frontend files belong in this directory.
pub fn router(root: impl AsRef<Path>) -> Router {
    let root = root.as_ref();
    let index = ServeFile::new(root.join("index.html"));
    let mut router = Router::new()
        .nest_service("/static", ServeDir::new(root.join("static")))
        .route_service("/", index.clone())
        .route_service("/:topic", index.clone())
        .route_service("/:server/:topic", index.clone());
    for file in [
        "index.html",
        "app.html",
        "config.js",
        "sw.js",
        "manifest.webmanifest",
    ] {
        router = router.route_service(&format!("/{file}"), ServeFile::new(root.join(file)));
    }
    router
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{to_bytes, Body},
        http::{Method, Request, StatusCode},
    };
    use tower::ServiceExt;

    #[tokio::test]
    async fn static_assets_and_spa_routes_do_not_replace_mutations_or_escape_root() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::create_dir(temp.path().join("static")).unwrap();
        std::fs::write(temp.path().join("index.html"), "<html>NTFY</html>").unwrap();
        std::fs::write(temp.path().join("static/app.js"), "console.log('NTFY')").unwrap();
        let app = router(temp.path());
        for path in [
            "/",
            "/settings",
            "/desk-events",
            "/host.example/desk-events",
        ] {
            let response = app
                .clone()
                .oneshot(Request::get(path).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            assert_eq!(
                to_bytes(response.into_body(), 1024).await.unwrap(),
                "<html>NTFY</html>"
            );
        }
        let asset = app
            .clone()
            .oneshot(Request::get("/static/app.js").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(asset.status(), StatusCode::OK);
        assert!(asset.headers()["content-type"]
            .to_str()
            .unwrap()
            .contains("javascript"));
        for path in ["/static/missing.js", "/static/%2e%2e/index.html"] {
            let response = app
                .clone()
                .oneshot(Request::get(path).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_ne!(response.status(), StatusCode::OK);
        }
        let response = app
            .oneshot(
                Request::builder()
                    .method(Method::PUT)
                    .uri("/desk-events")
                    .body(Body::from("test"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    }
}
