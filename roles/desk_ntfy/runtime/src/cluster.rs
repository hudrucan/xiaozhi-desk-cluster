use crate::{
    auth::Auth,
    journal::{self, Accepted, Ballot, Head, Journal, Receipt},
};
use anyhow::{ensure, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
#[cfg(not(test))]
use tokio::process::Command;
use tokio::{process::Child, sync::Mutex as AsyncMutex, task::JoinSet};

#[derive(Clone, Deserialize, Serialize)]
pub struct Config {
    pub node: String,
    pub members: BTreeMap<String, String>,
    pub key: String,
    pub certificate: String,
    pub tls_key: String,
    pub seed: String,
    pub journal: String,
    pub database: String,
    pub events: String,
    pub toml: String,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Status {
    pub head: Head,
    pub apply_ok: bool,
}
#[derive(Debug)]
pub struct RequestFailure(pub axum::http::StatusCode);
impl std::fmt::Display for RequestFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "request rejected")
    }
}
impl std::error::Error for RequestFailure {}
pub fn acknowledged(
    replies: &BTreeMap<String, Value>,
    node: &str,
    rev: i64,
    value: &Value,
) -> bool {
    let ready: Vec<_> = replies
        .iter()
        .filter_map(|(n, v)| {
            let s: Status = serde_json::from_value(v.clone()).ok()?;
            (s.head.revision == rev
                && s.head.applied_revision == rev
                && s.apply_ok
                && journal::digest(&s.head.value) == journal::digest(value))
            .then_some(n.as_str())
        })
        .collect();
    ready.len() >= 2 && ready.contains(&node)
}
fn carry(promises: &BTreeMap<String, Value>, wanted: &Value) -> Result<Value> {
    let accepted: Vec<Accepted> = promises
        .values()
        .filter_map(|p| p.get("accepted"))
        .filter(|v| !v.is_null())
        .map(|v| serde_json::from_value(v.clone()))
        .collect::<std::result::Result<_, _>>()?;
    Ok(accepted
        .into_iter()
        .max_by_key(|a| a.ballot.clone())
        .map(|a| a.value)
        .unwrap_or_else(|| wanted.clone()))
}
pub fn unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

