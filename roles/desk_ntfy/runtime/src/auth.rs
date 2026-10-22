use anyhow::{ensure, Result};
use rand::{distributions::Alphanumeric, Rng};
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct User {
    pub id: String,
    pub username: String,
    pub hash: String,
    pub role: String,
    pub deleted: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Token {
    pub token: String,
    pub user_id: String,
    pub label: String,
    pub expires: Option<i64>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Acl {
    pub user_id: String,
    pub topic: String,
    pub read: bool,
    pub write: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Auth {
    pub users: Vec<User>,
    pub tokens: Vec<Token>,
    pub acl: Vec<Acl>,
}
pub fn identity() -> String {
    rand::thread_rng()
        .sample_iter(Alphanumeric)
        .take(32)
        .map(char::from)
        .collect()
}
fn name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
fn text(s: &str) -> bool {
    s.len() <= 256 && !s.chars().any(char::is_control)
}
fn password(value: &Value) -> Result<String> {
    let p = value.as_str().unwrap_or("");
    ensure!(
        !p.is_empty() && p.len() <= 72,
        "password requires 1 to 72 UTF-8 bytes"
    );
    Ok(bcrypt::hash(p, 10)?)
}
impl Auth {
    pub fn users(&self) -> Value {
        json!(self.users.iter().filter(|u| !u.deleted).map(|u| {
            json!({
                "username": u.username, "role": u.role, "managed": u.username == "desk-panel",
                "access": self.acl.iter().filter(|a| a.user_id == u.id).map(|a|
                    json!({"topic":a.topic,"read":a.read,"write":a.write})
                ).collect::<Vec<_>>(),
                // Expose revocation handles, never existing bearer credentials or password hashes.
                "tokens": self.tokens.iter().filter(|t| t.user_id == u.id).map(|t|
                    json!({"id":crate::journal::digest(&t.token),"label":t.label,"expires":t.expires})
                ).collect::<Vec<_>>()
            })
        }).collect::<Vec<_>>())
    }
    pub fn seed(admin_password: &str) -> Result<Self> {
        let admin = User {
            id: identity(),
            username: "admin".into(),
            hash: password(&json!(admin_password))?,
            role: "admin".into(),
            deleted: false,
        };
        let panel = User {
            id: identity(),
            username: "desk-panel".into(),
            hash: password(&json!(identity()))?,
            role: "user".into(),
            deleted: false,
        };
        let token = Token {
            token: identity(),
            user_id: panel.id.clone(),
            label: "Pi Events read-only".into(),
            expires: None,
        };
        let acl = Acl {
            user_id: panel.id.clone(),
            topic: "desk-events".into(),
            read: true,
            write: false,
        };
        Ok(Self {
            users: vec![admin, panel],
            tokens: vec![token],
            acl: vec![acl],
        })
    }
    pub fn validate(&self) -> Result<()> {
        ensure!(
            crate::journal::bytes(self).len() <= 262144
                && self.users.len() <= 512
                && self.tokens.len() <= 512
                && self.acl.len() <= 512,
            "auth state exceeds boundary"
        );
        let ids: BTreeSet<_> = self.users.iter().map(|u| &u.id).collect();
        ensure!(
            ids.len() == self.users.len()
                && self
                    .users
                    .iter()
                    .map(|u| &u.username)
                    .collect::<BTreeSet<_>>()
                    .len()
                    == self.users.len(),
            "duplicate user"
        );
        ensure!(
            self.users.iter().any(|u| !u.deleted && u.role == "admin"),
            "retain active administrator"
        );
        for u in &self.users {
            ensure!(
                name(&u.username)
                    && name(&u.id)
                    && matches!(u.role.as_str(), "admin" | "user")
                    && u.hash.len() == 60
                    && (u.hash.starts_with("$2b$10$")
                        || u.hash.starts_with("$2a$10$")
                        || u.hash.starts_with("$2y$10$"))
                    && u.hash[7..]
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || c == b'.' || c == b'/'),
                "invalid user"
            );
        }
        ensure!(
            self.tokens
                .iter()
                .map(|t| &t.token)
                .collect::<BTreeSet<_>>()
                .len()
                == self.tokens.len(),
            "duplicate token"
        );
        for t in &self.tokens {
            ensure!(
                name(&t.token)
                    && t.token.len() >= 32
                    && ids.contains(&t.user_id)
                    && text(&t.label)
                    && t.expires.is_none_or(|x| x >= 0),
                "invalid token"
            );
        }
        ensure!(
            self.acl
                .iter()
                .map(|a| (&a.user_id, &a.topic))
                .collect::<BTreeSet<_>>()
                .len()
                == self.acl.len(),
            "duplicate ACL"
        );
        for a in &self.acl {
            ensure!(ids.contains(&a.user_id) && name(&a.topic), "invalid ACL");
        }
        let panel = self
            .users
            .iter()
            .find(|u| u.username == "desk-panel" && !u.deleted && u.role == "user");
        ensure!(panel.is_some(), "retain panel service identity");
        let panel = panel.unwrap();
        ensure!(
            self.acl
                .iter()
                .filter(|a| a.user_id == panel.id)
                .all(|a| a.topic == "desk-events" && a.read && !a.write),
            "panel is read-only"
        );
        ensure!(
            self.acl.iter().any(|a| a.user_id == panel.id)
                && self
                    .tokens
                    .iter()
                    .any(|t| t.user_id == panel.id && t.expires.is_none()),
            "retain panel credentials"
        );
        Ok(())
    }
    pub fn apply(&self, path: &str) -> Result<()> {
        self.validate()?;
        let mut db = Connection::open(path)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        db.execute_batch("PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,label TEXT NOT NULL DEFAULT '',expires INTEGER,last_access INTEGER,last_origin TEXT);
            CREATE TABLE IF NOT EXISTS topic_acl(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,topic TEXT NOT NULL,read INTEGER NOT NULL DEFAULT 0,write INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,topic));")?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch("DELETE FROM tokens; DELETE FROM topic_acl; DELETE FROM users;")?;
        for u in &self.users {
            tx.execute(
                "INSERT INTO users VALUES(?1,?2,?3,?4,?5)",
                params![u.id, u.username, u.hash, u.role, u.deleted],
            )?;
        }
        for t in &self.tokens {
            tx.execute(
                "INSERT INTO tokens(token,user_id,label,expires) VALUES(?1,?2,?3,?4)",
                params![t.token, t.user_id, t.label, t.expires],
            )?;
        }
        for a in &self.acl {
            tx.execute(
                "INSERT INTO topic_acl VALUES(?1,?2,?3,?4)",
                params![a.user_id, a.topic, a.read, a.write],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn mutate(
        &self,
        actor: &str,
        path: &str,
        method: &str,
        body: Value,
    ) -> Result<(Self, Value)> {
        let mut state = self.clone();
        let own = state
            .users
            .iter()
            .find(|u| u.username == actor && !u.deleted && u.role == "admin")
            .ok_or_else(|| anyhow::anyhow!("administrator required"))?
            .id
            .clone();
        let body = body
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("expected JSON object"))?;
        let keys = |allowed: &[&str]| -> Result<()> {
            ensure!(
                body.keys().all(|k| allowed.contains(&k.as_str())),
                "unknown mutation field"
            );
            Ok(())
        };
        let get = |key: &str| body.get(key).cloned().unwrap_or(Value::Null);
        let new_token = |user_id: String| -> Result<Token> {
            keys(&["label", "expires"])?;
            let expires = get("expires");
            ensure!(
                expires.is_null() || expires.as_i64().is_some(),
                "invalid expiry"
            );
            let label = get("label");
            ensure!(label.is_null() || label.is_string(), "invalid label");
            Ok(Token {
                token: identity(),
                user_id,
                label: label.as_str().unwrap_or("").into(),
                expires: expires.as_i64(),
            })
        };
        let parts: Vec<_> = path.trim_start_matches('/').split('/').collect();
        let mut response = json!({});
        if parts == ["v1", "admin", "users"] && method == "POST" {
            keys(&["username", "password", "role"])?;
            let username = get("username").as_str().unwrap_or("").to_string();
            ensure!(
                !state.users.iter().any(|u| u.username == username),
                "username already exists"
            );
            state.users.push(User {
                id: identity(),
                username: username.clone(),
                hash: password(&get("password"))?,
                role: get("role").as_str().unwrap_or("user").into(),
                deleted: false,
            });
            response = json!({"username":username});
        } else if parts.len() >= 4 && parts[..3] == ["v1", "admin", "users"] {
            ensure!(
                parts[3] != "desk-panel",
                "panel service identity is managed"
            );
            let user = state
                .users
                .iter_mut()
                .find(|u| u.username == parts[3] && !u.deleted)
                .ok_or_else(|| anyhow::anyhow!("user not found"))?;
            match (parts.len(), parts.get(4).copied(), method) {
                (5, Some("password"), "PUT") => {
                    keys(&["password"])?;
                    user.hash = password(&get("password"))?;
                }
                (4, None, "DELETE") => {
                    keys(&[])?;
                    user.deleted = true;
                    state.tokens.retain(|t| t.user_id != user.id);
                    state.acl.retain(|a| a.user_id != user.id);
                }
                (5, Some("role"), "PUT") => {
                    keys(&["role"])?;
                    user.role = get("role").as_str().unwrap_or("").into();
                }
                (5, Some("access"), "POST") => {
                    keys(&["topic", "read", "write"])?;
                    let topic = get("topic").as_str().unwrap_or("").to_string();
                    let flag = |key| -> Result<bool> {
                        let v = get(key);
                        ensure!(v.is_null() || v.is_boolean(), "invalid ACL flag");
                        Ok(v.as_bool().unwrap_or(false))
                    };
                    state
                        .acl
                        .retain(|a| a.user_id != user.id || a.topic != topic);
                    state.acl.push(Acl {
                        user_id: user.id.clone(),
                        topic,
                        read: flag("read")?,
                        write: flag("write")?,
                    });
                }
                (6, Some("access"), "DELETE") => {
                    keys(&[])?;
                    state
                        .acl
                        .retain(|a| a.user_id != user.id || a.topic != parts[5]);
                }
                (5, Some("token"), "POST") => {
                    let token = new_token(user.id.clone())?;
                    response =
                        json!({"token":token.token,"label":token.label,"expires":token.expires});
                    state.tokens.push(token);
                }
                (6, Some("token"), "DELETE") => {
                    keys(&[])?;
                    state.tokens.retain(|t| {
                        t.user_id != user.id
                            || (t.token != parts[5] && crate::journal::digest(&t.token) != parts[5])
                    });
                }
                _ => anyhow::bail!("unsupported admin mutation"),
            }
        } else if parts == ["v1", "account", "password"] && method == "PUT" {
            keys(&["password"])?;
            state.users.iter_mut().find(|u| u.id == own).unwrap().hash =
                password(&get("password"))?;
        } else if parts == ["v1", "account", "token"] && method == "POST" {
            let token = new_token(own)?;
            response = json!({"token":token.token,"label":token.label,"expires":token.expires});
            state.tokens.push(token);
        } else if parts.len() == 4 && parts[..3] == ["v1", "account", "token"] && method == "DELETE"
        {
            keys(&[])?;
            state
                .tokens
                .retain(|t| t.user_id != own || t.token != parts[3]);
        } else {
            anyhow::bail!("registration and self-service ACL disabled");
        }
        state.validate()?;
        Ok((state, response))
    }
    pub fn panel_token(&self) -> Option<&str> {
        let user = self.users.iter().find(|u| u.username == "desk-panel")?;
        self.tokens
            .iter()
            .find(|t| t.user_id == user.id && t.expires.is_none())
            .map(|t| t.token.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn logical_apply_preserves_identity_hash_token_acl_and_local_history() {
        let temp = tempfile::tempdir().unwrap();
        let a = temp.path().join("a.db");
        let b = temp.path().join("b.db");
        let seed = Auth::seed("secret").unwrap();
        let (state, _) = seed
            .mutate(
                "admin",
                "/v1/admin/users",
                "POST",
                json!({"username":"iphone","password":"p"}),
            )
            .unwrap();
        let (state, _) = state
            .mutate(
                "admin",
                "/v1/admin/users/iphone/access",
                "POST",
                json!({"topic":"desk-events","read":true}),
            )
            .unwrap();
        let (state, token) = state
            .mutate(
                "admin",
                "/v1/admin/users/iphone/token",
                "POST",
                json!({"label":"phone"}),
            )
            .unwrap();
        for path in [&a, &b] {
            let db = Connection::open(path).unwrap();
            db.execute_batch(
                "CREATE TABLE messages(id TEXT); INSERT INTO messages VALUES('local');",
            )
            .unwrap();
            state.apply(path.to_str().unwrap()).unwrap();
            let row: (String, String) = db
                .query_row(
                    "SELECT id,hash FROM users WHERE username='iphone'",
                    [],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .unwrap();
            let user = state.users.iter().find(|u| u.username == "iphone").unwrap();
            assert_eq!(row, (user.id.clone(), user.hash.clone()));
            assert_eq!(
                db.query_row(
                    "SELECT COUNT(*) FROM tokens WHERE token=?1",
                    [token["token"].as_str().unwrap()],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
                1
            );
            assert_eq!(
                db.query_row("SELECT id FROM messages", [], |r| r.get::<_, String>(0))
                    .unwrap(),
                "local"
            );
        }
    }
    #[test]
    fn unauthorized_acl_registration_and_last_admin_removal_fail() {
        let seed = Auth::seed("secret").unwrap();
        for (actor, path, method, body) in [
            ("desk-panel", "/v1/account/token", "POST", json!({})),
            (
                "desk-panel",
                "/v1/admin/users/admin/password",
                "PUT",
                json!({"password":"p"}),
            ),
            (
                "admin",
                "/v1/admin/users/desk-panel/password",
                "PUT",
                json!({"password":"p"}),
            ),
            (
                "desk-panel",
                "/v1/admin/users/admin/token",
                "POST",
                json!({}),
            ),
            (
                "desk-panel",
                "/v1/admin/users/admin/token/x",
                "DELETE",
                json!({}),
            ),
            ("admin", "/v1/account", "POST", json!({"username":"bad"})),
            (
                "admin",
                "/v1/account/access",
                "POST",
                json!({"topic":"secret","read":true}),
            ),
            ("admin", "/v1/admin/users/admin", "DELETE", json!({})),
        ] {
            assert!(seed.mutate(actor, path, method, body).is_err());
        }
    }
}