pub struct Cluster {
    pub config: Config,
    pub journal: Mutex<Journal>,
    pub key: [u8; 32],
    pub client: reqwest::Client,
    serial: AsyncMutex<()>,
    apply_lock: AsyncMutex<()>,
    child: AsyncMutex<Option<Child>>,
    applied_ok: AtomicBool,
    confirmed: Mutex<Option<Instant>>,
    #[cfg(test)]
    pub network: Mutex<BTreeMap<String, std::sync::Weak<Cluster>>>,
    #[cfg(test)]
    pub fail_apply: AtomicBool,
}
impl Cluster {
    pub fn new(
        config: Config,
        journal: Journal,
        key: [u8; 32],
        client: reqwest::Client,
    ) -> Arc<Self> {
        Arc::new(Self {
            config,
            journal: Mutex::new(journal),
            key,
            client,
            serial: AsyncMutex::new(()),
            apply_lock: AsyncMutex::new(()),
            child: AsyncMutex::new(None),
            applied_ok: AtomicBool::new(false),
            confirmed: Mutex::new(None),
            #[cfg(test)]
            network: Mutex::new(BTreeMap::new()),
            #[cfg(test)]
            fail_apply: AtomicBool::new(false),
        })
    }
    pub fn status(&self) -> Status {
        Status {
            head: self.journal.lock().unwrap().head.clone(),
            apply_ok: self.applied_ok.load(Ordering::SeqCst),
        }
    }
    pub fn fresh(&self) -> bool {
        let s = self.status();
        self.confirmed
            .lock()
            .unwrap()
            .is_some_and(|at| at.elapsed() < Duration::from_secs(6))
            && s.apply_ok
            && s.head.revision == s.head.applied_revision
            && s.head.accepted.is_none()
    }
    pub fn fence(&self) {
        *self.confirmed.lock().unwrap() = None;
    }
    async fn alive(&self) -> bool {
        #[cfg(test)]
        {
            self.applied_ok.load(Ordering::SeqCst)
        }
        #[cfg(not(test))]
        {
            self.child
                .lock()
                .await
                .as_mut()
                .is_some_and(|p| matches!(p.try_wait(), Ok(None)))
        }
    }
    async fn stop(&self) {
        if let Some(mut child) = self.child.lock().await.take() {
            let _ = child.kill().await;
        }
    }
    async fn start(&self) -> Result<()> {
        #[cfg(test)]
        {
            return Ok(());
        }
        #[cfg(not(test))]
        {
            let child = Command::new(std::env::current_exe()?)
                .arg("native")
                .arg(&self.config.toml)
                .kill_on_drop(true)
                .stdin(std::process::Stdio::null())
                .spawn()?;
            *self.child.lock().await = Some(child);
            for _ in 0..30 {
                if self
                    .client
                    .get("http://127.0.0.1:2587/v1/health")
                    .timeout(Duration::from_secs(1))
                    .send()
                    .await
                    .is_ok_and(|r| r.status().is_success())
                {
                    return Ok(());
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            anyhow::bail!("native startup failed")
        }
    }
    pub async fn apply(&self) -> Result<()> {
        let _guard = self.apply_lock.lock().await;
        loop {
            let head = self.status().head;
            if head.revision == head.applied_revision && self.alive().await {
                return Ok(());
            }
            self.applied_ok.store(false, Ordering::SeqCst);
            *self.confirmed.lock().unwrap() = None;
            self.stop().await;
            #[cfg(test)]
            ensure!(
                !self.fail_apply.load(Ordering::SeqCst),
                "simulated native failure"
            );
            let auth: Auth = serde_json::from_value(head.value)?;
            let path = self.config.database.clone();
            tokio::task::spawn_blocking(move || auth.apply(&path)).await??;
            if let Err(error) = self.start().await {
                self.stop().await;
                return Err(error);
            }
            // Persist apply only after native reopened exactly these identities successfully.
            self.journal.lock().unwrap().applied(head.revision)?;
            if self.status().head.revision == head.revision {
                self.applied_ok.store(true, Ordering::SeqCst);
                return Ok(());
            }
        }
    }
    pub async fn peer(&self, action: &str, value: Value) -> Result<Value> {
        match action {
            "status" => Ok(serde_json::to_value(self.status())?),
            "promise" => {
                let revision = value["revision"]
                    .as_i64()
                    .ok_or_else(|| anyhow::anyhow!("revision required"))?;
                let ballot: Ballot = serde_json::from_value(value["ballot"].clone())?;
                self.journal.lock().unwrap().promise(revision, ballot)
            }
            "accept" | "commit" => {
                let auth: Auth = serde_json::from_value(value["value"].clone())?;
                auth.validate()?;
                let revision = value["revision"]
                    .as_i64()
                    .ok_or_else(|| anyhow::anyhow!("revision required"))?;
                if action == "accept" {
                    let ballot: Ballot = serde_json::from_value(value["ballot"].clone())?;
                    Ok(serde_json::to_value(self.journal.lock().unwrap().accept(
                        revision,
                        ballot,
                        value["value"].clone(),
                    )?)?)
                } else {
                    let certificate: Vec<Receipt> =
                        serde_json::from_value(value["certificate"].clone())?;
                    self.journal.lock().unwrap().commit(
                        revision,
                        value["value"].clone(),
                        certificate,
                    )?;
                    if self.apply().await.is_err() {
                        self.applied_ok.store(false, Ordering::SeqCst);
                    }
                    Ok(serde_json::to_value(self.status())?)
                }
            }
            _ => anyhow::bail!("unknown peer operation"),
        }
    }
    pub fn signature(&self, path: &str, stamp: &str, nonce: &str, raw: &[u8]) -> String {
        use sha2::Digest;
        journal::sign(
            &self.key,
            format!(
                "POST\n{path}\n{stamp}\n{nonce}\n{}",
                hex::encode(sha2::Sha256::digest(raw))
            )
            .as_bytes(),
        )
    }
    async fn call(self: &Arc<Self>, node: &str, action: &str, value: Value) -> Result<Value> {
        if node == self.config.node {
            return self.peer(action, value).await;
        }
        #[cfg(test)]
        {
            let peer = self
                .network
                .lock()
                .unwrap()
                .get(node)
                .and_then(|n| n.upgrade());
            return peer
                .ok_or_else(|| anyhow::anyhow!("unreachable peer"))?
                .peer(action, value)
                .await;
        }
        #[cfg(not(test))]
        {
            let path = format!("/peer/{action}");
            let raw = journal::bytes(&value);
            let stamp = unix().to_string();
            let nonce = crate::auth::identity();
            let response = self
                .client
                .post(format!("https://{}:2591{path}", self.config.members[node]))
                .header("X-Desk-Time", &stamp)
                .header("X-Desk-Nonce", &nonce)
                .header(
                    "X-Desk-Signature",
                    self.signature(&path, &stamp, &nonce, &raw),
                )
                .header("Content-Type", "application/json")
                .timeout(Duration::from_secs(if action == "status" { 1 } else { 8 }))
                .body(raw)
                .send()
                .await?
                .error_for_status()?;
            Ok(serde_json::from_slice(&bounded(response, 786432).await?)?)
        }
    }
    async fn fanout(self: &Arc<Self>, action: &str, value: Value) -> BTreeMap<String, Value> {
        let mut jobs = JoinSet::new();
        let mut replies = BTreeMap::new();
        for node in self.config.members.keys() {
            let this = self.clone();
            let action = action.to_string();
            let node = node.clone();
            let value = value.clone();
            jobs.spawn(async move {
                let reply = this.call(&node, &action, value).await;
                (node, reply)
            });
        }
        while let Some(Ok((node, reply))) = jobs.join_next().await {
            if let Ok(reply) = reply {
                replies.insert(node, reply);
            }
        }
        replies
    }
    async fn refresh(self: &Arc<Self>) -> Result<BTreeMap<String, Value>> {
        let replies = self.fanout("status", json!({})).await;
        if replies.len() < 2 {
            *self.confirmed.lock().unwrap() = None;
            anyhow::bail!("two nodes required to confirm freshness");
        }
        let heads: Vec<Status> = replies
            .values()
            .cloned()
            .map(serde_json::from_value)
            .collect::<std::result::Result<_, _>>()?;
        let head = &heads.iter().max_by_key(|s| s.head.revision).unwrap().head;
        if !heads.iter().all(|s| {
            s.head.revision != head.revision
                || journal::digest(&s.head.value) == journal::digest(&head.value)
        }) {
            *self.confirmed.lock().unwrap() = None;
            anyhow::bail!("conflicting seed or checkpoint");
        }
        if head.revision > self.status().head.revision {
            let auth: Auth = serde_json::from_value(head.value.clone())?;
            auth.validate()?;
            self.journal.lock().unwrap().commit(
                head.revision,
                head.value.clone(),
                head.certificate.clone(),
            )?;
        }
        if !heads.iter().any(|s| s.head.accepted.is_some()) {
            self.apply().await?;
            *self.confirmed.lock().unwrap() = Some(Instant::now());
        } else {
            *self.confirmed.lock().unwrap() = None;
        }
        Ok(replies)
    }
    async fn choose(self: &Arc<Self>, wanted: Value, expected: i64) -> Result<()> {
        for _ in 0..4 {
            let replies = self.refresh().await?;
            ensure!(
                self.status().head.revision == expected,
                "configuration changed; refresh before retry"
            );
            let heads: Vec<Status> = replies
                .values()
                .cloned()
                .map(serde_json::from_value)
                .collect::<std::result::Result<_, _>>()?;
            let rev = expected + 1;
            let ballot = (
                heads.iter().map(|s| s.head.promised.0).max().unwrap() + 1,
                self.config.node.clone(),
            );
            let promises = self
                .fanout("promise", json!({"revision":rev,"ballot":ballot}))
                .await;
            if promises.len() < 2 || promises.values().any(|p| p.get("head").is_some()) {
                continue;
            }
            let chosen = carry(&promises, &wanted)?;
            let receipts = self
                .fanout(
                    "accept",
                    json!({"revision":rev,"ballot":ballot,"value":chosen}),
                )
                .await;
            if receipts.len() < 2 {
                continue;
            }
            let certificate: Vec<_> = receipts.into_values().collect();
            let replies = self
                .fanout(
                    "commit",
                    json!({"revision":rev,"value":chosen,"certificate":certificate}),
                )
                .await;
            ensure!(
                journal::digest(&chosen) == journal::digest(&wanted),
                "recovered another update; refresh before retry"
            );
            ensure!(
                acknowledged(&replies, &self.config.node, rev, &chosen),
                "committed; awaiting two apply ACKs including receiving node"
            );
            return Ok(());
        }
        anyhow::bail!("concurrent configuration update; refresh before retry")
    }
    pub async fn mutate(
        self: &Arc<Self>,
        actor: &str,
        path: &str,
        method: &str,
        body: Value,
    ) -> Result<Value> {
        let _guard = self.serial.lock().await;
        let replies = self.refresh().await?;
        let account: Value = {
            let response = self
                .client
                .get("http://127.0.0.1:2587/v1/account")
                .header("Authorization", actor)
                .send()
                .await?;
            if !response.status().is_success() {
                return Err(RequestFailure(axum::http::StatusCode::UNAUTHORIZED).into());
            }
            serde_json::from_slice(&bounded(response, 262144).await?)?
        };
        let actor = {
            if account["role"] != "admin" {
                return Err(RequestFailure(axum::http::StatusCode::FORBIDDEN).into());
            }
            account["username"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("missing native identity"))?
        };
        self.submit(actor, path, method, body, replies).await
    }
    async fn submit(
        self: &Arc<Self>,
        actor: &str,
        path: &str,
        method: &str,
        body: Value,
        replies: BTreeMap<String, Value>,
    ) -> Result<Value> {
        let head = self.status().head;
        let auth: Auth = serde_json::from_value(head.value.clone())?;
        let actor = actor.to_string();
        let path = path.to_string();
        let method = method.to_string();
        let (auth, result) =
            tokio::task::spawn_blocking(move || auth.mutate(&actor, &path, &method, body))
                .await?
                .map_err(|_| RequestFailure(axum::http::StatusCode::BAD_REQUEST))?;
        let wanted = serde_json::to_value(auth)?;
        if journal::digest(&wanted) != journal::digest(&head.value) {
            self.choose(wanted, head.revision).await?;
        } else {
            ensure!(
                acknowledged(&replies, &self.config.node, head.revision, &wanted),
                "awaiting apply ACKs"
            );
        }
        Ok(result)
    }
    #[cfg(test)]
    async fn save(self: &Arc<Self>, path: &str, body: Value) -> Result<Value> {
        let _guard = self.serial.lock().await;
        let replies = self.refresh().await?;
        self.submit("admin", path, "POST", body, replies).await
    }
    pub async fn reconcile(self: &Arc<Self>) -> Result<()> {
        let _guard = self.serial.lock().await;
        let heads = self.refresh().await?;
        if heads.values().any(|s| !s["head"]["accepted"].is_null()) {
            let head = self.status().head;
            let _ = self.choose(head.value, head.revision).await;
            self.refresh().await?;
        }
        Ok(())
    }
    pub async fn publish_events(&self) -> Result<()> {
        let auth: Auth = serde_json::from_value(self.status().head.value)?;
        let response = self
            .client
            .get(format!(
                "http://127.0.0.1:2587/desk-events/json?poll=1&since={}",
                unix().saturating_sub(60)
            ))
            .bearer_auth(
                auth.panel_token()
                    .ok_or_else(|| anyhow::anyhow!("panel token unavailable"))?,
            )
            .send()
            .await?
            .error_for_status()?;
        let raw = bounded(response, 262144).await?;
        let mut events = Vec::new();
        for line in raw
            .split(|b| *b == b'\n')
            .filter(|l| !l.is_empty() && l.len() <= 16384)
        {
            let v: Value = serde_json::from_slice(line)?;
            let id = v["id"].as_str().unwrap_or("");
            let time = v["time"].as_u64().unwrap_or(0);
            if v["event"] != "message"
                || v["topic"] != "desk-events"
                || id.len() != 12
                || !id.bytes().all(|c| c.is_ascii_alphanumeric())
                || time < unix().saturating_sub(60)
                || time > unix() + 5
            {
                continue;
            }
            let title = v["title"].as_str().unwrap_or("");
            let message = v["message"].as_str().unwrap_or("");
            let text = if title.is_empty() {
                message.into()
            } else {
                format!("{title}: {message}")
            };
            let mut text: String = text
                .chars()
                .map(|c| if c.is_control() { ' ' } else { c })
                .collect();
            while text.len() > 256 {
                text.pop();
            }
            events.push(json!({"id":id,"time":time,"message":text}));
        }
        events.sort_by_key(|e| std::cmp::Reverse(e["time"].as_u64().unwrap_or(0)));
        events.truncate(16);
        journal::atomic(
            &PathBuf::from(&self.config.events),
            &journal::bytes(&json!({"observed_at":unix(),"events":events})),
        )
    }
}
pub async fn bounded(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    let mut raw = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        ensure!(
            raw.len() + chunk.len() <= limit,
            "response exceeds boundary"
        );
        raw.extend_from_slice(&chunk);
    }
    Ok(raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn nodes(dir: &std::path::Path) -> Vec<Arc<Cluster>> {
        let seed = Auth::seed("secret").unwrap();
        let seed = serde_json::to_value(seed).unwrap();
        let members: BTreeMap<String, String> = ["a", "b", "c"]
            .into_iter()
            .map(|n| (n.into(), "127.0.0.1".into()))
            .collect();
        let nodes: Vec<_> = members
            .keys()
            .map(|n| {
                let journal = Journal::open(
                    dir.join(n),
                    [7; 32],
                    n.clone(),
                    members.keys().cloned().collect(),
                    seed.clone(),
                )
                .unwrap();
                let config = Config {
                    node: n.clone(),
                    members: members.clone(),
                    key: String::new(),
                    certificate: String::new(),
                    tls_key: String::new(),
                    seed: String::new(),
                    journal: String::new(),
                    database: dir.join(format!("{n}.db")).to_str().unwrap().into(),
                    events: String::new(),
                    toml: String::new(),
                };
                Cluster::new(config, journal, [7; 32], reqwest::Client::new())
            })
            .collect();
        for node in &nodes {
            *node.network.lock().unwrap() = nodes
                .iter()
                .filter(|n| n.config.node != node.config.node)
                .map(|n| (n.config.node.clone(), Arc::downgrade(n)))
                .collect();
        }
        nodes
    }
    #[tokio::test]
    async fn save_requires_two_apply_acks_including_receiver_and_catchup_fences_restart() {
        let dir = tempfile::tempdir().unwrap();
        let n = nodes(dir.path());
        n[2].fail_apply.store(true, Ordering::SeqCst);
        assert!(n[0]
            .save("/v1/account/token", json!({"label":"stable"}))
            .await
            .is_ok());
        assert!(!n[2].fresh());
        assert!(n[2].reconcile().await.is_err());
        n[2].fail_apply.store(false, Ordering::SeqCst);
        n[2].reconcile().await.unwrap();
        assert!(n[2].fresh());
        assert_eq!(
            journal::digest(&n[0].status().head.value),
            journal::digest(&n[2].status().head.value)
        );
        n[0].fail_apply.store(true, Ordering::SeqCst);
        assert!(n[0]
            .save("/v1/account/token", json!({"label":"pending"}))
            .await
            .is_err());
        assert_eq!(n[0].status().head.revision, n[1].status().head.revision);
        assert!(!n[0].fresh());
    }
    #[tokio::test]
    async fn admin_user_token_keeps_identity_acl_through_quorum_catchup_and_revocation() {
        let dir = tempfile::tempdir().unwrap();
        let n = nodes(dir.path());
        n[0].save(
            "/v1/admin/users",
            json!({"username":"publisher","password":"secret"}),
        )
        .await
        .unwrap();
        n[0].save(
            "/v1/admin/users/publisher/access",
            json!({"topic":"desk-events","write":true}),
        )
        .await
        .unwrap();
        let before = n[0].status().head.value;
        n[2].fail_apply.store(true, Ordering::SeqCst);
        let issued = n[0]
            .save("/v1/admin/users/publisher/token", json!({"label":"Beszel"}))
            .await
            .unwrap();
        assert!(!n[2].fresh());
        n[2].fail_apply.store(false, Ordering::SeqCst);
        n[2].reconcile().await.unwrap();
        for node in &n {
            let state = node.status().head.value;
            assert_eq!(state["users"], before["users"]);
            assert_eq!(state["acl"], before["acl"]);
            assert_eq!(state, n[0].status().head.value);
            let db = rusqlite::Connection::open(&node.config.database).unwrap();
            let owner: String = db.query_row("SELECT username FROM users JOIN tokens ON users.id=tokens.user_id WHERE token=?1", [issued["token"].as_str().unwrap()], |r| r.get(0)).unwrap();
            assert_eq!(owner, "publisher");
        }
        // A token path for another user must not revoke the publisher's token.
        for user in ["admin", "publisher"] {
            let node = &n[1];
            let _guard = node.serial.lock().await;
            let replies = node.refresh().await.unwrap();
            node.submit(
                "admin",
                &format!(
                    "/v1/admin/users/{user}/token/{}",
                    issued["token"].as_str().unwrap()
                ),
                "DELETE",
                json!({}),
                replies,
            )
            .await
            .unwrap();
            for replica in &n {
                let state = replica.status().head.value;
                assert_eq!(state["users"], before["users"]);
                assert_eq!(state["acl"], before["acl"]);
                assert_eq!(
                    state["tokens"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|t| t["token"] == issued["token"]),
                    user == "admin"
                );
            }
        }
    }
    #[tokio::test]
    async fn minority_cannot_save_or_advertise_freshness() {
        let dir = tempfile::tempdir().unwrap();
        let n = nodes(dir.path());
        n[0].network.lock().unwrap().clear();
        assert!(n[0].save("/v1/account/token", json!({})).await.is_err());
        assert!(!n[0].fresh());
        assert_eq!(n[0].status().head.revision, 0);
        let signature = n[0].signature("/peer/status", "1", "nonce", b"{}");
        use sha2::Digest;
        let message = format!(
            "POST\n/peer/status\n1\nnonce\n{}",
            hex::encode(sha2::Sha256::digest(b"{}"))
        );
        assert!(journal::verify(&[7; 32], message.as_bytes(), &signature));
        assert!(!journal::verify(
            &[7; 32],
            b"different path or payload",
            &signature
        ));
    }
    #[tokio::test]
    async fn accepted_quorum_recovers_original_token_instead_of_generating_another() {
        let dir = tempfile::tempdir().unwrap();
        let n = nodes(dir.path());
        let seed: Auth = serde_json::from_value(n[0].status().head.value).unwrap();
        let (wanted, token) = seed
            .mutate("admin", "/v1/account/token", "POST", json!({}))
            .unwrap();
        let wanted = serde_json::to_value(wanted).unwrap();
        for node in &n[..2] {
            node.peer(
                "accept",
                json!({"revision":1,"ballot":[1,"a"],"value":wanted}),
            )
            .await
            .unwrap();
        }
        assert!(!n[2].fresh());
        n[2].refresh().await.unwrap();
        assert!(
            !n[2].status().apply_ok,
            "unrecovered auth must not start native"
        );
        n[2].reconcile().await.unwrap();
        for node in &n {
            assert_eq!(node.status().head.value, wanted);
        }
        assert!(wanted["tokens"]
            .as_array()
            .unwrap()
            .iter()
            .any(|t| t["token"] == token["token"]));
    }
    #[tokio::test]
    async fn returning_replica_cannot_serve_until_it_loads_a_newer_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let mut n = nodes(dir.path());
        n[0].save("/v1/account/token", json!({"label":"before-offline"}))
            .await
            .unwrap();
        n[0].network.lock().unwrap().remove("c");
        n[1].network.lock().unwrap().remove("c");
        let old = n[2].config.clone();
        drop(n.pop());
        n[0].save("/v1/account/token", json!({"label":"while-offline"}))
            .await
            .unwrap();
        let seed = Auth::seed("unused").unwrap();
        // Revision > 0 retains its chosen identity, independent of a fresh initializer.
        let j = Journal::open(
            dir.path().join("c"),
            [7; 32],
            "c".into(),
            ["a", "b", "c"].map(String::from).into(),
            serde_json::to_value(seed).unwrap(),
        )
        .unwrap();
        let returning = Cluster::new(old, j, [7; 32], reqwest::Client::new());
        assert!(!returning.fresh());
        *returning.network.lock().unwrap() = n
            .iter()
            .map(|p| (p.config.node.clone(), Arc::downgrade(p)))
            .collect();
        returning.reconcile().await.unwrap();
        assert!(returning.fresh());
        assert_eq!(returning.status().head.value, n[0].status().head.value);
    }
    #[tokio::test]
    async fn concurrent_proposers_do_not_lose_an_acknowledged_token() {
        let dir = tempfile::tempdir().unwrap();
        let n = nodes(dir.path());
        let (a, b) = tokio::join!(
            n[0].save("/v1/account/token", json!({"label":"a"})),
            n[1].save("/v1/account/token", json!({"label":"b"}))
        );
        for node in &n {
            let _ = node.reconcile().await;
        }
        for value in [a, b].into_iter().flatten() {
            for node in &n {
                assert!(node.status().head.value["tokens"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|t| t["token"] == value["token"]));
            }
        }
        assert_eq!(n[0].status().head.value, n[1].status().head.value);
    }
}
